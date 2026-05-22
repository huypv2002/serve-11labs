"""
TTS Engine - Gọi ElevenLabs preview/anonymous endpoint
Dùng token đã solve sẵn từ TokenPool
"""
import asyncio
import base64
import binascii
import json
import time

import httpx

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


def extract_audio_from_stream(text: str) -> bytes:
    """Trích xuất audio bytes từ streaming response (JSON lines với audio_base64)."""
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


async def tts_preview(
    text: str,
    hcaptcha_token: str,
    proxy_http: str,
    voice_id: str = DEFAULT_VOICE,
    model_id: str = DEFAULT_MODEL,
    speed: float = 1.0,
    language_code: str = "vi",
    stability: float = 0.5,
    similarity_boost: float = 0.75,
) -> bytes:
    """
    Gọi ElevenLabs anonymous TTS endpoint.
    Trả về audio bytes (mp3).
    """
    url = f"{API_BASE}/v1/text-to-speech/{voice_id}/stream/with-timestamps/anonymous"

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "speed": speed,
            "stability": stability,
            "similarity_boost": similarity_boost,
        },
        "hcaptcha_token": hcaptcha_token,
        "language_code": language_code,
    }

    async with httpx.AsyncClient(
        proxy=proxy_http,
        timeout=120.0,
        verify=False,
        headers=ELEVENLABS_HEADERS,
    ) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code == 200:
        audio_bytes = extract_audio_from_stream(resp.text)
        if not audio_bytes:
            raise RuntimeError("Response không chứa audio_base64")
        return audio_bytes
    elif resp.status_code == 401:
        raise RuntimeError(f"401 - IP bị rate limit hoặc token hết hạn")
    elif resp.status_code == 400:
        raise RuntimeError(f"400 - {resp.text[:100]}")
    else:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:100]}")


async def tts_preview_no_stream(
    text: str,
    hcaptcha_token: str,
    proxy_http: str,
    voice_id: str = DEFAULT_VOICE,
    model_id: str = DEFAULT_MODEL,
    speed: float = 1.0,
    language_code: str = "vi",
    stability: float = 0.5,
    similarity_boost: float = 0.75,
) -> bytes:
    """
    Gọi ElevenLabs anonymous TTS endpoint (không stream, trả audio/mpeg trực tiếp).
    """
    url = f"{API_BASE}/v1/text-to-speech/{voice_id}/anonymous"

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "speed": speed,
            "stability": stability,
            "similarity_boost": similarity_boost,
        },
        "hcaptcha_token": hcaptcha_token,
        "language_code": language_code,
    }

    async with httpx.AsyncClient(
        proxy=proxy_http,
        timeout=120.0,
        verify=False,
        headers=ELEVENLABS_HEADERS,
    ) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code == 200:
        if not resp.content:
            raise RuntimeError("Response rỗng")
        return resp.content
    elif resp.status_code == 401:
        raise RuntimeError(f"401 - IP bị rate limit hoặc token hết hạn")
    elif resp.status_code == 400:
        raise RuntimeError(f"400 - {resp.text[:100]}")
    else:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:100]}")
