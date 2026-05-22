"""
Token Solver - Giải hCaptcha HSW bằng Camoufox
Logic từ elevenlabs_api_pool.py (đang chạy trên server)
"""
import asyncio
import json
import random
import re
import time
import warnings
from collections import deque

import httpx
import jwt
import tls_client
from camoufox.async_api import AsyncCamoufox

from proxy_pool import ProxyPool

warnings.filterwarnings("ignore")

SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
HOST = "elevenlabs.io"
TOKEN_TTL = 120  # seconds — token hết hạn sau 120s


def get_hcaptcha_materials(proxy_http: str) -> tuple[str, str, dict]:
    """Lấy req_token, version, config qua HTTP."""
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*', 'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://newassets.hcaptcha.com',
        'referer': 'https://newassets.hcaptcha.com/',
        'sec-ch-ua': '"Chromium";"v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
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
    """Giải HSW trong Camoufox browser page."""
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
    """Submit HSW → lấy hcaptcha pass UUID."""
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


class TokenPool:
    """
    Pre-solve hCaptcha tokens liên tục trong background.
    Mỗi token = (hcaptcha_token, proxy_dict, solved_at)
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
        self._total_failed = 0
        self._running = False
        self._solver_tasks = {}  # key -> Task
        self._cleanup_task = None
        # Callback cho UI log
        self._log_callback = None

    def set_log_callback(self, callback):
        """Set callback function(msg: str) để log ra UI."""
        self._log_callback = callback

    def _log(self, msg: str):
        if self._log_callback:
            self._log_callback(msg)
        else:
            print(msg, flush=True)

    @property
    def available(self) -> int:
        """Số token còn hợp lệ trong pool."""
        now = time.time()
        return sum(1 for _, _, t in self._tokens if now - t < TOKEN_TTL)

    @property
    def solving_count(self) -> int:
        return self._solving

    @property
    def stats(self) -> dict:
        return {
            "pool_size": self.available,
            "pool_target": self.target_size,
            "total_solved": self._total_solved,
            "total_served": self._total_served,
            "total_expired": self._total_expired,
            "total_failed": self._total_failed,
            "solving_now": self._solving,
        }

    async def get_token(self, timeout: float = 90.0) -> tuple[str, dict]:
        """Lấy token đã solve sẵn. Chờ tối đa timeout giây."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with self._lock:
                while self._tokens:
                    token, proxy, solved_at = self._tokens.popleft()
                    if time.time() - solved_at < TOKEN_TTL:
                        self._total_served += 1
                        age = time.time() - solved_at
                        self._log(f"[pool] ✓ served token (age={age:.0f}s, còn={len(self._tokens)})")
                        return token, proxy
                    else:
                        self._total_expired += 1
                        self._log(f"[pool] ⏰ token hết hạn, bỏ")
            await asyncio.sleep(1)
        raise RuntimeError("Token pool trống — timeout chờ token")

    async def start(self):
        """Bắt đầu solve token liên tục."""
        self._running = True
        self._log(f"[pool] Khởi động solver (target={self.target_size})")
        
        # Bắt đầu cleanup loop
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        # Bắt đầu solver loop cho mỗi proxy key
        self._solver_tasks = {}
        for key in list(self.proxy_pool.keys):
            self._solver_tasks[key] = asyncio.create_task(self._solver_loop(key))

    async def stop(self):
        """Dừng solve."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        for key, task in list(self._solver_tasks.items()):
            task.cancel()
        self._solver_tasks = {}
        self._log("[pool] Đã dừng tất cả solver")

    async def restart_solvers(self):
        """Restart solvers khi thêm/xóa proxy key."""
        await self.stop()
        await self.start()

    async def add_worker(self, key: str):
        """Dynamically start a worker for a new key."""
        if not self._running:
            return
        if key not in self._solver_tasks:
            self._solver_tasks[key] = asyncio.create_task(self._solver_loop(key))
            self._log(f"[pool] Khởi động worker cho key {key[:6]}...")

    async def remove_worker(self, key: str):
        """Dynamically stop a worker for a removed key."""
        task = self._solver_tasks.pop(key, None)
        if task:
            task.cancel()
            self._log(f"[pool] Dừng worker cho key {key[:6]}...")

    async def _cleanup_loop(self):
        """Background task to remove expired tokens (> 120s)."""
        while self._running:
            try:
                now = time.time()
                expired_count = 0
                async with self._lock:
                    while self._tokens and (now - self._tokens[0][2] >= TOKEN_TTL):
                        _, _, solved_at = self._tokens.popleft()
                        expired_count += 1
                        self._total_expired += 1
                if expired_count > 0:
                    self._log(f"[pool] Tự động xóa {expired_count} token hết hạn (còn={self.available})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"[pool] Lỗi trong cleanup loop: {e}")
            await asyncio.sleep(2)

    async def _solve_single_token(self, proxy: dict, browser) -> str:
        """Solve a single token using the given proxy and browser context."""
        req_token, version, config = await asyncio.to_thread(
            get_hcaptcha_materials, proxy["http"]
        )
        hsw_token = await solve_hsw(req_token, proxy["http"], browser)
        hcaptcha_token = await asyncio.to_thread(
            submit_captcha, hsw_token, version, config, proxy["http"]
        )
        return hcaptcha_token

    async def _solver_loop(self, key: str):
        """Liên tục solve token cho một proxy key cụ thể."""
        # Stagger start
        await asyncio.sleep(random.uniform(0.5, 3.0))

        while self._running:
            try:
                # Kiểm tra pool đã đủ chưa
                if self.available >= self.target_size:
                    await asyncio.sleep(2)
                    continue

                self._solving += 1
                self._log(f"[solver-{key[:6]}] Yêu cầu proxy...")

                t_ip_acquired = time.time()
                # Lấy proxy mới (API/cooldown được quản lý nội bộ bởi ProxyPool)
                proxy = await self.proxy_pool._fetch_proxy(key)

                # Solve 3-5 tokens bằng IP này
                num_tokens = random.randint(3, 5)
                self._log(f"[solver-{key[:6]}] Đã có IP {proxy['raw']}. Đang giải {num_tokens} token...")

                async with AsyncCamoufox(headless=True, os='windows', proxy={'server': proxy["http"]}) as browser:
                    tasks = [self._solve_single_token(proxy, browser) for _ in range(num_tokens)]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                success_count = 0
                now = time.time()
                for res in results:
                    if isinstance(res, Exception):
                        self._log(f"[solver-{key[:6]}] ✗ Lỗi giải token: {str(res)[:80]}")
                        self._total_failed += 1
                    else:
                        success_count += 1
                        self._total_solved += 1
                        async with self._lock:
                            self._tokens.append((res, proxy, now))

                self._solving -= 1
                self._log(f"[solver-{key[:6]}] ✓ Hoàn thành chu kỳ: {success_count}/{num_tokens} token giải thành công (pool={self.available})")

                # Chờ cooldown: Đảm bảo chờ ít nhất 60s kể từ lúc lấy IP
                elapsed = time.time() - t_ip_acquired
                wait_time = max(0.0, 60.0 - elapsed)
                if wait_time > 0 and self._running:
                    self._log(f"[solver-{key[:6]}] Chờ cooldown đổi IP {wait_time:.1f}s...")
                    await asyncio.sleep(wait_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._solving -= 1
                self._total_failed += 1
                self._log(f"[solver-{key[:6]}] ✗ Lỗi chu kỳ: {str(e)[:80]}")
                await asyncio.sleep(5)
