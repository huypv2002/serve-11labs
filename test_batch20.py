import asyncio
import httpx
import re
import time
import os

OUTPUT_DIR = "/Users/phamvanhuy/elevenlabs-re/output_tts_test20"
API_URL = "https://tts.liveyt.pro/tts"
API_KEY = "tts_5UWxg0l40D8Gj28Ss3KQgkQQX5CN8zPO"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Read chunks
with open("/Users/phamvanhuy/elevenlabs-re/1 Tin trong ngày_chunks.txt", "r") as f:
    content = f.read()

chunks = re.split(r'---CHUNK \d+---\n', content)
chunks = [c.strip() for c in chunks if c.strip()][:20]

print(f"Testing {len(chunks)} chunks via {API_URL}")
print(f"Avg chunk size: {sum(len(c) for c in chunks) // len(chunks)} chars")
print("=" * 50)

async def tts_chunk(client, idx, text):
    t0 = time.time()
    try:
        resp = await client.post(
            API_URL,
            json={"text": text, "lang": "vi"},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=120.0
        )
        elapsed = time.time() - t0
        if resp.status_code == 200:
            outpath = os.path.join(OUTPUT_DIR, f"chunk_{idx:04d}.mp3")
            with open(outpath, "wb") as f:
                f.write(resp.content)
            print(f"  [{idx:02d}] OK - {len(resp.content):,} bytes, {elapsed:.1f}s ({len(text)} chars)")
            return True
        else:
            print(f"  [{idx:02d}] FAIL - HTTP {resp.status_code}: {resp.text[:100]}, {elapsed:.1f}s")
            return False
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [{idx:02d}] ERROR - {e}, {elapsed:.1f}s")
        return False

async def main():
    t_start = time.time()
    success = 0
    failed = 0

    async with httpx.AsyncClient() as client:
        # Process 2 at a time (matching 2 proxy keys)
        for batch_start in range(0, len(chunks), 2):
            batch = chunks[batch_start:batch_start+2]
            tasks = []
            for i, chunk in enumerate(batch):
                idx = batch_start + i + 1
                tasks.append(tts_chunk(client, idx, chunk))
            
            results = await asyncio.gather(*tasks)
            for r in results:
                if r:
                    success += 1
                else:
                    failed += 1

    elapsed = time.time() - t_start
    print("=" * 50)
    print(f"Done: {success}/{len(chunks)} success, {failed} failed")
    print(f"Total time: {elapsed:.1f}s ({elapsed/len(chunks):.1f}s/chunk avg)")

asyncio.run(main())
