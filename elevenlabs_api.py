"""
ElevenLabs TTS API Server

Exposes a REST API that accepts text and returns MP3 audio.
Uses the same Camoufox + hCaptcha HSW bypass + proxy rotation.

Endpoints:
  POST /tts
    Body: {"text": "...", "voice": "...", "lang": "vi", "speed": 1.0}
    Returns: audio/mpeg stream

  GET /health
    Returns: {"status": "ok", "queue": N}

Usage:
    python elevenlabs_api.py --port 8899
    
    curl -X POST http://localhost:8899/tts \
      -H "Content-Type: application/json" \
      -d '{"text": "Xin chào", "lang": "vi"}' \
      --output test.mp3
"""

import asyncio
import json
import re
import sys
import time
import random
import warnings
from pathlib import Path

import httpx
import jwt
import tls_client
from camoufox.async_api import AsyncCamoufox
from aiohttp import web

warnings.filterwarnings("ignore")

SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
HOST = "elevenlabs.io"
API_BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE = "NOpBlnGInO9m6vDvFkFC"
DEFAULT_MODEL = "eleven_v3"
REQUESTS_PER_IP = 3

PROXY_API = "https://proxyxoay.shop/api/get.php"
PROXY_KEYS = [
    "WGVvsWnSIkNoBlcIffKFPJ",
    "HWgDVrhXHdgVlbOGFLucVp",
    "gIyeGmqZSePouzcZEwAKhy",
    "mrpLsUPXIBPnVFoQqAOVzu"
]


class ProxyPool:
    """Manages proxy rotation with IP tracking."""

    def __init__(self, keys: list[str]):
        self.keys = keys
        self._lock = asyncio.Lock()
        self._key_idx = 0
        self._ip_usage = {}  # ip -> request count
        self._current_proxies = {}  # key -> proxy dict

    async def get_proxy(self) -> dict:
        """Get a proxy with available quota (< 3 requests)."""
        async with self._lock:
            # Check if any current proxy still has quota
            for key, proxy in self._current_proxies.items():
                ip = proxy["raw"]
                if self._ip_usage.get(ip, 0) < REQUESTS_PER_IP:
                    self._ip_usage[ip] = self._ip_usage.get(ip, 0) + 1
                    return proxy

        # Need fresh proxy — rotate key
        async with self._lock:
            key = self.keys[self._key_idx]
            self._key_idx = (self._key_idx + 1) % len(self.keys)

        proxy = await self._fetch_proxy(key)
        async with self._lock:
            self._current_proxies[key] = proxy
            self._ip_usage[proxy["raw"]] = 1
        return proxy

    async def mark_quota_hit(self, proxy: dict):
        """Mark proxy IP as exhausted."""
        async with self._lock:
            self._ip_usage[proxy["raw"]] = REQUESTS_PER_IP

    async def _fetch_proxy(self, key: str) -> dict:
        """Fetch proxy from API, handling cooldown."""
        url = f"{PROXY_API}?key={key}&nhamang=random&tinhthanh=0&whitelist="
        for retry in range(20):
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                data = resp.json()

            if data.get("status") == 100:
                http_raw = data["proxyhttp"].rstrip(":")
                return {
                    "http": f"http://{http_raw}",
                    "raw": http_raw,
                }
            elif data.get("status") == 101:
                msg = data.get("message", "")
                m = re.search(r"Con (\d+)s", msg)
                wait = int(m.group(1)) + 1 if m else (5 + retry * 2)
                print(f"  [proxy|{key[:6]}] cooldown {wait}s...", flush=True)
                await asyncio.sleep(wait)
            else:
                raise RuntimeError(f"Proxy error: {data}")
        raise RuntimeError(f"Proxy timeout for key {key[:8]}")


class TTSEngine:
    """Manages browser instances and generates TTS audio."""

    def __init__(self, proxy_pool: ProxyPool):
        self.proxy_pool = proxy_pool
        self._semaphore = asyncio.Semaphore(3)  # Max 3 concurrent requests
        self._browser = None
        self._browser_proxy = None
        self._browser_lock = asyncio.Lock()
        self._request_count = 0

    async def _get_browser(self, proxy: dict):
        """Get or create browser with matching proxy."""
        async with self._browser_lock:
            if self._browser is None or self._browser_proxy != proxy["raw"]:
                if self._browser:
                    try:
                        await self._browser.__aexit__(None, None, None)
                    except Exception:
                        pass
                cm = AsyncCamoufox(headless=True, os='windows', proxy={'server': proxy["http"]})
                self._browser = await cm.__aenter__()
                self._browser_proxy = proxy["raw"]
                self._request_count = 0
            return self._browser

    async def generate(self, text: str, voice_id: str = DEFAULT_VOICE,
                       model_id: str = DEFAULT_MODEL, speed: float = 1.0,
                       language: str = "vi") -> bytes:
        """Generate TTS audio. Returns MP3 bytes."""
        async with self._semaphore:
            proxy = await self.proxy_pool.get_proxy()
            browser = await self._get_browser(proxy)

            t0 = time.time()

            # 1. Get hCaptcha materials
            req_token, version, config = await asyncio.to_thread(
                get_hcaptcha_materials, proxy["http"]
            )

            # 2. Solve HSW
            hsw_token = await solve_hsw(req_token, proxy["http"], browser)

            # 3. Submit captcha
            hcaptcha_token = await asyncio.to_thread(
                submit_captcha, hsw_token, version, config, proxy["http"]
            )

            t_token = time.time() - t0

            # 4. TTS API call
            url = f"{API_BASE}/v1/text-to-speech/{voice_id}/anonymous"
            payload = {
                "text": text, "model_id": model_id,
                "voice_settings": {"speed": speed},
                "hcaptcha_token": hcaptcha_token,
                "language_code": language,
            }
            async with httpx.AsyncClient(proxy=proxy["http"], timeout=60.0, verify=False) as client:
                resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})

            if resp.status_code == 200:
                print(f"  [tts] OK ({time.time()-t0:.1f}s, {len(resp.content)}B, text={text[:30]}...)", flush=True)
                return resp.content
            elif resp.status_code == 401:
                await self.proxy_pool.mark_quota_hit(proxy)
                # Retry with new proxy
                raise RuntimeError(f"401 quota hit, need retry")
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:80]}")


def get_hcaptcha_materials(proxy_http: str) -> tuple[str, str, dict]:
    """Get req_token, version, config via HTTP."""
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*', 'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://newassets.hcaptcha.com',
        'referer': 'https://newassets.hcaptcha.com/',
        'sec-ch-ua': '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }
    if proxy_http:
        session.proxies = {'http': proxy_http, 'https': proxy_http}

    api_js = session.get('https://hcaptcha.com/1/api.js?render=explicit&onload=hcaptchaOnLoad').text
    versions = re.findall(r'v1/([A-Za-z0-9]+)/static', api_js)
    version = versions[1] if len(versions) > 1 else "unknown"

    config = session.post("https://api2.hcaptcha.com/checksiteconfig", params={
        'v': version, 'host': HOST, 'sitekey': SITEKEY, 'sc': '1', 'swa': '1', 'spst': '1',
    }).json()

    if 'c' not in config or 'req' not in config.get('c', {}):
        raise RuntimeError(f"checksiteconfig failed: {json.dumps(config)[:100]}")

    return config['c']['req'], version, config


async def solve_hsw(req_token: str, proxy_http: str, browser) -> str:
    """Solve HSW in Camoufox browser page."""
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*', 'accept-language': 'en-US,en;q=0.9',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }
    if proxy_http:
        session.proxies = {'http': proxy_http, 'https': proxy_http}

    decoded = jwt.decode(req_token, options={"verify_signature": False})
    hsw_url = "https://newassets.hcaptcha.com" + decoded["l"] + "/hsw.js"
    hsw_js = session.get(hsw_url).text

    page = await browser.new_page()
    try:
        await page.route(f"https://{HOST}/hsw", lambda r: r.fulfill(
            status=200, content_type="text/html",
            body="<html><head></head><body></body></html>"
        ))
        await page.goto(f"https://{HOST}/hsw", wait_until='domcontentloaded', timeout=10000)
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => false})")

        # Inject hsw.js
        injected = False
        try:
            await page.add_script_tag(content=hsw_js)
            await asyncio.sleep(0.2)
            if await page.evaluate("typeof hsw === 'function'"):
                injected = True
        except Exception:
            pass

        if not injected:
            try:
                await page.evaluate(f"""(function() {{
                    const s = document.createElement('script');
                    s.textContent = {json.dumps(hsw_js)};
                    document.head.appendChild(s);
                }})();""")
                await asyncio.sleep(0.2)
                if await page.evaluate("typeof hsw === 'function'"):
                    injected = True
            except Exception:
                pass

        if not injected:
            await page.evaluate(hsw_js)
            await asyncio.sleep(0.2)

        result = await page.evaluate("(req) => hsw(req)", req_token)
        return result
    finally:
        await page.close()


def submit_captcha(hsw_token: str, version: str, config: dict, proxy_http: str) -> str:
    """Submit HSW → get hcaptcha pass UUID."""
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*', 'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://newassets.hcaptcha.com', 'referer': 'https://newassets.hcaptcha.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }
    if proxy_http:
        session.proxies = {'http': proxy_http, 'https': proxy_http}

    motion = {
        "st": int(time.time() * 1000), "dct": int(time.time() * 1000),
        "mm": [[random.randint(100, 800), random.randint(100, 600), random.randint(10, 500)] for _ in range(3)],
    }
    data = {
        'v': version, 'sitekey': SITEKEY, 'host': HOST, 'hl': 'en',
        'motionData': json.dumps(motion), 'n': hsw_token, 'c': json.dumps(config['c']),
    }
    resp = session.post(f"https://api2.hcaptcha.com/getcaptcha/{SITEKEY}", data=data)
    result = resp.json()

    if 'generated_pass_UUID' in result:
        return result['generated_pass_UUID']
    if 'tasklist' in result:
        raise RuntimeError("image_challenge")
    raise RuntimeError(f"getcaptcha failed: {json.dumps(result)[:100]}")


# === Logging ===

LOG_FILE = Path(__file__).parent / "api_requests.log"


def log_request(client_ip: str, text_preview: str, status: str, detail: str):
    """Log each API request to file."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{status.upper()}] ip={client_ip} text=\"{text_preview}\" {detail}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"  [log] {status} | {client_ip} | {text_preview[:30]}...", flush=True)


# === API Handlers ===

async def handle_tts(request):
    """POST /tts — generate TTS audio and return as MP3 stream."""
    client_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For") or request.remote
    try:
        body = await request.json()
    except Exception:
        log_request(client_ip, "", "error", "Invalid JSON")
        return web.json_response({"error": "Invalid JSON"}, status=400)

    text = body.get("text", "").strip()
    if not text:
        log_request(client_ip, "", "error", "text is required")
        return web.json_response({"error": "text is required"}, status=400)
    if len(text) > 1000:
        log_request(client_ip, text[:30], "error", "text too long")
        return web.json_response({"error": "text too long (max 1000 chars)"}, status=400)

    voice = body.get("voice", DEFAULT_VOICE)
    model = body.get("model", DEFAULT_MODEL)
    speed = body.get("speed", 1.0)
    lang = body.get("lang", "vi")

    engine: TTSEngine = request.app["engine"]
    t0 = time.time()

    # Retry up to 3 times on quota errors
    for attempt in range(3):
        try:
            audio = await engine.generate(text, voice, model, speed, lang)
            elapsed = time.time() - t0
            log_request(client_ip, text[:50], "success", f"{len(audio)}B in {elapsed:.1f}s")
            return web.Response(
                body=audio,
                content_type="audio/mpeg",
                headers={"Content-Disposition": "attachment; filename=tts.mp3"}
            )
        except RuntimeError as e:
            if "401" in str(e) and attempt < 2:
                print(f"  [api] retry {attempt+1} due to quota...", flush=True)
                continue
            elapsed = time.time() - t0
            log_request(client_ip, text[:50], "fail", f"{str(e)[:60]} ({elapsed:.1f}s)")
            return web.json_response({"error": str(e)}, status=500)
        except Exception as e:
            elapsed = time.time() - t0
            log_request(client_ip, text[:50], "fail", f"{str(e)[:60]} ({elapsed:.1f}s)")
            return web.json_response({"error": str(e)}, status=500)


async def handle_health(request):
    """GET /health"""
    return web.json_response({"status": "ok", "time": time.time()})


async def handle_voices(request):
    """GET /voices — list available voices."""
    voices = [
        {"id": "NOpBlnGInO9m6vDvFkFC", "name": "Aria", "lang": "multi"},
        {"id": "9BWtsMINqrJLrRacOk9x", "name": "Aria (v2)", "lang": "multi"},
        {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Sarah", "lang": "en"},
        {"id": "FGY2WhTYpPnrIDTdsKH5", "name": "Laura", "lang": "en"},
        {"id": "IKne3meq5aSn9XLyUdCD", "name": "Charlie", "lang": "en"},
        {"id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "lang": "en"},
        {"id": "TX3LPaxmHKxFdv7VOQHJ", "name": "Liam", "lang": "en"},
        {"id": "pFZP5JQG7iQjIQuC4Bku", "name": "Lily", "lang": "en"},
    ]
    return web.json_response({"voices": voices})


async def on_startup(app):
    """Initialize engine on startup."""
    proxy_pool = ProxyPool(PROXY_KEYS)
    app["engine"] = TTSEngine(proxy_pool)
    print("[*] TTS API server ready", flush=True)


async def on_cleanup(app):
    """Cleanup on shutdown."""
    engine = app.get("engine")
    if engine and engine._browser:
        try:
            await engine._browser.__aexit__(None, None, None)
        except Exception:
            pass


def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_post("/tts", handle_tts)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/voices", handle_voices)
    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ElevenLabs TTS API Server")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"[*] Starting TTS API on {args.host}:{args.port}", flush=True)
    print(f"[*] POST /tts  — generate audio", flush=True)
    print(f"[*] GET /health — health check", flush=True)
    print(f"[*] GET /voices — list voices", flush=True)

    app = create_app()
    web.run_app(app, host=args.host, port=args.port, print=None)
