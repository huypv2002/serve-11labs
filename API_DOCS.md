# TTS API - Hướng dẫn sử dụng

## Thông tin cơ bản

- URL: https://tts.liveyt.pro
- Auth: Header `Authorization: Bearer <API_KEY>`
- Max text: 1000 ký tự/request
- Giá: 1 VND/ký tự
- Response: file MP3

---

## Chuyển text thành giọng nói

```bash
curl -X POST https://tts.liveyt.pro/tts \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Xin chào, đây là test", "voice_id": "NOpBlnGInO9m6vDvFkFC"}' \
  --output audio.mp3
```

---

## Voices có sẵn

| ID | Tên | Ngôn ngữ |
|----|-----|-----------|
| NOpBlnGInO9m6vDvFkFC | Aria | Đa ngôn ngữ |
| 9BWtsMINqrJLrRacOk9x | Aria v2 | Đa ngôn ngữ |
| EXAVITQu4vr4xnSDxMaL | Sarah | English |
| FGY2WhTYpPnrIDTdsKH5 | Laura | English |
| IKne3meq5aSn9XLyUdCD | Charlie | English |
| JBFqnCBsd6RMkjVDRZzb | George | English |
| TX3LPaxmHKxFdv7VOQHJ | Liam | English |
| pFZP5JQG7iQjIQuC4Bku | Lily | English |
| g14YnDYCsy3k7XLlcKlO | Custom Voice | Đa ngôn ngữ |

Mặc định: `NOpBlnGInO9m6vDvFkFC` (Aria)

---

## Ví dụ

### Tiếng Việt
```bash
curl -X POST https://tts.liveyt.pro/tts \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hôm nay thời tiết rất đẹp"}' \
  --output output.mp3
```

### Chọn voice khác
```bash
curl -X POST https://tts.liveyt.pro/tts \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello world", "voice_id": "TX3LPaxmHKxFdv7VOQHJ"}' \
  --output output.mp3
```

---

## Python
```python
import requests

resp = requests.post(
    "https://tts.liveyt.pro/tts",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={"text": "Xin chào", "voice_id": "NOpBlnGInO9m6vDvFkFC"}
)

with open("output.mp3", "wb") as f:
    f.write(resp.content)
```

---

## Lỗi thường gặp

| HTTP Code | Nghĩa |
|-----------|--------|
| 401 | API key sai hoặc thiếu |
| 402 | Hết số dư |
| 400 | Text quá dài (>1000 ký tự) |
| 429 | Quá giới hạn rate limit (mặc định 3 request/phút, có thể chỉnh riêng từng API key trong admin) |

---

## Kiểm tra server

```bash
curl https://tts.liveyt.pro/health
```
