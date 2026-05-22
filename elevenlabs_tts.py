"""
ElevenLabs Anonymous TTS - hCaptcha Bypass with Proxy Rotation (proxyxoay.shop)
Automatically rotates Vietnamese proxies to bypass per-IP anonymous quota limits.

Strategy:
- Each IP gets ~3 anonymous TTS requests before quota exhaustion
- After quota hit, rotate to new proxy IP via proxyxoay.shop API
- hCaptcha solved via CDP in anti-detection browser (patchright + chrome)
- Browser traffic routed through proxy so hcaptcha token matches API request IP

Usage:
    python elevenlabs_tts.py "Hello world" -o output.mp3
    python elevenlabs_tts.py "Text" --batch 100 -o batch/audio.mp3
    python elevenlabs_tts.py --file input.txt -o output.mp3
    python elevenlabs_tts.py "Text" --voice JBFqnCBsd6RMkjVDRZzb --lang vi

Requires: pip install patchright httpx
Browser: Google Chrome must be installed
"""

import asyncio
import argparse
import os
import sys
import time
import random
import httpx
from pathlib import Path
from patchright.async_api import async_playwright

# Constants
SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
API_BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE = "NOpBlnGInO9m6vDvFkFC"  # Aria
DEFAULT_MODEL = "eleven_v3"
MAX_RETRIES = 5
REQUESTS_PER_IP = 3  # rotate proxy after this many successful requests
TOKEN_COOLDOWN = 2.0  # seconds between token generations

# Proxy config
PROXY_API = "https://proxyxoay.shop/api/get.php"
PROXY_KEY = "mrpLsUPXIBPnVFoQqAOVzu"


class ProxyRotator:
    """Manages proxy rotation via proxyxoay.shop API."""

    def __init__(self, key: str = PROXY_KEY):
        self.key = key
        self.current_proxy: str | None = None
        self.current_socks5: str | None = None
        self.proxy_info: dict = {}
        self._rotate_count = 0

    async def rotate(self) -> str:
        """Get a new proxy IP from the API. Returns http proxy string."""
        url = f"{PROXY_API}?key={self.key}&nhamang=random&tinhthanh=0&whitelist="

        for retry in range(10):
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                data = resp.json()

            if data.get("status") == 100:
                break
            elif data.get("status") == 101:
                # "Con Xs moi co the doi proxy" - need to wait
                wait = 5 + retry * 2
                print(f"[*] Proxy API cooldown, waiting {wait}s... (retry {retry+1}/10)", file=sys.stderr)
                await asyncio.sleep(wait)
                continue
            else:
                raise RuntimeError(f"Proxy API error: {data}")
        else:
            raise RuntimeError(f"Proxy API failed after 10 retries")

        # Parse proxy - format is "ip:port::" (no auth)
        http_proxy = data["proxyhttp"].rstrip(":")  # "ip:port"
        socks5_proxy = data["proxysocks5"].rstrip(":")

        self.current_proxy = f"http://{http_proxy}"
        self.current_socks5 = f"socks5://{socks5_proxy}"
        self.proxy_info = data
        self._rotate_count += 1

        ttl = data.get("message", "")
        location = data.get("Vi Tri", "?")
        carrier = data.get("Nha Mang", "?")
        print(f"[*] New proxy #{self._rotate_count}: {http_proxy} ({carrier}/{location}) - {ttl}", file=sys.stderr)

        return self.current_proxy

    @property
    def http_proxy(self) -> str | None:
        return self.current_proxy

    @property
    def socks5_proxy(self) -> str | None:
        return self.current_socks5


class HCaptchaSolver:
    """Manages browser with proxy to solve hCaptcha invisible challenges."""

    def __init__(self, headless=False):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._ready = False
        self._solve_count = 0

    async def start(self, proxy: str = None):
        """Launch browser with optional proxy and navigate to elevenlabs.io."""
        if self.playwright is None:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )

        # Close old context if exists
        if self.context:
            await self.context.close()

        # Create new context with proxy
        context_opts = {
            "viewport": {"width": 1280, "height": 720},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "ignore_https_errors": True,
        }
        if proxy:
            context_opts["proxy"] = {"server": proxy}

        self.context = await self.browser.new_context(**context_opts)
        self.page = await self.context.new_page()

        await self.page.goto("https://elevenlabs.io", wait_until="domcontentloaded", timeout=30000)
        await self.page.wait_for_function(
            "() => typeof window.hcaptcha !== 'undefined' && typeof window.hcaptcha.render === 'function'",
            timeout=20000,
        )
        self._ready = True
        self._solve_count = 0

    async def get_token(self) -> str:
        """Generate a fresh hCaptcha token via CDP in main world."""
        if not self._ready:
            raise RuntimeError("Solver not started")

        cdp = await self.context.new_cdp_session(self.page)
        try:
            result = await cdp.send("Runtime.evaluate", {
                "expression": """
                    (async () => {
                        return new Promise((resolve, reject) => {
                            const div = document.createElement('div');
                            div.style.cssText = 'position:absolute;left:-9999px';
                            document.body.appendChild(div);
                            const wid = window.hcaptcha.render(div, {
                                sitekey: '""" + SITEKEY + """',
                                size: 'invisible',
                                callback: (t) => { resolve(t); div.remove(); },
                                'error-callback': (e) => { reject(String(e)); div.remove(); }
                            });
                            window.hcaptcha.execute(wid);
                            setTimeout(() => { reject('hcaptcha timeout'); div.remove(); }, 20000);
                        });
                    })()
                """,
                "awaitPromise": True,
                "returnByValue": True,
            })
        finally:
            await cdp.detach()

        if "exceptionDetails" in result:
            err = result["exceptionDetails"]
            err_text = err.get("text", "") or str(err.get("exception", {}).get("value", ""))
            raise RuntimeError(f"hCaptcha error: {err_text}")

        self._solve_count += 1
        return result["result"]["value"]

    async def stop(self):
        """Close everything."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


class ElevenLabsTTS:
    """ElevenLabs TTS with automatic proxy rotation for unlimited anonymous generation."""

    def __init__(self, headless: bool = False, requests_per_ip: int = REQUESTS_PER_IP):
        self.headless = headless
        self.requests_per_ip = requests_per_ip
        self.proxy_rotator = ProxyRotator()
        self.solver = HCaptchaSolver(headless=headless)
        self._ip_request_count = 0
        self._total_success = 0
        self._total_fail = 0

    async def _ensure_fresh_proxy(self):
        """Rotate proxy and restart browser context if needed."""
        need_rotate = (
            self._ip_request_count >= self.requests_per_ip
            or self.proxy_rotator.current_proxy is None
        )

        if need_rotate:
            proxy = await self.proxy_rotator.rotate()
            socks5 = self.proxy_rotator.socks5_proxy
            print(f"[*] Starting browser session with new proxy...", file=sys.stderr)
            # Use SOCKS5 for browser (better HTTPS support)
            await self.solver.start(proxy=socks5)
            self._ip_request_count = 0
            print(f"[+] Session ready.", file=sys.stderr)

    async def start(self):
        """Initialize first proxy and browser session."""
        await self._ensure_fresh_proxy()

    async def generate(
        self,
        text: str,
        voice_id: str = DEFAULT_VOICE,
        model_id: str = DEFAULT_MODEL,
        speed: float = 1.0,
        language: str = "en",
    ) -> bytes:
        """Generate TTS audio. Auto-rotates proxy on quota exhaustion."""
        for attempt in range(MAX_RETRIES):
            await self._ensure_fresh_proxy()

            # Cooldown between requests
            if self._ip_request_count > 0:
                await asyncio.sleep(TOKEN_COOLDOWN + random.uniform(0, 1.5))

            # Get hcaptcha token
            try:
                token = await self.solver.get_token()
            except RuntimeError as e:
                print(f"[!] Token gen failed: {e}", file=sys.stderr)
                self._ip_request_count = self.requests_per_ip  # force rotate
                await asyncio.sleep(2)
                continue

            # Call TTS API through same proxy (so IP matches token)
            url = f"{API_BASE}/v1/text-to-speech/{voice_id}/anonymous"
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": {"speed": speed},
                "hcaptcha_token": token,
                "language_code": language,
            }

            proxy_url = self.proxy_rotator.socks5_proxy
            try:
                async with httpx.AsyncClient(proxy=proxy_url, timeout=60.0, verify=False) as client:
                    resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            except Exception as e:
                print(f"[!] Request error: {e}", file=sys.stderr)
                self._ip_request_count = self.requests_per_ip  # force rotate
                continue

            if resp.status_code == 200:
                self._ip_request_count += 1
                self._total_success += 1
                return resp.content
            elif resp.status_code == 401:
                body = resp.text
                if "quota_exceeded" in body or "sign_in_required" in body:
                    print(f"[!] IP quota exhausted, rotating...", file=sys.stderr)
                    self._ip_request_count = self.requests_per_ip  # force rotate
                    self._total_fail += 1
                    continue
                else:
                    print(f"[!] Token rejected (attempt {attempt+1})", file=sys.stderr)
                    self._total_fail += 1
                    await asyncio.sleep(2)
                    continue
            elif resp.status_code == 429:
                print(f"[!] Rate limited, rotating proxy...", file=sys.stderr)
                self._ip_request_count = self.requests_per_ip
                await asyncio.sleep(5)
                continue
            else:
                raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")

        raise RuntimeError(f"Failed after {MAX_RETRIES} attempts")

    async def generate_batch(
        self,
        texts: list[str],
        voice_id: str = DEFAULT_VOICE,
        **kwargs,
    ) -> list[bytes]:
        """Generate multiple TTS audio files with automatic proxy rotation."""
        results = []
        for i, text in enumerate(texts):
            print(f"\n[*] === Generating {i+1}/{len(texts)} ===", file=sys.stderr)
            try:
                audio = await self.generate(text, voice_id=voice_id, **kwargs)
                results.append(audio)
                print(f"[+] {i+1}/{len(texts)} OK ({len(audio)} bytes) [success: {self._total_success}, fail: {self._total_fail}]", file=sys.stderr)
            except RuntimeError as e:
                print(f"[-] {i+1}/{len(texts)} FAILED: {e}", file=sys.stderr)
                results.append(None)
        return results

    async def stop(self):
        await self.solver.stop()


async def main():
    parser = argparse.ArgumentParser(description="ElevenLabs TTS - hCaptcha bypass + proxy rotation")
    parser.add_argument("text", nargs="?", help="Text to convert to speech")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Voice ID (default: {DEFAULT_VOICE} / Aria)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--speed", type=float, default=1.0, help="Speed (default: 1.0)")
    parser.add_argument("--lang", default="en", help="Language code (default: en)")
    parser.add_argument("--output", "-o", default="output.mp3", help="Output file")
    parser.add_argument("--batch", type=int, default=0, help="Generate N audio files")
    parser.add_argument("--headless", action="store_true", default=False, help="Run headless")
    parser.add_argument("--file", "-f", help="Read text from file")
    parser.add_argument("--requests-per-ip", type=int, default=REQUESTS_PER_IP, help=f"Requests before rotating (default: {REQUESTS_PER_IP})")
    args = parser.parse_args()

    # Get text
    if args.file:
        text = Path(args.file).read_text().strip()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide text or --file")
        return

    # Override requests per IP if specified
    if args.requests_per_ip != REQUESTS_PER_IP:
        tts = ElevenLabsTTS(headless=args.headless, requests_per_ip=args.requests_per_ip)
    else:
        tts = ElevenLabsTTS(headless=args.headless)

    try:
        await tts.start()

        if args.batch > 0:
            n_proxies_needed = (args.batch + REQUESTS_PER_IP - 1) // REQUESTS_PER_IP
            print(f"[*] Batch: {args.batch} files, ~{n_proxies_needed} proxy rotations needed", file=sys.stderr)
            start = time.time()
            texts = [text] * args.batch
            results = await tts.generate_batch(texts, voice_id=args.voice, model_id=args.model, speed=args.speed, language=args.lang)
            elapsed = time.time() - start

            # Save files
            out_path = Path(args.output)
            out_dir = out_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = out_path.stem
            suffix = out_path.suffix or ".mp3"

            saved = 0
            for i, audio in enumerate(results):
                if audio:
                    fname = out_dir / f"{stem}_{i+1:03d}{suffix}"
                    fname.write_bytes(audio)
                    saved += 1

            print(f"\n{'='*50}", file=sys.stderr)
            print(f"[+] DONE! {saved}/{args.batch} files saved in {elapsed:.1f}s ({elapsed/max(saved,1):.1f}s/file)", file=sys.stderr)
            print(f"[+] Stats: {tts._total_success} success, {tts._total_fail} retries", file=sys.stderr)
            print(f"[+] Proxies used: {tts.proxy_rotator._rotate_count}", file=sys.stderr)
        else:
            print(f"[*] Generating: {text[:60]}...", file=sys.stderr)
            start = time.time()
            audio = await tts.generate(text, voice_id=args.voice, model_id=args.model, speed=args.speed, language=args.lang)
            elapsed = time.time() - start

            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(audio)
            print(f"[+] Saved: {out_path} ({len(audio)} bytes, {elapsed:.1f}s)", file=sys.stderr)

    finally:
        await tts.stop()


if __name__ == "__main__":
    asyncio.run(main())
