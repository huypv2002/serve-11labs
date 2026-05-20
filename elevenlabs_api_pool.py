"""
ElevenLabs TTS API Server — Token Pool Version

Pre-solves hCaptcha tokens in background → instant TTS response (~2-5s).
Background workers continuously solve tokens and keep a pool ready.

Endpoints:
  POST /tts
    Body: {"text": "...", "voice": "...", "lang": "vi", "speed": 1.0}
    Returns: audio/mpeg stream

  GET /health
    Returns: {"status": "ok", "pool_size": N, "total_served": N}

  GET /voices
    Returns: list of available voices

Usage:
    python3 elevenlabs_api_pool.py --port 8900 --pool-size 5
    
    curl -X POST http://localhost:8900/tts \
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
from collections import deque

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
TOKEN_TTL = 100  # seconds — tokens expire after ~120s, use 100s as safe margin

PROXY_API = "https://proxyxoay.shop/api/get.php"
PROXY_KEYS = [
    "mWaQAhVpDxNSxQMshMpfvV",
    "ejvdCkHfVufMQdUCefruiR",
]


class ProxyPool:
    """Manages proxy rotation with IP tracking."""

    def __init__(self, keys: list[str]):
        self.keys = list(keys)
        self._lock = asyncio.Lock()
        self._key_idx = 0
        self._ip_usage = {}  # ip -> request count
        self._current_proxies = {}  # key -> proxy dict

    async def add_key(self, key: str) -> bool:
        """Add a proxy key. Returns False if already exists."""
        async with self._lock:
            if key in self.keys:
                return False
            self.keys.append(key)
            return True

    async def remove_key(self, key: str) -> bool:
        """Remove a proxy key. Returns False if not found."""
        async with self._lock:
            if key not in self.keys:
                return False
            self.keys.remove(key)
            self._current_proxies.pop(key, None)
            if self._key_idx >= len(self.keys):
                self._key_idx = 0
            return True

    async def get_proxy(self) -> dict:
        """Get a proxy with available quota (< 3 requests)."""
        async with self._lock:
            for key, proxy in self._current_proxies.items():
                ip = proxy["raw"]
                if self._ip_usage.get(ip, 0) < REQUESTS_PER_IP:
                    self._ip_usage[ip] = self._ip_usage.get(ip, 0) + 1
                    return proxy

        # Need fresh proxy
        async with self._lock:
            key = self.keys[self._key_idx]
            self._key_idx = (self._key_idx + 1) % len(self.keys)

        proxy = await self._fetch_proxy(key)
        async with self._lock:
            self._current_proxies[key] = proxy
            self._ip_usage[proxy["raw"]] = 1
        return proxy

    async def get_proxy_for_solve(self) -> dict:
        """Get proxy for token solving (doesn't count toward TTS quota)."""
        async with self._lock:
            key = self.keys[self._key_idx]
            self._key_idx = (self._key_idx + 1) % len(self.keys)
        return await self._fetch_proxy(key)

    async def mark_quota_hit(self, proxy: dict):
        """Mark proxy IP as exhausted."""
        async with self._lock:
            self._ip_usage[proxy["raw"]] = REQUESTS_PER_IP

    async def reset_ip(self, ip: str):
        """Reset usage for an IP."""
        async with self._lock:
            self._ip_usage.pop(ip, None)

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


class TokenPool:
    """
    Pre-solves hCaptcha tokens in background.
    Each token = (hcaptcha_token, proxy_dict, solved_at)
    """

    def __init__(self, proxy_pool: ProxyPool, target_size: int = 5):
        self.proxy_pool = proxy_pool
        self.target_size = target_size
        self._tokens = deque()  # (hcaptcha_token, proxy, solved_at)
        self._lock = asyncio.Lock()
        self._solving = 0
        self._total_solved = 0
        self._total_expired = 0
        self._total_served = 0
        self._running = False
        self._browser = None

    @property
    def available(self) -> int:
        """Number of valid tokens in pool."""
        now = time.time()
        return sum(1 for _, _, t in self._tokens if now - t < TOKEN_TTL)

    async def get_token(self, timeout: float = 90.0) -> tuple[str, dict]:
        """Get a pre-solved token. Waits up to timeout seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with self._lock:
                while self._tokens:
                    token, proxy, solved_at = self._tokens.popleft()
                    # Check if token is still valid
                    if time.time() - solved_at < TOKEN_TTL:
                        self._total_served += 1
                        print(f"  [pool] served token (age={time.time()-solved_at:.0f}s, remaining={len(self._tokens)})", flush=True)
                        return token, proxy
                    else:
                        self._total_expired += 1
                        print(f"  [pool] expired token discarded (age={time.time()-solved_at:.0f}s)", flush=True)
            # No token available, wait a bit
            await asyncio.sleep(1)
        raise RuntimeError("Token pool empty — timeout waiting for token")

    async def start(self):
        """Start background token solving."""
        self._running = True
        print(f"[*] Token pool started (target={self.target_size})", flush=True)
        # Start solver workers (1 per key to avoid proxy conflicts)
        tasks = [
            asyncio.create_task(self._solver_loop(i))
            for i in range(len(PROXY_KEYS))
        ]
        return tasks

    async def stop(self):
        """Stop background solving."""
        self._running = False

    async def _solver_loop(self, worker_id: int):
        """Continuously solve tokens to keep pool full."""
        key = PROXY_KEYS[worker_id]
        # Stagger start
        await asyncio.sleep(worker_id * 3)

        while self._running:
            try:
                # Check if pool needs more tokens
                if self.available >= self.target_size:
                    await asyncio.sleep(2)
                    continue

                self._solving += 1
                print(f"  [solver-{worker_id}] solving token (pool={self.available}, solving={self._solving})...", flush=True)

                # Get proxy
                proxy = await self.proxy_pool._fetch_proxy(key)

                # Solve captcha
                t0 = time.time()
                req_token, version, config = await asyncio.to_thread(
                    get_hcaptcha_materials, proxy["http"]
                )

                async with AsyncCamoufox(headless=True, os='windows', proxy={'server': proxy["http"]}) as browser:
                    hsw_token = await solve_hsw(req_token, proxy["http"], browser)

                hcaptcha_token = await asyncio.to_thread(
                    submit_captcha, hsw_token, version, config, proxy["http"]
                )

                solve_time = time.time() - t0
                self._total_solved += 1
                self._solving -= 1

                # Add to pool
                async with self._lock:
                    self._tokens.append((hcaptcha_token, proxy, time.time()))

                print(f"  [solver-{worker_id}] ✓ token solved ({solve_time:.1f}s, pool={self.available})", flush=True)

            except Exception as e:
                self._solving -= 1
                print(f"  [solver-{worker_id}] ✗ error: {str(e)[:60]}", flush=True)
                await asyncio.sleep(5)


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

LOG_FILE = Path(__file__).parent / "api_pool_requests.log"


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
    """POST /tts — get pre-solved token, call TTS API instantly."""
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

    voice = body.get("voice_id") or body.get("voice", DEFAULT_VOICE)
    model = body.get("model", DEFAULT_MODEL)
    speed = body.get("speed", 1.0)
    lang = body.get("lang", "vi")

    token_pool: TokenPool = request.app["token_pool"]
    t0 = time.time()

    try:
        # Get pre-solved token from pool
        hcaptcha_token, proxy = await token_pool.get_token(timeout=90.0)
        t_token = time.time() - t0

        # Call TTS API directly
        url = f"{API_BASE}/v1/text-to-speech/{voice}/anonymous"
        payload = {
            "text": text, "model_id": model,
            "voice_settings": {"speed": speed},
            "hcaptcha_token": hcaptcha_token,
            "language_code": lang,
        }
        async with httpx.AsyncClient(proxy=proxy["http"], timeout=120.0, verify=False) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})

        if resp.status_code == 200:
            elapsed = time.time() - t0
            log_request(client_ip, text[:50], "success", f"{len(resp.content)}B in {elapsed:.1f}s (token_wait={t_token:.1f}s)")
            return web.Response(
                body=resp.content,
                content_type="audio/mpeg",
                headers={"Content-Disposition": "attachment; filename=tts.mp3"}
            )
        elif resp.status_code == 401:
            await token_pool.proxy_pool.mark_quota_hit(proxy)
            elapsed = time.time() - t0
            log_request(client_ip, text[:50], "fail", f"401 quota hit ({elapsed:.1f}s)")
            return web.json_response({"error": "TTS service temporarily unavailable, please retry"}, status=503)
        else:
            elapsed = time.time() - t0
            err = f"HTTP {resp.status_code}: {resp.text[:60]}"
            log_request(client_ip, text[:50], "fail", f"{err} ({elapsed:.1f}s)")
            return web.json_response({"error": err}, status=500)

    except RuntimeError as e:
        elapsed = time.time() - t0
        log_request(client_ip, text[:50], "fail", f"{str(e)[:60]} ({elapsed:.1f}s)")
        return web.json_response({"error": str(e)}, status=503)
    except Exception as e:
        elapsed = time.time() - t0
        log_request(client_ip, text[:50], "fail", f"{str(e)[:60]} ({elapsed:.1f}s)")
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request):
    """GET /health"""
    token_pool: TokenPool = request.app["token_pool"]
    return web.json_response({
        "status": "ok",
        "pool_size": token_pool.available,
        "pool_target": token_pool.target_size,
        "total_solved": token_pool._total_solved,
        "total_served": token_pool._total_served,
        "total_expired": token_pool._total_expired,
        "solving_now": token_pool._solving,
        "time": time.time(),
    })


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
        {"id": "g14YnDYCsy3k7XLlcKlO", "name": "Custom Voice", "lang": "multi"},
    ]
    return web.json_response({"voices": voices})


async def handle_proxy_keys(request):
    """GET /proxy/keys — list proxy keys (masked)."""
    token_pool: TokenPool = request.app["token_pool"]
    keys = token_pool.proxy_pool.keys
    masked = [f"{k[:6]}...{k[-4:]}" for k in keys]
    return web.json_response({
        "keys": masked,
        "count": len(keys),
        "solvers": len(keys),  # 1 solver per key
    })


async def handle_proxy_add(request):
    """POST /proxy/add — add a proxy key."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = body.get("key", "").strip()
    if not key:
        return web.json_response({"error": "key is required"}, status=400)
    if len(key) < 10:
        return web.json_response({"error": "invalid key"}, status=400)

    token_pool: TokenPool = request.app["token_pool"]
    added = await token_pool.proxy_pool.add_key(key)
    if not added:
        return web.json_response({"error": "key already exists"}, status=409)

    # Start a new solver for this key
    solver_task = asyncio.create_task(token_pool._solver_loop(len(token_pool.proxy_pool.keys) - 1))
    request.app["solver_tasks"].append(solver_task)

    print(f"  [admin] added proxy key {key[:6]}... (total: {len(token_pool.proxy_pool.keys)})", flush=True)
    return web.json_response({
        "status": "added",
        "key": f"{key[:6]}...{key[-4:]}",
        "total_keys": len(token_pool.proxy_pool.keys),
    })


async def handle_proxy_remove(request):
    """POST /proxy/remove — remove a proxy key."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = body.get("key", "").strip()
    if not key:
        return web.json_response({"error": "key is required"}, status=400)

    token_pool: TokenPool = request.app["token_pool"]
    removed = await token_pool.proxy_pool.remove_key(key)
    if not removed:
        return web.json_response({"error": "key not found"}, status=404)

    print(f"  [admin] removed proxy key {key[:6]}... (total: {len(token_pool.proxy_pool.keys)})", flush=True)
    return web.json_response({
        "status": "removed",
        "key": f"{key[:6]}...{key[-4:]}",
        "total_keys": len(token_pool.proxy_pool.keys),
    })


async def on_startup(app):
    """Initialize token pool on startup."""
    proxy_pool = ProxyPool(PROXY_KEYS)
    token_pool = TokenPool(proxy_pool, target_size=app["pool_target"])
    app["token_pool"] = token_pool
    app["solver_tasks"] = await token_pool.start()
    print("[*] TTS API server (pool mode) ready", flush=True)


async def on_cleanup(app):
    """Cleanup on shutdown."""
    token_pool = app.get("token_pool")
    if token_pool:
        await token_pool.stop()
    for task in app.get("solver_tasks", []):
        task.cancel()


def create_app(pool_target: int = 5):
    app = web.Application()
    app["pool_target"] = pool_target
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_post("/tts", handle_tts)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/voices", handle_voices)
    app.router.add_get("/proxy/keys", handle_proxy_keys)
    app.router.add_post("/proxy/add", handle_proxy_add)
    app.router.add_post("/proxy/remove", handle_proxy_remove)
    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ElevenLabs TTS API Server (Token Pool)")
    parser.add_argument("--port", type=int, default=8900)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--pool-size", type=int, default=5, help="Target token pool size")
    args = parser.parse_args()

    print(f"[*] Starting TTS API (pool mode) on {args.host}:{args.port}", flush=True)
    print(f"[*] Token pool target: {args.pool_size}", flush=True)
    print(f"[*] POST /tts  — generate audio (fast, pre-solved tokens)", flush=True)
    print(f"[*] GET /health — health check + pool stats", flush=True)
    print(f"[*] GET /voices — list voices", flush=True)

    app = create_app(pool_target=args.pool_size)
    web.run_app(app, host=args.host, port=args.port, print=None)
