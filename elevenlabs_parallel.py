"""
ElevenLabs TTS - 2-key round-robin batch with proxy rotation

Strategy:
  - 2 proxy keys, round-robin
  - Key1: chunks 1,2,3 → Key2: chunks 4,5,6 → Key1: chunks 7,8,9 → ...
  - While one key is in cooldown, the other key is working
  - Each IP handles 3 requests (ElevenLabs quota per IP)
  - 3 concurrent requests per IP for speed

100 chunks ≈ 17 minutes (vs 35 min with 1 key)

Usage:
    python elevenlabs_parallel.py "Text" --batch 100 -o out/audio.mp3 --lang vi
    python elevenlabs_parallel.py --file input.txt --batch 100 -o out/audio.mp3 --lang vi
"""

import asyncio
import argparse
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

warnings.filterwarnings("ignore")

SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
HOST = "elevenlabs.io"
API_BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE = "NOpBlnGInO9m6vDvFkFC"
DEFAULT_MODEL = "eleven_v3"
REQUESTS_PER_IP = 3

PROXY_API = "https://proxyxoay.shop/api/get.php"
PROXY_KEYS = [
    "mWaQAhVpDxNSxQMshMpfvV",
    "ejvdCkHfVufMQdUCefruiR",
]


class ProxyManager:
    """Manages 2 keys — ensures each worker gets a UNIQUE IP."""

    def __init__(self, keys: list[str]):
        self.keys = keys
        self._lock = asyncio.Lock()
        self._active_ips = set()  # IPs currently in use by workers

    async def get_proxy_for_key(self, key: str) -> dict:
        """Get proxy for a specific key, ensuring unique IP."""
        for attempt in range(10):
            proxy = await self._fetch_proxy(key)
            async with self._lock:
                if proxy["raw"] not in self._active_ips:
                    self._active_ips.add(proxy["raw"])
                    return proxy
            # Same IP as another worker — wait and retry
            print(f"  [{key[:6]}] got duplicate IP {proxy['raw']}, waiting for new IP...", file=sys.stderr)
            await asyncio.sleep(15)
        raise RuntimeError(f"Could not get unique IP for key {key[:8]}")

    async def release_ip(self, proxy: dict):
        """Release IP when worker is done with it."""
        async with self._lock:
            self._active_ips.discard(proxy["raw"])

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
                print(f"  [{key[:6]}] cooldown {wait}s...", file=sys.stderr)
                await asyncio.sleep(wait)
            else:
                raise RuntimeError(f"Proxy error: {data}")
        raise RuntimeError(f"Proxy timeout for key {key[:8]}")


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


async def generate_one(idx: int, text: str, voice_id: str, model_id: str,
                       speed: float, language: str, proxy: dict, browser) -> bytes:
    """Generate one TTS audio. Returns audio bytes or raises."""
    t0 = time.time()
    tag = f"#{idx+1}"

    # 1. Get hCaptcha materials (blocking → run in thread)
    print(f"    [{tag}] getting captcha materials...", file=sys.stderr)
    req_token, version, config = await asyncio.to_thread(get_hcaptcha_materials, proxy["http"])
    t1 = time.time()
    print(f"    [{tag}] materials OK ({t1-t0:.1f}s), solving HSW...", file=sys.stderr)

    # 2. Solve HSW
    hsw_token = await solve_hsw(req_token, proxy["http"], browser)
    t2 = time.time()
    print(f"    [{tag}] HSW solved ({t2-t1:.1f}s), submitting captcha...", file=sys.stderr)

    # 3. Submit captcha (blocking → run in thread)
    hcaptcha_token = await asyncio.to_thread(submit_captcha, hsw_token, version, config, proxy["http"])
    t3 = time.time()
    print(f"    [{tag}] captcha OK ({t3-t2:.1f}s), calling TTS API...", file=sys.stderr)

    t_token = t3 - t0

    # 4. TTS API call
    url = f"{API_BASE}/v1/text-to-speech/{voice_id}/anonymous"
    payload = {
        "text": text, "model_id": model_id,
        "voice_settings": {"speed": speed},
        "hcaptcha_token": hcaptcha_token,
        "language_code": language,
    }
    async with httpx.AsyncClient(proxy=proxy["http"], timeout=120.0, verify=False) as client:
        resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})

    t4 = time.time()
    if resp.status_code == 200:
        print(f"  [#{idx+1}] OK (total {t4-t0:.1f}s | token {t_token:.1f}s | api {t4-t3:.1f}s | {len(resp.content)}B)", file=sys.stderr)
        return resp.content
    elif resp.status_code == 401:
        print(f"  [#{idx+1}] 401 after {t4-t0:.1f}s: {resp.text[:60]}", file=sys.stderr)
        raise RuntimeError(f"401: {resp.text[:80]}")
    else:
        print(f"  [#{idx+1}] HTTP {resp.status_code} after {t4-t0:.1f}s: {resp.text[:60]}", file=sys.stderr)
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:80]}")


async def worker(worker_id: int, proxy_mgr: ProxyManager, task_queue: asyncio.Queue,
                 results: list, texts: list, voice_id: str, model_id: str,
                 speed: float, language: str, stats: dict,
                 out_dir: Path, stem: str, suffix: str, start_num: int,
                 key: str, stagger_delay: int = 0):
    """Worker: get unique proxy → open browser → gen 3 chunks → release IP → repeat."""
    if stagger_delay > 0:
        print(f"  [w{worker_id}] stagger delay {stagger_delay}s...", file=sys.stderr)
        await asyncio.sleep(stagger_delay)
    max_retries_per_chunk = 5
    while True:
        # Check if there's work to do
        if task_queue.empty():
            # Double check: any results still None means retry pending
            await asyncio.sleep(1)
            if task_queue.empty():
                break
        # Get proxy with unique IP
        proxy = None
        try:
            proxy = await proxy_mgr.get_proxy_for_key(key)
            print(f"\n[w{worker_id}|{key[:6]}] proxy: {proxy['raw']}", file=sys.stderr)
        except RuntimeError as e:
            print(f"  [w{worker_id}] proxy err: {e}", file=sys.stderr)
            await asyncio.sleep(5)
            continue

        # Collect up to 3 tasks for this IP
        batch_indices = []
        retry_items = []
        for _ in range(REQUESTS_PER_IP):
            try:
                idx = task_queue.get_nowait()
                batch_indices.append(idx)
            except asyncio.QueueEmpty:
                break

        if not batch_indices:
            await proxy_mgr.release_ip(proxy)
            break

        # Open Camoufox with this proxy, gen 3 chunks concurrently
        browser_opts = {
            'headless': True,
            'os': 'windows',
            'proxy': {'server': proxy["http"]},
        }

        try:
            async with AsyncCamoufox(**browser_opts) as browser:
                tasks = [
                    generate_one(i, texts[i], voice_id, model_id, speed, language, proxy, browser)
                    for i in batch_indices
                ]
                task_results = await asyncio.gather(*tasks, return_exceptions=True)

                for i, result in zip(batch_indices, task_results):
                    if isinstance(result, Exception):
                        err_str = str(result)
                        print(f"  [#{start_num+i}] FAIL: {err_str[:80]}", file=sys.stderr)
                        stats['fail'] += 1
                        # Retry immediately — put at front of queue (priority)
                        retry_items.append(i)
                        print(f"  [#{start_num+i}] → QUEUED FOR RETRY", file=sys.stderr)
                    else:
                        results[i] = result
                        stats['success'] += 1
                        # Save immediately
                        fpath = out_dir / f"{stem}_{start_num+i:04d}{suffix}"
                        fpath.write_bytes(result)
                        print(f"  [#{start_num+i}] SAVED → {fpath.name}", file=sys.stderr)

        except Exception as e:
            print(f"  [w{worker_id}] browser err: {str(e)[:80]}", file=sys.stderr)
            for i in batch_indices:
                if results[i] is None:
                    retry_items.append(i)
            await asyncio.sleep(3)
        finally:
            # Release IP so other worker won't get blocked
            if proxy:
                await proxy_mgr.release_ip(proxy)

        # Put retry items at front of queue (will be picked next)
        if retry_items:
            # Drain queue, prepend retries, put all back
            remaining = []
            while not task_queue.empty():
                try:
                    remaining.append(task_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for i in retry_items:
                task_queue.put_nowait(i)
            for i in remaining:
                task_queue.put_nowait(i)


async def main():
    parser = argparse.ArgumentParser(description="ElevenLabs TTS - 2-key round-robin batch")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--output", "-o", default="output.mp3")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--file", "-f")
    parser.add_argument("--chunks-file", help="File with chunks (---CHUNK N--- format)")
    parser.add_argument("--start", type=int, default=1, help="Start chunk number (1-indexed)")
    parser.add_argument("--count", type=int, default=0, help="Number of chunks to process (0=all)")
    parser.add_argument("--workers", "-w", type=int, default=2,
                        help="Number of parallel workers (default: 2 = number of keys)")
    args = parser.parse_args()

    # Load text(s)
    if args.chunks_file:
        # Parse chunks file
        import re as re2
        content = Path(args.chunks_file).read_text(encoding='utf-8')
        parts = re2.split(r'---CHUNK \d+---\n', content)
        all_chunks = [p.strip() for p in parts if p.strip()]
        
        # Apply start/count
        start_idx = args.start - 1
        if args.count > 0:
            all_chunks = all_chunks[start_idx:start_idx + args.count]
        else:
            all_chunks = all_chunks[start_idx:]
        
        texts = all_chunks
        print(f"[*] Loaded {len(texts)} chunks (from #{args.start})", file=sys.stderr)
    elif args.file:
        texts = [Path(args.file).read_text().strip()] * args.batch
    elif args.text:
        texts = [args.text] * args.batch
    else:
        parser.error("Provide text, --file, or --chunks-file")
        return

    num_files = len(texts)
    num_workers = min(args.workers, len(PROXY_KEYS))
    results = [None] * num_files
    stats = {'success': 0, 'fail': 0}

    task_queue = asyncio.Queue()
    for i in range(num_files):
        task_queue.put_nowait(i)

    proxy_mgr = ProxyManager(PROXY_KEYS)

    print(f"[*] {num_workers} workers, {len(PROXY_KEYS)} keys, {num_files} files", file=sys.stderr)
    print(f"[*] Round-robin: key1→3chunks, key2→3chunks, key1→3chunks...", file=sys.stderr)
    start = time.time()

    # Prepare output dir
    out_path = Path(args.output)
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem, suffix = out_path.stem, out_path.suffix or ".mp3"

    # Launch workers in parallel — each worker gets its own key
    # Stagger start: worker 1 delays 5s to avoid getting same proxy IP
    worker_tasks = [
        worker(i, proxy_mgr, task_queue, results, texts,
               args.voice, args.model, args.speed, args.lang, stats,
               out_dir, stem, suffix, args.start, PROXY_KEYS[i], stagger_delay=i*5)
        for i in range(num_workers)
    ]
    await asyncio.gather(*worker_tasks)

    elapsed = time.time() - start

    # Count saved
    saved = sum(1 for r in results if r is not None)

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"[DONE] {saved}/{num_files} saved in {elapsed:.1f}s ({elapsed/max(saved,1):.1f}s/file)", file=sys.stderr)
    print(f"[STATS] success={stats['success']} fail={stats['fail']}", file=sys.stderr)
    if saved > 0:
        print(f"[OUTPUT] {out_dir}/", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
