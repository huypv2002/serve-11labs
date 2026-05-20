# ElevenLabs TTS Server

TTS API server sử dụng ElevenLabs anonymous endpoint với hCaptcha token pool.

## Cài đặt

```bash
# Clone repo
git clone https://github.com/huypv2002/serve-11labs.git
cd serve-11labs

# Cài Python dependencies
pip install -r requirements.txt

# Cài Node dependencies (cho hCaptcha solver)
npm install
```

## Chạy server

```bash
# Pool mode (khuyến nghị) - pre-solve tokens, response nhanh
python3 tts_server.py start --mode pool --pool-size 5

# Hoặc chạy trực tiếp
python3 elevenlabs_api_pool.py --port 8899 --pool-size 5
```

## Quản lý server

```bash
python3 tts_server.py status    # Xem trạng thái
python3 tts_server.py stop      # Dừng server
python3 tts_server.py logs -f   # Xem logs realtime
```

## API

### POST /tts
```bash
curl -X POST http://localhost:8899/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Xin chào", "voice_id": "NOpBlnGInO9m6vDvFkFC", "lang": "vi"}' \
  --output output.mp3
```

Body params:
- `text` (required): Văn bản cần chuyển thành giọng nói (max 1000 ký tự)
- `voice_id`: Voice ID từ ElevenLabs (default: NOpBlnGInO9m6vDvFkFC)
- `voice`: Alias cho voice_id (tương thích ngược)
- `lang`: Mã ngôn ngữ (default: "vi")
- `speed`: Tốc độ đọc (default: 1.0)
- `model`: Model ID (default: "eleven_v3")

### GET /health
```bash
curl http://localhost:8899/health
```

### GET /voices
```bash
curl http://localhost:8899/voices
```

## Yêu cầu hệ thống

- Python 3.10+
- Node.js 18+
- Proxy keys từ proxyxoay.shop (đã cấu hình trong code)
