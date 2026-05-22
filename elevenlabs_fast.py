"""
ElevenLabs Anonymous TTS - Ultra Fast (Node.js HSW Solver + Proxy Rotation)

Architecture:
- Node.js subprocess (Playwright headless Chromium) for HSW proof-of-work (~1s vs 15s)
- hCaptcha flow via HTTP (tls_client) - no browser for main flow
- Proxy rotation via proxyxoay.shop API
- Each proxy IP gets ~3 TTS requests before rotation

Speed: ~3-5s per TTS file (vs 25s before)

Usage:
    python elevenlabs_fast.py "Hello world" -o output.mp3
    python elevenlabs_fast.py "Text" --batch 100 -o batch/audio.mp3
    python elevenlabs_fast.py --file input.txt -o output.mp3 --lang vi

Requires: pip install tls_client PyJWT httpx[socks]
          npm install playwright (+ npx playwright install chromium)
"""

import asyncio
import argparse
import json
import os
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path

import httpx
import jwt
import tls_client

warnings.filterwarnings("ignore")

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

SCRIPT_DIR = Path(__file__).parent


class NodeHSWSolver:
    """Manages a persistent Node.js process for fast HSW solving (~1s per solve)."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def start(self):
        """Start the Node.js solver process."""
        if self._proc and self._proc.poll() is None:
            return

        self._proc = subprocess.Popen(
            ['node', str(SCRIPT_DIR / 'hsw_solver.js')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(SCRIPT_DIR),
        )
        # Wait for ready signal
        line = self._proc.stdout.readline()
        msg = json.loads(line)
        if not msg.get('ready'):
            raise RuntimeError(f"Solver failed to start: {line}")

    def stop(self):
        """Stop the solver process."""
        if self._proc and self._proc.poll() is None:
            try:
                self._send({'cmd': 'quit'})
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    def _send(self, msg: dict) -> dict:
        """Send command and get response."""
        self._proc.stdin.write(json.dumps(msg) + '\n')
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("Solver process died")
        return json.loads(line)

    def init_browser(self, proxy_server: str = None):
        """Initialize/reinitialize browser with optional proxy."""
        self.start()
        resp = self._send({'cmd': 'init', 'proxy': proxy_server})
        if not resp.get('ok'):
            raise RuntimeError(f"Browser init failed: {resp.get('error')}")

    def solve(self, hsw_js: str, req_token: str) -> str:
        """Solve HSW proof-of-work. Returns proof token. ~1s."""
        resp = self._send({'cmd': 'solve', 'hsw_js': hsw_js, 'req_token': req_token})
        if not resp.get('ok'):
            raise RuntimeError(f"HSW solve failed: {resp.get('error')}")
        return resp['token']


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

        http_raw = data["proxyhttp"].rstrip(":")
        socks5_raw = data["proxysocks5"].rstrip(":")

        self.current_http = f"http://{http_raw}"
        self.current_socks5 = f"socks5://{socks5_raw}"
        self.current_ip = http_raw.split(":")[0]
        self._rotate_count += 1

        carrier = data.get("Nha Mang", "?")
        location = data.get("Vi Tri", "?")
        print(f"  [proxy] #{self._rotate_count}: {http_raw} ({carrier}/{location})", file=sys.stderr)

        return {"http": self.current_http, "socks5": self.current_socks5, "raw": http_raw}


def get_hcaptcha_token_sync(solver: NodeHSWSolver, proxy: dict = None) -> str:
    """
    Full hCaptcha token generation:
    1. checksiteconfig → JWT
    2. Decode JWT → hsw.js URL
    3. Solve HSW via Node.js (~1s)
    4. getcaptcha → passive pass token
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

    # Step 1: Get version
    api_js = session.get('https://hcaptcha.com/1/api.js?render=explicit&onload=hcaptchaOnLoad').text
    version_matches = re.findall(r'v1/([A-Za-z0-9]+)/static', api_js)
    version = version_matches[1] if len(version_matches) > 1 else "unknown"

    # Step 2: checksiteconfig
    config_resp = session.post("https://api2.hcaptcha.com/checksiteconfig", params={
        'v': version, 'host': HOST, 'sitekey': SITEKEY,
        'sc': '1', 'swa': '1', 'spst': '1',
    })
    config = config_resp.json()

    if 'c' not in config or 'req' not in config.get('c', {}):
        raise RuntimeError(f"checksiteconfig failed: {config}")

    req_token = config['c']['req']

    # Step 3: Get hsw.js and solve
    decoded = jwt.decode(req_token, options={"verify_signature": False})
    hsw_url = "https://newassets.hcaptcha.com" + decoded["l"] + "/hsw.js"
    hsw_js = session.get(hsw_url).text

    hsw_token = solver.solve(hsw_js, req_token)
    if not hsw_token:
        raise RuntimeError("HSW solve returned empty")

    # Step 4: getcaptcha
    import random
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

    captcha_resp = session.post(f"https://api2.hcaptcha.com/getcaptcha/{SITEKEY}", data=captcha_data)
    captcha = captcha_resp.json()

    if 'generated_pass_UUID' in captcha:
        return captcha['generated_pass_UUID']

    if 'tasklist' in captcha:
        raise RuntimeError("Got image challenge (detected as bot)")

    raise RuntimeError(f"Unexpected response: {list(captcha.keys())}")


class ElevenLabsTTS:
    """ElevenLabs TTS with fast headless hCaptcha bypass + proxy rotation."""

    def __init__(self, requests_per_ip: int = REQUESTS_PER_IP):
        self.requests_per_ip = requests_per_ip
        self.proxy_rotator = ProxyRotator()
        self.solver = NodeHSWSolver()
        self._current_proxy: dict | None = None
        self._ip_request_count = 0
        self._total_success = 0
        self._total_fail = 0

    async def _ensure_fresh_proxy(self):
        """Rotate proxy if needed and reinit browser."""
        if self._ip_request_count >= self.requests_per_ip or self._current_proxy is None:
            proxy_info = await self.proxy_rotator.rotate()
            self._current_proxy = proxy_info
            self._ip_request_count = 0
            # Reinit browser with new proxy
            self.solver.init_browser(proxy_info["http"])

    async def generate(
        self,
        text: str,
        voice_id: str = DEFAULT_VOICE,
        model_id: str = DEFAULT_MODEL,
        speed: float = 1.0,
        language: str = "en",
    ) -> bytes:
        """Generate TTS audio. ~3-5s per call."""
        for attempt in range(MAX_RETRIES):
            await self._ensure_fresh_proxy()

            # Get hcaptcha token
            try:
                t0 = time.time()
                token = get_hcaptcha_token_sync(self.solver, self._current_proxy)
                t_token = time.time() - t0
                print(f"  [token] OK ({t_token:.1f}s)", file=sys.stderr)
            except RuntimeError as e:
                print(f"  [token] FAIL: {e}", file=sys.stderr)
                self._total_fail += 1
                self._ip_request_count = self.requests_per_ip  # force rotate
                await asyncio.sleep(2)
                continue

            # Call TTS API
            url = f"{API_BASE}/v1/text-to-speech/{voice_id}/anonymous"
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": {"speed": speed},
                "hcaptcha_token": token,
                "language_code": language,
            }

            try:
                async with httpx.AsyncClient(
                    proxy=self._current_proxy["socks5"],
                    timeout=60.0, verify=False
                ) as client:
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
                    print(f"  [api] IP quota hit, rotating...", file=sys.stderr)
                    self._ip_request_count = self.requests_per_ip
                    self._total_fail += 1
                    continue
                else:
                    print(f"  [api] 401: {body[:100]}", file=sys.stderr)
                    self._total_fail += 1
                    await asyncio.sleep(1)
                    continue
            elif resp.status_code == 429:
                print(f"  [api] Rate limited, rotating...", file=sys.stderr)
                self._ip_request_count = self.requests_per_ip
                await asyncio.sleep(3)
                continue
            else:
                raise RuntimeError(f"API {resp.status_code}: {resp.text[:200]}")

        raise RuntimeError(f"Failed after {MAX_RETRIES} attempts")

    async def generate_batch(self, texts: list[str], **kwargs) -> list[bytes]:
        """Generate multiple TTS files."""
        results = []
        for i, text in enumerate(texts):
            print(f"\n[{i+1}/{len(texts)}]", file=sys.stderr)
            try:
                audio = await self.generate(text, **kwargs)
                results.append(audio)
                print(f"  [OK] {len(audio)} bytes (success={self._total_success} fail={self._total_fail})", file=sys.stderr)
            except RuntimeError as e:
                print(f"  [FAIL] {e}", file=sys.stderr)
                results.append(None)
        return results

    def stop(self):
        self.solver.stop()


async def main():
    parser = argparse.ArgumentParser(description="ElevenLabs TTS - Fast headless (Node.js HSW ~1s)")
    parser.add_argument("text", nargs="?", help="Text to speak")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--output", "-o", default="output.mp3")
    parser.add_argument("--batch", type=int, default=0)
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
            print(f"[*] Estimated time: {args.batch * 4}s ({args.batch * 4 / 60:.1f}min)", file=sys.stderr)
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
            print(f"[DONE] {saved}/{args.batch} in {elapsed:.1f}s ({elapsed/max(saved,1):.1f}s/file)", file=sys.stderr)
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
        tts.stop()


if __name__ == "__main__":
    asyncio.run(main())
