"""
PreviewClient - Drop-in replacement cho ElevenClient
Dùng TokenPool + HSW resolve thay vì API key.
Interface giống ElevenClient.tts_direct() để không cần sửa ChunkWorker.
"""
import asyncio
import base64
import binascii
import json
import os
import time
import threading
from pathlib import Path

import httpx

from proxy_pool import ProxyPool
from token_solver import TokenPool

API_BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE = "NOpBlnGInO9m6vDvFkFC"
DEFAULT_MODEL = "eleven_v3"

ELEVENLABS_HEADERS = {
    "accept": "*/*",
    "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.6,en;q=0.5",
    "content-type": "application/json",
    "origin": "https://elevenlabs.io",
    "referer": "https://elevenlabs.io/",
    "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
}


def _extract_audio_from_stream(text: str) -> bytes:
    """Trích xuất audio bytes từ streaming response."""
    output = bytearray()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        chunk = parsed.get("audio_base64")
        if not chunk:
            continue
        try:
            output.extend(base64.b64decode(chunk, validate=True))
        except binascii.Error:
            output.extend(base64.b64decode(chunk))
    return bytes(output)


class PreviewClient:
    """
    Drop-in replacement cho ElevenClient.
    Dùng TokenPool (HSW pre-solve) + anonymous endpoint.
    
    Interface:
        client.tts_direct(voice_id, text, model_id, settings, outpath, ...)
    """

    def __init__(self, proxy_pool: ProxyPool, token_pool: TokenPool, log_fn=None, settings=None):
        self.proxy_pool = proxy_pool
        self.token_pool = token_pool
        self.settings = settings
        self._log_fn = log_fn
        self._loop = None
        self._loop_thread = None
        self._start_async_loop()

    def _start_async_loop(self):
        """Khởi động asyncio loop riêng cho preview client."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

    def _run_async(self, coro):
        """Chạy coroutine trên async loop, block cho đến khi xong."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)

    def log(self, msg: str):
        if self._log_fn:
            self._log_fn(msg)
        else:
            print(msg, flush=True)

    def tts_direct(self, voice_id: str, text: str, model_id: str, settings: dict,
                   outpath: str, output_format: str = "mp3_44100_128", api_key: str = None):
        """
        Drop-in replacement cho ElevenClient.tts_direct().
        Bỏ qua api_key (không cần), dùng token pool thay thế.
        """
        self.log(f"[Preview] Đang lấy token từ pool...")
        
        # Lấy token từ pool (blocking)
        token, proxy = self._run_async(self.token_pool.get_token(timeout=90.0))
        
        self.log(f"[Preview] Có token (age OK), gọi API với proxy {proxy['raw'][:15]}...")

        # Gọi TTS API
        audio_bytes = self._run_async(
            self._call_tts(text, token, proxy["http"], voice_id, model_id, settings)
        )

        # Lưu file
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        tmp = outpath + ".part"
        with open(tmp, "wb") as f:
            f.write(audio_bytes)
        
        if os.path.exists(outpath):
            os.remove(outpath)
        os.rename(tmp, outpath)
        
        size_kb = len(audio_bytes) / 1024
        self.log(f"[Preview] ✓ Saved {size_kb:.1f}KB → {os.path.basename(outpath)}")

    async def _call_tts(self, text: str, hcaptcha_token: str, proxy_http: str,
                        voice_id: str, model_id: str, settings: dict) -> bytes:
        """Gọi ElevenLabs anonymous TTS endpoint."""
        # Thử stream endpoint trước
        url = f"{API_BASE}/v1/text-to-speech/{voice_id}/stream/with-timestamps/anonymous"

        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": settings,
            "hcaptcha_token": hcaptcha_token,
        }

        # Thêm language_code nếu có trong settings app
        if self.settings and hasattr(self.settings, 'language_code') and self.settings.language_code:
            payload["language_code"] = self.settings.language_code

        async with httpx.AsyncClient(
            proxy=proxy_http,
            timeout=120.0,
            verify=False,
            headers=ELEVENLABS_HEADERS,
        ) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code == 200:
            # Stream response: JSON lines với audio_base64
            audio_bytes = _extract_audio_from_stream(resp.text)
            if audio_bytes:
                return audio_bytes
            # Fallback: có thể là binary trực tiếp
            if resp.content and len(resp.content) > 100:
                return resp.content
            raise RuntimeError("Response không chứa audio data")
        elif resp.status_code == 401:
            # Token hết hạn hoặc IP bị rate limit
            await self.proxy_pool.mark_quota_hit({"raw": proxy_http.replace("http://", "")})
            raise RuntimeError(f"401 Client Error: Unauthorized (IP rate limited)")
        elif resp.status_code == 400:
            raise RuntimeError(f"400 Client Error: {resp.text[:150]}")
        elif resp.status_code == 429:
            raise RuntimeError(f"429 Rate Limit: Too Many Requests")
        else:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:100]}")


class DummyKeyManager:
    """
    Dummy KeyManager - thay thế KeyPoolManager.
    PreviewClient không cần key, nhưng ChunkWorker gọi self.client.keys.*
    Trả về dummy values để không crash.
    """
    def __init__(self):
        self.keys = ["preview_mode"]
        self.i = 0

    def cur(self):
        return "preview_mode"

    def cur_index(self):
        return 0

    def rotate(self):
        pass

    def acquire_key(self, required_chars=0, line_id=None, excluded_keys=None):
        return "preview_mode"

    def release_key(self, key, chars_used=0, success=True, error=None, line_id=None):
        pass

    def mark_401(self, key):
        pass

    def mark_success(self, key):
        pass

    def set_client(self, client, log_fn=None):
        pass

    def set_key_pool_db(self, db):
        pass

    def load(self, path):
        return 1
