"""
Proxy Pool - Quản lý proxy từ proxyxoay.shop
Hỗ trợ nhiều key, xoay vòng, tracking IP usage.
"""
import asyncio
import re
import httpx

PROXY_API = "https://proxyxoay.shop/api/get.php"
REQUESTS_PER_IP = 3


class ProxyPool:
    def __init__(self, keys: list[str] = None):
        self.keys = list(keys or [])
        self._lock = asyncio.Lock()
        self._key_idx = 0
        self._ip_usage = {}  # ip -> request count
        self._current_proxies = {}  # key -> proxy dict

    async def add_key(self, key: str) -> bool:
        async with self._lock:
            if key in self.keys:
                return False
            self.keys.append(key)
            return True

    async def remove_key(self, key: str) -> bool:
        async with self._lock:
            if key not in self.keys:
                return False
            self.keys.remove(key)
            self._current_proxies.pop(key, None)
            if self._key_idx >= len(self.keys):
                self._key_idx = 0
            return True

    @property
    def key_count(self) -> int:
        return len(self.keys)

    async def get_proxy(self) -> dict:
        """Lấy proxy có quota còn (< REQUESTS_PER_IP)."""
        if not self.keys:
            raise RuntimeError("Chưa có proxy key nào")

        async with self._lock:
            for key, proxy in self._current_proxies.items():
                ip = proxy["raw"]
                if self._ip_usage.get(ip, 0) < REQUESTS_PER_IP:
                    self._ip_usage[ip] = self._ip_usage.get(ip, 0) + 1
                    return proxy

        # Cần proxy mới
        async with self._lock:
            key = self.keys[self._key_idx]
            self._key_idx = (self._key_idx + 1) % len(self.keys)

        proxy = await self._fetch_proxy(key)
        async with self._lock:
            self._current_proxies[key] = proxy
            self._ip_usage[proxy["raw"]] = 1
        return proxy

    async def get_proxy_for_solve(self) -> dict:
        """Lấy proxy cho solve token (không tính quota TTS)."""
        if not self.keys:
            raise RuntimeError("Chưa có proxy key nào")
        async with self._lock:
            key = self.keys[self._key_idx]
            self._key_idx = (self._key_idx + 1) % len(self.keys)
        return await self._fetch_proxy(key)

    async def mark_quota_hit(self, proxy: dict):
        """Đánh dấu IP đã hết quota."""
        async with self._lock:
            self._ip_usage[proxy["raw"]] = REQUESTS_PER_IP

    async def _fetch_proxy(self, key: str) -> dict:
        """Lấy proxy từ API, xử lý cooldown."""
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
                await asyncio.sleep(wait)
            else:
                raise RuntimeError(f"Proxy error: {data}")
        raise RuntimeError(f"Proxy timeout cho key {key[:8]}")
