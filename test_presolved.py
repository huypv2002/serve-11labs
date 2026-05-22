"""
Test: pre-solve hCaptcha token, then use it for TTS API call.
Verifies if tokens can be stored and used later (within TTL).
"""
import asyncio
import time
import sys
sys.path.insert(0, '/Users/phamvanhuy/elevenlabs-re')

from elevenlabs_api import get_hcaptcha_materials, solve_hsw, submit_captcha, ProxyPool, PROXY_KEYS

API_BASE = "https://api.elevenlabs.io"
VOICE = "NOpBlnGInO9m6vDvFkFC"
MODEL = "eleven_v3"

async def test_presolved_token():
    import httpx
    from camoufox.async_api import AsyncCamoufox

    pool = ProxyPool(PROXY_KEYS)
    proxy = await pool.get_proxy()
    print(f"[1] Got proxy: {proxy['raw']}")

    # Step 1: Solve token
    t0 = time.time()
    print(f"[2] Solving captcha token...")
    req_token, version, config = await asyncio.to_thread(get_hcaptcha_materials, proxy["http"])
    print(f"    materials OK ({time.time()-t0:.1f}s)")

    async with AsyncCamoufox(headless=True, os='windows', proxy={'server': proxy["http"]}) as browser:
        hsw_token = await solve_hsw(req_token, proxy["http"], browser)
    print(f"    HSW solved ({time.time()-t0:.1f}s)")

    hcaptcha_token = await asyncio.to_thread(submit_captcha, hsw_token, version, config, proxy["http"])
    solve_time = time.time() - t0
    print(f"    Token ready ({solve_time:.1f}s)")
    print(f"    Token preview: {hcaptcha_token[:50]}...")

    # Step 2: Wait 10s to simulate "pre-solved" delay
    wait = 10
    print(f"\n[3] Waiting {wait}s to simulate pre-solved delay...")
    await asyncio.sleep(wait)

    # Step 3: Use token for TTS
    print(f"[4] Using pre-solved token for TTS API call...")
    url = f"{API_BASE}/v1/text-to-speech/{VOICE}/anonymous"
    payload = {
        "text": "Đây là test token đã solve trước",
        "model_id": MODEL,
        "voice_settings": {"speed": 1.0},
        "hcaptcha_token": hcaptcha_token,
        "language_code": "vi",
    }
    async with httpx.AsyncClient(proxy=proxy["http"], timeout=60.0, verify=False) as client:
        resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})

    if resp.status_code == 200:
        print(f"    ✅ SUCCESS! {len(resp.content)} bytes, total {time.time()-t0:.1f}s")
        with open("/tmp/presolved_test.mp3", "wb") as f:
            f.write(resp.content)
        print(f"    Saved to /tmp/presolved_test.mp3")
    else:
        print(f"    ❌ FAIL: HTTP {resp.status_code}: {resp.text[:100]}")

    # Step 4: Try reusing same token (should fail — single-use)
    print(f"\n[5] Trying to REUSE same token (expect fail)...")
    async with httpx.AsyncClient(proxy=proxy["http"], timeout=60.0, verify=False) as client:
        resp2 = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
    if resp2.status_code == 200:
        print(f"    ⚠️ Token REUSABLE! {len(resp2.content)} bytes")
    else:
        print(f"    ✅ Confirmed single-use: HTTP {resp2.status_code}")

asyncio.run(test_presolved_token())
