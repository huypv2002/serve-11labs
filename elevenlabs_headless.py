"""
ElevenLabs Anonymous TTS - Headless hCaptcha Bypass (Camoufox + Proxy Rotation)

Architecture:
- hCaptcha flow done entirely via HTTP (tls_client) - no browser needed for main flow
- Camoufox headless ONLY for executing hsw.js proof-of-work (fast, ~500ms)
- Proxy rotation via proxyxoay.shop API
- Each proxy IP gets ~3 TTS requests before rotation

This is MUCH faster than the browser-based approach because:
1. No need to load elevenlabs.io in browser
2. No need to wait for hcaptcha widget to render
3. HSW execution in Camoufox is ~500ms
4. Direct API calls are instant

Usage:
    python elevenlabs_headless.py "Hello world" -o output.mp3
    python elevenlabs_headless.py "Text" --batch 100 -o batch/audio.mp3
    python elevenlabs_headless.py --file input.txt -o output.mp3 --lang vi

Requires: pip install camoufox tls_client PyJWT httpx[socks]
"""

import asyncio
import argparse
import json
import os
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

warnings.filterwarnings("ignore", message=".*proxy.*geoip.*")

# Constants
SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
HOST = "elevenlabs.io"
API_BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE = "NOpBlnGInO9m6vDvFkFC"  # Aria
DEFAULT_MODEL = "eleven_v3"
REQUESTS_PER_IP = 3
MAX_RETRIES = 5

# Proxy config
PROXY_API = "https://proxyxoay.shop/api/get.php"
PROXY_KEY = "mrpLsUPXIBPnVFoQqAOVzu"


class ProxyRotator:
    """Manages proxy rotation via proxyxoay.shop API."""

    def __init__(self, key: str = PROXY_KEY):
        self.key = key
        self.current_http: str | None = None
        self.current_socks5: str | None = None
        self.current_ip: str | None = None
        self._rotate_count = 0

    async def rotate(self) -> dict:
        """Get a new proxy. Returns dict with http and socks5 URLs."""
        url = f"{PROXY_API}?key={self.key}&nhamang=random&tinhthanh=0&whitelist="

        for retry in range(10):
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                data = resp.json()

            if data.get("status") == 100:
                break
            elif data.get("status") == 101:
                wait = 5 + retry * 2
                print(f"  [proxy] cooldown {wait}s (retry {retry+1}/10)", file=sys.stderr)
                await asyncio.sleep(wait)
                continue
            else:
                raise RuntimeError(f"Proxy API error: {data}")
        else:
            raise RuntimeError("Proxy API failed after 10 retries")

        # Parse - format "ip:port::"
        http_raw = data["proxyhttp"].rstrip(":")
        socks5_raw = data["proxysocks5"].rstrip(":")

        self.current_http = f"http://{http_raw}"
        self.current_socks5 = f"socks5://{socks5_raw}"
        self.current_ip = http_raw.split(":")[0]
        self._rotate_count += 1

        carrier = data.get("Nha Mang", "?")
        location = data.get("Vi Tri", "?")
        ttl = data.get("message", "")
        print(f"  [proxy] #{self._rotate_count}: {http_raw} ({carrier}/{location}) {ttl}", file=sys.stderr)

        return {"http": self.current_http, "socks5": self.current_socks5}


async def solve_hsw(req_token: str, proxy: dict = None, browser_context=None) -> str:
    """
    Execute hCaptcha HSW proof-of-work in Camoufox headless.
    If browser_context is provided, reuses it (much faster).
    ~500ms per solve when reusing context, ~10s cold start.
    """
    # Create tls_client session for fetching hsw.js
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9',
        'sec-ch-ua': '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'script',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'cross-site',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }

    if proxy:
        session.proxies = {'http': proxy["http"], 'https': proxy["http"]}

    # Decode JWT to get hsw.js URL
    decoded = jwt.decode(req_token, options={"verify_signature": False})
    hsw_url = "https://newassets.hcaptcha.com" + decoded["l"] + "/hsw.js"
    hsw_js = session.get(hsw_url).text

    if browser_context:
        # Reuse existing context - much faster
        page = await browser_context.new_page()
        try:
            await page.route(f"https://{HOST}/hsw", lambda r: r.fulfill(
                status=200, content_type="text/html",
                body="<html><head></head><body></body></html>"
            ))
            await page.goto(f"https://{HOST}/hsw", wait_until='domcontentloaded', timeout=5000)
            await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => false})")

            # Inject hsw.js - try multiple methods
            injected = False
            
            # Method 1: add_script_tag
            try:
                await page.add_script_tag(content=hsw_js)
                await asyncio.sleep(0.1)
                if await page.evaluate("typeof hsw === 'function'"):
                    injected = True
            except Exception:
                pass

            # Method 2: evaluate as IIFE
            if not injected:
                try:
                    await page.evaluate(f"""
                        (function() {{
                            const script = document.createElement('script');
                            script.textContent = {json.dumps(hsw_js)};
                            (document.head || document.documentElement).appendChild(script);
                        }})();
                    """)
                    await asyncio.sleep(0.1)
                    if await page.evaluate("typeof hsw === 'function'"):
                        injected = True
                except Exception:
                    pass

            # Method 3: direct evaluate of the script content
            if not injected:
                await page.evaluate(hsw_js)
                await asyncio.sleep(0.1)
                if await page.evaluate("typeof hsw === 'function'"):
                    injected = True

            if not injected:
                raise RuntimeError("hsw function not available after all injection methods")

            result = await page.evaluate("(req) => hsw(req)", req_token)
            return result
        finally:
            await page.close()
    else:
        # Cold start - launch new browser
        browser_opts = {
            'headless': True,
            'os': 'macos',
        }
        if proxy:
            browser_opts['proxy'] = {'server': proxy["http"]}

        async with AsyncCamoufox(**browser_opts) as browser:
            context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = await context.new_page()

            await page.route(f"https://{HOST}/", lambda r: r.fulfill(
                status=200, content_type="text/html",
                body="<html><head></head><body></body></html>"
            ))
            await page.goto(f"https://{HOST}/", wait_until='domcontentloaded', timeout=5000)
            await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => false})")

            try:
                await page.add_script_tag(content=hsw_js)
            except Exception:
                await page.evaluate(f"""
                    (function() {{
                        const script = document.createElement('script');
                        script.textContent = {json.dumps(hsw_js)};
                        (document.head || document.documentElement).appendChild(script);
                    }})();
                """)

            for _ in range(50):
                if await page.evaluate("typeof hsw === 'function'"):
                    break
                await asyncio.sleep(0.02)

            result = await page.evaluate("(req) => hsw(req)", req_token)
            return result


async def get_hcaptcha_token(proxy: dict = None, browser_context=None) -> str:
    """
    Full hCaptcha token generation flow via direct API calls.
    No browser needed for the main flow - only Camoufox for HSW.
    """
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)

    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
    session.headers = {
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://newassets.hcaptcha.com',
        'referer': 'https://newassets.hcaptcha.com/',
        'sec-ch-ua': '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
        'user-agent': ua,
    }

    if proxy:
        session.proxies = {'http': proxy["http"], 'https': proxy["http"]}

    # Step 1: Get hcaptcha version
    api_js = session.get('https://hcaptcha.com/1/api.js?render=explicit&onload=hcaptchaOnLoad').text
    version_matches = re.findall(r'v1/([A-Za-z0-9]+)/static', api_js)
    version = version_matches[1] if len(version_matches) > 1 else "c3663008fb8d8104807d55045f8251cbe96a2f84"

    # Step 2: checksiteconfig - get challenge JWT
    config_resp = session.post("https://api2.hcaptcha.com/checksiteconfig", params={
        'v': version,
        'host': HOST,
        'sitekey': SITEKEY,
        'sc': '1',
        'swa': '1',
        'spst': '1',
    })
    config = config_resp.json()

    if 'c' not in config or 'req' not in config.get('c', {}):
        raise RuntimeError(f"checksiteconfig failed: {config}")

    req_token = config['c']['req']

    # Step 3: Solve HSW proof-of-work in Camoufox
    hsw_token = await solve_hsw(req_token, proxy, browser_context)
    if not hsw_token:
        raise RuntimeError("HSW solve failed")

    # Step 4: getcaptcha - submit proof and get passcode
    # Generate minimal motion data
    motion_data = {
        "st": int(time.time() * 1000),
        "dct": int(time.time() * 1000),
        "mm": [[random.randint(100, 800), random.randint(100, 600), random.randint(10, 500)] for _ in range(3)],
    }

    captcha_data = {
        'v': version,
        'sitekey': SITEKEY,
        'host': HOST,
        'hl': 'en',
        'motionData': json.dumps(motion_data),
        'n': hsw_token,
        'c': json.dumps(config['c']),
    }

    captcha_resp = session.post(
        f"https://api2.hcaptcha.com/getcaptcha/{SITEKEY}",
        data=captcha_data,
    )
    captcha = captcha_resp.json()

    # Check for passive pass (invisible captcha auto-pass)
    if 'generated_pass_UUID' in captcha:
        return captcha['generated_pass_UUID']

    # If we get a challenge (shouldn't happen for invisible), it means detection
    if 'tasklist' in captcha:
        raise RuntimeError(f"Got image challenge instead of passive pass (detected as bot)")

    if not captcha.get('success', True):
        raise RuntimeError(f"getcaptcha failed: {captcha.get('error-codes', [])}")

    raise RuntimeError(f"Unexpected getcaptcha response: {list(captcha.keys())}")


class ElevenLabsTTS:
    """ElevenLabs TTS with headless hCaptcha bypass + proxy rotation."""

    def __init__(self, requests_per_ip: int = REQUESTS_PER_IP):
        self.requests_per_ip = requests_per_ip
        self.proxy_rotator = ProxyRotator()
        self._current_proxy: dict | None = None
        self._ip_request_count = 0
        self._total_success = 0
        self._total_fail = 0
        self._browser = None
        self._browser_context = None
        self._camoufox_cm = None

    async def _start_browser(self, proxy: dict = None):
        """Start or restart Camoufox browser with given proxy."""
        await self._stop_browser()

        browser_opts = {
            'headless': True,
            'os': 'macos',
        }
        if proxy:
            browser_opts['proxy'] = {'server': proxy["http"]}

        self._camoufox_cm = AsyncCamoufox(**browser_opts)
        self._browser = await self._camoufox_cm.__aenter__()
        self._browser_context = await self._browser.new_context(viewport={'width': 1920, 'height': 1080})

    async def _stop_browser(self):
        """Stop current browser."""
        if self._browser_context:
            try:
                await self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
        if self._camoufox_cm:
            try:
                await self._camoufox_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._camoufox_cm = None
            self._browser = None

    async def _ensure_fresh_proxy(self):
        """Rotate proxy if needed and restart browser."""
        if self._ip_request_count >= self.requests_per_ip or self._current_proxy is None:
            proxy_info = await self.proxy_rotator.rotate()
            self._current_proxy = proxy_info
            self._ip_request_count = 0
            await self._start_browser(proxy_info)

    async def generate(
        self,
        text: str,
        voice_id: str = DEFAULT_VOICE,
        model_id: str = DEFAULT_MODEL,
        speed: float = 1.0,
        language: str = "en",
    ) -> bytes:
        """Generate TTS audio with automatic proxy rotation."""
        for attempt in range(MAX_RETRIES):
            await self._ensure_fresh_proxy()

            # Get hcaptcha token via headless HSW solve
            try:
                t0 = time.time()
                token = await get_hcaptcha_token(self._current_proxy, self._browser_context)
                t_token = time.time() - t0
                print(f"  [token] OK ({t_token:.1f}s)", file=sys.stderr)
            except RuntimeError as e:
                print(f"  [token] FAIL: {e}", file=sys.stderr)
                self._total_fail += 1
                self._ip_request_count = self.requests_per_ip  # force rotate
                await asyncio.sleep(2)
                continue

            # Call TTS API through same proxy
            url = f"{API_BASE}/v1/text-to-speech/{voice_id}/anonymous"
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": {"speed": speed},
                "hcaptcha_token": token,
                "language_code": language,
            }

            try:
                async with httpx.AsyncClient(proxy=self._current_proxy["socks5"], timeout=60.0, verify=False) as client:
                    resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            except Exception as e:
                print(f"  [api] Request error: {e}", file=sys.stderr)
                self._ip_request_count = self.requests_per_ip
                continue

            if resp.status_code == 200:
                self._ip_request_count += 1
                self._total_success += 1
                return resp.content
            elif resp.status_code == 401:
                body = resp.text
                if "quota_exceeded" in body or "sign_in_required" in body:
                    print(f"  [api] IP quota exhausted, rotating...", file=sys.stderr)
                    self._ip_request_count = self.requests_per_ip
                    self._total_fail += 1
                    continue
                else:
                    print(f"  [api] Token rejected (attempt {attempt+1}): {body[:100]}", file=sys.stderr)
                    self._total_fail += 1
                    await asyncio.sleep(1)
                    continue
            elif resp.status_code == 429:
                print(f"  [api] Rate limited, rotating...", file=sys.stderr)
                self._ip_request_count = self.requests_per_ip
                await asyncio.sleep(3)
                continue
            else:
                raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")

        raise RuntimeError(f"Failed after {MAX_RETRIES} attempts")

    async def generate_batch(self, texts: list[str], **kwargs) -> list[bytes]:
        """Generate multiple TTS files."""
        results = []
        for i, text in enumerate(texts):
            print(f"\n[{i+1}/{len(texts)}]", file=sys.stderr)
            try:
                audio = await self.generate(text, **kwargs)
                results.append(audio)
                print(f"  [OK] {len(audio)} bytes (total: {self._total_success} ok, {self._total_fail} fail)", file=sys.stderr)
            except RuntimeError as e:
                print(f"  [FAIL] {e}", file=sys.stderr)
                results.append(None)
        return results

    async def stop(self):
        await self._stop_browser()


async def main():
    parser = argparse.ArgumentParser(description="ElevenLabs TTS - Headless hCaptcha bypass + proxy rotation")
    parser.add_argument("text", nargs="?", help="Text to speak")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Voice ID (default: Aria)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model (default: {DEFAULT_MODEL})")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--lang", default="en", help="Language code")
    parser.add_argument("--output", "-o", default="output.mp3")
    parser.add_argument("--batch", type=int, default=0, help="Generate N files")
    parser.add_argument("--file", "-f", help="Read text from file")
    parser.add_argument("--requests-per-ip", type=int, default=REQUESTS_PER_IP)
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text().strip()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide text or --file")
        return

    tts = ElevenLabsTTS(requests_per_ip=args.requests_per_ip)

    try:
        if args.batch > 0:
            n_proxies = (args.batch + args.requests_per_ip - 1) // args.requests_per_ip
            print(f"[*] Batch: {args.batch} files, ~{n_proxies} proxy rotations", file=sys.stderr)
            start = time.time()
            results = await tts.generate_batch(
                [text] * args.batch,
                voice_id=args.voice, model_id=args.model,
                speed=args.speed, language=args.lang,
            )
            elapsed = time.time() - start

            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            stem, suffix = out_path.stem, out_path.suffix or ".mp3"

            saved = 0
            for i, audio in enumerate(results):
                if audio:
                    (out_path.parent / f"{stem}_{i+1:03d}{suffix}").write_bytes(audio)
                    saved += 1

            print(f"\n{'='*50}", file=sys.stderr)
            print(f"[DONE] {saved}/{args.batch} saved in {elapsed:.1f}s ({elapsed/max(saved,1):.1f}s/file)", file=sys.stderr)
            print(f"[STATS] success={tts._total_success} fail={tts._total_fail} proxies={tts.proxy_rotator._rotate_count}", file=sys.stderr)
        else:
            print(f"[*] Generating: {text[:60]}...", file=sys.stderr)
            start = time.time()
            audio = await tts.generate(text, voice_id=args.voice, model_id=args.model, speed=args.speed, language=args.lang)
            elapsed = time.time() - start

            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(audio)
            print(f"[DONE] {out_path} ({len(audio)} bytes, {elapsed:.1f}s)", file=sys.stderr)
    finally:
        await tts.stop()


if __name__ == "__main__":
    asyncio.run(main())
