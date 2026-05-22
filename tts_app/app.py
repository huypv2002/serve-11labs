# app_elevenlabs_seq.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, re, json, time, threading, subprocess, shutil, math
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QThreadPool, QRunnable, QObject, Signal
from PySide6.QtWidgets import QHeaderView

# ========== Preview TTS Backend (HSW + TokenPool) ==========
import asyncio
from preview_client import PreviewClient, DummyKeyManager
from proxy_pool import ProxyPool
from token_solver import TokenPool

SERVICES_AVAILABLE = False  # Không cần login services cho preview mode
print("✅ Preview TTS mode (HSW + TokenPool)")

APP_NAME = "HuyViet_AutoTTS"
ELEVEN_BASE = "https://api.elevenlabs.io"

import sys
from datetime import datetime

def _get_app_dir() -> str:
    """
    Get application directory - works for both script and compiled exe.
    - When running as exe: returns folder containing exe
    - When running as script: returns folder containing script
    """
    if "__compiled__" in globals():
        # Nuitka compiled
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    if getattr(sys, 'frozen', False):
        # PyInstaller or other frozen
        return os.path.dirname(sys.executable)
    try:
        # Running as script
        return os.path.dirname(os.path.abspath(__file__))
    except:
        return os.getcwd()

APP_DIR = _get_app_dir()

# 🐛 FIX: Lưu config CÙng THƯ MỤC với app (works for both script and exe)
# CFG_FILE = os.path.join(os.path.expanduser("~"), f".{APP_NAME}.json")  # ← CŨ (bất tiện)
CFG_FILE = os.path.join(APP_DIR, f"{APP_NAME}_config.json")  # ← MỚI (works with exe)

# Login temp file (simple - chỉ lưu username/password)
LOGIN_TEMP_FILE = os.path.join(APP_DIR, "login_temp.json")

# JWT TTS Settings file - cho tab "Giọng Trả Phí"
JWT_TTS_SETTINGS_FILE = os.path.join(APP_DIR, "jwt_tts_settings.json")

def _init_jwt_tts_settings():
    """Khởi tạo file jwt_tts_settings.json nếu chưa tồn tại - giống login_temp.json"""
    if not os.path.exists(JWT_TTS_SETTINGS_FILE):
        default_settings = {
            'voice_id': '21m00Tcm4TlvDq8ikWAM',
            'voice_name': 'Rachel',
            'model_id': 'eleven_multilingual_v2',
            'stability': 50,
            'similarity': 75,
            'speed': 1.0,
            'style': 0,
            'speaker_boost': False,
            'change_settings': True,
            'thread_count': 5,
            'accounts_file': '',
            'last_folder': '',
            'last_search_text': '',
            'voice_list': [],
            'advanced': {
                'gap_enabled': False,
                'gap_seconds': 1.3,
                'gap_every': 5,
                'pause_char_enabled': True,
                'char1': ',',
                'char1_sec': 0.3,
                'char2': '.',
                'char2_sec': 0.5,
                'max_chars': 300,
            }
        }
        try:
            with open(JWT_TTS_SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_settings, f, indent=2, ensure_ascii=False)
            print(f"✅ Created jwt_tts_settings.json at {JWT_TTS_SETTINGS_FILE}")
        except Exception as e:
            print(f"❌ Cannot create jwt_tts_settings.json: {e}")
    else:
        print(f"✅ jwt_tts_settings.json exists at {JWT_TTS_SETTINGS_FILE}")

# Khởi tạo file ngay khi app start
_init_jwt_tts_settings()

LOG_DIR = os.path.join(APP_DIR, "logtts")
LOG_FILE = None

try:
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
except Exception as e:
    pass

def _get_session_log_file() -> str:
    global LOG_FILE
    if LOG_FILE:
        return LOG_FILE
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    session_num = 1
    while True:
        log_name = f"tts_{today}_{session_num}.log"
        log_path = os.path.join(LOG_DIR, log_name)
        if not os.path.exists(log_path):
            break
        session_num += 1
    LOG_FILE = log_path
    return LOG_FILE

_log_buffer: List[str] = []
_log_last_flush = time.time()
_log_lock = threading.Lock()
LOG_BUFFER_SIZE = 50
LOG_FLUSH_INTERVAL = 20.0

def _flush_log_buffer():
    global _log_buffer, _log_last_flush
    if not _log_buffer:
        return
    try:
        log_file = _get_session_log_file()
        with open(log_file, "a", encoding="utf-8") as f:
            f.writelines(_log_buffer)
        _log_buffer = []
        _log_last_flush = time.time()
    except:
        pass


def log_to_file(text: str):
    global _log_buffer, _log_last_flush
    skip_patterns = [
        "[Proxy", "proxy", "Proxy:", "socks5", "http://", "https://",
        "[KeyPool]", "Acquired key", "xoay key", "check key", "Key sk_", 
        "credits (cần", "credits còn", "Key OK", "Key không",
        "Thử lại key", "blacklist",
        "history-item-id", "Download MP3",
        "[Cleanup]", "_parts", "_silence_", "silence:",
        "[Config] Thread", "active:", "worker", "slot",
        "Nội dung", "ký tự →", "Rate limit: đợi", "Check Verry",
        "Xác nhận key hoạt động", "Key error retry", "http 401",
        "History chưa sẵn sàng", "[400 Response]", "🔄 400 - Retry",
        "đợi 3s rồi retry", "đợi 5.0s",
        "DIRECT | Sent: 0.0KB Recv: 0.1KB", "Loaded 1/1 file", "dòng từ",
        "S:1.0KB R:", "S:1.2KB R:",
        "→ download", "Download (key:", "Part 1/1 done",
        "Found ffprobe", "Duration from", "Duration estimated",
        "ffprobe error", "mutagen error", "[DEBUG]", "to_requests output",
        "Joining",
    ]
    text_lower = text.lower()
    for pattern in skip_patterns:
        if pattern.lower() in text_lower:
            return
    with _log_lock:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _log_buffer.append(f"[{timestamp}] {text}\n")
        should_flush = (
            len(_log_buffer) >= LOG_BUFFER_SIZE or
            (time.time() - _log_last_flush) >= LOG_FLUSH_INTERVAL
        )
        if should_flush:
            _flush_log_buffer()

def flush_all_logs():
    with _log_lock:
        _flush_log_buffer()

def ensure_dir(p: str):
    if p and not os.path.exists(p):
        os.makedirs(p, exist_ok=True)

# ---------------- Default Voices (Premade) ----------------
DEFAULT_VOICES = [
    ("Roger", "CwhRBWXzGAHq8TQ4Fs17"),
    ("Sarah", "EXAVITQu4vr4xnSDxMaL"),
    ("Laura", "FGY2WhTYpPnrIDTdsKH5"),
    ("Charlie", "IKne3meq5aSn9XLyUdCD"),
    ("George", "JBFqnCBsd6RMkjVDRZzb"),
    ("Callum", "N2lVS1w4EtoT3dr4eOWO"),
    ("River", "SAz9YHcvj6GT2YYXdXww"),
    ("Harry", "SOYHLrjzK2X1ezoPC6cr"),
    ("Liam", "TX3LPaxmHKxFdv7VOQHJ"),
    ("Alice", "Xb7hH8MSUJpSbSDYk0k2"),
    ("Matilda", "XrExE9yKIg1WjnnlVkGX"),
    ("Will", "bIHbv24MWmeRgasZH58o"),
    ("Jessica", "cgSgspJ2msm6clMCkdW9"),
    ("Eric", "cjVigY5qzO86Huf0OWal"),
    ("Bella", "hpp4J3VqNfWAUOO0d1Us"),
    ("Chris", "iP95p4xoKVk53GoZ742B"),
    ("Brian", "nPczCjzI2devNBz1zQrb"),
    ("Daniel", "onwK4e9ZLuTAKqWW03F9"),
    ("Lily", "pFZP5JQG7iQjIQuC4Bku"),
    ("Adam", "pNInz6obpgDQGcFmaJgB"),
    ("Bill", "pqHfZKP75CvOlQylNhV4"),
]

# ---------------- Settings ----------------
@dataclass
class AppSettings:
    # Adv
    gap_segments_enabled: bool = False
    gap_seconds: float = 1.3
    gap_every: int = 5
    gap_srt_enabled: bool = False
    pause_char_enabled: bool = True
    char1: str = ","
    char1_sec: float = 0.3
    char2: str = "."
    char2_sec: float = 0.6
    sanitize: bool = True
    download_type: str = "1 <ORIGINAL>"
    max_chars_per_line: int = 300  # 🔧 FIX: Giảm từ 400 xuống 300 để chunk nhỏ hơn, API response nhanh hơn
    keys_file: str = ""

    proxies_text: str = ""
    proxy_phase_log: bool = False
    proxy_enabled: bool = False
    proxyxoay_key: str = ""  # Key xoay proxy từ proxyxoay.shop
    proxy_sticky_enabled: bool = True  # Giữ proxy lâu (sticky session)
    proxy_sticky_minutes: int = 3  # Số phút giữ proxy trước khi đổi
    request_delay: float = 6.0
    retry_401_count: int = 1  # 🔧 Giảm từ 3 xuống 1 - chỉ retry 1 lần rồi đổi key
    error_400_retry_before_rotate: int = 3
    error_400_delay: float = 5.0

    # main options
    loop: bool = False
    auto_split: bool = False
    split_chars: str = ",.;!?"
    thread_count: int = 10

    # voice settings
    change_settings: bool = True
    speed: float = 1.0
    style: int = 0
    stability: int = 50
    similarity: int = 75
    speaker_boost: bool = False

    # last selections
    last_folder: str = ""
    last_model_id: str = ""
    last_voice_id: str = ""
    last_voice_name: str = ""
    last_search_text: str = ""
    last_voice_display: str = ""
    keys_file_path: str = ""
    
    auto_srt_enabled: bool = False
    
    char_counter: int = 0
    
    # Language selection (ISO 639-1)
    language_code: str = ""  # Empty = auto-detect, or "vi", "en", "es", etc.

def load_settings() -> AppSettings:
    try:
        if os.path.exists(CFG_FILE):
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            s = AppSettings()
            for k,v in d.items():
                if hasattr(s,k): setattr(s,k,v)
            return s
    except Exception:
        pass
    return AppSettings()

def save_settings(s: AppSettings):
    try:
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(s.__dict__, f, ensure_ascii=False, indent=2)
        print(f"✅ Đã lưu settings vào {CFG_FILE}")
    except Exception as e:
        print(f"⚠️ Error saving settings: {e}")
        import traceback
        traceback.print_exc()

# ---------------- Keys / Proxy ----------------
class KeyPoolManager:
    STATUS_ACTIVE = 'active'
    STATUS_LOW_CREDIT = 'low'
    STATUS_ERROR_401 = '401'
    STATUS_ERROR_400 = '400'
    STATUS_FULL_SLOTS = 'slots'
    
    def __init__(self):
        self.keys: List[str] = []
        self.i = 0
        
        # Credit tracking
        self.credits_cache: Dict[str, int] = {}
        self.status: Dict[str, str] = {}
        self.cooldowns: Dict[str, float] = {}
        
        # Locks
        self.lock = threading.Lock()  # Lock cho internal data
        self.acquire_lock = threading.Lock()  # Sequential lock cho acquire_key
        
        # 🔧 SMART KEY SELECTION - Giảm cooldown để pick key nhanh hơn
        self.COOLDOWN_401 = 60      # Giảm từ 300s xuống 60s - key 401 có thể recover nhanh
        self.COOLDOWN_400 = 120     # Giảm từ 300s xuống 120s
        self.COOLDOWN_SLOTS = 15    # Giảm từ 30s xuống 15s - slots free nhanh
        self.COOLDOWN_LOW = 30      # Giảm từ 60s xuống 30s
        
        # 🔧 HEALTH TRACKING - Track success/fail để ưu tiên key khỏe
        self._success_count: Dict[str, int] = {}   # key -> số lần success liên tiếp
        self._fail_count: Dict[str, int] = {}      # key -> số lần fail liên tiếp
        self._last_success_time: Dict[str, float] = {}  # key -> timestamp success gần nhất
        self._healthy_keys: List[str] = []         # Danh sách key vừa success (ưu tiên cao)
        
        self._client = None
        self._log_fn = None
        
        self._blacklist_file = os.path.join(APP_DIR, "keys_status.json")
        self._blacklist: Dict[str, dict] = {}  # key -> {status, last_credits, blocked_at}
        self._load_blacklist()
        
        self._keys_in_use: set = set()
        
        # 🔧 NEW: Lock key per line - mỗi line dùng 1 key riêng biệt
        self._key_line_lock: Dict[str, str] = {}  # key -> line_id (key đang được line nào dùng)
        
        # 🔧 NEW: Reference to KeyPoolV2 (DB-backed) - sẽ được set từ MainWindow
        self._key_pool_db = None
    
    def set_client(self, client, log_fn=None):
        self._client = client
        self._log_fn = log_fn
    
    def set_key_pool_db(self, key_pool_db):
        """🔧 NEW: Set reference to KeyPoolV2 (DB-backed pool)"""
        self._key_pool_db = key_pool_db
        self._log(f"[KeyPool] ✅ Connected to KeyPoolV2 (DB-backed)")
    
    def _log(self, msg: str):
        if self._log_fn:
            try:
                self._log_fn(msg)
            except:
                pass
    
    def _load_blacklist(self):
        try:
            if os.path.exists(self._blacklist_file):
                with open(self._blacklist_file, "r", encoding="utf-8") as f:
                    self._blacklist = json.load(f)
        except Exception:
            self._blacklist = {}
    
    def _save_blacklist(self):
        try:
            with open(self._blacklist_file, "w", encoding="utf-8") as f:
                json.dump(self._blacklist, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def _add_to_blacklist(self, key: str, status: str, credits: int = 0):
        self._blacklist[key] = {
            "status": status,
            "last_credits": credits,
            "blocked_at": time.time()
        }
        self._save_blacklist()
    
    def _remove_from_blacklist(self, key: str):
        if key in self._blacklist:
            del self._blacklist[key]
            self._save_blacklist()
    
    def _is_blacklisted(self, key: str) -> bool:
        return key in self._blacklist
    
    def _get_blacklist_hours(self, key: str) -> float:
        if key not in self._blacklist:
            return 0
        blocked_at = self._blacklist[key].get("blocked_at", 0)
        return (time.time() - blocked_at) / 3600
    
    def load(self, path: str) -> int:
        with self.lock:
            self.keys.clear()
            self.i = 0
            self._keys_file_path = path
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for ln in f:
                        k = ln.strip()
                        if k:
                            self.keys.append(k)
                            if k in self._blacklist:
                                bl_status = self._blacklist[k].get('status', self.STATUS_ACTIVE)
                                self.status[k] = bl_status
                                self.cooldowns[k] = time.time() + 86400
                            elif k not in self.status:
                                self.status[k] = self.STATUS_ACTIVE
            blacklisted_count = sum(1 for k in self.keys if k in self._blacklist)
            if blacklisted_count > 0:
                print(f"[KeyPool] Loaded {len(self.keys)} keys, {blacklisted_count} trong blacklist")
            return len(self.keys)
    
    def _save_error_key_to_file(self, key: str):
        """Lưu key bị 401/400 vào file riêng (keys401.txt)"""
        if not hasattr(self, '_keys_file_path') or not self._keys_file_path:
            return
        
        try:
            base, ext = os.path.splitext(self._keys_file_path)
            error_file = f"{base}401{ext}"
            
            existing_keys = set()
            if os.path.exists(error_file):
                with open(error_file, "r", encoding="utf-8", errors="ignore") as f:
                    for ln in f:
                        k = ln.strip()
                        if k:
                            existing_keys.add(k)
            
            if key not in existing_keys:
                with open(error_file, "a", encoding="utf-8") as f:
                    f.write(key + "\n")
                self._log(f"[KeyPool] Đã lưu key lỗi vào {os.path.basename(error_file)}")
        except Exception as e:
            pass
    
    def _is_on_cooldown(self, key: str) -> bool:
        """Kiểm tra key có đang trong cooldown không"""
        if key not in self.cooldowns:
            return False
        if time.time() >= self.cooldowns[key]:
            del self.cooldowns[key]
            if key in self.status:
                self.status[key] = self.STATUS_ACTIVE
            return False
        return True
    
    def _mark_cooldown(self, key: str, status: str, duration: int):
        """Đánh dấu key vào cooldown"""
        with self.lock:
            self.cooldowns[key] = time.time() + duration
            self.status[key] = status
    
    def mark_401(self, key: str):
        """Đánh dấu key bị lỗi 401 - loại bỏ key DEAD, cooldown key tạm thời"""
        # 🔧 FIX: Nếu có DB, key 401 sẽ được đánh dấu DEAD trong DB
        # Local pool nên loại bỏ key này luôn thay vì cooldown
        if self._key_pool_db:
            # Key sẽ được report DEAD qua release_key() -> report_error()
            # Ở đây chỉ cần loại bỏ khỏi local pool
            with self.lock:
                if key in self.keys:
                    self.keys.remove(key)
                    self._log(f"[KeyPool] ☠️ Key {key[:10]}... bị 401 → LOẠI BỎ khỏi pool (DB sẽ đánh dấu DEAD)")
                # Xóa khỏi các cache
                self.cooldowns.pop(key, None)
                self.status.pop(key, None)
                self.credits_cache.pop(key, None)
                self._keys_in_use.discard(key)
                self._key_line_lock.pop(key, None)
                if key in self._healthy_keys:
                    self._healthy_keys.remove(key)
        else:
            # Fallback: cooldown như cũ nếu không có DB
            self._mark_cooldown(key, self.STATUS_ERROR_401, self.COOLDOWN_401)
            self._log(f"[KeyPool] ⚠️ Key {key[:10]}... bị 401 → cooldown {self.COOLDOWN_401}s")
    
    def mark_400(self, key: str):
        """Đánh dấu key bị lỗi 400 - cooldown tạm thời (400 có thể recover)"""
        # 400 thường là lỗi tạm thời (bad request, rate limit), không phải key chết
        self._mark_cooldown(key, self.STATUS_ERROR_400, self.COOLDOWN_400)
        self._log(f"[KeyPool] ⚠️ Key {key[:10]}... bị 400 → cooldown {self.COOLDOWN_400}s")
    
    def mark_full_slots(self, key: str):
        """Đánh dấu key bị full slots - KHÔNG persistent (chỉ tạm thời)"""
        self._mark_cooldown(key, self.STATUS_FULL_SLOTS, self.COOLDOWN_SLOTS)
        self._log(f"[KeyPool] ⚠️ Key {key[:10]}... full slots → cooldown {self.COOLDOWN_SLOTS}s")
    
    def mark_low_credit(self, key: str, credits: int):
        """Đánh dấu key còn ít credits - KHÔNG blacklist, sẽ được chuyển xuống cuối file"""
        with self.lock:
            self.credits_cache[key] = credits
            if credits < 100:
                self._mark_cooldown(key, self.STATUS_LOW_CREDIT, self.COOLDOWN_LOW)
    
    def update_credits(self, key: str, credits: int):
        """Cập nhật credits cache cho key"""
        with self.lock:
            self.credits_cache[key] = credits
    
    def _check_key_credits(self, key: str) -> int:
        """Check credits của key qua API, trả về credits còn lại"""
        if not self._client:
            return 10000
        
        try:
            sub = self._client.subscription_for_key_silent(key)
            if sub:
                limit = sub.get("character_limit", 0) or 0
                used = sub.get("character_count", 0) or 0
                credits = max(0, int(limit) - int(used))
                with self.lock:
                    self.credits_cache[key] = credits
                return credits
        except Exception as e:
            self._log(f"[KeyPool] Lỗi check credits: {e}")
        
        return self.credits_cache.get(key, 0)
    
    def acquire_key(self, required_chars: int, max_attempts: int = 50, timeout: float = 30.0, line_id: str = None, excluded_keys: set = None) -> Optional[str]:
        """
        🔧 STATE MACHINE KEY SELECTION - Query từ DB (KeyPoolV2)
        
        Nếu có _key_pool_db (KeyPoolV2):
        - Query trực tiếp từ DB: key_state = 'READY', credit_remaining DESC
        - 1 query, 0 retry, ưu tiên credit cao nhất
        
        Fallback về logic cũ nếu không có DB.
        
        Args:
            required_chars: Số ký tự cần gen
            max_attempts: Số lần thử tối đa (cho fallback)
            timeout: Timeout cho acquire lock
            line_id: ID của line đang xử lý (để lock key riêng cho line này)
            excluded_keys: Set các key đã thử và fail (để không pick lại)
        
        Returns: key nếu tìm được, None nếu không có key available
        """
        acquired = self.acquire_lock.acquire(timeout=timeout)
        if not acquired:
            self._log(f"[KeyPool] ⚠️ acquire_lock timeout sau {timeout}s - skip lần này")
            return None
        
        try:
            # 🔧 NEW: Ưu tiên dùng KeyPoolV2 (DB-backed) nếu có
            if self._key_pool_db:
                return self._acquire_key_from_db(required_chars, line_id, excluded_keys)
            
            # === FALLBACK: Logic cũ khi không có DB ===
            return self._acquire_key_local(required_chars, line_id, excluded_keys)
        finally:
            self.acquire_lock.release()
    
    def _acquire_key_from_db(self, required_chars: int, line_id: str = None, excluded_keys: set = None) -> Optional[str]:
        """
        🔧 NEW: Pick key từ DB (KeyPoolV2) - 1 query, ưu tiên credit cao nhất
        """
        min_credits = int(required_chars * 1.2)
        
        # Build excluded set (keys đang được dùng bởi line khác + keys đã thử fail)
        excluded = set(self._keys_in_use)
        for k, locked_by in self._key_line_lock.items():
            if locked_by != line_id:
                excluded.add(k)
        
        # 🔧 FIX: Thêm excluded_keys (keys đã thử và fail 401/voice_limit trong session này)
        if excluded_keys:
            excluded.update(excluded_keys)
        
        # Query từ DB
        key = self._key_pool_db.get_key(required_credits=min_credits, excluded=excluded)
        
        if key:
            with self.lock:
                self._keys_in_use.add(key)
                if line_id:
                    self._key_line_lock[key] = line_id
            self._log(f"[KeyPool] ✅ DB PICK key {key[:10]}... for line {line_id} (need {min_credits:,} credits)")
            return key
        
        # Không tìm được key
        in_use_count = len(self._keys_in_use)
        line_locked_count = len(self._key_line_lock)
        self._log(f"[KeyPool] ❌ DB: Không có key READY đủ credit: đang dùng={in_use_count}, locked={line_locked_count}")
        return None
    
    def _acquire_key_local(self, required_chars: int, line_id: str = None, excluded_keys: set = None) -> Optional[str]:
        """
        Logic cũ: Pick key từ local cache (round-robin + health tracking)
        """
        with self.lock:
            if not self.keys:
                return None
            total_keys = len(self.keys)
        
        min_credits = int(required_chars * 1.2)
        now = time.time()
        
        # 🔧 FIX: Merge excluded_keys vào check
        excluded_set = excluded_keys or set()
        
        # Helper function: Check key có available không
        def is_key_available(k):
            if k in excluded_set:  # 🔧 FIX: Check excluded_keys
                return False
            if k in self._keys_in_use:
                return False
            if self._is_on_cooldown(k):
                return False
            if k in self._key_line_lock:
                locked_by = self._key_line_lock[k]
                if locked_by != line_id:
                    return False
            return True
        
        # === PHASE 1: Thử key vừa success gần đây (trong 60s) ===
        with self.lock:
            recent_healthy = [
                k for k in self._healthy_keys 
                if is_key_available(k)
                and self._last_success_time.get(k, 0) > now - 60
            ]
        
        for key in recent_healthy[:5]:
            credits = self.credits_cache.get(key, 999999)
            if credits >= min_credits:
                with self.lock:
                    self.credits_cache[key] = max(0, credits - required_chars)
                    self._keys_in_use.add(key)
                    if line_id:
                        self._key_line_lock[key] = line_id
                self._log(f"[KeyPool] ⚡ FAST PICK healthy key {key[:10]}... for line {line_id}")
                return key
        
        # === PHASE 2: Thử key có success_count cao ===
        with self.lock:
            sorted_by_success = sorted(
                [k for k in self.keys if is_key_available(k)],
                key=lambda k: self._success_count.get(k, 0),
                reverse=True
            )
        
        for key in sorted_by_success[:10]:
            credits = self.credits_cache.get(key, 999999)
            if credits >= min_credits:
                with self.lock:
                    self.credits_cache[key] = max(0, credits - required_chars)
                    self._keys_in_use.add(key)
                    if line_id:
                        self._key_line_lock[key] = line_id
                success_streak = self._success_count.get(key, 0)
                if success_streak > 0:
                    self._log(f"[KeyPool] ✅ Pick key {key[:10]}... for line {line_id} (streak: {success_streak})")
                return key
        
        # === PHASE 3: Round-robin thông thường ===
        tried_keys = set()
        low_credit_keys = []
        
        while len(tried_keys) < total_keys:
            with self.lock:
                key = self.keys[self.i % len(self.keys)]
            
            if key in tried_keys:
                with self.lock:
                    self.i = (self.i + 1) % len(self.keys)
                continue
            
            if not is_key_available(key):
                tried_keys.add(key)
                with self.lock:
                    self.i = (self.i + 1) % len(self.keys)
                continue
            
            tried_keys.add(key)
            credits = self.credits_cache.get(key, 999999)
            
            if credits >= min_credits:
                with self.lock:
                    self.credits_cache[key] = max(0, credits - required_chars)
                    self.i = (self.i + 1) % len(self.keys)
                    self._keys_in_use.add(key)
                    if line_id:
                        self._key_line_lock[key] = line_id
                return key
            else:
                low_credit_keys.append((key, credits))
                with self.lock:
                    self.i = (self.i + 1) % len(self.keys)
        
        if low_credit_keys and hasattr(self, '_keys_file_path') and self._keys_file_path:
            self._move_low_credit_keys_to_bottom(low_credit_keys)
        
        cooldown_count = sum(1 for k in self.keys if self._is_on_cooldown(k))
        in_use_count = len(self._keys_in_use)
        line_locked_count = len(self._key_line_lock)
        self._log(f"[KeyPool] ❌ Không có key available: cooldown={cooldown_count}, đang dùng={in_use_count}, locked by lines={line_locked_count}")
        return None
    
    def _move_low_credit_keys_to_bottom(self, low_credit_keys: list):
        """Chuyển các keys không đủ credits xuống cuối file txt"""
        try:
            if not hasattr(self, '_keys_file_path') or not self._keys_file_path:
                return
            
            keys_to_move = set(k for k, c in low_credit_keys)
            
            with self.lock:
                good_keys = [k for k in self.keys if k not in keys_to_move]
                bad_keys = [k for k in self.keys if k in keys_to_move]
                
                bad_keys.sort(key=lambda k: self.credits_cache.get(k, 0), reverse=True)
                
                new_order = good_keys + bad_keys
                self.keys = new_order
                self.i = 0
            
            with open(self._keys_file_path, 'w', encoding='utf-8') as f:
                for key in new_order:
                    f.write(key + '\n')
            
            self._log(f"[KeyPool] 📝 Đã chuyển {len(bad_keys)} keys ít credits xuống cuối file")
        except Exception as e:
            self._log(f"[KeyPool] Lỗi chuyển keys: {e}")
    
    def release_key(self, key: str, chars_used: int, success: bool, error_code: str = None, line_id: str = None):
        """
        Release key sau khi thread xử lý xong.
        🔧 STATE MACHINE: Report kết quả về DB (KeyPoolV2) nếu có.
        """
        with self.lock:
            self._keys_in_use.discard(key)
            
            # Xóa line lock
            if key in self._key_line_lock:
                del self._key_line_lock[key]
        
        # 🔧 NEW: Report về DB (KeyPoolV2) nếu có
        if self._key_pool_db:
            try:
                if success:
                    self._key_pool_db.report_success(key, chars_used)
                    self._log(f"[KeyPool] ✅ DB: Reported success for {key[:10]}... ({chars_used:,} chars)")
                elif error_code:
                    # Map error_code sang error_type cho KeyPoolV2
                    error_type = "unknown"
                    error_code_str = str(error_code).lower()
                    if "401" in error_code_str:
                        error_type = "auth_error"
                    elif "voice_limit" in error_code_str or "voice_add_edit" in error_code_str:
                        error_type = "voice_limit"  # 🔧 FIX: Voice limit -> DEAD
                    elif "400" in error_code_str:
                        error_type = "unknown"  # 400 có thể là nhiều lý do
                    elif "429" in error_code_str:
                        error_type = "rate_limit"
                    elif "slot" in error_code_str or "concurrent" in error_code_str:
                        error_type = "rate_limit"
                    
                    self._key_pool_db.report_error(key, error_type, str(error_code))
                    self._log(f"[KeyPool] ⚠️ DB: Reported error {error_type} for {key[:10]}...")
            except Exception as e:
                self._log(f"[KeyPool] ⚠️ DB report error: {e}")
        
        # === LOCAL TRACKING (giữ lại để fallback) ===
        with self.lock:
            if success:
                if key in self.credits_cache:
                    self.credits_cache[key] = max(0, self.credits_cache[key] - chars_used)
                
                self._success_count[key] = self._success_count.get(key, 0) + 1
                self._fail_count[key] = 0
                self._last_success_time[key] = time.time()
                
                if key not in self._healthy_keys:
                    self._healthy_keys.insert(0, key)
                    if len(self._healthy_keys) > 50:
                        self._healthy_keys.pop()
                else:
                    self._healthy_keys.remove(key)
                    self._healthy_keys.insert(0, key)
                
                self.status[key] = self.STATUS_ACTIVE
                if key in self.cooldowns:
                    del self.cooldowns[key]
            else:
                self._fail_count[key] = self._fail_count.get(key, 0) + 1
                self._success_count[key] = 0
                
                if key in self._healthy_keys:
                    self._healthy_keys.remove(key)
                
                if error_code:
                    if '401' in str(error_code):
                        self.cooldowns[key] = time.time() + self.COOLDOWN_401
                        self.status[key] = self.STATUS_ERROR_401
                    elif '400' in str(error_code):
                        self.cooldowns[key] = time.time() + self.COOLDOWN_400
                        self.status[key] = self.STATUS_ERROR_400
                    elif 'slot' in str(error_code).lower() or 'concurrent' in str(error_code).lower():
                        self.cooldowns[key] = time.time() + self.COOLDOWN_SLOTS
                        self.status[key] = self.STATUS_FULL_SLOTS
    
    def mark_success(self, key: str):
        """Đánh dấu key vừa gen thành công - dùng cho code cũ không dùng release_key"""
        with self.lock:
            self._success_count[key] = self._success_count.get(key, 0) + 1
            self._fail_count[key] = 0
            self._last_success_time[key] = time.time()
            
            if key not in self._healthy_keys:
                self._healthy_keys.insert(0, key)
                if len(self._healthy_keys) > 50:
                    self._healthy_keys.pop()
            
            # Reset cooldown nếu có
            if key in self.cooldowns:
                del self.cooldowns[key]
            self.status[key] = self.STATUS_ACTIVE
        
        # 🔧 NOTE: Không report về DB ở đây vì không biết chars_used
        # Caller nên dùng release_key() thay vì mark_success()
    
    # === Backward compatible methods ===
    def cur(self) -> Optional[str]:
        """Lấy key hiện tại (compatible với code cũ)"""
        with self.lock:
            if not self.keys:
                return None
            start_i = self.i
            for _ in range(len(self.keys)):
                key = self.keys[self.i % len(self.keys)]
                if not self._is_on_cooldown(key):
                    return key
                self.i = (self.i + 1) % len(self.keys)
            self.i = start_i
            return self.keys[self.i % len(self.keys)]
    
    def rotate(self) -> Optional[str]:
        """Xoay sang key tiếp theo (compatible với code cũ)"""
        with self.lock:
            if not self.keys:
                return None
            start_i = self.i
            for _ in range(len(self.keys)):
                self.i = (self.i + 1) % len(self.keys)
                key = self.keys[self.i]
                if not self._is_on_cooldown(key):
                    return key
            return self.keys[self.i]
    
    def cur_index(self) -> int:
        """Trả về index hiện tại"""
        with self.lock:
            return self.i
    
    def mark_bad(self, key: str, duration: int = None):
        """Đánh dấu key bad (compatible với code cũ)"""
        self.mark_401(key)
    
    def get_cooldown_count(self) -> int:
        """Đếm số key đang bị cooldown"""
        with self.lock:
            now = time.time()
            return sum(1 for t in self.cooldowns.values() if t > now)
    
    def get_total_credits(self) -> int:
        """Lấy tổng credits ước tính của tất cả keys"""
        with self.lock:
            return sum(self.credits_cache.values())
    
    def get_keys_info(self) -> Dict:
        """Lấy thông tin chi tiết về trạng thái keys"""
        with self.lock:
            now = time.time()
            active = sum(1 for k in self.keys if k not in self.cooldowns or self.cooldowns[k] < now)
            cooldown = len(self.keys) - active
            total_credits = sum(self.credits_cache.values())
            known_keys = len(self.credits_cache)
            return {
                'total': len(self.keys),
                'active': active,
                'cooldown': cooldown,
                'known_credits': known_keys,
                'total_credits': total_credits
            }
    
    def reset_runtime_state(self):
        """
        🔧 Reset runtime state - giống như khi restart app.
        Xóa cooldowns và keys_in_use để unlock các keys bị stuck.
        """
        with self.lock:
            old_cooldown_count = len(self.cooldowns)
            old_in_use_count = len(self._keys_in_use)
            
            self.cooldowns.clear()
            self._keys_in_use.clear()
            
            # Reset tất cả status về ACTIVE
            for key in self.keys:
                self.status[key] = self.STATUS_ACTIVE
            
            self._log(f"[KeyPool] 🔄 RESET STATE: cleared {old_cooldown_count} cooldowns, {old_in_use_count} in-use keys")
            return old_cooldown_count, old_in_use_count

# Backward compatible alias
KeyManager = KeyPoolManager


class ProxyManager:
    """
    Quản lý proxy - hỗ trợ 2 chế độ:
    1. Static proxy list (dán nhiều dòng proxy)
    2. Rotating proxy key từ proxyxoay.shop (dán 1 key duy nhất)
    
    🔧 INTEGRATION với ProxyServiceDB:
    - Nếu có proxy_service_db, delegate failure/success reporting
    - Retry logic: 5-10 lần fail trước khi chuyển proxy
    """
    PROXYXOAY_API = "https://proxyxoay.shop/api/get.php"
    
    def __init__(self):
        self.raw = ""
        self.enabled = False
        self.phase_log = False
        self.i = 0
        self.lock = threading.Lock()
        
        # Rotating proxy state
        self._is_rotating_key = False
        self._rotating_key = ""
        self._current_proxy = None
        self._proxy_expire_time = 0
        self._log_fn = None
        
        # 🔧 NEW: Reference to ProxyServiceDB for retry logic
        self._proxy_service_db = None
        self._is_rotating = False  # Flag nếu dùng proxyxoay/rotating proxy
        self._proxies = []  # Cache proxy list
    
    def set_log_fn(self, log_fn):
        """Set logging function"""
        self._log_fn = log_fn
    
    def set_proxy_service_db(self, proxy_service_db):
        """Set reference to ProxyServiceDB for retry logic"""
        self._proxy_service_db = proxy_service_db
        if proxy_service_db:
            self._log(f"[Proxy] ✅ Linked to ProxyServiceDB")
    
    def _log(self, msg: str):
        if self._log_fn:
            try:
                self._log_fn(msg)
            except:
                pass
    
    def _is_proxyxoay_key(self, text: str) -> bool:
        """Kiểm tra xem text có phải là key xoay proxyxoay.shop không"""
        text = text.strip()
        # Key xoay thường là 22 ký tự alphanumeric, không chứa : hoặc @
        if len(text) >= 15 and len(text) <= 30:
            if ':' not in text and '@' not in text and '.' not in text:
                if text.isalnum():
                    return True
        return False
    
    def set(self, text: str, phase_log: bool = True):
        self.raw = text or ""
        self.phase_log = phase_log
        self.i = 0
        
        # Check nếu là key xoay
        lines = [x.strip() for x in self.raw.splitlines() if x.strip()]
        if len(lines) == 1 and self._is_proxyxoay_key(lines[0]):
            self._is_rotating_key = True
            self._rotating_key = lines[0]
            self._current_proxy = None
            self._proxy_expire_time = 0
            # 🐛 FIX: KHÔNG set self.enabled ở đây - để MainWindow quyết định
            # self.enabled = True  # ← REMOVED
            self._log(f"[Proxy] ✅ Phát hiện key xoay proxyxoay.shop: {self._rotating_key[:10]}...")
        else:
            self._is_rotating_key = False
            self._rotating_key = ""
            # 🐛 FIX: KHÔNG set self.enabled ở đây - để MainWindow quyết định
            # self.enabled = bool(lines)  # ← REMOVED - đây là bug gây mất proxy settings!
    
    def _fetch_rotating_proxy(self) -> Optional[str]:
        """Gọi API proxyxoay.shop để lấy proxy mới"""
        if not self._rotating_key:
            return None
        
        try:
            url = f"{self.PROXYXOAY_API}?key={self._rotating_key}&nhamang=random&tinhthanh=random"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            
            if data.get("status") == 100:
                # Lấy proxy http (format: ip:port::)
                proxy_http = data.get("proxyhttp", "")
                if proxy_http:
                    # Format: "ip:port::" hoặc "ip:port:user:pass"
                    parts = proxy_http.split(":")
                    if len(parts) >= 2:
                        ip, port = parts[0], parts[1]
                        # Nếu có user:pass
                        if len(parts) >= 4 and parts[2] and parts[3]:
                            proxy_url = f"http://{parts[2]}:{parts[3]}@{ip}:{port}"
                        else:
                            proxy_url = f"http://{ip}:{port}"
                        
                        # Parse thời gian sống từ message (vd: "proxy nay se die sau 1503s")
                        msg = data.get("message", "")
                        try:
                            import re
                            match = re.search(r'(\d+)s', msg)
                            if match:
                                ttl = int(match.group(1))
                                self._proxy_expire_time = time.time() + ttl - 30  # Trừ 30s buffer
                        except:
                            self._proxy_expire_time = time.time() + 1200  # Default 20 phút
                        
                        location = data.get("Vi Tri", "Unknown")
                        isp = data.get("Nha Mang", "Unknown")
                        self._log(f"[Proxy] 🔄 Lấy proxy mới: {ip}:{port} ({isp}/{location})")
                        return proxy_url
            
            elif data.get("status") == 101:
                # Có thể là rate limit hoặc key không tồn tại
                msg = data.get("message", "")
                if "moi co the doi" in msg.lower() or "doi proxy" in msg.lower():
                    # Rate limit - giữ proxy cũ, đợi thêm
                    self._log(f"[Proxy] ⏳ Rate limit - đợi thêm giây...")
                    self._proxy_expire_time = time.time() + 10  # Đợi 10 giây
                else:
                    self._log(f"[Proxy] ❌ Key không tồn tại: {msg}")
            elif data.get("status") == 102:
                self._log(f"[Proxy] ❌ Key hết hạn")
            else:
                self._log(f"[Proxy] ❌ Lỗi API: {data}")
                
        except Exception as e:
            self._log(f"[Proxy] ❌ Lỗi kết nối API proxyxoay: {e}")
        
        return None
    
    def list(self) -> List[str]:
        return [x.strip() for x in self.raw.splitlines() if x.strip()]
    
    def cur(self) -> Optional[str]:
        with self.lock:
            if not self.enabled:
                return None
            
            # 🔧 NEW: Ưu tiên proxy từ DB (ProxyServiceDB)
            if self._proxy_service_db:
                # Lấy proxy từ DB service
                proxy_url = self._proxy_service_db.get_current_proxy()
                if proxy_url:
                    # Update cache
                    if len(self._proxies) > 0:
                        self._proxies[0] = proxy_url
                    else:
                        self._proxies = [proxy_url]
                    self._current_proxy = proxy_url
                    return proxy_url
                # Nếu DB không có, thử dùng cache
                if self._proxies and len(self._proxies) > 0:
                    return self._proxies[self._idx % len(self._proxies)]
                if self._current_proxy:
                    return self._current_proxy
            
            # Nếu là key xoay - giữ proxy lâu (theo TTL từ API, không xoay liên tục)
            if self._is_rotating_key:
                # Giữ proxy hiện tại đến khi hết TTL (như V30 tool)
                if self._current_proxy and time.time() < self._proxy_expire_time:
                    return self._current_proxy
                
                # Lấy proxy mới chỉ khi hết TTL
                new_proxy = self._fetch_rotating_proxy()
                if new_proxy:
                    self._current_proxy = new_proxy
                    # TTL đã được set trong _fetch_rotating_proxy() từ message API
                    return new_proxy
                return self._current_proxy  # Fallback proxy cũ nếu không lấy được mới
            
            # Static proxy list
            lst = self.list()
            if not lst:
                return None
            return lst[self.i % len(lst)]
    
    def rotate(self) -> Optional[str]:
        """
        Xoay proxy.
        
        🔧 INTEGRATION:
        - Nếu có proxy_service_db: delegate report_failure() + lấy proxy mới
        - Nếu không: dùng logic local
        """
        with self.lock:
            if not self.enabled:
                return None
            
            # 🔧 NEW: Delegate to ProxyServiceDB if available
            if self._proxy_service_db:
                self._log(f"[Proxy] 🔄 Reporting failure to ProxyServiceDB...")
                switched = self._proxy_service_db.report_failure()
                new_proxy = self._proxy_service_db.get_current_proxy()
                if new_proxy:
                    self._current_proxy = new_proxy
                    if len(self._proxies) > 0:
                        self._proxies[0] = new_proxy
                    else:
                        self._proxies = [new_proxy]
                    
                    info = self._proxy_service_db.get_proxy_info()
                    fail_count = info.get('fail_count', 0)
                    max_retry = self._proxy_service_db.MAX_RETRY_BEFORE_SWITCH
                    
                    if switched:
                        self._log(f"[Proxy] 🔀 Switched to new proxy: {new_proxy[:40]}...")
                    else:
                        self._log(f"[Proxy] 🔄 Retry {fail_count}/{max_retry}: {new_proxy[:40]}...")
                    
                    return new_proxy
                return self._current_proxy
            
            # Nếu là key xoay - force lấy proxy mới
            if self._is_rotating_key:
                self._proxy_expire_time = 0  # Force refresh
                new_proxy = self._fetch_rotating_proxy()
                if new_proxy:
                    self._current_proxy = new_proxy
                return self._current_proxy
            
            # Static proxy list
            lst = self.list()
            if not lst:
                return None
            self.i = (self.i + 1) % len(lst)
            return lst[self.i]
    
    def report_success(self):
        """Report proxy success - reset fail count"""
        if self._proxy_service_db:
            self._proxy_service_db.report_success()
    
    def sync_index(self, key_index: int):
        with self.lock:
            if self._is_rotating_key:
                return  # Không cần sync với key xoay
            lst = self.list()
            if lst:
                self.i = key_index % len(lst)
    
    @staticmethod
    def to_requests(p: Optional[str]) -> Optional[dict]:
        """
        Parse proxy string thành dict cho requests
        Hỗ trợ nhiều định dạng:
        - hostname:port
        - hostname:port:username:password
        - username:password:hostname:port
        - username:password@hostname:port
        - http://hostname:port
        - http://username:password@hostname:port
        - socks5://username:password@hostname:port
        """
        if not p or not p.strip():
            return None
        
        p = p.strip()
        
        if "://" in p:
            return {"http": p, "https": p}
        
        if "@" in p:
            auth_part, host_part = p.rsplit("@", 1)
            if ":" in auth_part and ":" in host_part:
                user_pass = auth_part  # username:password
                host_port = host_part   # hostname:port
                proxy_url = f"http://{user_pass}@{host_port}"
                return {"http": proxy_url, "https": proxy_url}
        
        parts = p.split(":")
        
        if len(parts) == 2:
            # hostname:port
            host, port = parts
            proxy_url = f"http://{host}:{port}"
            
        elif len(parts) == 4:
            # 2. username:password:hostname:port
            
            try:
                port_val = int(parts[1])
                if 1 <= port_val <= 65535:
                    host, port, user, passwd = parts
                    proxy_url = f"http://{user}:{passwd}@{host}:{port}"
                else:
                    raise ValueError()
            except:
                try:
                    port_val = int(parts[3])
                    if 1 <= port_val <= 65535:
                        user, passwd, host, port = parts
                        proxy_url = f"http://{user}:{passwd}@{host}:{port}"
                    else:
                        raise ValueError()
                except:
                    host, port, user, passwd = parts
                    proxy_url = f"http://{user}:{passwd}@{host}:{port}"
                    
        elif len(parts) >= 5:
            try:
                port_val = int(parts[1])
                if 1 <= port_val <= 65535:
                    host = parts[0]
                    port = parts[1]
                    passwd = parts[-1]
                    user = ":".join(parts[2:-1])
                    proxy_url = f"http://{user}:{passwd}@{host}:{port}"
                else:
                    raise ValueError()
            except:
                try:
                    port_val = int(parts[-1])
                    if 1 <= port_val <= 65535:
                        port = parts[-1]
                        host = parts[-2]
                        user = parts[0]
                        passwd = ":".join(parts[1:-2])
                        proxy_url = f"http://{user}:{passwd}@{host}:{port}"
                    else:
                        raise ValueError()
                except:
                    # Fallback
                    proxy_url = f"http://{p}"
        else:
            proxy_url = f"http://{p}"
        
        return {"http": proxy_url, "https": proxy_url}


# ---------------- ElevenLabs client ----------------
class ElevenClient:
    def __init__(self, keys: KeyManager, proxies: ProxyManager, log_cb, settings: Optional[AppSettings] = None):
        self.keys = keys
        self.proxies = proxies
        self.log = log_cb
        self.settings = settings
        self.s_post = self._make_session(True)
        self.s_get  = self._make_session(False)
        
        # Data usage tracking
        self.data_proxy_sent = 0
        self.data_proxy_recv = 0
        self.data_direct_sent = 0
        self.data_direct_recv = 0
    
    def log_data_summary(self):
        proxy_total = (self.data_proxy_sent + self.data_proxy_recv) / 1024
        direct_total = (self.data_direct_sent + self.data_direct_recv) / 1024
        self.log(f"📊 Data Usage Summary:")
        self.log(f"   PROXY:  Sent {self.data_proxy_sent/1024:.1f}KB | Recv {self.data_proxy_recv/1024:.1f}KB | Total: {proxy_total:.1f}KB")
        self.log(f"   DIRECT: Sent {self.data_direct_sent/1024:.1f}KB | Recv {self.data_direct_recv/1024:.1f}KB | Total: {direct_total:.1f}KB")

    def _make_session(self, proxied: bool) -> requests.Session:
        s = requests.Session()
        s.trust_env = False
        retries = Retry(total=3, connect=3, read=3, backoff_factor=0.5,
                        status_forcelist=(429,500,502,503,504),
                        allowed_methods=frozenset(["GET","POST"]))
        ad = HTTPAdapter(max_retries=retries, pool_maxsize=32)
        s.mount("https://", ad); s.mount("http://", ad)
        s._proxied = proxied
        return s

    def refresh_session(self):
        """Tạo lại session mới để fix lỗi ProtocolError/Connection aborted"""
        try:
            if self.s_post: self.s_post.close()
            if self.s_get: self.s_get.close()
        except:
            pass
        self.s_post = self._make_session(True)
        self.s_get = self._make_session(False)
        self.log("🔄 Session refreshed (clean connection pool)")

    def _req(self, sess: requests.Session, method: str, path: str, *, key: Optional[str]=None, **kw):
        url = ELEVEN_BASE + path
        headers = kw.pop("headers", {})
        headers["accept"] = "application/json"
        key = key or self.keys.cur()
        if not key:
            raise RuntimeError("Chưa chọn file API Keys trong Cài đặt nâng cao.")
        headers["xi-api-key"] = key
        kw["headers"] = headers

        if sess._proxied:
            key_idx = self.keys.cur_index()
            self.proxies.sync_index(key_idx)
            p = self.proxies.cur()
            if p:
                try:
                    masked = p
                    if "@" in p:
                        proto_end = p.find("://") + 3 if "://" in p else 0
                        at_pos = p.rfind("@")
                        masked = p[:proto_end] + "***:***@" + p[at_pos+1:]
                    self.log(f"[DEBUG-PROXY] key_idx={key_idx} proxy={masked}")
                except:
                    self.log(f"[DEBUG-PROXY] key_idx={key_idx} proxy_len={len(p) if p else 0}")
            else:
                self.log(f"[DEBUG-PROXY] key_idx={key_idx} NO PROXY (p=None)")
            kw["proxies"] = ProxyManager.to_requests(p) if p else None
        else:
            kw["proxies"] = None

        t0=time.time()
        body = kw.get("data") or kw.get("json")
        sent_bytes = len(str(body).encode('utf-8')) if body else 0
        r=sess.request(method, url, timeout=60, **kw)
        dt=int((time.time()-t0)*1000)
        recv_bytes = len(r.content) if r.content else 0
        is_proxy = sess._proxied and kw.get("proxies")
        if is_proxy:
            self.data_proxy_sent += sent_bytes
            self.data_proxy_recv += recv_bytes
        else:
            self.data_direct_sent += sent_bytes
            self.data_direct_recv += recv_bytes
        phase = "PROXY" if is_proxy else "DIRECT"
        self.log(f"{method} {path} {r.status_code} {dt}ms {phase} | Sent: {sent_bytes/1024:.1f}KB Recv: {recv_bytes/1024:.1f}KB")

        if r.status_code == 400:
            try:
                err_json = r.json()
                detail = err_json.get("detail", err_json)
                if isinstance(detail, dict):
                    status = detail.get("status", "N/A")
                    message = detail.get("message", "N/A")
                    self.log(f"   [400 Response] status={status}, message={message[:200]}")
                else:
                    self.log(f"   [400 Response] {str(detail)[:250]}")
            except:
                self.log(f"   [400 Response] Raw: {r.text[:250] if r.text else 'empty'}")

        if r.status_code in (401, 429, 403, 503):
            self.keys.rotate()
            if sess._proxied and self.proxies.list():
                old_proxy = self.proxies.cur()
                new_proxy = self.proxies.rotate()
                self.log(f"⚠️ Lỗi {r.status_code} - xoay proxy: {old_proxy[:20] if old_proxy else 'None'}... → {new_proxy[:20] if new_proxy else 'None'}...")
                kw["proxies"] = ProxyManager.to_requests(new_proxy) if new_proxy else None
            time.sleep(1.0)
            r=sess.request(method, url, timeout=60, **kw)
            self.log(f"Retry → {r.status_code}")
        r.raise_for_status()
        return r

    # ----- public APIs -----
    def list_models(self) -> List[dict]:
        r = self._req(self.s_get, "GET", "/v1/models")
        if r.content:
            try:
                return r.json()
            except:
                return []
        return []

    def list_voices(self) -> List[dict]:
        r = self._req(self.s_get, "GET", "/v1/voices")
        if r.content:
            try:
                js = r.json()
                return js.get("voices", [])
            except:
                return []
        return []

    def get_voice(self, voice_id: str) -> Optional[dict]:
        r = self._req(self.s_get, "GET", f"/v1/voices/{voice_id}")
        if r.content:
            try:
                return r.json()
            except:
                return None
        return None

    def search_voices(self, query: str = "", voice_id: str = "", page_size: int = 100, max_pages: int = 10) -> List[dict]:
        """Tìm kiếm voice từ API v2 (voice trong thư viện của bạn)
        
        Args:
            query: Tìm theo tên voice
            voice_id: Tìm theo voice_id cụ thể
        """
        all_voices = []
        next_token = None
        pages_fetched = 0
        
        while pages_fetched < max_pages:
            params = {"page_size": page_size, "include_total_count": "true"}
            if voice_id:
                params["voice_ids"] = voice_id
            elif query:
                params["search"] = query
            if next_token:
                params["next_page_token"] = next_token
            
            try:
                r = self._req(self.s_get, "GET", "/v2/voices", params=params)
                if not r.content:
                    break
                try:
                    js = r.json()
                except:
                    break
                voices = js.get("voices", [])
                all_voices.extend(voices)
                
                # Check for more pages
                has_more = js.get("has_more", False)
                next_token = js.get("next_page_token")
                
                if not has_more or not next_token:
                    break
                pages_fetched += 1
            except Exception as e:
                self.log(f"Search voices error: {e}")
                break
        
        return all_voices
    
    def search_shared_voices(self, query: str = "", page_size: int = 100, max_pages: int = 5) -> List[dict]:
        all_voices = []
        page = 0
        
        while page < max_pages:
            params = {"page_size": page_size, "page": page}
            if query:
                params["search"] = query
            
            try:
                r = self._req(self.s_get, "GET", "/v1/shared-voices", params=params)
                if not r.content:
                    break
                try:
                    js = r.json()
                except:
                    break
                voices = js.get("voices", [])
                all_voices.extend(voices)
                
                # Check if more pages
                if len(voices) < page_size:
                    break
                page += 1
            except Exception as e:
                self.log(f"Search shared voices error: {e}")
                break
        
        return all_voices


    def _post_process_audio(self, mp3_path: str, enable_processing: bool = True):
        """
        Post-process audio file để cải thiện chất lượng.
        Inspired by Viterbox TTS best practices.
        
        Processing pipeline:
        1. Fade in/out để tránh click
        2. Highpass filter để cắt low-freq noise (<80Hz)
        3. Normalize volume
        
        Args:
            mp3_path: Path to MP3 file
            enable_processing: Enable/disable processing (default True)
            
        Raises:
            RuntimeError: Nếu FFmpeg không tìm thấy (BẮT BUỘC)
        """
        if not enable_processing:
            return
        
        # Find ffmpeg (BẮT BUỘC)
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # Try common paths
            common_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe", 
                r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
                r"D:\ffmpeg\bin\ffmpeg.exe",
            ]
            for path in common_paths:
                if os.path.exists(path):
                    ffmpeg = path
                    break
        
        if not ffmpeg:
            error_msg = (
                "❌ FFmpeg KHÔNG TÌM THẤY - BẮT BUỘC để xử lý audio chất lượng cao!\n"
                "   \n"
                "   📥 Cài đặt FFmpeg:\n"
                "   1. Download: https://www.gyan.dev/ffmpeg/builds/\n"
                "   2. Giải nén vào C:\\ffmpeg\\ hoặc\n"
                "   3. Cài qua Chocolatey: choco install ffmpeg\n"
                "   \n"
                "   ⚠️  Không có FFmpeg = Audio bị méo/ồm ồm (low-freq noise)\n"
            )
            self.log(error_msg)
            raise RuntimeError("FFmpeg not found. Audio post-processing is REQUIRED for quality output.")
        
        try:
            # Create temp output file
            temp_out = mp3_path + ".processed.mp3"
            
            # Build filter chain (minimal - ONLY highpass to cut rumble):
            # highpass=f=50 - cut frequencies below 50Hz (very gentle, only remove deep rumble)
            # NO fade, NO loudnorm - keep it simple
            
            filter_chain = "highpass=f=50"
            
            cmd = [
                ffmpeg,
                "-i", mp3_path,
                "-af", filter_chain,
                "-ar", "44100",  # Keep sample rate
                "-b:a", "128k",  # Keep same bitrate as input
                "-y",  # Overwrite
                temp_out
            ]
            
            # Run processing with hidden window
            import subprocess
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if result.returncode == 0 and os.path.exists(temp_out):
                # Replace original with processed
                os.replace(temp_out, mp3_path)
                self.log(f"✨ Audio post-processed: fade in/out + highpass + normalize")
            else:
                # Processing failed - this is critical
                error_detail = result.stderr[:300] if result.stderr else "Unknown error"
                self.log(f"❌ Audio processing FAILED: {error_detail}")
                # Clean up temp file if exists
                try:
                    if os.path.exists(temp_out):
                        os.remove(temp_out)
                except:
                    pass
                raise RuntimeError(f"Audio post-processing failed: {error_detail}")
                    
        except subprocess.TimeoutExpired:
            self.log("❌ Audio processing TIMEOUT (>60s)")
            raise RuntimeError("Audio post-processing timeout")
        except Exception as e:
            if "FFmpeg not found" in str(e):
                raise  # Re-raise FFmpeg not found
            self.log(f"❌ Audio post-processing error: {e}")
            raise RuntimeError(f"Audio post-processing failed: {e}")



    def tts_direct(self, voice_id: str, text: str, model_id: str, settings: dict, outpath: str, output_format: str = "mp3_44100_128", api_key: str = None):
        """
        TTS Direct - gọi ElevenLabs API trực tiếp
        
        Args:
            api_key: Key để dùng. Nếu None, sẽ lấy từ self.keys.cur()
        """
        import json as json_module
        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": settings,
        }
        
        # Add language_code if specified (for manual language override)
        if self.settings and hasattr(self.settings, 'language_code') and self.settings.language_code:
            payload["language_code"] = self.settings.language_code
            self.log(f"🌐 Language Override: {self.settings.language_code.upper()} (đảm bảo phát âm chính xác)")
        else:
            self.log(f"🌐 Language: Auto-detect (có thể phát âm sai với tiếng Việt)")
        
        payload_json = json_module.dumps(payload)
        sent_bytes = len(payload_json.encode('utf-8'))
        url = f"/v1/text-to-speech/{voice_id}?output_format={output_format}"
        
        try:
            headers = {"accept": "audio/mpeg"}
            # 🔧 FIX: Dùng api_key được truyền vào, fallback về cur() nếu không có
            key = api_key or self.keys.cur()
            if not key:
                raise RuntimeError("Chưa chọn file API Keys trong Cài đặt nâng cao.")
            headers["xi-api-key"] = key
            headers["Content-Type"] = "application/json"
            
            full_url = ELEVEN_BASE + url
            
            proxies = None
            if self.s_post._proxied and self.proxies.enabled:
                key_idx = self.keys.cur_index()
                self.proxies.sync_index(key_idx)
                p = self.proxies.cur()
                if p:
                    try:
                        masked = p
                        if "@" in p:
                            proto_end = p.find("://") + 3 if "://" in p else 0
                            at_pos = p.rfind("@")
                            masked = p[:proto_end] + "***:***@" + p[at_pos+1:]
                        self.log(f"[tts_direct-PROXY] key_idx={key_idx} proxy={masked}")
                    except:
                        pass
                proxies = ProxyManager.to_requests(p) if p else None
            
            is_using_proxy = bool(proxies)
            
            # 🔧 FIX: Tăng timeout lên 45s cho các chunk lớn (tiếng Nhật/Trung cần nhiều thời gian hơn)
            request_timeout = 45
            
            t0 = time.time()
            r = self.s_post.request("POST", full_url, json=payload, headers=headers, 
                                     timeout=request_timeout, proxies=proxies, stream=True)
            dt = int((time.time() - t0) * 1000)
            
            self.log(f"POST {url} {r.status_code} {dt}ms" + (" PROXY" if is_using_proxy else " DIRECT"))
            
            if r.status_code == 401:
                # 🔧 FIX: Throw error ngay để ChunkWorker đổi key từ DB
                # Không retry ở đây vì key đã chết, xoay proxy cũng vô ích
                raise RuntimeError(f"401 Client Error: Unauthorized for url: {full_url}")
            
            if r.status_code == 429:
                # Rate limit - xoay proxy và retry 1 lần
                if self.s_post._proxied and self.proxies.enabled:
                    new_proxy = self.proxies.rotate()
                    if new_proxy:
                        proxies = ProxyManager.to_requests(new_proxy)
                        self.log(f"🔄 429 Rate limit - Rotated proxy → {new_proxy[:30]}...")
                
                time.sleep(0.5)  # 🔧 FIX: Giảm từ 1s xuống 0.5s
                r = self.s_post.request("POST", full_url, json=payload, headers=headers, 
                                         timeout=request_timeout, proxies=proxies, stream=True)
                self.log(f"Retry → {r.status_code}")
                
                if r.status_code == 429:
                    # Vẫn rate limit - throw error để ChunkWorker đổi key
                    raise RuntimeError(f"429 Rate Limit: Too Many Requests")
            
            if r.status_code == 400:
                # Raise error kèm body để LineWorker bắt được "voice_limit"
                raise RuntimeError(f"400 Client Error: {r.text}")

            r.raise_for_status()
            
            ensure_dir(os.path.dirname(outpath))
            tmp = outpath + ".part"
            recv_bytes = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        recv_bytes += len(chunk)
            
            os.replace(tmp, outpath)
            
            # Track data usage
            if is_using_proxy:
                self.data_proxy_sent += sent_bytes
                self.data_proxy_recv += recv_bytes
            else:
                self.data_direct_sent += sent_bytes
                self.data_direct_recv += recv_bytes
            
            self.log(f"   → Sent: {sent_bytes/1024:.1f}KB Recv: {recv_bytes/1024:.1f}KB ({recv_bytes} bytes)")
            
            # Post-processing: MINIMAL - chỉ highpass 50Hz
            try:
                self._post_process_audio(outpath, enable_processing=True)
            except Exception as proc_err:
                self.log(f"⚠️  Post-processing error: {proc_err}")
                # File vẫn OK nếu lỗi
            
            return True
            
        except Exception as e:
            self.log(f"TTS Direct error: {e}")
            raise

    # ----- credits per key -----
    def subscription_for_key(self, key: str) -> Optional[dict]:
        try:
            r = self._req(self.s_get, "GET", "/v1/user/subscription", key=key)
            return r.json()
        except Exception:
            return None
    
    def subscription_for_key_silent(self, key: str) -> Optional[dict]:
        """Check subscription không log (dùng cho worker thread)"""
        try:
            url = ELEVEN_BASE + "/v1/user/subscription"
            headers = {"accept": "application/json", "xi-api-key": key}
            r = self.s_get.request("GET", url, headers=headers, timeout=30, proxies=None)
            if r.status_code == 200 and r.content:
                try:
                    return r.json()
                except:
                    return None
            return None
        except Exception:
            return None
    
    # ----- Voice slot management -----
    def list_voices_for_key(self, key: str) -> List[dict]:
        """Lấy danh sách voices của 1 key cụ thể"""
        try:
            url = ELEVEN_BASE + "/v1/voices"
            headers = {"accept": "application/json", "xi-api-key": key}
            r = self.s_get.request("GET", url, headers=headers, timeout=30, proxies=None)
            if r.status_code == 200:
                data = r.json()
                voices_list = data.get("voices", [])
                # self.log(f"[DEBUG] Found {len(voices_list)} total voices")
                return voices_list
            else:
                 pass # self.log(f"[DEBUG] List voices failed: {r.status_code} {r.text}")
            return []
        except Exception as e:
            # self.log(f"[DEBUG] List voices error: {e}")
            return []
    
    def delete_voice(self, key: str, voice_id: str) -> tuple:
        """Xóa 1 voice khỏi key"""
        try:
            url = ELEVEN_BASE + f"/v1/voices/{voice_id}"
            headers = {"accept": "application/json", "xi-api-key": key}
            r = self.s_get.request("DELETE", url, headers=headers, timeout=30, proxies=None)
            if r.status_code in (200, 204):
                return True, "Deleted"
            return False, f"{r.status_code}: {r.text}"
        except Exception as e:
            return False, f"Error: {e}"
    
    def cleanup_voice_slots(self, key: str, max_voices: int = 2, protected_voice_ids: List[str] = None) -> int:
        """
        Dọn dẹp voice slots - xóa các voice thừa để đảm bảo còn slot trống
        
        Args:
            key: API key
            max_voices: Số voice tối đa giữ lại (mặc định 2, để 1 slot trống cho add mới)
            protected_voice_ids: Danh sách voice_id không được xóa
            
        Returns:
            Số voice đã xóa
        """
        if protected_voice_ids is None:
            protected_voice_ids = []
        
        voices = self.list_voices_for_key(key)
        
        deletable = []
        for v in voices:
            vid = v.get("voice_id", "")
            category = v.get("category", "").lower()
            
            if vid in protected_voice_ids:
                continue
            
            if category in ("premade", "default"):
                continue
            
            deletable.append(vid)
        
        current_count = len(voices)
        if current_count <= max_voices:
            return 0
        
        to_delete = current_count - max_voices
        deleted = 0
        
        for vid in deletable[:to_delete]:
            ok, msg = self.delete_voice(key, vid)
            if ok:
                deleted += 1
                self.log(f"[Cleanup] Đã xóa voice {vid[:8]}... → slot freed")
            else:
                # Nếu đã bị xóa bởi luồng khác (400/404) -> coi như thành công
                if "voice_does_not_exist" in msg or "404" in msg:
                     deleted += 1
                else:
                     self.log(f"[Cleanup] Lỗi xóa voice {vid[:8]}...: {msg}")
            time.sleep(0.3)  # Rate limit
            time.sleep(0.3)  # Rate limit
        
        return deleted

# ---------------- text utils ----------------
def read_text_or_srt(path: str) -> str:
    if path.lower().endswith(".srt"):
        lines=[]
        with open(path,"r",encoding="utf-8",errors="ignore") as f:
            for ln in f:
                ln=ln.strip()
                if not ln: continue
                if re.match(r"^\d+$", ln): continue
                if re.match(r"^\d{2}:\d{2}:\d{2}", ln): continue
                lines.append(ln)
        return " ".join(lines)
    else:
        return open(path,"r",encoding="utf-8",errors="ignore").read()

def parse_srt_timings(path: str) -> List[tuple]:
    """
    Parse file SRT và trả về danh sách [(start_sec, end_sec, text), ...]
    
    Ví dụ SRT:
    1
    00:00:00,000 --> 00:00:05,500
    Hello world
    
    => [(0.0, 5.5, "Hello world"), ...]
    """
    entries = []
    if not path.lower().endswith(".srt"):
        return entries
    
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        
        # Parse SRT format
        # Pattern: number, newline, timestamp --> timestamp, newline, text lines, blank line
        pattern = r'(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n((?:(?!\n\n|\n\d+\n).)*)'
        matches = re.findall(pattern, content, re.DOTALL)
        
        for match in matches:
            idx, start_ts, end_ts, text = match
            start_sec = _ts_to_seconds(start_ts)
            end_sec = _ts_to_seconds(end_ts)
            text = text.strip()
            if text:
                entries.append((start_sec, end_sec, text))
    except Exception as e:
        print(f"[SRT Parse] Error: {e}")
    
    return entries

def _ts_to_seconds(ts: str) -> float:
    """Convert SRT timestamp (00:01:23,456 or 00:01:23.456) to seconds"""
    ts = ts.replace(',', '.')
    parts = ts.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def smart_split_text(text: str, max_len: int = 300, tolerance: int = 0) -> List[str]:
    """
    Chia đoạn văn bản thông minh theo dấu chấm câu.
    
    Logic GREEDY:
    - Tích lũy các câu cho đến khi thêm câu tiếp theo sẽ vượt max_len
    - Khi đó mới cắt tại dấu chấm hiện tại
    - KHÔNG cắt theo dấu phẩy
    """
    if not text or not text.strip():
        return []
    
    if len(text) <= max_len:
        return [text]
    
    SENTENCE_ENDS = '.!?。！？'
    
    # Tìm tất cả vị trí dấu chấm
    periods = []
    for i, c in enumerate(text):
        if c in SENTENCE_ENDS:
            periods.append(i)
    
    if not periods:
        # Không có dấu chấm -> fallback cắt tại khoảng trắng
        chunks = []
        start = 0
        while start < len(text):
            if len(text) - start <= max_len:
                chunks.append(text[start:].strip())
                break
            # Tìm khoảng trắng gần max_len nhất
            end = start + max_len
            space_pos = text.rfind(' ', start, end)
            if space_pos > start:
                chunks.append(text[start:space_pos].strip())
                start = space_pos + 1
            else:
                chunks.append(text[start:end].strip())
                start = end
        return chunks
    
    # Logic GREEDY: tích lũy câu cho đến khi thêm câu tiếp theo sẽ vượt max_len
    chunks = []
    current_start = 0
    
    for i, period_pos in enumerate(periods):
        # Kiểm tra nếu thêm câu tiếp theo sẽ vượt max_len
        if i + 1 < len(periods):
            next_period_pos = periods[i + 1]
            next_chunk_len = next_period_pos - current_start + 1
            
            if next_chunk_len > max_len:
                # Thêm câu tiếp theo sẽ vượt -> cắt tại đây
                chunk = text[current_start:period_pos + 1].strip()
                if chunk:
                    chunks.append(chunk)
                current_start = period_pos + 1
                # Skip whitespace
                while current_start < len(text) and text[current_start] in ' \t':
                    current_start += 1
        else:
            # Đây là dấu chấm cuối cùng -> lấy hết phần còn lại
            chunk = text[current_start:].strip()
            if chunk:
                chunks.append(chunk)
    
    return chunks if chunks else [text]

def split_by_chars(text: str, split_chars: str) -> List[str]:
    """
    Chia văn bản theo các dấu được chỉ định.
    Mỗi khi gặp dấu trong split_chars, sẽ tách thành 1 đoạn riêng.
    VD: split_chars = ". ! ?" sẽ chia tại mỗi dấu . hoặc ! hoặc ?
    """
    if not text or not text.strip():
        return []
    
    text = ' '.join(text.split())  # Normalize whitespace
    
    chars = set(c for c in split_chars.replace(' ', '') if c)
    if not chars:
        chars = {'.', '!', '?'}
    
    result = []
    current = ""
    
    for char in text:
        current += char
        if char in chars:
            segment = current.strip()
            if segment:
                result.append(segment)
            current = ""
    
    if current.strip():
        result.append(current.strip())
    
    return result

def split_auto(text: str, splits: str, max_len: int=1000) -> List[str]:
    """Wrapper để tương thích ngược - giờ dùng smart_split_text"""
    return smart_split_text(text, max_len, tolerance=50)

def split_by_paragraphs_then_chunks(text: str, max_chars: int = 1000) -> List[dict]:
    """
    Split text theo quy trình mới:
    1. Split theo dòng xuống dòng (paragraph) trước
    2. Nếu 1 đoạn > max_chars, split đoạn đó thành chunks nhỏ
    
    Returns:
        List[dict] với format: {
            'paragraph_idx': int,  # Số thứ tự đoạn (1, 2, 3...)
            'paragraph_text': str,  # Nội dung đoạn gốc
            'chunks': List[str],    # Danh sách chunks (nếu đoạn > max_chars)
            'total_chunks': int     # Tổng số chunks cần xử lý
        }
    """
    if not text or not text.strip():
        return []
    
    # Bước 1: Split theo dòng xuống dòng (paragraph)
    # Normalize line breaks
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Split theo \n\n hoặc \n (nếu chỉ có 1 dòng)
    paragraphs = []
    if '\n\n' in text:
        # Có nhiều paragraph (ngăn cách bởi \n\n)
        raw_paragraphs = text.split('\n\n')
        paragraphs = [p.strip() for p in raw_paragraphs if p.strip()]
    elif '\n' in text:
        # Chỉ có \n (single line breaks) - mỗi dòng là 1 paragraph
        raw_lines = text.split('\n')
        paragraphs = [line.strip() for line in raw_lines if line.strip()]
    else:
        # Không có line break - toàn bộ là 1 paragraph
        paragraphs = [text.strip()] if text.strip() else []
    
    if not paragraphs:
        return []
    
    # Bước 2: Xử lý từng paragraph
    result = []
    for para_idx, para_text in enumerate(paragraphs, start=1):
        if len(para_text) <= max_chars:
            # Đoạn nhỏ - không cần split
            result.append({
                'paragraph_idx': para_idx,
                'paragraph_text': para_text,
                'chunks': [para_text],
                'total_chunks': 1
            })
        else:
            # Đoạn dài - cần split thành chunks
            # 🔧 FIX: Tối ưu thuật toán split cho text lớn (50k-100k chars)
            chunks = _split_large_text_optimized(para_text, max_chars)
            
            result.append({
                'paragraph_idx': para_idx,
                'paragraph_text': para_text,
                'chunks': chunks,
                'total_chunks': len(chunks)
            })
    
    return result


def _split_large_text_optimized(text: str, max_chars: int) -> List[str]:
    """
    Split text thành chunks - CHỈ cắt tại dấu chấm (. ! ?)
    
    Logic GREEDY:
    - Tích lũy các câu cho đến khi thêm câu tiếp theo sẽ vượt max_chars
    - Khi đó mới cắt tại dấu chấm hiện tại
    - KHÔNG cắt theo dấu phẩy
    
    Ví dụ với max_chars=300:
    - Câu 1 (100 ký tự) + Câu 2 (70 ký tự) + Câu 3 (100 ký tự) = 270 < 300 → tiếp tục
    - Nếu thêm Câu 4 (150 ký tự) → 270 + 150 = 420 > 300 → cắt tại Câu 3
    """
    if not text or not text.strip():
        return []
    
    if len(text) <= max_chars:
        return [text]
    
    import re
    # Tách thành các câu dựa trên dấu chấm câu
    # Pattern: tìm các câu kết thúc bằng . ! ? (và các biến thể tiếng Việt/Trung)
    sentence_pattern = r'[^.!?。！？]*[.!?。！？]+'
    sentences = re.findall(sentence_pattern, text)
    
    # Phần còn lại không có dấu chấm cuối
    remaining = text
    for s in sentences:
        remaining = remaining.replace(s, '', 1)
    remaining = remaining.strip()
    
    if not sentences:
        # Không có dấu chấm -> fallback cắt tại khoảng trắng
        chunks = []
        start = 0
        while start < len(text):
            if len(text) - start <= max_chars:
                chunk = text[start:].strip()
                if chunk:
                    chunks.append(chunk)
                break
            end = start + max_chars
            space_pos = text.rfind(' ', start, end)
            if space_pos > start:
                chunks.append(text[start:space_pos].strip())
                start = space_pos + 1
            else:
                chunks.append(text[start:end].strip())
                start = end
        return chunks if chunks else [text]
    
    # Logic GREEDY: tích lũy câu cho đến khi vượt max_chars
    chunks = []
    current_chunk = ""
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        
        # Nếu thêm câu này vào sẽ vượt max_chars
        test_chunk = (current_chunk + " " + sentence).strip() if current_chunk else sentence
        
        if len(test_chunk) > max_chars:
            # Lưu chunk hiện tại (nếu có)
            if current_chunk:
                chunks.append(current_chunk)
            # Bắt đầu chunk mới với câu này
            current_chunk = sentence
        else:
            # Thêm câu vào chunk hiện tại
            current_chunk = test_chunk
    
    # Thêm chunk cuối cùng
    if current_chunk:
        # Nếu còn phần remaining, thêm vào chunk cuối
        if remaining:
            test_final = (current_chunk + " " + remaining).strip()
            if len(test_final) <= max_chars:
                chunks.append(test_final)
            else:
                chunks.append(current_chunk)
                chunks.append(remaining)
        else:
            chunks.append(current_chunk)
    elif remaining:
        chunks.append(remaining)
    
    return chunks if chunks else [text]


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename để ffmpeg/ffprobe có thể xử lý được.
    Loại bỏ các ký tự đặc biệt có thể gây lỗi.
    """
    if not filename:
        return "unnamed"
    
    import re
    # Loại bỏ các ký tự không hợp lệ cho filename
    # Windows: < > : " / \ | ? *
    # Unix: / và null
    invalid_chars = r'[<>:"/\\|?*\x00]'
    filename = re.sub(invalid_chars, '_', filename)
    
    # Loại bỏ các ký tự control
    filename = re.sub(r'[\x00-\x1f\x7f]', '_', filename)
    
    # Loại bỏ khoảng trắng đầu/cuối và nhiều khoảng trắng liên tiếp
    filename = re.sub(r'\s+', '_', filename.strip())
    
    # Giới hạn độ dài (255 ký tự cho hầu hết filesystem)
    if len(filename) > 200:
        filename = filename[:200]
    
    # Đảm bảo không rỗng
    if not filename:
        filename = "unnamed"
    
    return filename



def ssml_with_speed(txt: str, speed: float) -> str:
    if abs(speed-1.0) < 1e-3: return txt
    rate=f"{max(0.5, min(2.0, speed))}x"
    return f'<speak><prosody rate="{rate}">{txt}</prosody></speak>'

def insert_ssml_breaks(txt: str, char1: str, char1_sec: float, char2: str, char2_sec: float) -> str:
    """Chèn SSML break tags sau các ký tự được cấu hình.
    
    Sử dụng regex để tránh lỗi khi có nhiều ký tự liên tiếp.
    Ví dụ: "Hello, world." -> "Hello,<break time=\"300ms\"/> world.<break time=\"500ms\"/>"
    """
    if not txt:
        return txt
    
    result = txt
    
    if char1 and char1_sec > 0:
        ms1 = int(min(3000, char1_sec * 1000))
        break_tag1 = f'<break time="{ms1}ms"/>'
        import re
        escaped_char1 = re.escape(char1)
        pattern1 = f'({escaped_char1})(?!<break)'
        result = re.sub(pattern1, f'\\1{break_tag1}', result)
    
    if char2 and char2_sec > 0:
        ms2 = int(min(3000, char2_sec * 1000))
        break_tag2 = f'<break time="{ms2}ms"/>'
        import re
        escaped_char2 = re.escape(char2)
        pattern2 = f'({escaped_char2})(?!<break)'
        result = re.sub(pattern2, f'\\1{break_tag2}', result)
    
    return result

# ---------------- FFmpeg helpers ----------------
def find_ffmpeg() -> Optional[str]:
    """
    Tìm đường dẫn ffmpeg - hỗ trợ Nuitka onefile và cross-platform.
    Với Nuitka onefile, data files được giải nén vào thư mục tạm.
    """
    import sys
    import tempfile
    import glob
    import platform
    
    # Detect OS để chọn đúng extension
    is_windows = platform.system() == "Windows"
    exe_ext = ".exe" if is_windows else ""
    
    candidates = []
    is_nuitka = False
    
    try:
        # Nuitka set __compiled__ trong builtins
        import builtins
        if hasattr(builtins, '__compiled__'):
            is_nuitka = True
    except:
        pass
    
    try:
        if hasattr(sys, '__nuitka_binary_dir') or hasattr(sys, 'nuitka'):
            is_nuitka = True
    except:
        pass
    
    if getattr(sys, 'frozen', False) and not hasattr(sys, '_MEIPASS'):
        is_nuitka = True
    
    # === NUITKA COMPILED ===
    if is_nuitka:
        try:
            nuitka_data_dir = Path(os.path.dirname(__file__))
            candidates.append(nuitka_data_dir / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
            candidates.append(nuitka_data_dir / f"ffmpeg{exe_ext}")
        except:
            pass
        
        try:
            exe_dir = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
            candidates.append(exe_dir / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
            candidates.append(exe_dir / f"ffmpeg{exe_ext}")
        except:
            pass
        
        try:
            exec_dir = Path(os.path.dirname(sys.executable))
            candidates.append(exec_dir / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
            candidates.append(exec_dir / f"ffmpeg{exe_ext}")
        except:
            pass
        
        try:
            temp_base = Path(tempfile.gettempdir())
            for pattern in ["onefile_*", "nuitka_*"]:
                for temp_dir in temp_base.glob(pattern):
                    if temp_dir.is_dir():
                        candidates.append(temp_dir / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
                        candidates.append(temp_dir / f"ffmpeg{exe_ext}")
        except:
            pass
        
        try:
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            if local_app_data:
                local_path = Path(local_app_data)
                for subdir in ["Temp", "Programs"]:
                    base = local_path / subdir
                    if base.exists():
                        for pattern in ["onefile_*", "nuitka_*", "*Dgtautoelvenlabs*"]:
                            for d in base.glob(pattern):
                                if d.is_dir():
                                    candidates.append(d / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
                                    candidates.append(d / f"ffmpeg{exe_ext}")
        except:
            pass
        
        try:
            if sys.executable:
                exe_path = Path(sys.executable)
                data_folder = exe_path.parent / (exe_path.stem + ".dist")
                if data_folder.exists():
                    candidates.append(data_folder / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
                    candidates.append(data_folder / f"ffmpeg{exe_ext}")
        except:
            pass
    
    # === PYINSTALLER FROZEN ===
    elif getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        candidates.append(Path(sys._MEIPASS) / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
        candidates.append(Path(sys._MEIPASS) / f"ffmpeg{exe_ext}")
        candidates.append(Path(sys.executable).parent / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
        candidates.append(Path(sys.executable).parent / f"ffmpeg{exe_ext}")
    
    else:
        try:
            script_dir = Path(__file__).parent
            candidates.append(script_dir / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
            candidates.append(script_dir / f"ffmpeg{exe_ext}")
        except:
            pass
    
    try:
        cwd = Path.cwd()
        candidates.append(cwd / "ffmpeg_bin" / f"ffmpeg{exe_ext}")
        candidates.append(cwd / f"ffmpeg{exe_ext}")
    except:
        pass
    
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    
    return None

def find_ffprobe() -> Optional[str]:
    """
    Tìm đường dẫn ffprobe - hỗ trợ Nuitka onefile và cross-platform.
    Logic tương tự find_ffmpeg().
    """
    import sys
    import tempfile
    import glob
    import platform
    
    # Detect OS để chọn đúng extension
    is_windows = platform.system() == "Windows"
    exe_ext = ".exe" if is_windows else ""
    
    candidates = []
    is_nuitka = False
    
    try:
        import builtins
        if hasattr(builtins, '__compiled__'):
            is_nuitka = True
    except:
        pass
    
    try:
        if hasattr(sys, '__nuitka_binary_dir') or hasattr(sys, 'nuitka'):
            is_nuitka = True
    except:
        pass
    
    if getattr(sys, 'frozen', False) and not hasattr(sys, '_MEIPASS'):
        is_nuitka = True
    
    # === NUITKA COMPILED ===
    if is_nuitka:
        try:
            nuitka_data_dir = Path(os.path.dirname(__file__))
            candidates.append(nuitka_data_dir / "ffmpeg_bin" / f"ffprobe{exe_ext}")
            candidates.append(nuitka_data_dir / f"ffprobe{exe_ext}")
        except:
            pass
        
        try:
            exe_dir = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
            candidates.append(exe_dir / "ffmpeg_bin" / f"ffprobe{exe_ext}")
            candidates.append(exe_dir / f"ffprobe{exe_ext}")
        except:
            pass
        
        try:
            exec_dir = Path(os.path.dirname(sys.executable))
            candidates.append(exec_dir / "ffmpeg_bin" / f"ffprobe{exe_ext}")
            candidates.append(exec_dir / f"ffprobe{exe_ext}")
        except:
            pass
        
        try:
            temp_base = Path(tempfile.gettempdir())
            for pattern in ["onefile_*", "nuitka_*"]:
                for temp_dir in temp_base.glob(pattern):
                    if temp_dir.is_dir():
                        candidates.append(temp_dir / "ffmpeg_bin" / f"ffprobe{exe_ext}")
                        candidates.append(temp_dir / f"ffprobe{exe_ext}")
        except:
            pass
        
        try:
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            if local_app_data:
                local_path = Path(local_app_data)
                for subdir in ["Temp", "Programs"]:
                    base = local_path / subdir
                    if base.exists():
                        for pattern in ["onefile_*", "nuitka_*", "*Dgtautoelvenlabs*"]:
                            for d in base.glob(pattern):
                                if d.is_dir():
                                    candidates.append(d / "ffmpeg_bin" / f"ffprobe{exe_ext}")
                                    candidates.append(d / f"ffprobe{exe_ext}")
        except:
            pass
        
        try:
            if sys.executable:
                exe_path = Path(sys.executable)
                data_folder = exe_path.parent / (exe_path.stem + ".dist")
                if data_folder.exists():
                    candidates.append(data_folder / "ffmpeg_bin" / f"ffprobe{exe_ext}")
                    candidates.append(data_folder / f"ffprobe{exe_ext}")
        except:
            pass
    
    # === PYINSTALLER FROZEN ===
    elif getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        candidates.append(Path(sys._MEIPASS) / "ffmpeg_bin" / f"ffprobe{exe_ext}")
        candidates.append(Path(sys._MEIPASS) / f"ffprobe{exe_ext}")
        candidates.append(Path(sys.executable).parent / "ffmpeg_bin" / f"ffprobe{exe_ext}")
        candidates.append(Path(sys.executable).parent / f"ffprobe{exe_ext}")
    
    else:
        try:
            script_dir = Path(__file__).parent
            candidates.append(script_dir / "ffmpeg_bin" / f"ffprobe{exe_ext}")
            candidates.append(script_dir / f"ffprobe{exe_ext}")
        except:
            pass
    
    try:
        cwd = Path.cwd()
        candidates.append(cwd / "ffmpeg_bin" / f"ffprobe{exe_ext}")
        candidates.append(cwd / f"ffprobe{exe_ext}")
    except:
        pass
    
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    
    ff = shutil.which("ffprobe")
    if ff:
        return ff
    
    return None


def get_mp3_duration_str(filepath: str) -> str:
    """
    Lấy duration của file MP3 dưới dạng string (e.g. "12.34s")
    Thử ffprobe trước, fallback sang mutagen, cuối cùng estimate từ file size.
    """
    if not filepath or not os.path.exists(filepath):
        return ""
    
    timing = ""
    duration = 0.0
    
    # Thử ffprobe trước
    ffprobe_path = find_ffprobe()
    if ffprobe_path:
        try:
            # 🔧 FIX: Thêm startupinfo để ẩn console window trên Windows
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=5, startupinfo=si
            )
            if result.returncode == 0 and result.stdout.strip():
                duration = float(result.stdout.strip())
                timing = f"{duration:.2f}s"
                return timing
        except Exception:
            pass
    
    # Fallback sang mutagen
    try:
        from mutagen.mp3 import MP3
        audio = MP3(filepath)
        duration = audio.info.length
        timing = f"{duration:.2f}s"
        return timing
    except Exception:
        pass
    
    # Fallback cuối cùng: estimate từ file size
    try:
        file_size = os.path.getsize(filepath)
        duration = file_size / (128000 / 8)  # bytes / bytes-per-second @ 128kbps
        timing = f"~{duration:.1f}s"
        return timing
    except Exception:
        pass
    
    return ""


def run_hidden(cmd: list) -> subprocess.CompletedProcess:
    """Chạy command ẩn cửa sổ console (Windows)"""
    si = None
    if os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    return subprocess.run(cmd, capture_output=True, text=True, 
                          encoding='utf-8', errors='ignore', startupinfo=si)

def create_silence_mp3(ffmpeg: str, output_path: Path, seconds: float) -> bool:
    """Tạo file mp3 im lặng với độ dài cho trước"""
    if output_path.exists() and output_path.stat().st_size > 1000:
        return True
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(seconds),
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-b:a", "128k",
        str(output_path)
    ]
    r = run_hidden(cmd)
    return r.returncode == 0 and output_path.exists()

def join_mp3_with_silence(ffmpeg: str, mp3_files: List[Path], output_path: Path, 
                          gap_enabled: bool, gap_seconds: float, gap_every: int,
                          srt_gaps: List[float] = None) -> tuple:
    """
    Nối các file mp3 với chèn file im lặng mỗi N đoạn.
    Hỗ trợ batch processing cho số lượng file lớn (500+).
    
    Args:
        ffmpeg: Đường dẫn ffmpeg
        mp3_files: Danh sách file mp3 cần nối (1.mp3, 2.mp3, ...)
        output_path: File mp3 đầu ra
        gap_enabled: Có bật chèn silence không
        gap_seconds: Số giây im lặng
        gap_every: Chèn sau mỗi N đoạn
        srt_gaps: Danh sách khoảng cách (giây) giữa các subtitle từ SRT file
    
    Returns:
        (success: bool, message: str)
    """
    if not mp3_files:
        return False, "Không có file để nối"
    
    files_to_concat = list(mp3_files)
    
    if srt_gaps and len(srt_gaps) == len(mp3_files) - 1:
        new_files = []
        for i, f in enumerate(mp3_files):
            new_files.append(f)
            if i < len(srt_gaps):
                gap = srt_gaps[i]
                if gap > 0.05:
                    silence_path = mp3_files[0].parent / f"_silence_{gap:.1f}s.mp3"
                    if create_silence_mp3(ffmpeg, silence_path, gap):
                        new_files.append(silence_path)
        files_to_concat = new_files
    
    elif gap_enabled and gap_seconds > 0 and gap_every > 0 and len(mp3_files) > gap_every:
        silence_path = mp3_files[0].parent / f"_silence_{gap_seconds:.1f}s.mp3"
        if not create_silence_mp3(ffmpeg, silence_path, gap_seconds):
            return False, "Không thể tạo file im lặng"
        
        new_files = []
        for i, f in enumerate(mp3_files, start=1):
            new_files.append(f)
            if i % gap_every == 0 and i < len(mp3_files):
                new_files.append(silence_path)
        files_to_concat = new_files
    
    MAX_FILES_PER_BATCH = 100
    
    if len(files_to_concat) <= MAX_FILES_PER_BATCH:
        return _concat_files_direct(ffmpeg, files_to_concat, output_path)
    else:
        import shutil
        temp_dir = mp3_files[0].parent / "_temp_merge"
        temp_dir.mkdir(exist_ok=True)
        
        try:
            batch_files = []
            batch_idx = 0
            
            for i in range(0, len(files_to_concat), MAX_FILES_PER_BATCH):
                batch = files_to_concat[i:i + MAX_FILES_PER_BATCH]
                batch_output = temp_dir / f"_batch_{batch_idx:04d}.mp3"
                
                ok, msg = _concat_files_direct(ffmpeg, batch, batch_output)
                if not ok:
                    return False, f"Lỗi merge batch {batch_idx}: {msg}"
                
                batch_files.append(batch_output)
                batch_idx += 1
            
            if len(batch_files) == 1:
                shutil.copy2(batch_files[0], output_path)
            else:
                ok, msg = _concat_files_direct(ffmpeg, batch_files, output_path)
                if not ok:
                    return False, f"Lỗi merge final: {msg}"
            
            return True, "OK"
            
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass

def _concat_files_direct(ffmpeg: str, files: List[Path], output_path: Path) -> tuple:
    """Helper: Nối trực tiếp danh sách file bằng FFmpeg concat"""
    if not files:
        return False, "Không có file"
    
    concat_list = files[0].parent / f"_concat_{output_path.stem}.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for file in files:
            f.write(f"file '{file.as_posix()}'\n")
    
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output_path)]
    r = run_hidden(cmd)
    
    try:
        concat_list.unlink()
    except:
        pass
    
    if r.returncode == 0 and output_path.exists():
        return True, "OK"
    return False, f"FFmpeg error: {r.stderr or r.stdout}"

def join_mp3_simple(mp3_files: List[str], output_path: str, crossfade_ms: int = 50) -> bool:
    """
    Nối các file mp3 với crossfade mượt mà để tránh audio méo/pop/click.
    Sử dụng pydub nếu có, fallback về FFmpeg.
    
    Args:
        mp3_files: Danh sách đường dẫn file mp3 cần nối
        output_path: Đường dẫn file mp3 đầu ra
        crossfade_ms: Thời gian crossfade (ms), mặc định 50ms
    """
    if len(mp3_files) == 0:
        raise RuntimeError("Không có file để nối")
    
    if len(mp3_files) == 1:
        import shutil
        shutil.copy2(mp3_files[0], output_path)
        return True
    
    # Thử dùng pydub cho crossfade mượt mà
    try:
        from pydub import AudioSegment
        
        # Load file đầu tiên
        combined = AudioSegment.from_mp3(mp3_files[0])
        
        # Nối các file tiếp theo với crossfade
        for mp3_path in mp3_files[1:]:
            next_segment = AudioSegment.from_mp3(mp3_path)
            
            # Áp dụng crossfade để chuyển tiếp mượt mà
            # crossfade_ms nhỏ (50ms) để không làm mất nội dung
            if crossfade_ms > 0 and len(combined) > crossfade_ms and len(next_segment) > crossfade_ms:
                combined = combined.append(next_segment, crossfade=crossfade_ms)
            else:
                # Nếu segment quá ngắn, nối thẳng với fade ngắn
                # Thêm fade out nhẹ ở cuối segment trước
                if len(combined) > 10:
                    combined = combined.fade_out(10)
                # Thêm fade in nhẹ ở đầu segment sau
                if len(next_segment) > 10:
                    next_segment = next_segment.fade_in(10)
                combined = combined + next_segment
        
        # Export với chất lượng cao
        combined.export(output_path, format="mp3", bitrate="192k")
        return True
        
    except ImportError:
        # Fallback về FFmpeg nếu không có pydub
        pass
    except Exception as e:
        # Nếu pydub lỗi, thử fallback về FFmpeg
        print(f"[join_mp3_simple] pydub error: {e}, falling back to FFmpeg")
    
    # Fallback: Dùng FFmpeg với audio filter để crossfade
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg không tìm thấy. Vui lòng cài đặt FFmpeg.")
    
    out = Path(output_path)
    
    # Với nhiều file, dùng phương pháp concat với re-encode
    if len(mp3_files) <= 2:
        # Với 2 file, có thể dùng acrossfade filter
        cmd = [
            ffmpeg, "-y",
            "-i", mp3_files[0],
            "-i", mp3_files[1],
            "-filter_complex", f"[0:a][1:a]acrossfade=d=0.05:c1=tri:c2=tri[out]",
            "-map", "[out]",
            "-c:a", "libmp3lame", "-ar", "44100", "-b:a", "192k",
            str(out)
        ]
        r = run_hidden(cmd)
        if r.returncode == 0:
            return True
    
    # Fallback cuối: concat đơn giản với re-encode
    concat_list = out.with_suffix('.concat.txt')
    
    with open(concat_list, 'w', encoding='utf-8') as f:
        for mp3 in mp3_files:
            f.write(f"file '{mp3}'\n")
    
    # Re-encode với -af afade để làm mượt các điểm nối
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
           "-af", "aresample=async=1:first_pts=0",  # Sync audio để tránh glitch
           "-c:a", "libmp3lame", "-ar", "44100", "-b:a", "192k", str(out)]
    r = run_hidden(cmd)
    
    try:
        concat_list.unlink()
    except:
        pass
    
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {r.stderr or r.stdout}")
    return True

class Sig(QObject):
    status = Signal(int, str)   # row (batch), status
    progress = Signal(int, int, int)
    subtitle_status = Signal(int, str)  # row (subtitle), status (Check Key/Uploading/Processing/Synthesizing/Done)
    subtitle_output = Signal(int, str, str)  # row (subtitle), output_name, timing
    chunk_status = Signal(int, str)  # chunk_row, status - 🔧 NEW: Cập nhật status cho từng chunk row
    chunk_done = Signal(int, int, int, str)  # paragraph_idx, chunk_idx, row, output_path - 🔧 NEW: Khi 1 chunk hoàn thành
    done   = Signal(int, str)

# ---------------- worker check credits ----------------
class CreditsSig(QObject):
    progress = Signal(int, int, int)  # current_key, total_keys, chars_so_far
    done = Signal(int, int, int)  # ok_count, total_count, total_chars
    error = Signal(str)

class CreditsWorker(QRunnable):
    """Worker để check credits song song nhiều key cùng lúc"""
    def __init__(self, keys: list, client: ElevenClient, sig: CreditsSig, max_workers: int = 20, keys_file_path: str = None):
        super().__init__()
        self.keys = keys
        self.client = client
        self.sig = sig
        self.max_workers = max_workers
        self.keys_file_path = keys_file_path
        self._stop = False
        self.setAutoDelete(False)
    
    def stop(self):
        self._stop = True
    
    def _check_single_key(self, key: str) -> tuple:
        """Check 1 key, trả về (success, chars_left)"""
        try:
            sub = self.client.subscription_for_key_silent(key)
            if sub:
                limit = sub.get("character_limit", 0) or 0
                used = sub.get("character_count", 0) or 0
                left = max(0, int(limit) - int(used))
                return (True, left)
            return (False, 0)
        except:
            return (False, 0)
    
    def run(self):
        import threading
        from queue import Queue, Empty
        
        unique_keys = list(dict.fromkeys(self.keys))
        
        total = len(unique_keys)
        if total == 0:
            try:
                self.sig.done.emit(0, 0, 0)
            except:
                pass
            return
        
        ok = 0
        remain = 0
        checked = 0
        key_credits = {key: 0 for key in unique_keys}
        lock = threading.Lock()
        
        def check_key_thread(key):
            nonlocal ok, remain, checked
            if self._stop:
                return
            success, chars = self._check_single_key(key)
            with lock:
                checked += 1
                if success:
                    key_credits[key] = chars
                    ok += 1
                    remain += chars
                    # 🔧 Update cache để acquire_key() dùng luôn
                    if hasattr(self.client, 'keys') and hasattr(self.client.keys, 'credits_cache'):
                        self.client.keys.credits_cache[key] = chars
        
        start_batch_size = 5
        max_batch_size = min(10, self.max_workers, total)
        current_batch_size = start_batch_size
        batch_count = 0
        
        i = 0
        while i < total:
            if self._stop:
                break
            
            if batch_count > 0 and batch_count % 2 == 0 and current_batch_size < max_batch_size:
                current_batch_size = min(current_batch_size + 1, max_batch_size)
            
            batch_keys = unique_keys[i:i + current_batch_size]
            batch_threads = []
            
            for key in batch_keys:
                if self._stop:
                    break
                t = threading.Thread(target=check_key_thread, args=(key,), daemon=True)
                t.start()
                batch_threads.append(t)
            
            for t in batch_threads:
                t.join(timeout=3)
            
            if not self._stop:
                try:
                    self.sig.progress.emit(checked, total, remain)
                except:
                    pass
            
            i += current_batch_size
            batch_count += 1
        
        # if not self._stop and self.keys_file_path and key_credits:
        #     try:
        #         sorted_keys = sorted(key_credits.keys(), key=lambda k: key_credits[k], reverse=True)
        #         with open(self.keys_file_path, 'w', encoding='utf-8') as f:
        #             for key in sorted_keys:
        #                 f.write(key + '\n')
        #     except Exception as e:
        #         pass
        
        # Emit done
        if not self._stop:
            try:
                self.sig.done.emit(ok, total, remain)
            except:
                pass


# 🔧 NEW: ChunkWorker - Xử lý 1 chunk duy nhất (để tận dụng multi-thread)
class ChunkWorker(QRunnable):
    """Worker xử lý 1 chunk duy nhất - cho phép chạy nhiều chunks song song"""
    def __init__(self, row: int, paragraph_idx: int, chunk_idx: int, content: str, 
                 file_base: str, outdir: str, tts_mini_dir: str,
                 voice_id: str, model_id: str, s: AppSettings, client: ElevenClient, 
                 sig: Sig, log, stop_flag_ref=None, total_chunks: int = 1):
        super().__init__()
        self.row = row
        self.paragraph_idx = paragraph_idx
        self.chunk_idx = chunk_idx
        self.total_chunks = total_chunks  # 🔧 NEW: Tổng số chunks của đoạn này
        self.content = content  # Nội dung chunk
        self.file_base = file_base
        self.outdir = outdir
        self.tts_mini_dir = tts_mini_dir
        self.voice_id = voice_id
        self.model_id = model_id
        self.s = s
        self.client = client
        self.sig = sig
        self.log = log
        self._stop = False
        self._stop_flag_ref = stop_flag_ref
        self._acquired_key = None
    
    def stop(self): 
        self._stop = True
    
    def _should_stop(self):
        if self._stop:
            return True
        if self._stop_flag_ref and callable(self._stop_flag_ref):
            return self._stop_flag_ref()
        return False
    
    def _validate_current_key(self, tried_keys: set = None):
        """Kiểm tra và acquire key có đủ credits, loại trừ các key đã thử fail"""
        required_chars = int(len(self.content) * 1.2)
        unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
        
        for retry in range(3):
            if self._should_stop():
                return False
            
            # 🔧 FIX: Truyền tried_keys để loại trừ các key đã fail 401/voice_limit
            key = self.client.keys.acquire_key(required_chars=required_chars, line_id=unique_line_id, excluded_keys=tried_keys)
            if key:
                self._acquired_key = key
                self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Acquired key: {key[:15]}...")
                return True
            
            # 🔧 FIX: Không gọi reset_all_states (không tồn tại), chỉ đợi và thử lại
            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Không có key available, retry {retry+1}/3")
            time.sleep(2)
        
        return False
    
    def run(self):
        try:
            self.sig.chunk_status.emit(self.row, "Check Key...")
            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Bắt đầu xử lý {len(self.content):,} ký tự")
            
            # 🔧 FIX: Khởi tạo tried_keys TRƯỚC khi gọi _validate_current_key
            tried_keys = set()
            
            if not self._validate_current_key(tried_keys):
                self.sig.chunk_status.emit(self.row, "Fail")
                self.sig.done.emit(self.row, "Fail")
                return
            
            voice_settings = {
                "stability": max(0.0, min(1.0, self.s.stability/100.0)),
                "similarity_boost": max(0.0, min(1.0, self.s.similarity/100.0)),
                "style": max(0.0, min(1.0, self.s.style/100.0)),
                "use_speaker_boost": bool(self.s.speaker_boost),
                "speed": max(0.5, min(2.0, self.s.speed)),  # API supports 0.5-2.0
            }
            
            # 🔧 FIX: Tạo folder _chunks trong tts_mini_dir (không phải outdir)
            # Để _check_existing_mp3_files có thể tìm thấy cached chunks
            chunks_dir = os.path.join(self.tts_mini_dir, "_chunks")
            ensure_dir(chunks_dir)
            
            # Xử lý content
            processed_content = self.content
            if self.s.pause_char_enabled:
                processed_content = insert_ssml_breaks(
                    self.content, 
                    self.s.char1, self.s.char1_sec,
                    self.s.char2, self.s.char2_sec
                )
            
            # Use processed content directly - speed is now in voice_settings
            payload = processed_content
            
            # Output path - format: 2.mp3 (1 chunk) hoặc 1.1.mp3, 1.2.mp3 (nhiều chunks)
            if self.total_chunks == 1:
                # Đoạn chỉ có 1 chunk → đặt tên đơn giản: 2.mp3
                chunk_filename = sanitize_filename(f"{self.paragraph_idx}.mp3")
            else:
                # Đoạn có nhiều chunks → đặt tên: 1.1.mp3, 1.2.mp3...
                chunk_filename = sanitize_filename(f"{self.paragraph_idx}.{self.chunk_idx}.mp3")
            part_outpath = os.path.join(chunks_dir, chunk_filename)
            
            self.sig.chunk_status.emit(self.row, "Loading...")
            
            max_retries = 999  # 🔧 FIX: Retry cho đến khi hết key
            connection_errors = 0  # 🔧 NEW: Đếm lỗi connection
            max_connection_errors = 3  # 🔧 FIX: Sau 3 lỗi connection thì đổi key (nhanh hơn)
            # tried_keys đã được khởi tạo ở trên
            
            for attempt in range(max_retries):
                if self._should_stop():
                    self.sig.chunk_status.emit(self.row, "Stopped")
                    self.sig.done.emit(self.row, "Stopped")
                    return
                
                try:
                    request_delay = getattr(self.s, 'request_delay', 0.0)
                    if request_delay > 0:
                        time.sleep(request_delay)
                    
                    self.sig.chunk_status.emit(self.row, "Processing")
                    # 🔧 FIX: Truyền _acquired_key vào tts_direct() để dùng đúng key đã acquire từ DB
                    self.client.tts_direct(self.voice_id, payload, self.model_id, voice_settings, part_outpath, api_key=self._acquired_key)
                    
                    # 🔧 FIX: Verify file was created successfully
                    if not os.path.exists(part_outpath) or os.path.getsize(part_outpath) == 0:
                        self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ⚠️ File không hợp lệ sau TTS, retry...")
                        if os.path.exists(part_outpath):
                            try:
                                os.remove(part_outpath)
                            except:
                                pass
                        continue  # Retry
                    
                    # Đánh dấu key thành công
                    if self._acquired_key:
                        self.client.keys.mark_success(self._acquired_key)
                    
                    connection_errors = 0  # Reset connection error count
                    break
                    
                except Exception as api_err:
                    err_msg = str(api_err).lower()
                    
                    # 🔧 FIX: Handle connection/proxy errors - XOAY PROXY NGAY LẬP TỨC
                    if "connection" in err_msg or "reset" in err_msg or "max retries" in err_msg or "proxy" in err_msg or "remotedisconnected" in err_msg:
                        connection_errors += 1
                        self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Connection/Proxy error ({connection_errors}/{max_connection_errors}) - XOAY PROXY!")
                        
                        # 🔧 FIX: Xoay proxy ngay lập tức
                        # ElevenClient.proxies là ProxyManager, ProxyManager._proxy_service_db là ProxyServiceDB
                        proxy_rotated = False
                        if hasattr(self.client, 'proxies') and self.client.proxies:
                            if hasattr(self.client.proxies, '_proxy_service_db') and self.client.proxies._proxy_service_db:
                                self.client.proxies._proxy_service_db.report_failure(is_rate_limited=False)
                                proxy_rotated = True
                            elif hasattr(self.client.proxies, 'report_failure'):
                                self.client.proxies.report_failure()
                                proxy_rotated = True
                        
                        if proxy_rotated:
                            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] 🔄 Đã xoay proxy!")
                        
                        if connection_errors >= max_connection_errors:
                            # 🔧 FIX: Đổi key mới, retry cho đến khi hết tất cả key
                            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] 🔄 Connection errors={connection_errors}, đổi key!")
                            
                            # Track key đã thử
                            if self._acquired_key:
                                tried_keys.add(self._acquired_key)
                                unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                                self.client.keys.release_key(self._acquired_key, 0, False, "connection_error", unique_line_id)
                                self._acquired_key = None
                            
                            # Đổi key mới
                            self.client.keys.rotate()
                            if not self._validate_current_key(tried_keys):
                                # Kiểm tra đã thử hết key chưa
                                total_keys = len(self.client.keys.keys) if hasattr(self.client.keys, 'keys') else 0
                                self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ Không còn key available (đã thử {len(tried_keys)}/{total_keys} keys)")
                                self.sig.chunk_status.emit(self.row, "No Key")
                                self.sig.done.emit(self.row, "Fail")
                                return
                            connection_errors = 0  # Reset counter sau khi đổi key
                        # Không cần sleep lâu vì đã xoay proxy
                        time.sleep(0.5)
                        continue
                    
                    if "401" in err_msg:
                        self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] 401 - đổi key")
                        if self._acquired_key:
                            tried_keys.add(self._acquired_key)  # 🔧 Track key đã thử
                            self.client.keys.mark_401(self._acquired_key)
                            unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                            self.client.keys.release_key(self._acquired_key, 0, False, "401", unique_line_id)
                            self._acquired_key = None
                        self.client.keys.rotate()
                        if not self._validate_current_key(tried_keys):
                            total_keys = len(self.client.keys.keys) if hasattr(self.client.keys, 'keys') else 0
                            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ Không còn key available (đã thử {len(tried_keys)}/{total_keys} keys)")
                            self.sig.chunk_status.emit(self.row, "No Key")
                            self.sig.done.emit(self.row, "Fail")
                            return
                        continue
                    
                    # 🔧 NEW: Handle voice_limit và voice_add_edit_limit errors - cần đổi API KEY
                    if "voice_limit" in err_msg or "voice_add_edit_limit" in err_msg:
                        self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Voice limit - đổi key ngay!")
                        if self._acquired_key:
                            tried_keys.add(self._acquired_key)  # 🔧 Track key đã thử
                            # Mark key as having voice limit issue (tương tự 401)
                            self.client.keys.mark_401(self._acquired_key)  # Dùng mark_401 để loại key này
                            unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                            self.client.keys.release_key(self._acquired_key, 0, False, "voice_limit", unique_line_id)
                            self._acquired_key = None
                        self.client.keys.rotate()
                        if not self._validate_current_key(tried_keys):
                            total_keys = len(self.client.keys.keys) if hasattr(self.client.keys, 'keys') else 0
                            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ Không còn key available (đã thử {len(tried_keys)}/{total_keys} keys)")
                            self.sig.chunk_status.emit(self.row, "No Key")
                            self.sig.done.emit(self.row, "Fail")
                            return
                        continue
                    
                    if "429" in err_msg or "403" in err_msg:
                        self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Rate limit - đổi key!")
                        # 🔧 FIX: Đổi key ngay khi rate limit, không đợi
                        if self._acquired_key:
                            tried_keys.add(self._acquired_key)
                            unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                            self.client.keys.release_key(self._acquired_key, 0, False, "rate_limit", unique_line_id)
                            self._acquired_key = None
                        self.client.keys.rotate()
                        if not self._validate_current_key(tried_keys):
                            total_keys = len(self.client.keys.keys) if hasattr(self.client.keys, 'keys') else 0
                            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ Không còn key available (đã thử {len(tried_keys)}/{total_keys} keys)")
                            self.sig.chunk_status.emit(self.row, "No Key")
                            self.sig.done.emit(self.row, "Fail")
                            return
                        time.sleep(0.5)
                        continue
                    
                    # 🔧 FIX: Lỗi khác - đổi key và retry
                    self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] Error: {err_msg[:100]} - đổi key!")
                    if self._acquired_key:
                        tried_keys.add(self._acquired_key)
                        unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                        self.client.keys.release_key(self._acquired_key, 0, False, "unknown_error", unique_line_id)
                        self._acquired_key = None
                    self.client.keys.rotate()
                    if not self._validate_current_key(tried_keys):
                        total_keys = len(self.client.keys.keys) if hasattr(self.client.keys, 'keys') else 0
                        self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ Không còn key available (đã thử {len(tried_keys)}/{total_keys} keys)")
                        self.sig.chunk_status.emit(self.row, "No Key")
                        self.sig.done.emit(self.row, "Fail")
                        return
                    time.sleep(0.5)
                    continue
            
            # 🔧 FIX: Verify file exists BEFORE releasing key as success
            if not os.path.exists(part_outpath):
                self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ File không tồn tại sau TTS: {part_outpath}")
                # Release key as FAIL (not success)
                if self._acquired_key:
                    unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                    self.client.keys.release_key(self._acquired_key, 0, False, "file_not_created", unique_line_id)
                    self._acquired_key = None
                self.sig.chunk_status.emit(self.row, "Fail")
                self.sig.done.emit(self.row, "Fail")
                return
            
            # Release key as SUCCESS (file exists)
            if self._acquired_key:
                unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                self.client.keys.release_key(self._acquired_key, len(self.content), True, None, unique_line_id)
                self._acquired_key = None
            
            # Emit chunk done
            self.sig.chunk_status.emit(self.row, "✅ Done")
            self.sig.chunk_done.emit(self.paragraph_idx, self.chunk_idx, self.row, part_outpath)
            self.sig.done.emit(self.row, "DONE")
            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ✅ Hoàn thành → {chunk_filename} (exists={os.path.exists(part_outpath)})")
            
        except Exception as e:
            self.log(f"[Chunk {self.paragraph_idx}.{self.chunk_idx}] ❌ Exception: {e}")
            self.sig.chunk_status.emit(self.row, "Fail")
            self.sig.done.emit(self.row, "Fail")
        finally:
            if self._acquired_key:
                try:
                    unique_line_id = f"{self.file_base}_{self.paragraph_idx}_{self.chunk_idx}"
                    self.client.keys.release_key(self._acquired_key, 0, False, None, unique_line_id)
                except:
                    pass


class LineWorker(QRunnable):
    """Worker xử lý từng paragraph - xử lý chunks và ghép thành doan_X.mp3"""
    def __init__(self, row: int, line_id: int, content: str, file_base: str, outdir: str,
                 voice_id: str, model_id: str, s: AppSettings, client: ElevenClient, sig: Sig, log,
                 stop_flag_ref=None, paragraph_idx: int = None, chunks: list = None, tts_mini_dir: str = None,
                 chunk_rows: list = None):
        super().__init__()
        self.row = row
        self.line_id = line_id
        self.content = content  # Nội dung paragraph gốc
        self.file_base = file_base
        self.outdir = outdir
        self.voice_id = voice_id
        self.model_id = model_id
        self.s = s
        self.client = client
        self.sig = sig
        self.log = log
        self._stop = False
        self._stop_flag_ref = stop_flag_ref
        # Thông tin mới cho quy trình split mới
        self.paragraph_idx = paragraph_idx or line_id
        self.chunks = chunks or [content]  # Danh sách chunks (nếu paragraph > max_chars)
        self.tts_mini_dir = tts_mini_dir or outdir  # Folder tts_mini/
        # 🔧 NEW: Danh sách row trong bảng cho từng chunk
        self.chunk_rows = chunk_rows or [row]
    
    def stop(self): 
        self._stop = True
    
    def _should_stop(self):
        """Check cả local stop và global stop"""
        if self._stop:
            return True
        if self._stop_flag_ref and callable(self._stop_flag_ref):
            return self._stop_flag_ref()
        return False
    
    def _validate_current_key(self, max_attempts=30, max_retries=3, excluded_keys: set = None):
        """
        Kiểm tra và acquire key có đủ credits.
        Sử dụng KeyPoolManager.acquire_key() với Sequential Queue + Credit Lock.
        
        🔧 FIX: Thay vì đợi 60 cycles (5 phút), sẽ:
        1. Thử acquire key
        2. Nếu fail → auto-reset state (giống restart app)
        3. Retry ngay lập tức
        
        🔧 NEW: Truyền line_id để lock key riêng cho line này
        """
        min_credits_needed = getattr(self, '_temp_min_credits', None)
        if min_credits_needed is None:
            min_credits_needed = int(len(self.content) * 1.2)
        
        # 🔧 NEW: Tạo unique line_id để lock key
        unique_line_id = f"{self.file_base}_{self.line_id}"
        
        for retry in range(max_retries):
            if self._should_stop():
                return False
            
            # 🔧 FIX: Truyền excluded_keys để loại trừ các key đã fail
            key = self.client.keys.acquire_key(min_credits_needed, max_attempts=max_attempts, timeout=15.0, line_id=unique_line_id, excluded_keys=excluded_keys)
            
            if key:
                self._acquired_key = key
                self._chars_used = 0
                self.log(f"[Line {self.line_id}] ✅ Acquired key {key[:10]}... (cần {min_credits_needed:,} chars)")
                return True
            
            # Không tìm được key - auto-reset state và retry
            if retry < max_retries - 1:
                self.log(f"[Line {self.line_id}] ⚠️ Không có key available - AUTO RESET STATE và retry ({retry+1}/{max_retries})")
                self.client.keys.reset_runtime_state()
                time.sleep(1)  # Đợi 1 giây để các thread khác cập nhật
            
        self._acquired_key = None
        self.log(f"[Line {self.line_id}] ⛔ Không tìm được key sau {max_retries} lần reset state")
        return False
    
    def run(self):
        try:
            # Sử dụng chunks đã được split sẵn từ paragraph data
            parts = self.chunks
            self.log(f"[Line {self.line_id}] Paragraph {self.paragraph_idx}: {len(self.content):,} ký tự → {len(parts)} chunks")
            
            max_part_chars = max(len(p) for p in parts) if parts else len(content)
            
            self.sig.subtitle_status.emit(self.row, "Check Verry...")
            self.log(f"[Line {self.line_id}] Check Verry - Xác nhận key hoạt động...")
            
            self._temp_min_credits = int(max_part_chars * 1.2)
            
            if not self._validate_current_key():
                self.sig.subtitle_status.emit(self.row, "Fail")
                self.sig.done.emit(self.row, "Fail")
                self.log(f"[Line {self.line_id}] ⛔ Không có key hoạt động, dừng dòng này")
                return
            
            voice_settings = {
                "stability": max(0.0, min(1.0, self.s.stability/100.0)),
                "similarity_boost": max(0.0, min(1.0, self.s.similarity/100.0)),
                "style": max(0.0, min(1.0, self.s.style/100.0)),
                "use_speaker_boost": bool(self.s.speaker_boost),
                "speed": max(0.5, min(2.0, self.s.speed)),  # API supports 0.5-2.0
            }
            
            # 🔧 FIX: Tạo folder _chunks trong tts_mini_dir (không phải outdir)
            # Để _check_existing_mp3_files có thể tìm thấy cached chunks
            chunks_dir = os.path.join(self.tts_mini_dir, "_chunks")
            ensure_dir(chunks_dir)
            
            mp3_parts = []
            
            for i, seg in enumerate(parts, start=1):
                if self._should_stop():
                    self.sig.subtitle_status.emit(self.row, "Stopped")
                    self.sig.done.emit(self.row, "Stopped")
                    return
                
                # 🔧 NEW: Lấy row tương ứng với chunk này
                chunk_row = self.chunk_rows[i - 1] if i <= len(self.chunk_rows) else self.row
                
                processed_seg = seg
                if self.s.pause_char_enabled:
                    processed_seg = insert_ssml_breaks(
                        seg, 
                        self.s.char1, self.s.char1_sec,
                        self.s.char2, self.s.char2_sec
                    )
                
                # Use processed content directly - speed is now in voice_settings
                payload = processed_seg
                
                # 🔧 NEW: Emit status cho chunk row cụ thể
                self.sig.chunk_status.emit(chunk_row, "Check Key...")
                if not self._validate_current_key():
                    self.sig.chunk_status.emit(chunk_row, "Fail")
                    self.sig.subtitle_status.emit(self.row, "Fail")
                    self.sig.done.emit(self.row, "Fail")
                    return
                
                if self._should_stop():
                    self.sig.subtitle_status.emit(self.row, "Stopped")
                    self.sig.done.emit(self.row, "Stopped")
                    return
                
                # 🔧 NEW: Emit Loading status cho chunk row
                self.sig.chunk_status.emit(chunk_row, "Loading...")
                self.log(f"[Line {self.line_id}] Part {i}/{len(parts)} - Loading...")
                
                self.sig.chunk_status.emit(chunk_row, "Processing")
                
                # 🔧 FIX: Đặt tên chunk - 2.mp3 (1 chunk) hoặc 1.1.mp3, 1.2.mp3 (nhiều chunks)
                if len(parts) == 1:
                    chunk_filename = sanitize_filename(f"{self.paragraph_idx}.mp3")
                else:
                    chunk_filename = sanitize_filename(f"{self.paragraph_idx}.{i}.mp3")
                part_outpath = os.path.join(chunks_dir, chunk_filename)
                
                max_wait_time = 720
                retry_interval = 1  # Giảm từ 6 xuống 1 giây (như V30)
                max_retries = 120
                
                for attempt in range(max_retries):
                    if self._should_stop():
                        self.sig.subtitle_status.emit(self.row, "Stopped")
                        self.sig.done.emit(self.row, "Stopped")
                        return
                    
                    try:
                        request_delay = getattr(self.s, 'request_delay', 0.0)
                        if request_delay > 0:
                            self.log(f"[Line {self.line_id}] Rate limit: đợi {request_delay}s...")
                            time.sleep(request_delay)
                        
                        # === SINGLE-PHASE TTS (Direct) ===
                        self.sig.subtitle_status.emit(self.row, "Loading...")
                        # 🔧 FIX: Truyền _acquired_key vào tts_direct() để dùng đúng key đã acquire từ DB
                        self.client.tts_direct(self.voice_id, payload, self.model_id, voice_settings, part_outpath, api_key=self._acquired_key)
                        
                        # 🔧 SMART KEY: Đánh dấu key vừa gen thành công
                        if self._acquired_key:
                            self.client.keys.mark_success(self._acquired_key)
                        
                        break
                    except Exception as api_err:
                        err_msg = str(api_err).lower()
                        
                        if "429" in err_msg or "403" in err_msg or "503" in err_msg:
                            # 🔧 FIX: Giảm timeout để tránh UI freeze
                            ip_block_timeout = 30   # Giảm từ 120s xuống 30s
                            ip_block_interval = 2   # 2s giữa các retry
                            ip_block_max = ip_block_timeout // ip_block_interval
                            
                            # Report rate limit để proxy service xử lý
                            if hasattr(self.client, 'proxies') and self.client.proxies:
                                if hasattr(self.client.proxies, 'report_rate_limit'):
                                    switched = self.client.proxies.report_rate_limit()
                                    if switched:
                                        # Đã chuyển sang proxy khác
                                        new_proxy = self.client.proxies.get_current_proxy()
                                        self.log(f"[Line {self.line_id}] 🔄 Rate limit - switched to new proxy: {new_proxy[:30] if new_proxy else 'None'}...")
                                        time.sleep(1)  # Wait before retry with new proxy
                                        continue
                                    else:
                                        # Chỉ có 1 proxy - đã refresh IP (hoặc đang chờ), retry ngay
                                        self.log(f"[Line {self.line_id}] 🔄 Rate limit - IP refreshed, retrying...")
                                        time.sleep(2)  # Chờ 2s để IP mới có hiệu lực
                                        continue
                                elif hasattr(self.client.proxies, 'list') and self.client.proxies.list() and len(self.client.proxies.list()) > 1:
                                    old_proxy = self.client.proxies.cur()
                                    new_proxy = self.client.proxies.rotate()
                                    self.log(f"[Line {self.line_id}] ⚠️ IP Block - xoay proxy → {new_proxy[:20] if new_proxy else 'None'}...")
                            else:
                                self.log(f"[Line {self.line_id}] ⚠️ Lỗi chặn IP: {err_msg[:50]}...")
                            
                            if attempt < ip_block_max - 1:
                                elapsed = (attempt + 1) * ip_block_interval
                                remaining = ip_block_timeout - elapsed
                                self.sig.subtitle_status.emit(self.row, "Loading...")
                                self.log(f"[Line {self.line_id}] 429 retry ({attempt+1}/{ip_block_max}), còn {remaining}s")
                                time.sleep(ip_block_interval)
                                continue
                            else:
                                # 🔧 FIX: Thay vì fail, đổi key và thử lại
                                self.log(f"[Line {self.line_id}] ⚠️ 429 timeout - đổi key mới")
                                self.client.keys.rotate()
                                if not self._validate_current_key():
                                    self.log(f"[Line {self.line_id}] ❌ Không có key available")
                                    self.sig.subtitle_status.emit(self.row, "Fail")
                                    self.sig.done.emit(self.row, "Fail")
                                    return
                                continue  # Thử lại với key mới
                        
                        
                        if "400" in err_msg:
                            error_400_retry = getattr(self.s, 'error_400_retry_before_rotate', 3)
                            error_400_delay = getattr(self.s, 'error_400_delay', 2.0)
                            
                            self.log(f"[Line {self.line_id}] ⚠️ 400 ERROR DETAIL: {str(api_err)[:200]}")
                            
                            # Check voice limit
                            if "voice_limit_reached" in str(api_err) or "maximum amount of custom voices" in str(api_err):
                                self.log(f"[Line {self.line_id}] ⚠️ Full Voice Slots (3/3) - Cleaning up...")
                                try:
                                    # Xóa TOÀN BỘ voice cũ để đảm bảo trống slot
                                    removed = self.client.cleanup_voice_slots(self.client.keys.cur(), max_voices=0)
                                    self.log(f"[Line {self.line_id}] ♻️ Đã xóa {removed} voice cũ. Retry key hiện tại...")
                                    
                                    # Không rotate nữa, dùng luôn key vừa dọn
                                    
                                    # Retry sau 2s để server cập nhật
                                    time.sleep(2)
                                    continue
                                except Exception as e:
                                    self.log(f"[Line {self.line_id}] ❌ Lỗi khi cleanup voice: {e}")
                            
                            if not hasattr(self, '_400_retry_count'):
                                self._400_retry_count = 0
                            if not hasattr(self, '_key_rotate_count'):
                                self._key_rotate_count = 0
                            
                            self._400_retry_count += 1
                            
                            if self._400_retry_count < error_400_retry:
                                self.log(f"[Line {self.line_id}] 🔄 400 - Retry {self._400_retry_count}/{error_400_retry}")
                                self.sig.subtitle_status.emit(self.row, "Loading...")
                                time.sleep(1)  # Giảm từ error_400_delay xuống 1 giây
                                continue
                            
                            self._400_retry_count = 0  # Reset counter
                            self._key_rotate_count += 1
                            key_rotate_max = 500
                            
                            if self._key_rotate_count >= key_rotate_max:
                                self.log(f"[Line {self.line_id}] ❌ Đã xoay {key_rotate_max} key không thành công (400)")
                                self.sig.subtitle_status.emit(self.row, "Fail")
                                self.sig.done.emit(self.row, "Fail")
                                return
                            
                            # Xoay key
                            self.client.keys.rotate()
                            self.log(f"[Line {self.line_id}] 🔄 400 - Xoay key lần {self._key_rotate_count}/{key_rotate_max}")
                            self.sig.subtitle_status.emit(self.row, "Loading...")
                            time.sleep(1)  # Giảm từ error_400_delay xuống 1 giây
                            
                            self.sig.subtitle_status.emit(self.row, "Check Verry...")
                            if not self._validate_current_key():
                                self.log(f"[Line {self.line_id}] ❌ Key mới không hợp lệ")
                                continue
                            
                            self.sig.subtitle_status.emit(self.row, "Loading...")
                            time.sleep(1.0)
                            continue
                        
                        if "401" in err_msg or "402" in err_msg or "422" in err_msg:
                            key_retry_max = getattr(self.s, 'retry_401_count', 1)  # Số lần retry (không tính lần đầu)
                            key_rotate_max = 400
                            
                            if not hasattr(self, '_key_retry_count'):
                                self._key_retry_count = 0
                            if not hasattr(self, '_key_rotate_count'):
                                self._key_rotate_count = 0
                            
                            self._key_retry_count += 1
                            
                            # 🔧 FIX: retry_401_count = 1 nghĩa là retry 1 lần (tổng 2 lần thử)
                            if self._key_retry_count <= key_retry_max:
                                self.sig.subtitle_status.emit(self.row, "Loading...")
                                self.log(f"[Line {self.line_id}] Key 401 retry {self._key_retry_count}/{key_retry_max}: {err_msg[:50]}...")
                                time.sleep(0.5)  # Giảm delay xuống 0.5s
                                continue
                            else:
                                # Đã retry đủ số lần → đổi key mới
                                self._key_retry_count = 0
                                self._key_rotate_count += 1
                                
                                if self._key_rotate_count >= key_rotate_max:
                                    self.log(f"[Line {self.line_id}] ❌ Đã xoay {key_rotate_max} key không thành công")
                                    self.sig.subtitle_status.emit(self.row, "Fail")
                                    self.sig.done.emit(self.row, "Fail")
                                    return
                                
                                bad_key = self.client.keys.cur()
                                self.client.keys.mark_401(bad_key)
                                self.log(f"[KeyPool] 🚫 Key {bad_key[:8]}... bị 401 → đổi key mới")
                                
                                # Xoay key
                                self.client.keys.rotate()
                                
                                # Xoay PROXY luôn (để tránh trường hợp IP này bị ban khiến key nào cũng 401)
                                if self.client.proxies.enabled:
                                    new_proxy = self.client.proxies.rotate()
                                    self.log(f"[Line {self.line_id}] 🔄 401 - Xoay Key & Proxy → {new_proxy[:20] if new_proxy else 'None'}...")
                                else:
                                    self.log(f"[Line {self.line_id}] 🔄 401 - Xoay Key (Proxy OFF)")

                                self.sig.subtitle_status.emit(self.row, "Loading...")
                                if bad_key:
                                    self.client.keys.mark_bad(bad_key)
                                    self.log(f"[Line {self.line_id}] ⚠️ Key {bad_key[:10]}... đã bị cooldown 5 phút")
                                
                                self.client.keys.rotate()
                                self.log(f"[Line {self.line_id}] 🔄 Xoay key lần {self._key_rotate_count}/{key_rotate_max}")
                                self.sig.subtitle_status.emit(self.row, "Loading...")
                                time.sleep(2.0)
                                
                                self.sig.subtitle_status.emit(self.row, "Check Verry...")
                                if not self._validate_current_key():
                                    self.log(f"[Line {self.line_id}] ❌ Key mới không hợp lệ")
                                    self.sig.subtitle_status.emit(self.row, "Fail")
                                    self.sig.done.emit(self.row, "Fail")
                                    return
                                
                                self.sig.subtitle_status.emit(self.row, "Loading...")
                                time.sleep(2.0)
                                continue
                        
                        if "voice_limit" in err_msg or "maximum" in err_msg:
                            self.sig.subtitle_status.emit(self.row, "Cleanup Voice")
                            current_key = self.client.keys.cur()
                            if current_key:
                                deleted = self.client.cleanup_voice_slots(
                                    current_key, 
                                    max_voices=1,
                                    protected_voice_ids=[self.voice_id]
                                )
                                if deleted > 0:
                                    self.log(f"[Line {self.line_id}] Đã xóa {deleted} voice thừa")
                            time.sleep(6)
                            continue
                        
                        # Handle Connection/Protocol Errors specially
                        if "protocolerror" in err_msg or "connection aborted" in err_msg or "connection reset" in err_msg or "10054" in err_msg or "10053" in err_msg or "max retries exceeded" in err_msg:
                            self.log(f"[Line {self.line_id}] ⚠️ Lỗi kết nối (reset/aborted) - Refresh Session & Matrix Proxy...")
                            
                            # 1. Refresh Session (quan trọng để clear pool lỗi)
                            self.client.refresh_session()
                            
                            # 2. Xoay Proxy (để tránh IP xấu gây reset)
                            if self.client.proxies.list() and len(self.client.proxies.list()) > 1:
                                new_proxy = self.client.proxies.rotate()
                                self.log(f"[Line {self.line_id}] 🔄 Đã xoay proxy sang: {new_proxy if new_proxy else 'None'}")
                            else:
                                # Nếu dùng key xoay
                                new_proxy = self.client.proxies.rotate()
                                if new_proxy:
                                     self.log(f"[Line {self.line_id}] 🔄 Đã lấy proxy xoay mới")

                            # 3. Retry nhanh
                            self.sig.subtitle_status.emit(self.row, "Loading...")
                            time.sleep(1)
                            continue

                        if attempt < max_retries - 1:
                            self.log(f"[Line {self.line_id}] Lỗi khác retry {attempt+1}/{max_retries}: {err_msg[:50]}...")
                            self.sig.subtitle_status.emit(self.row, "Verry Check...")
                            time.sleep(retry_interval)
                        else:
                            self.log(f"[Line {self.line_id}] ❌ Thất bại sau {max_retries} lần thử")
                            self.sig.chunk_status.emit(chunk_row, "TIMEOUT")
                            self.sig.subtitle_status.emit(self.row, "TIMEOUT")
                            self.sig.done.emit(self.row, "TIMEOUT")
                            return
                
                mp3_parts.append(part_outpath)
                
                # 🔧 NEW: Emit DONE status cho chunk row cụ thể
                self.sig.chunk_status.emit(chunk_row, "✅ Done")
                self.log(f"[Line {self.line_id}] Part {i}/{len(parts)} done - Downloaded")
                time.sleep(2.0)
                
                if self._should_stop():
                    self.sig.subtitle_status.emit(self.row, "Stopped")
                    self.sig.done.emit(self.row, "Stopped")
                    return
            
            time.sleep(2.0)
            
            # Ghép chunks thành doan_X.mp3 trong tts_mini folder
            doan_filename = sanitize_filename(f"doan_{self.paragraph_idx}.mp3")
            final_outpath = os.path.join(self.tts_mini_dir, doan_filename)
            ensure_dir(self.tts_mini_dir)
            
            if len(mp3_parts) > 1:
                self.log(f"[Line {self.line_id}] Ghép {len(mp3_parts)} chunks → doan_{self.paragraph_idx}.mp3...")
                try:
                    join_mp3_simple(mp3_parts, final_outpath)
                    # 🔧 FIX: Giữ lại chunks trong folder _chunks (không xóa)
                    # User có thể cần kiểm tra từng chunk riêng lẻ
                except Exception as e:
                    self.log(f"[Line {self.line_id}] Join error: {e}")
                    import shutil
                    shutil.copy2(mp3_parts[0], final_outpath)
            else:
                import shutil
                shutil.copy2(mp3_parts[0], final_outpath)
            
            timing = ""
            duration = 0.0
            
            try:
                ffprobe = find_ffprobe()
                if ffprobe:
                    self.log(f"[Line {self.line_id}] Found ffprobe: {ffprobe}")
                    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", final_outpath]
                    result = run_hidden(cmd)
                    if result.returncode == 0 and result.stdout.strip():
                        duration = float(result.stdout.strip())
                        timing = f"{duration:.2f}s"
                        self.log(f"[Line {self.line_id}] Duration from ffprobe: {timing}")
                    else:
                        self.log(f"[Line {self.line_id}] ffprobe failed: returncode={result.returncode}, stdout='{result.stdout}', stderr='{result.stderr}'")
                        raise Exception("ffprobe failed")
                else:
                    self.log(f"[Line {self.line_id}] ffprobe not found, trying fallback...")
                    raise Exception("ffprobe not found")
            except Exception as e:
                self.log(f"[Line {self.line_id}] ffprobe error: {e}")
                try:
                    from mutagen.mp3 import MP3
                    audio = MP3(final_outpath)
                    duration = audio.info.length
                    timing = f"{duration:.2f}s"
                    self.log(f"[Line {self.line_id}] Duration from mutagen: {timing}")
                except Exception as e2:
                    self.log(f"[Line {self.line_id}] mutagen error: {e2}")
                    try:
                        file_size = os.path.getsize(final_outpath)
                        duration = file_size / (128000 / 8)  # bytes / bytes-per-second @ 128kbps
                        timing = f"~{duration:.1f}s"
                        self.log(f"[Line {self.line_id}] Duration estimated from size: {timing}")
                    except Exception as e3:
                        self.log(f"[Line {self.line_id}] size fallback error: {e3}")
                        timing = ""
            
            output_name = f"{self.line_id}.mp3"
            
            # 🔧 NEW: Report proxy success to reset fail count
            if self.client.proxies.enabled:
                self.client.proxies.report_success()
            
            # Status: Done + emit Output/Timing (gửi cả đường dẫn đầy đủ)
            self.sig.subtitle_status.emit(self.row, "DONE")
            self.sig.subtitle_output.emit(self.row, final_outpath, timing)  # 🔧 FIX: Gửi full path
            self.sig.done.emit(self.row, "DONE")
            self.log(f"[Line {self.line_id}] DONE → {final_outpath} ({timing})")
            
            # 🔧 NEW: Release key với line_id để xóa lock
            if hasattr(self, '_acquired_key') and self._acquired_key:
                unique_line_id = f"{self.file_base}_{self.line_id}"
                self.client.keys.release_key(self._acquired_key, len(self.content), success=True, line_id=unique_line_id)
            
        except Exception as e:
            self.log(f"[Line {self.line_id}] ERROR: {e}")
            self.sig.subtitle_status.emit(self.row, "Fail")
            self.sig.done.emit(self.row, "Fail")
            
            # 🔧 NEW: Release key với line_id để xóa lock
            if hasattr(self, '_acquired_key') and self._acquired_key:
                unique_line_id = f"{self.file_base}_{self.line_id}"
                self.client.keys.release_key(self._acquired_key, 0, success=False, error_code=str(e), line_id=unique_line_id)



class FileWorker(QRunnable):
    def __init__(self, row: int, path: str, voice_id: str, model_id: str, s: AppSettings, client: ElevenClient, sig: Sig, log, subtitle_start_row: int = 0):
        super().__init__()
        self.row=row; self.path=path; self.voice_id=voice_id; self.model_id=model_id
        self.s=s; self.client=client; self.sig=sig; self.log=log
        self._stop=False
        self.subtitle_start_row = subtitle_start_row
    def stop(self): self._stop=True
    def run(self):
        self.sig.done.emit(self.row, "Done")

class ProxyDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, initial_text="", initial_phase=True, initial_enabled=False,
                 initial_delay=3.0, initial_retry_401=1,
                 initial_400_retry=3, initial_400_delay=2.0):
        super().__init__(parent)
        self.setWindowTitle("Proxy Settings"); self.resize(400,350)
        lay=QtWidgets.QGridLayout(self); lay.setContentsMargins(10,8,10,8)
        lay.addWidget(QtWidgets.QLabel("Danh sách Proxy HTTP (mỗi dòng 1 proxy)"),0,0,1,2)
        self.txt=QtWidgets.QPlainTextEdit()
        self.txt.setPlainText(initial_text)
        lay.addWidget(self.txt,1,0,1,2)
        self.cb=QtWidgets.QCheckBox("Bật log kiểm tra Pha 1 / Pha 2"); self.cb.setVisible(False); self.cb.setChecked(initial_phase)
        
        self.cb_enabled = QtWidgets.QCheckBox("Bật sử dụng Proxy")
        self.cb_enabled.setChecked(initial_enabled)
        self.cb_enabled.setToolTip("Tích = dùng Proxy\nBỏ tích = dùng mạng máy tính trực tiếp")
        lay.addWidget(self.cb_enabled, 2, 0, 1, 2)
        
        # --- Rate Limiting (Delay) ---
        delay_frame = QtWidgets.QFrame()
        delay_lay = QtWidgets.QHBoxLayout(delay_frame)
        delay_lay.setContentsMargins(0, 5, 0, 5)
        
        delay_lay.addWidget(QtWidgets.QLabel("Delay request:"))
        self.sb_delay = QtWidgets.QDoubleSpinBox()
        self.sb_delay.setRange(0, 30)
        self.sb_delay.setValue(initial_delay)
        self.sb_delay.setSingleStep(0.5)
        self.sb_delay.setFixedWidth(60)
        self.sb_delay.setToolTip("Đợi X giây giữa mỗi request TTS để tránh bị rate limit")
        delay_lay.addWidget(self.sb_delay)
        delay_lay.addWidget(QtWidgets.QLabel("giây"))
        delay_lay.addStretch()
        
        lay.addWidget(delay_frame, 3, 0, 1, 2)
        
        # --- Retry 401 Count ---
        retry_frame = QtWidgets.QFrame()
        retry_lay = QtWidgets.QHBoxLayout(retry_frame)
        retry_lay.setContentsMargins(0, 5, 0, 5)
        
        retry_lay.addWidget(QtWidgets.QLabel("401 X 1:"))
        self.sb_retry_401 = QtWidgets.QSpinBox()
        self.sb_retry_401.setRange(1, 100)
        self.sb_retry_401.setValue(initial_retry_401)
        self.sb_retry_401.setFixedWidth(60)
        self.sb_retry_401.setToolTip("Số lần gặp lỗi 401 liên tiếp trước khi xoay sang key khác")
        retry_lay.addWidget(self.sb_retry_401)
        retry_lay.addWidget(QtWidgets.QLabel("lần"))
        retry_lay.addStretch()
        
        lay.addWidget(retry_frame, 4, 0, 1, 2)
        
        retry400_frame = QtWidgets.QFrame()
        retry400_lay = QtWidgets.QHBoxLayout(retry400_frame)
        retry400_lay.setContentsMargins(0, 5, 0, 5)
        
        retry400_lay.addWidget(QtWidgets.QLabel("400 X 3:"))
        self.sb_400_retry = QtWidgets.QSpinBox()
        self.sb_400_retry.setRange(1, 50)
        self.sb_400_retry.setValue(initial_400_retry)
        self.sb_400_retry.setFixedWidth(60)
        self.sb_400_retry.setToolTip("Số lần retry lỗi 400 trước khi xoay sang key khác (1-50)")
        retry400_lay.addWidget(self.sb_400_retry)
        retry400_lay.addWidget(QtWidgets.QLabel("lần"))
        retry400_lay.addStretch()
        
        lay.addWidget(retry400_frame, 5, 0, 1, 2)
        
        delay400_frame = QtWidgets.QFrame()
        delay400_lay = QtWidgets.QHBoxLayout(delay400_frame)
        delay400_lay.setContentsMargins(0, 5, 0, 5)
        
        delay400_lay.addWidget(QtWidgets.QLabel("Delay 40X2:"))
        self.sb_400_delay = QtWidgets.QDoubleSpinBox()
        self.sb_400_delay.setRange(0, 30)
        self.sb_400_delay.setValue(initial_400_delay)
        self.sb_400_delay.setSingleStep(0.5)
        self.sb_400_delay.setFixedWidth(60)
        self.sb_400_delay.setToolTip("Đợi X giây khi gặp lỗi 400 trước khi retry")
        delay400_lay.addWidget(self.sb_400_delay)
        delay400_lay.addWidget(QtWidgets.QLabel("giây"))
        delay400_lay.addStretch()
        
        lay.addWidget(delay400_frame, 6, 0, 1, 2)
        
        btn=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok|QtWidgets.QDialogButtonBox.Cancel)
        lay.addWidget(btn,7,0,1,2); btn.accepted.connect(self.accept); btn.rejected.connect(self.reject)

class AdvDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advance Settings"); self.resize(400,400)
        QtWidgets.QApplication.setStyle("Fusion"); f=self.font(); f.setPointSize(9); self.setFont(f)
        root=QtWidgets.QGridLayout(self); root.setContentsMargins(10,8,10,8); root.setVerticalSpacing(6)
        r=0
        self.cb_gap_segments=QtWidgets.QCheckBox("Ngắt âm giữa các đoạn"); self.cb_gap_segments.setChecked(False); self.sb_gap=QtWidgets.QDoubleSpinBox(); self.sb_gap.setRange(0,10); self.sb_gap.setValue(1.3); self.sb_gap.setSingleStep(0.1); self.sb_gap.setFixedWidth(50)
        root.addWidget(self.cb_gap_segments,r,0,1,2); root.addWidget(QtWidgets.QLabel("(s) (khi nối file)"),r,2); root.addWidget(self.sb_gap,r,1); r+=1
        root.addWidget(QtWidgets.QLabel("    Cách nhau"),r,0); self.sb_every=QtWidgets.QSpinBox(); self.sb_every.setRange(1,100); self.sb_every.setValue(5); self.sb_every.setFixedWidth(50)
        root.addWidget(self.sb_every,r,1); root.addWidget(QtWidgets.QLabel("đoạn"),r,2); r+=1
        self.cb_gap_srt=QtWidgets.QCheckBox("Ngắt âm theo file srt"); root.addWidget(self.cb_gap_srt,r,0,1,3); r+=1
        self.cb_pause_char=QtWidgets.QCheckBox("Ngắt âm theo ký tự"); root.addWidget(self.cb_pause_char,r,0,1,3); r+=1
        grid=QtWidgets.QGridLayout(); grid.setHorizontalSpacing(6)
        grid.addWidget(QtWidgets.QLabel("Ký tự:"),0,0); self.ed_char1=QtWidgets.QLineEdit(","); self.ed_char1.setFixedWidth(36)
        self.sb_char1=QtWidgets.QDoubleSpinBox(); self.sb_char1.setRange(0,10); self.sb_char1.setValue(0.3); self.sb_char1.setSingleStep(0.1); self.sb_char1.setFixedWidth(50)
        grid.addWidget(self.ed_char1,0,1); grid.addWidget(self.sb_char1,0,2); grid.addWidget(QtWidgets.QLabel("(s)"),0,3)
        grid.addWidget(QtWidgets.QLabel("Ký tự:"),1,0); self.ed_char2=QtWidgets.QLineEdit("."); self.ed_char2.setFixedWidth(36)
        self.sb_char2=QtWidgets.QDoubleSpinBox(); self.sb_char2.setRange(0,10); self.sb_char2.setValue(0.5); self.sb_char2.setSingleStep(0.1); self.sb_char2.setFixedWidth(50)
        grid.addWidget(self.ed_char2,1,1); grid.addWidget(self.sb_char2,1,2); grid.addWidget(QtWidgets.QLabel("(s)"),1,3)
        root.addLayout(grid,r,0,1,3); r+=1
        self.cb_sanitize=QtWidgets.QCheckBox("Tự động loại bỏ ký tự đặc biệt")
        self.cb_sanitize.setChecked(True)  # Luôn bật
        self.cb_sanitize.setVisible(False)  # Ẩn checkbox
        root.addWidget(self.cb_sanitize,r,0,1,3); r+=1
        self.cmb_dl=QtWidgets.QComboBox(); self.cmb_dl.addItems(["1 <ORIGINAL>"]); self.cmb_dl.setVisible(False)
        root.addWidget(QtWidgets.QLabel("Số ký tự tối đa / dòng"),r,0)
        self.ed_max_chars=QtWidgets.QLineEdit(); self.ed_max_chars.setText("1000"); self.ed_max_chars.setFixedWidth(100)
        # Validator: chỉ cho phép số từ 1-1000
        from PySide6.QtGui import QIntValidator
        validator = QIntValidator(1, 1000, self.ed_max_chars)
        self.ed_max_chars.setValidator(validator)
        root.addWidget(self.ed_max_chars,r,1); r+=1
        h=QtWidgets.QHBoxLayout(); self.btn_proxy=QtWidgets.QPushButton("PVIP"); self.btn_keys=QtWidgets.QPushButton("API Keys…")
        self.btn_reset_counter=QtWidgets.QPushButton("Xóa Bộ Đếm")
        # Ẩn các nút PVIP, API Keys, Xóa Bộ Đếm (đã quản lý qua DB)
        self.btn_proxy.setVisible(False)
        self.btn_keys.setVisible(False)
        self.btn_reset_counter.setVisible(False)
        h.addWidget(self.btn_proxy); h.addWidget(self.btn_keys); h.addWidget(self.btn_reset_counter); h.addStretch(1); root.addLayout(h,r,0,1,3); r+=1
        self.keys_path=QtWidgets.QLineEdit(); self.keys_path.setReadOnly(True)
        self.keys_info=QtWidgets.QLabel("Chưa chọn tệp keys")
        # Ẩn keys input và label (đã quản lý qua DB)
        self.keys_path.setVisible(False)
        self.keys_info.setVisible(False)
        root.addWidget(self.keys_path,r,0,1,2); root.addWidget(self.keys_info,r,2); r+=1
        btn=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok|QtWidgets.QDialogButtonBox.Cancel); root.addWidget(btn,r,0,1,3)
        btn.accepted.connect(self.accept); btn.rejected.connect(self.reject)
        self.btn_proxy.clicked.connect(self.open_proxy); self.btn_keys.clicked.connect(self.pick_keys)
        self.btn_reset_counter.clicked.connect(self.reset_counter)
        self._parent_window = parent
        
        # 🐛 FIX: Load proxy settings từ parent thay vì dùng giá trị mặc định
        # Điều này ngăn việc proxy bị xóa khi user mở AdvDialog mà không mở ProxyDialog
        if parent and hasattr(parent, 's'):
            self._proxy_text = parent.s.proxies_text or ""
            self._proxy_phase = parent.s.proxy_phase_log
            self._proxy_enabled = getattr(parent.s, 'proxy_enabled', False)
            self._proxy_delay = getattr(parent.s, 'request_delay', 3.0)
            self._proxy_retry_401 = getattr(parent.s, 'retry_401_count', 1)
            self._proxy_400_retry = getattr(parent.s, 'error_400_retry_before_rotate', 3)
            self._proxy_400_delay = getattr(parent.s, 'error_400_delay', 2.0)
            self._proxy_sticky_enabled = getattr(parent.s, 'proxy_sticky_enabled', True)
            self._proxy_sticky_minutes = getattr(parent.s, 'proxy_sticky_minutes', 3)
        else:
            self._proxy_text = ""
            self._proxy_phase = False
            self._proxy_enabled = False
            self._proxy_delay = 3.0
            self._proxy_retry_401 = 1
            self._proxy_400_retry = 3
            self._proxy_400_delay = 2.0
            self._proxy_sticky_enabled = True
            self._proxy_sticky_minutes = 3
        
        # Flag để biết user có thực sự mở ProxyDialog không
        self._proxy_changed = False
    
    def reset_counter(self):
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận", 
            "Bạn có chắc muốn xóa bộ đếm ký tự?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        if self._parent_window and hasattr(self._parent_window, 's'):
            self._parent_window.s.char_counter = 0
            save_settings(self._parent_window.s)
            if hasattr(self._parent_window, 'lbl_char_counter'):
                self._parent_window.lbl_char_counter.setText("Ký Tự Đã Dùng: 0")
            QtWidgets.QMessageBox.information(self, "Thành công", "Đã xóa bộ đếm ký tự!")
    
    def open_proxy(self):
        saved_proxy = ""
        saved_phase = True
        saved_enabled = False
        saved_delay = 3.0
        saved_retry_401 = 1
        saved_400_retry = 3
        saved_400_delay = 2.0
        if self._parent_window and hasattr(self._parent_window, 's'):
            saved_proxy = self._parent_window.s.proxies_text or ""
            saved_phase = self._parent_window.s.proxy_phase_log
            saved_enabled = getattr(self._parent_window.s, 'proxy_enabled', False)
            saved_delay = getattr(self._parent_window.s, 'request_delay', 3.0)
            saved_retry_401 = getattr(self._parent_window.s, 'retry_401_count', 1)
            saved_400_retry = getattr(self._parent_window.s, 'error_400_retry_before_rotate', 3)
            saved_400_delay = getattr(self._parent_window.s, 'error_400_delay', 2.0)
            if hasattr(self._parent_window, 'log'):
                self._parent_window.log(f"[Proxy] Loading: enabled={saved_enabled}, delay={saved_delay}s, retry401={saved_retry_401}, 400_retry={saved_400_retry}, 400_delay={saved_400_delay}s")
        dlg=ProxyDialog(self, initial_text=saved_proxy, initial_phase=saved_phase, initial_enabled=saved_enabled,
                        initial_delay=saved_delay, initial_retry_401=saved_retry_401,
                        initial_400_retry=saved_400_retry, initial_400_delay=saved_400_delay)
        if dlg.exec()==QtWidgets.QDialog.Accepted:
            self._proxy_text=dlg.txt.toPlainText()
            self._proxy_phase=dlg.cb.isChecked()
            self._proxy_enabled=dlg.cb_enabled.isChecked()
            self._proxy_delay=dlg.sb_delay.value()
            self._proxy_retry_401=dlg.sb_retry_401.value()
            self._proxy_400_retry=dlg.sb_400_retry.value()
            self._proxy_400_delay=dlg.sb_400_delay.value()
            
            # NOTE: KHÔNG save ở đây! MainWindow.open_adv() sẽ save sau khi dialog đóng
            # Nếu save 2 lần sẽ bị conflict!
            
            if self._parent_window and hasattr(self._parent_window, 'log'):
                self._parent_window.log(f"[Proxy] Dialog OK: enabled={self._proxy_enabled}, {len(self._proxy_text.splitlines()) if self._proxy_text else 0} proxy(s)")
            
            # 🐛 FIX: Đánh dấu proxy đã được thay đổi
            self._proxy_changed = True
    def pick_keys(self):
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,"Chọn tệp .txt chứa API Keys","","Text Files (*.txt);;All Files (*)")
        if not path: return
        self.keys_path.setText(path)
        try:
            n=sum(1 for ln in open(path,"r",encoding="utf-8",errors="ignore") if ln.strip())
            self.keys_info.setText(f"Đã chọn: {n} key(s)")
        except Exception as e:
            self.keys_info.setText(f"Lỗi đọc tệp: {e}")

# ---------------- Main Window ----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, show_login=True):
        super().__init__()
        
        # ========== CRITICAL: Initialize these FIRST to avoid AttributeError in closeEvent ==========
        self.current_worker = None
        self.stop_requested = False
        self.queue_paths = []
        self.queue_rows = []
        
        # 🔧 FIX: Khởi tạo progress lock NGAY TỪ ĐẦU để tránh race condition
        import threading
        self._progress_lock = threading.Lock()
        self._completed_lines = 0
        self._failed_lines = 0
        self._active_worker_count = 0
        self._total_lines = 0
        self._current_line_index = 0
        
        # ========== Preview Mode: Skip login ==========
        self.current_user_id = "preview_user"
        self.current_username = "Preview Mode"
        self.supabase = None
        self.auth_service = None
        self.key_pool_db = None
        self.proxy_service_db = None
        
        # Setup UI directly (no login needed)
        self._setup_ui()
    
    # ========== NEW: Login using Qt Dialog ==========
    def _do_login(self) -> bool:
        """
        Show login dialog using Qt (NOT ttkbootstrap - avoid GUI framework conflicts).
        Returns: True if login successful, False if cancelled
        """
        # Qt và Tkinter/ttkbootstrap xung đột trên macOS
        # => Sử dụng trực tiếp Qt dialog
        return self._simple_qt_login()
    
    def _load_login_temp(self) -> dict:
        """Load username/password từ file temp (đơn giản - base64 encode password)"""
        try:
            if os.path.exists(LOGIN_TEMP_FILE):
                with open(LOGIN_TEMP_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    username = data.get('username', '')
                    # Decode password từ base64
                    password_encoded = data.get('password', '')
                    if password_encoded:
                        import base64
                        password = base64.b64decode(password_encoded.encode()).decode('utf-8')
                    else:
                        password = ''
                    return {'username': username, 'password': password}
        except Exception as e:
            print(f"⚠️ Error loading login temp: {e}")
        return {'username': '', 'password': ''}
    
    def _save_login_temp(self, username: str, password: str):
        """Lưu username/password vào file temp (đơn giản - base64 encode password)"""
        try:
            import base64
            password_encoded = base64.b64encode(password.encode('utf-8')).decode('utf-8')
            data = {
                'username': username,
                'password': password_encoded
            }
            with open(LOGIN_TEMP_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            print(f"✅ Đã lưu login temp vào {LOGIN_TEMP_FILE}")
        except Exception as e:
            print(f"⚠️ Error saving login temp: {e}")
            import traceback
            traceback.print_exc()
    
    def _simple_qt_login(self) -> bool:
        """Modern Qt login dialog - căn giữa màn hình với error message inline"""
        try:
            main_window = self
            
            # Load saved credentials
            saved_creds = self._load_login_temp()
            
            class ModernLoginDialog(QtWidgets.QDialog):
                def __init__(self, parent=None, saved_username='', saved_password=''):
                    super().__init__(parent)
                    self.main_window = parent
                    self.setWindowTitle("Huy Việt Elevenlabs - Đăng nhập")
                    self.setModal(True)
                    self.setFixedSize(450, 480)
                    self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
                    
                    # Main container với gradient background
                    self.setStyleSheet("""
                        QDialog {
                            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 #1a1a2e, stop:0.5 #16213e, stop:1 #0f3460);
                            border-radius: 15px;
                            border: 2px solid #0f3460;
                        }
                        QLabel {
                            color: #eaeaea;
                            font-size: 13px;
                            background: transparent;
                        }
                        QLineEdit {
                            background-color: rgba(255, 255, 255, 0.1);
                            border: 2px solid #3a506b;
                            border-radius: 10px;
                            padding: 14px 18px;
                            color: #ffffff;
                            font-size: 14px;
                            selection-background-color: #e94560;
                        }
                        QLineEdit:focus {
                            border: 2px solid #e94560;
                            background-color: rgba(255, 255, 255, 0.15);
                        }
                        QPushButton {
                            border-radius: 10px;
                            padding: 14px 30px;
                            font-size: 14px;
                            font-weight: bold;
                        }
                    """)
                    
                    layout = QtWidgets.QVBoxLayout(self)
                    layout.setContentsMargins(40, 25, 40, 30)
                    layout.setSpacing(0)
                    
                    # Close button
                    close_layout = QtWidgets.QHBoxLayout()
                    close_layout.addStretch()
                    btn_close = QtWidgets.QPushButton("✕")
                    btn_close.setFixedSize(32, 32)
                    btn_close.setCursor(Qt.PointingHandCursor)
                    btn_close.setStyleSheet("""
                        QPushButton {
                            background: transparent;
                            color: #6c757d;
                            border: none;
                            font-size: 18px;
                        }
                        QPushButton:hover {
                            color: #e94560;
                        }
                    """)
                    btn_close.clicked.connect(self.reject)
                    close_layout.addWidget(btn_close)
                    layout.addLayout(close_layout)
                    
                    layout.addSpacing(10)
                    
                    # Logo/Icon
                    icon_label = QtWidgets.QLabel("🎙️")
                    icon_label.setStyleSheet("font-size: 48px; background: transparent;")
                    icon_label.setAlignment(Qt.AlignCenter)
                    layout.addWidget(icon_label)
                    
                    layout.addSpacing(12)
                    
                    # Title
                    title = QtWidgets.QLabel("HUY VIỆT ELEVENLABS")
                    title.setStyleSheet("""
                        font-size: 20px;
                        font-weight: bold;
                        color: #e94560;
                        letter-spacing: 3px;
                        background: transparent;
                    """)
                    title.setAlignment(Qt.AlignCenter)
                    layout.addWidget(title)
                    
                    layout.addSpacing(6)
                    
                    subtitle = QtWidgets.QLabel("Đăng nhập để tiếp tục")
                    subtitle.setStyleSheet("color: #8b9dc3; font-size: 12px; background: transparent;")
                    subtitle.setAlignment(Qt.AlignCenter)
                    layout.addWidget(subtitle)
                    
                    layout.addSpacing(25)
                    
                    # Username
                    self.username = QtWidgets.QLineEdit()
                    self.username.setPlaceholderText("👤  Tên đăng nhập")
                    self.username.setMinimumHeight(50)
                    # Fill saved username
                    if saved_username:
                        self.username.setText(saved_username)
                    layout.addWidget(self.username)
                    
                    layout.addSpacing(15)
                    
                    # Password
                    self.password = QtWidgets.QLineEdit()
                    self.password.setEchoMode(QtWidgets.QLineEdit.Password)
                    self.password.setPlaceholderText("🔒  Mật khẩu")
                    self.password.setMinimumHeight(50)
                    # Fill saved password
                    if saved_password:
                        self.password.setText(saved_password)
                    layout.addWidget(self.password)
                    
                    layout.addSpacing(12)
                    
                    # Error message label
                    self.lbl_error = QtWidgets.QLabel("")
                    self.lbl_error.setStyleSheet("""
                        color: #ff6b6b;
                        font-size: 12px;
                        background: transparent;
                        padding: 5px;
                    """)
                    self.lbl_error.setAlignment(Qt.AlignCenter)
                    self.lbl_error.setWordWrap(True)
                    self.lbl_error.setVisible(False)
                    layout.addWidget(self.lbl_error)
                    
                    layout.addSpacing(15)
                    
                    # Login Button
                    self.btn_login = QtWidgets.QPushButton("ĐĂNG NHẬP")
                    self.btn_login.setMinimumHeight(52)
                    self.btn_login.setCursor(Qt.PointingHandCursor)
                    self.btn_login.setStyleSheet("""
                        QPushButton {
                            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #e94560, stop:1 #ff6b6b);
                            color: white;
                            border: none;
                        }
                        QPushButton:hover {
                            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #ff6b6b, stop:1 #e94560);
                        }
                        QPushButton:pressed {
                            background: #c73e54;
                        }
                        QPushButton:disabled {
                            background: #555555;
                            color: #888888;
                        }
                    """)
                    layout.addWidget(self.btn_login)
                    
                    layout.addSpacing(18)
                    
                    # 🔧 NEW: Exit Button
                    self.btn_exit = QtWidgets.QPushButton("THOÁT")
                    self.btn_exit.setMinimumHeight(45)
                    self.btn_exit.setCursor(Qt.PointingHandCursor)
                    self.btn_exit.setStyleSheet("""
                        QPushButton {
                            background: transparent;
                            color: #8b9dc3;
                            border: 2px solid #3a506b;
                        }
                        QPushButton:hover {
                            background: rgba(255, 255, 255, 0.1);
                            color: #ffffff;
                            border: 2px solid #e94560;
                        }
                        QPushButton:pressed {
                            background: rgba(233, 69, 96, 0.2);
                        }
                    """)
                    self.btn_exit.clicked.connect(self.exit_app)
                    layout.addWidget(self.btn_exit)
                    
                    layout.addStretch()
                    
                    # Footer
                    footer = QtWidgets.QLabel("© 2025 Huy Việt Elevenlabs")
                    footer.setStyleSheet("color: #4a5568; font-size: 11px; background: transparent;")
                    footer.setAlignment(Qt.AlignCenter)
                    layout.addWidget(footer)
                    
                    # Connections
                    self.btn_login.clicked.connect(self.do_login)
                    self.username.returnPressed.connect(lambda: self.password.setFocus())
                    self.password.returnPressed.connect(self.do_login)
                    
                    self.login_result = None
                    
                    # Center on screen
                    self.center_on_screen()
                
                def show_error(self, message):
                    """Hiển thị thông báo lỗi"""
                    self.lbl_error.setText(f"❌ {message}")
                    self.lbl_error.setVisible(True)
                
                def hide_error(self):
                    """Ẩn thông báo lỗi"""
                    self.lbl_error.setVisible(False)
                
                def do_login(self):
                    """Xử lý đăng nhập"""
                    self.hide_error()
                    
                    username = self.username.text().strip()
                    password = self.password.text().strip()
                    
                    if not username or not password:
                        self.show_error("Vui lòng nhập đầy đủ thông tin!")
                        return
                    
                    # Disable button while processing
                    self.btn_login.setEnabled(False)
                    self.btn_login.setText("Đang đăng nhập...")
                    QtWidgets.QApplication.processEvents()
                    
                    try:
                        # Authenticate
                        auth = SupabaseAuth()
                        user = auth.sign_in_custom_user_table(username, password)
                        
                        if not user:
                            self.show_error("Sai tên đăng nhập hoặc mật khẩu!")
                            self.btn_login.setEnabled(True)
                            self.btn_login.setText("ĐĂNG NHẬP")
                            return
                        
                        # Success - store result and close
                        self.login_result = {
                            'user': user,
                            'auth': auth,
                            'username': username
                        }
                        # Lưu credentials sau khi đăng nhập thành công
                        if self.main_window:
                            self.main_window._save_login_temp(username, password)
                        self.accept()
                        
                    except Exception as e:
                        self.show_error(f"Lỗi kết nối: {str(e)[:50]}")
                        self.btn_login.setEnabled(True)
                        self.btn_login.setText("ĐĂNG NHẬP")
                
                def center_on_screen(self):
                    """Căn dialog ra giữa màn hình"""
                    screen = QtWidgets.QApplication.primaryScreen().geometry()
                    x = (screen.width() - self.width()) // 2
                    y = (screen.height() - self.height()) // 2
                    self.move(x, y)
                
                def exit_app(self):
                    """Thoát ứng dụng hoàn toàn"""
                    self.reject()
                    QtWidgets.QApplication.quit()
                    sys.exit(0)
                
                def mousePressEvent(self, event):
                    """Allow dragging window"""
                    if event.button() == Qt.LeftButton:
                        self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                        event.accept()
                
                def mouseMoveEvent(self, event):
                    """Move window when dragging"""
                    if event.buttons() == Qt.LeftButton and hasattr(self, '_drag_pos'):
                        self.move(event.globalPosition().toPoint() - self._drag_pos)
                        event.accept()
            
            dlg = ModernLoginDialog(self, saved_creds['username'], saved_creds['password'])
            if dlg.exec() != QtWidgets.QDialog.Accepted or not dlg.login_result:
                return False
            
            # Get login result
            result = dlg.login_result
            user = result['user']
            auth = result['auth']
            
            self.supabase = auth.supabase
            self.auth_service = auth
            
            self.current_user_id = user.get('id')
            self.current_username = user.get('username', result.get('username', 'User'))
            print(f"✅ Đăng nhập thành công: {self.current_username} (ID: {self.current_user_id})")
            
            # ========== Load keys từ database ==========
            print(f"📦 Loading keys for user {self.current_user_id}...")
            try:
                self.key_pool_db = LocalKeyPool(
                    supabase_client=self.supabase, 
                    user_id=self.current_user_id
                )
                self.key_pool_db.load()
                key_count = len(self.key_pool_db._keys)
                total_credits = sum(k.credit_remaining for k in self.key_pool_db._keys)
                print(f"✅ Loaded {key_count} keys from DB, total credits: {total_credits:,}")
            except Exception as e:
                print(f"⚠️ Error loading keys from DB: {e}")
                self.key_pool_db = None
            
            # ========== Load proxy từ database ==========
            print(f"📦 Loading proxy for user {self.current_user_id}...")
            try:
                self.proxy_service_db = ProxyServiceDB(supabase=self.supabase, user_id=self.current_user_id)
                proxy_count = self.proxy_service_db.load_from_database()
                print(f"   Found {proxy_count} proxy config(s) in DB")
                
                proxy_url = self.proxy_service_db.get_current_proxy()
                if proxy_url:
                    display = proxy_url[:50] + "..." if len(proxy_url) > 50 else proxy_url
                    print(f"✅ Proxy loaded: {display}")
                else:
                    print("⚠️ No proxy configured in DB")
            except Exception as e:
                print(f"⚠️ Error loading proxy from DB: {e}")
                import traceback
                traceback.print_exc()
                self.proxy_service_db = None
            
            print("✅ All services loaded successfully!")
            
            # 🐛 FIX: KHÔNG gọi _update_subscription_label ở đây vì UI chưa được setup
            # Sẽ được gọi sau trong __init__ sau khi _setup_ui() hoàn thành
            
            return True
        
        except Exception as e:
            print(f"❌ Login error: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _setup_ui(self):
        """Setup main window UI - được gọi từ __init__ sau khi login thành công"""
        # Set window title (không có username - đã chuyển xuống status bar)
        title = "Huy Việt Elevenlabs v2.12"
        self.setWindowTitle(title)
        
        icon_path = os.path.join(APP_DIR, "assets", "ap.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))
        
        DEFAULT_WIDTH = 900
        DEFAULT_HEIGHT = 650
        DEFAULT_FONT_SIZE = 9
        
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        screen_w, screen_h = screen.width(), screen.height()
        
        scale_x = screen_w / DEFAULT_WIDTH
        scale_y = screen_h / DEFAULT_HEIGHT
        scale = min(scale_x, scale_y, 1.0)
        
        if scale < 0.95:
            win_w = int(DEFAULT_WIDTH * scale * 0.95)
            win_h = int(DEFAULT_HEIGHT * scale * 0.95)
            font_size = max(7, int(DEFAULT_FONT_SIZE * scale))
        else:
            win_w = DEFAULT_WIDTH
            win_h = DEFAULT_HEIGHT
            font_size = DEFAULT_FONT_SIZE
        
        self.setFixedSize(win_w, win_h)
        
        QtWidgets.QApplication.setStyle("Fusion")
        
        palette = QtGui.QPalette()
        dim_white = QtGui.QColor(230, 230, 230)
        palette.setColor(QtGui.QPalette.Window, dim_white)
        palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(30, 30, 30))
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor(245, 245, 245))
        palette.setColor(QtGui.QPalette.AlternateBase, dim_white)
        palette.setColor(QtGui.QPalette.Text, QtGui.QColor(30, 30, 30))
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor(235, 235, 235))
        palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(30, 30, 30))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(0, 120, 215))
        palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(255, 255, 255))
        QtWidgets.QApplication.setPalette(palette)
        
        f=self.font(); f.setPointSize(font_size); self.setFont(f)

        # state
        self.s = load_settings()
        
        # ========== Preview Mode: TokenPool + ProxyPool ==========
        self.keys = DummyKeyManager()
        
        # Init proxy pool với keys từ proxyxoay.shop
        proxy_keys = []
        if hasattr(self.s, 'proxyxoay_key') and self.s.proxyxoay_key:
            proxy_keys = [k.strip() for k in self.s.proxyxoay_key.split(',') if k.strip()]
        
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._async_loop.run_forever, daemon=True)
        self._async_thread.start()
        
        self.proxy_pool = ProxyPool(proxy_keys)
        self.token_pool = TokenPool(self.proxy_pool, target_size=5)
        self.token_pool.set_log_callback(lambda msg: self.log(msg))
        
        # Start token pool nếu có proxy keys
        if proxy_keys:
            asyncio.run_coroutine_threadsafe(self.token_pool.start(), self._async_loop)
            self.log(f"[Preview] TokenPool started: {len(proxy_keys)} proxy key(s), target=5")
        else:
            self.log("[Preview] ⚠️ Chưa có proxy key - thêm trong Cài đặt nâng cao")
        
        self.client = PreviewClient(self.proxy_pool, self.token_pool, self.log, self.s)
        self.client.keys = self.keys  # ChunkWorker truy cập self.client.keys
        
        # Dummy proxies cho compatibility
        self.proxies = ProxyManager()
        self.proxies.set_log_fn(self.log)

        # sequential queue
        self.queue_paths: List[str] = []
        self.queue_rows: List[int] = []
        self.current_worker: Optional[FileWorker] = None
        self.stop_requested = False
        
        # Active workers (for multi-threaded processing)
        self._active_workers: List = []
        
        # Tracking progress
        self.total_files = 0
        self.completed_files = 0
        self._total_credits_remaining = 0

        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)

        cw=QtWidgets.QWidget(); self.setCentralWidget(cw)
        root=QtWidgets.QGridLayout(cw); root.setHorizontalSpacing(8); root.setVerticalSpacing(6)
        
        self.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #b0b0b0;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 5px;
                background-color: #e6e6e6;
            }
        """)

        # Voice
        grp_voice=QtWidgets.QGroupBox("Voice"); v=QtWidgets.QGridLayout(grp_voice)
        self.ed_name=QtWidgets.QLineEdit()
        self.ed_name.setMinimumWidth(120)
        
        # Search + Save buttons in horizontal layout (compact icons)
        btn_search_save = QtWidgets.QHBoxLayout()
        btn_search_save.setSpacing(2)
        self.bt_search=QtWidgets.QPushButton("🔍")
        self.bt_search.setToolTip("Tìm voice")
        self.bt_search.setFixedWidth(30)
        self.bt_save_voice=QtWidgets.QPushButton("💾")
        self.bt_save_voice.setToolTip("Lưu voice đang chọn")
        self.bt_save_voice.setFixedWidth(30)
        btn_search_save.addWidget(self.bt_search)
        btn_search_save.addWidget(self.bt_save_voice)
        
        self.cb_voice=QtWidgets.QComboBox(); self.cb_voice.setEditable(False)
        self.cb_voice.setMaximumWidth(200)
        self.cb_voice.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_model=QtWidgets.QComboBox(); self.cb_model.setEditable(False)
        self.cb_model.setMaximumWidth(200)
        if self.s.last_model_id:
            self.cb_model.addItem(self.s.last_model_id)
        v.addWidget(QtWidgets.QLabel("Name:"),0,0); v.addWidget(self.ed_name,0,1); v.addLayout(btn_search_save,0,2)
        v.addWidget(QtWidgets.QLabel("Voice:"),1,0); v.addWidget(self.cb_voice,1,1,1,2)
        v.addWidget(QtWidgets.QLabel("Model:"),2,0); v.addWidget(self.cb_model,2,1,1,2)
        
        # Language selection (ISO 639-1 codes)
        self.cb_language=QtWidgets.QComboBox(); self.cb_language.setEditable(False)
        self.cb_language.setMaximumWidth(200)
        # Add common languages (format: "Display Name - code")
        lang_options = [
            ("Auto-detect (Default)", ""),
            ("Vietnamese - vi", "vi"),
            ("English - en", "en"),
            ("Spanish - es", "es"),
            ("French - fr", "fr"),
            ("German - de", "de"),
            ("Italian - it", "it"),
            ("Portuguese - pt", "pt"),
            ("Chinese - zh", "zh"),
            ("Japanese - ja", "ja"),
            ("Korean - ko", "ko"),
            ("Russian - ru", "ru"),
            ("Arabic - ar", "ar"),
            ("Hindi - hi", "hi"),
            ("Dutch - nl", "nl"),
            ("Polish - pl", "pl"),
            ("Turkish - tr", "tr"),
            ("Swedish - sv", "sv"),
        ]
        for display, code in lang_options:
            self.cb_language.addItem(display, userData=code)
        
        # Set saved language
        if self.s.language_code:
            for i in range(self.cb_language.count()):
                if self.cb_language.itemData(i) == self.s.language_code:
                    self.cb_language.setCurrentIndex(i)
                    break
        
        v.addWidget(QtWidgets.QLabel("Language:"),3,0); v.addWidget(self.cb_language,3,1,1,3)
        grp_voice.setMaximumWidth(280)

        # Change settings
        grp_set=QtWidgets.QGroupBox(""); s=QtWidgets.QGridLayout(grp_set); s.setHorizontalSpacing(8)
        self.cb_change=QtWidgets.QCheckBox("Change voice settings"); self.cb_change.setChecked(self.s.change_settings); s.addWidget(self.cb_change,0,0,1,3)
        self.cb_boost=QtWidgets.QCheckBox("Speaker Boost"); self.cb_boost.setChecked(self.s.speaker_boost); s.addWidget(self.cb_boost,0,3,1,2)
        s.addWidget(QtWidgets.QLabel("Speed:"),1,0); self.sb_speed=QtWidgets.QDoubleSpinBox(); self.sb_speed.setRange(0.5,2.0); self.sb_speed.setValue(self.s.speed); self.sb_speed.setSingleStep(0.05); s.addWidget(self.sb_speed,1,1)
        s.addWidget(QtWidgets.QLabel("Style:"),1,2); self.sb_style=QtWidgets.QSpinBox(); self.sb_style.setRange(0,100); self.sb_style.setValue(self.s.style); self.sb_style.setSuffix(" %"); s.addWidget(self.sb_style,1,3)
        s.addWidget(QtWidgets.QLabel("Stability:"),2,0)
        self.cb_stab=QtWidgets.QComboBox(); self.cb_stab.addItems(["0%", "50%", "100%"])
        # Map saved stability value to combo index
        stab_val = self.s.stability
        if stab_val <= 25: self.cb_stab.setCurrentIndex(0)
        elif stab_val <= 75: self.cb_stab.setCurrentIndex(1)
        else: self.cb_stab.setCurrentIndex(2)
        self.cb_stab.setToolTip("API chỉ hỗ trợ 3 giá trị: 0%, 50%, 100%")
        s.addWidget(self.cb_stab,2,1)
        s.addWidget(QtWidgets.QLabel("Similarity:"),2,2); self.sb_sim=QtWidgets.QSpinBox(); self.sb_sim.setRange(0,100); self.sb_sim.setValue(self.s.similarity); self.sb_sim.setSuffix(" %"); s.addWidget(self.sb_sim,2,3)
        self.bt_reset=QtWidgets.QPushButton("Reset"); s.addWidget(self.bt_reset,1,4)
        self.bt_load=QtWidgets.QPushButton("Load"); s.addWidget(self.bt_load,2,4)
        grp_set.setMaximumWidth(320)

        # Options
        grp_opt=QtWidgets.QGroupBox("Options"); o=QtWidgets.QGridLayout(grp_opt)
        self.cb_loop=QtWidgets.QCheckBox("Loop"); self.cb_loop.setChecked(self.s.loop)
        self.cb_loop.setVisible(False)  # Ẩn
        o.addWidget(self.cb_loop,0,0)
        # Thread - căn trái
        th=QtWidgets.QHBoxLayout()
        th.addWidget(QtWidgets.QLabel("Thread:"))
        self.sb_thread=QtWidgets.QSpinBox(); self.sb_thread.setRange(5,10); self.sb_thread.setValue(min(max(self.s.thread_count, 5), 10))
        th.addWidget(self.sb_thread)
        th.addStretch(1)  # Đẩy sang trái
        o.addLayout(th,0,0,1,3)  # Đặt ở vị trí (0,0) span 3 cột
        self.cb_split=QtWidgets.QCheckBox("Auto Split"); self.cb_split.setChecked(False)  # Mặc định False
        self.ed_split=QtWidgets.QLineEdit(self.s.split_chars or ". ! ?"); self.ed_split.setFixedWidth(60)
        self.ed_split.setToolTip("Dấu ngắt khi bật Auto Split (VD: . ! ?)")
        self.cb_split.setVisible(False)  # Ẩn
        self.ed_split.setVisible(False)  # Ẩn
        o.addWidget(self.cb_split,1,0); o.addWidget(self.ed_split,1,1,1,2)
        self.bt_adv=QtWidgets.QPushButton("Cài đặt nâng cao"); o.addWidget(self.bt_adv,1,0,1,3)  # Đặt ở row 1
        grp_opt.setMaximumWidth(200)

        # Top row
        top=QtWidgets.QHBoxLayout(); top.addWidget(grp_voice,4); top.addWidget(grp_set,4); top.addWidget(grp_opt,2); root.addLayout(top,0,0,1,2)

        grp_b = QtWidgets.QGroupBox("Batch Job")
        b_layout = QtWidgets.QHBoxLayout(grp_b)
        
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 5, 0)
        
        path_row = QtWidgets.QHBoxLayout()
        self.lbl_path = QtWidgets.QLabel("Đường Dẫn:")
        self.lbl_path.setFixedWidth(60)
        path_row.addWidget(self.lbl_path)
        self.ed_folder = QtWidgets.QLineEdit()
        self.ed_folder.setPlaceholderText("Chọn file .txt hoặc thư mục...")
        path_row.addWidget(self.ed_folder)
        left_layout.addLayout(path_row)
        
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(4)
        btn_row.addSpacing(60)
        
        self.bt_browse_file = QtWidgets.QPushButton("📄 File")
        self.bt_browse_folder = QtWidgets.QPushButton("📁 Folder")
        self.bt_browse_srt = QtWidgets.QPushButton("📜 SRT")
        self.bt_browse_file.setToolTip("Chọn 1 file .txt")
        self.bt_browse_folder.setToolTip("Chọn thư mục chứa file .txt")
        self.bt_browse_srt.setToolTip("Chọn file .srt để gen voice")
        btn_row.addWidget(self.bt_browse_file, 1)
        btn_row.addWidget(self.bt_browse_folder, 1)
        btn_row.addWidget(self.bt_browse_srt, 1)
        left_layout.addLayout(btn_row)
        
        result_row = QtWidgets.QHBoxLayout()
        result_row.addSpacing(60)
        
        self.cb_autosrt = QtWidgets.QCheckBox("Tự động tạo Srt")
        result_row.addWidget(self.cb_autosrt, 1)
        
        self.lbl_result = QtWidgets.QLabel("Kết Quả: 0/0")
        self.lbl_result.setStyleSheet("""
            QLabel {
                border: 1px solid #999;
                border-radius: 3px;
                padding: 2px 6px;
                background-color: #f5f5f5;
            }
        """)
        self.lbl_result.setAlignment(Qt.AlignCenter)
        result_row.addWidget(self.lbl_result, 1)
        
        result_row.addStretch(1)
        
        left_layout.addLayout(result_row)
        left_layout.addStretch(1)
        
        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 0, 0, 0)
        
        self.tbl_queue = QtWidgets.QTableWidget(0, 4)
        self.tbl_queue.setHorizontalHeaderLabels(["ID", "FileName", "Status", "Tiến độ"])
        hdr = self.tbl_queue.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(2, 100)  # Status
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(3, 100)
        self.tbl_queue.verticalHeader().setVisible(False)
        small_font = QtGui.QFont(); small_font.setPointSize(8)
        self.tbl_queue.setFont(small_font)
        self.tbl_queue.verticalHeader().setDefaultSectionSize(20)
        right_layout.addWidget(self.tbl_queue)
        
        b_layout.addWidget(left_widget, 35)
        b_layout.addWidget(right_widget, 65)
        
        grp_b.setFixedHeight(130)
        root.addWidget(grp_b, 1, 0, 1, 2)

        # Subtitles
        grp_s=QtWidgets.QGroupBox("Subtitles"); vs=QtWidgets.QVBoxLayout(grp_s)
        tb=QtWidgets.QHBoxLayout(); self.bt_start=QtWidgets.QPushButton("Start"); self.bt_stop=QtWidgets.QPushButton("Stop"); self.bt_more=QtWidgets.QPushButton("📁 Output")
        self.bt_stop.setEnabled(False)
        for w in [self.bt_start,self.bt_stop,self.bt_more]: tb.addWidget(w)
        tb.addStretch(1); vs.addLayout(tb)
        self.tbl_sub=QtWidgets.QTableWidget(0,7); self.tbl_sub.setHorizontalHeaderLabels(["ID","Output","Timing","Chars","Content","Voice #","Status"])
        self.tbl_sub.verticalHeader().setVisible(False)
        self.tbl_sub.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_sub.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        hdr_sub = self.tbl_sub.horizontalHeader()
        hdr_sub.setSectionResizeMode(0, QHeaderView.Fixed); self.tbl_sub.setColumnWidth(0, 40)   # Id
        hdr_sub.setSectionResizeMode(1, QHeaderView.Fixed); self.tbl_sub.setColumnWidth(1, 80)   # Output
        hdr_sub.setSectionResizeMode(2, QHeaderView.Fixed); self.tbl_sub.setColumnWidth(2, 80)   # Timing
        hdr_sub.setSectionResizeMode(3, QHeaderView.Fixed); self.tbl_sub.setColumnWidth(3, 50)   # Chars
        self.tbl_sub.setColumnHidden(3, True)  # Ẩn cột Chars
        hdr_sub.setSectionResizeMode(4, QHeaderView.Stretch)                                     # Content
        hdr_sub.setSectionResizeMode(5, QHeaderView.Fixed); self.tbl_sub.setColumnWidth(5, 70)   # Voice #
        hdr_sub.setSectionResizeMode(6, QHeaderView.Fixed); self.tbl_sub.setColumnWidth(6, 80)   # Status
        # Double-click để mở file/folder
        self.tbl_sub.cellDoubleClicked.connect(self._on_subtitle_cell_double_clicked)
        vs.addWidget(self.tbl_sub)
        root.addWidget(grp_s,2,0,1,2); root.setRowStretch(2,1)

        self.log_box=QtWidgets.QPlainTextEdit(); self.log_box.setReadOnly(True); self.log_box.setVisible(False)
        fmono=QtGui.QFont("Consolas, Courier New, monospace"); fmono.setPointSize(9)

        # Status bar + credits label + character counter
        self.status=QtWidgets.QStatusBar(); self.setStatusBar(self.status)
        self.lbl_status=QtWidgets.QLabel("|Sẵn Sàng|"); self.lbl_status.setFont(fmono)
        self.lbl_char_counter=QtWidgets.QLabel(f"Đã Dùng: {self.s.char_counter:,}"); self.lbl_char_counter.setFont(fmono)
        
        # 🐛 FIX: Tên tài khoản và thông tin gói cùng hàng bên trái
        # Font size lớn hơn và màu dễ nhìn hơn
        font_large = QtGui.QFont("Arial", 11, QtGui.QFont.Bold)  # Tăng size từ 9 lên 11 và bold
        
        # Tên tài khoản
        username_text = getattr(self, 'current_username', 'Guest') if hasattr(self, 'current_username') and self.current_username else 'Guest'
        self.lbl_username = QtWidgets.QLabel(f"👤 Tài khoản: {username_text}")
        self.lbl_username.setFont(font_large)
        self.lbl_username.setStyleSheet("color: #007bff; padding: 0 8px;")  # Màu xanh dương, padding
        
        # Thông tin gói (subscription)
        self.lbl_subscription = QtWidgets.QLabel("📦 Gói: --- | ⏰ Hạn: --- | 🔤 Ký tự: ---")
        self.lbl_subscription.setFont(font_large)
        self.lbl_subscription.setStyleSheet("color: #28a745; padding: 0 8px;")  # Màu xanh lá
        
        # Ẩn lbl_status và lbl_char_counter
        self.lbl_status.setVisible(False)
        self.lbl_char_counter.setVisible(False)
        
        # Thêm vào status bar: username và subscription cùng hàng bên trái
        self.status.addPermanentWidget(self.lbl_username, 0)
        self.status.addPermanentWidget(self.lbl_subscription, 0)

        # signals
        self.bt_adv.clicked.connect(self.open_adv)
        self.bt_browse_file.clicked.connect(self.pick_file)
        self.bt_browse_folder.clicked.connect(self.pick_folder)
        self.bt_browse_srt.clicked.connect(self.pick_srt)
        self.bt_load.clicked.connect(self.check_credits_async)
        self.bt_reset.clicked.connect(self.reset_all_settings)
        self.bt_search.clicked.connect(self.search_voice)
        self.bt_save_voice.clicked.connect(self.save_voice_choice)
        self.bt_start.clicked.connect(self.start_queue_sequential)
        self.bt_stop.clicked.connect(self.stop_all)
        self.bt_more.clicked.connect(self.open_output_folder)
        self.cb_model.currentIndexChanged.connect(self._save_model_choice)
        self.cb_voice.currentIndexChanged.connect(self._save_voice_choice)
        self.cb_language.currentIndexChanged.connect(self._save_language_choice)
        
        self._path_debounce_timer = QtCore.QTimer()
        self._path_debounce_timer.setSingleShot(True)
        self._path_debounce_timer.timeout.connect(self._load_files_from_path)
        self.ed_folder.textChanged.connect(self._on_path_text_changed)
        
        self.cb_change.stateChanged.connect(self._save_all_settings)
        self.cb_boost.stateChanged.connect(self._save_all_settings)
        self.sb_speed.valueChanged.connect(self._save_all_settings)
        self.sb_style.valueChanged.connect(self._save_all_settings)
        self.cb_stab.currentIndexChanged.connect(self._save_all_settings)
        self.sb_sim.valueChanged.connect(self._save_all_settings)
        self.cb_loop.stateChanged.connect(self._save_all_settings)
        self.cb_split.stateChanged.connect(self._on_split_settings_changed)
        self.ed_split.editingFinished.connect(self._on_split_settings_changed)
        self.sb_thread.valueChanged.connect(self._save_all_settings)
        
        if self.s.last_search_text:
            self.ed_name.setText(self.s.last_search_text)
        
        # Load all default voices
        self._load_default_voices()
        
        # Select saved voice if exists
        if self.s.last_voice_id:
            for i in range(self.cb_voice.count()):
                if self.cb_voice.itemData(i) == self.s.last_voice_id:
                    self.cb_voice.setCurrentIndex(i)
                    # Set voice_id vào textbox Name
                    self.ed_name.setText(self.s.last_voice_id)
                    break
        
        # Load auto SRT checkbox state
        self.cb_autosrt.setChecked(self.s.auto_srt_enabled)
        self.cb_autosrt.stateChanged.connect(self._on_autosrt_changed)
        
        self.log(f"Current dir: {os.getcwd()}")
        self.log("🚀 Code Version: 2025.12.30 - Connection Fix Applied (Reduce Delay + Session Refresh)")
        
        QtCore.QTimer.singleShot(1500, self._auto_check_credits_on_start)
        
        # ========== Auto Update Check ==========
        QtCore.QTimer.singleShot(3000, self._auto_check_for_updates)
        
        # ========== Setup Multiple Voice Tab System (NEW) ==========
        try:
            from ui.qt_tab_multiple_voice import setup_tab_system
            setup_tab_system(self)
        except Exception as e:
            print(f"⚠️ [TAB_SYSTEM] Failed to setup: {e}")
            import traceback
            traceback.print_exc()
        
    # ========== Sync keys từ DB vào KeyManager local ==========
    def _sync_keys_from_db(self):
        """Sync keys từ LocalKeyPool (DB) vào KeyManager local"""
        if not hasattr(self, 'key_pool_db') or not self.key_pool_db:
            return
        
        # Clear existing keys
        self.keys.keys = []
        self.keys.credits_cache = {}
        self.keys.cooldowns = {}
        
        # Copy keys từ DB pool
        for entry in self.key_pool_db._keys:
            if entry.is_active and entry.api_key:
                self.keys.keys.append(entry.api_key)
                self.keys.credits_cache[entry.api_key] = entry.credit_remaining
        
        # Sort by credits descending (highest first)
        self.keys.keys.sort(key=lambda k: self.keys.credits_cache.get(k, 0), reverse=True)
        
        total = sum(self.keys.credits_cache.values())
        self.log(f"[Keys] Synced {len(self.keys.keys)} keys from DB, total credits: {total:,}")
    
    # ========== Sync proxy từ DB vào ProxyManager local ==========
    def _sync_proxy_from_db(self):
        """Sync proxy từ ProxyServiceDB vào ProxyManager local"""
        if not hasattr(self, 'proxy_service_db') or not self.proxy_service_db:
            self.log("[Proxy] ⚠️ proxy_service_db not available")
            return
        
        # 🔧 NEW: Link ProxyManager to ProxyServiceDB for retry logic
        self.proxies.set_proxy_service_db(self.proxy_service_db)
        
        # 🔧 FIX: Check proxy_service_db._proxies trực tiếp để xem có proxy không
        proxy_info = self.proxy_service_db.get_proxy_info()
        total_proxies = proxy_info.get('total', 0)
        
        if total_proxies > 0:
            # CÓ PROXY trong DB - enable proxy manager
            proxy_type = proxy_info.get('current_type', 'unknown')
            proxy_label = proxy_info.get('current_label', '')
            
            self.log(f"[Proxy] Found {total_proxies} proxy(s) in DB")
            self.log(f"[Proxy] Type={proxy_type}, Label={proxy_label}")
            
            # Get current proxy URL
            proxy_url = self.proxy_service_db.get_current_proxy()
            
            if proxy_url:
                # Proxy URL sẵn có (regular proxy hoặc proxyxoay đã cache)
                self.proxies.enabled = True
                self.proxies._proxies = [proxy_url]
                self.proxies._idx = 0
                self.proxies._is_rotating = proxy_type == 'proxyxoay'
                
                display_url = proxy_url[:50] + "..." if len(proxy_url) > 50 else proxy_url
                self.log(f"[Proxy] ✅ Synced from DB: {display_url}")
            else:
                # Proxy URL chưa có (có thể là proxyxoay cần fetch)
                # Vẫn enable để TTS worker sẽ tự fetch khi cần
                self.proxies.enabled = True
                self.proxies._proxies = []  # Empty - sẽ được fetch bởi worker
                self.proxies._idx = 0
                self.proxies._is_rotating = proxy_type == 'proxyxoay'
                
                self.log(f"[Proxy] ⏳ Proxy configured but URL pending (will fetch on demand)")
                
                # Thử fetch ngay bây giờ
                if proxy_type == 'proxyxoay':
                    self.log("[Proxy] 🔄 Trying to fetch proxyxoay now...")
                    proxy_url = self.proxy_service_db.force_refresh()
                    if proxy_url:
                        self.proxies._proxies = [proxy_url]
                        self.log(f"[Proxy] ✅ Fetched: {proxy_url[:50]}...")
        else:
            # Thử load lại từ DB
            self.log("[Proxy] ⚠️ No proxy found, trying to reload from DB...")
            count = self.proxy_service_db.load_from_database()
            
            if count > 0:
                proxy_url = self.proxy_service_db.get_current_proxy()
                if proxy_url:
                    self.proxies.enabled = True
                    self.proxies._proxies = [proxy_url]
                    self.proxies._idx = 0
                    self.log(f"[Proxy] ✅ Loaded after reload: {proxy_url[:50]}...")
                else:
                    # Có config nhưng chưa có URL - vẫn enable
                    self.proxies.enabled = True
                    self.proxies._proxies = []
                    self.log(f"[Proxy] ⏳ {count} proxy config(s) loaded, URL pending")
            else:
                self.log("[Proxy] ❌ No proxy configured in DB")
                self.proxies.enabled = False

    # ========== SUBSCRIPTION MONITORING ==========
    def _check_subscription_active(self) -> bool:
        """
        Check if user has active subscription.
        Kiểm tra:
        - is_active = True
        - end_date chưa hết hạn  
        - count_characters > 0 (nếu có)
        
        Returns: True nếu subscription còn hợp lệ
        """
        try:
            if not hasattr(self, 'current_user_id') or not self.current_user_id:
                self.log("⚠️ [Subscription] No user logged in")
                return False
            
            if not hasattr(self, 'supabase') or not self.supabase:
                self.log("⚠️ [Subscription] No database connection")
                return False
            
            result = self.supabase.table('user_subscriptions')\
                .select('is_active, end_date, count_characters, subscription_type')\
                .eq('user_id', self.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                self.log("❌ [Subscription] No active subscription found")
                return False
            
            sub = result.data[0]
            
            # Check if subscription has expired
            if sub.get('end_date'):
                from datetime import datetime, timezone
                try:
                    end_date_str = sub['end_date']
                    if end_date_str.endswith('Z'):
                        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    elif '+' in end_date_str or '-' in end_date_str[-6:]:
                        end_date = datetime.fromisoformat(end_date_str)
                    else:
                        end_date = datetime.fromisoformat(end_date_str)
                    
                    # Compare với UTC time
                    now = datetime.now(timezone.utc) if end_date.tzinfo else datetime.now()
                    if now > end_date:
                        self.log(f"❌ [Subscription] Expired: {end_date}")
                        return False
                except Exception as e:
                    self.log(f"⚠️ [Subscription] Error parsing end_date: {e}")
            
            # Check character-based quota
            try:
                count_chars = sub.get('count_characters')
                if count_chars is not None and int(count_chars) <= 0:
                    self.log("❌ [Subscription] No remaining characters (count_characters <= 0)")
                    return False
            except Exception:
                pass
            
            self.log(f"✅ [Subscription] Active: {sub.get('subscription_type', 'unknown')}")
            return True
            
        except Exception as e:
            self.log(f"❌ [Subscription] Check error: {e}")
            return False
    
    def _load_subscription_info(self) -> dict:
        """
        Load subscription info từ database.
        Returns dict với thông tin gói hoặc {} nếu lỗi.
        """
        try:
            if not hasattr(self, 'current_user_id') or not self.current_user_id:
                return {}
            
            if not hasattr(self, 'supabase') or not self.supabase:
                return {}
            
            result = self.supabase.table('user_subscriptions')\
                .select('subscription_type, end_date, is_active, count_characters, max_videos_per_day')\
                .eq('user_id', self.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                return {'status': 'no_subscription'}
            
            sub = result.data[0]
            
            # Calculate days remaining
            days_remaining = None
            if sub.get('end_date'):
                from datetime import datetime, timezone
                try:
                    end_str = sub['end_date']
                    if end_str.endswith('Z'):
                        end_date = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    else:
                        end_date = datetime.fromisoformat(end_str)
                    
                    now = datetime.now(timezone.utc) if end_date.tzinfo else datetime.now()
                    days_remaining = max(0, (end_date - now).days)
                except:
                    pass
            
            return {
                'type': sub.get('subscription_type', 'unknown'),
                'end_date': sub.get('end_date'),
                'days_remaining': days_remaining,
                'is_active': sub.get('is_active', False),
                'count_characters': sub.get('count_characters'),
                'max_videos_per_day': sub.get('max_videos_per_day'),
                'status': 'active' if sub.get('is_active') and (days_remaining is None or days_remaining > 0) else 'expired'
            }
        except Exception as e:
            self.log(f"❌ [Subscription] Load error: {e}")
            return {'error': str(e)}
    
    def _update_subscription_label(self):
        """Update subscription label và username trên status bar"""
        try:
            # 🐛 FIX: Cập nhật tên tài khoản với label rõ ràng
            if hasattr(self, 'lbl_username') and hasattr(self, 'current_username') and self.current_username:
                username_text = self.current_username
                self.lbl_username.setText(f"👤 Tài khoản: {username_text}")
                self.lbl_username.setStyleSheet("color: #007bff; padding: 0 8px; font-weight: bold;")
            elif hasattr(self, 'lbl_username'):
                self.lbl_username.setText("👤 Tài khoản: Guest")
                self.lbl_username.setStyleSheet("color: #6c757d; padding: 0 8px; font-weight: bold;")
            
            # Load subscription info
            info = self._load_subscription_info()
            
            if not info or info.get('status') == 'no_subscription':
                self.lbl_subscription.setText("📦 Gói: Chưa có")
                self.lbl_subscription.setStyleSheet("color: #ff6b6b; padding: 0 8px; font-weight: bold;")
                return
            
            if info.get('error'):
                self.lbl_subscription.setText("📦 Gói: Lỗi")
                self.lbl_subscription.setStyleSheet("color: #ff6b6b; padding: 0 8px; font-weight: bold;")
                return
            
            sub_type = info.get('type', 'unknown').upper()
            days_left = info.get('days_remaining')
            count_chars = info.get('count_characters')
            
            # Format display text với label rõ ràng
            if days_left is not None:
                if days_left <= 0:
                    text = f"📦 Gói: {sub_type} | ⏰ Hạn: Hết hạn!"
                    color = "#ff6b6b"  # Red
                elif days_left <= 7:
                    text = f"📦 Gói: {sub_type} | ⏰ Hạn: {days_left} ngày"
                    color = "#ffc107"  # Yellow warning
                else:
                    text = f"📦 Gói: {sub_type} | ⏰ Hạn: {days_left} ngày"
                    color = "#28a745"  # Green
            else:
                text = f"📦 Gói: {sub_type}"
                color = "#28a745"
            
            # Add character count if available với label rõ ràng
            if count_chars is not None:
                if count_chars <= 0:
                    text += " | 🔤 Ký tự: Hết!"
                    color = "#ff6b6b"
                else:
                    # Chia 1000 để hiển thị gọn hơn
                    credits_display = count_chars // 1000
                    text += f" | 🔤 Ký tự: {credits_display:,}"
                    if count_chars < 10000:
                        color = "#ffc107" if color != "#ff6b6b" else color
            
            # 🐛 FIX: Sử dụng font lớn hơn và màu đã định nghĩa với padding
            self.lbl_subscription.setText(text)
            self.lbl_subscription.setStyleSheet(f"color: {color}; padding: 0 8px; font-weight: bold;")
            
        except Exception as e:
            self.log(f"❌ [Subscription] Update label error: {e}")
            if hasattr(self, 'lbl_subscription'):
                self.lbl_subscription.setText("📦 Gói: ---")
                self.lbl_subscription.setStyleSheet("color: #6c757d; padding: 0 8px; font-weight: bold;")
    
    def _check_subscription_credits_before_start(self) -> bool:
        """
        🔧 NEW: Kiểm tra subscription credits trước khi bắt đầu TTS.
        Returns: True nếu còn credits, False nếu hết.
        """
        try:
            if not hasattr(self, 'current_user_id') or not self.current_user_id:
                return True  # Không có user_id, bỏ qua kiểm tra
            
            if not hasattr(self, 'supabase') or not self.supabase:
                return True  # Không có supabase, bỏ qua kiểm tra
            
            # Get current subscription
            result = self.supabase.table('user_subscriptions')\
                .select('id, count_characters, plan_name')\
                .eq('user_id', self.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                return True  # Không có subscription, bỏ qua kiểm tra
            
            sub = result.data[0]
            current_chars = sub.get('count_characters')
            plan_name = sub.get('plan_name', 'Unknown')
            
            if current_chars is None:
                return True  # Không track characters, bỏ qua kiểm tra
            
            if current_chars <= 0:
                # Hết credits - hiện popup và không cho bắt đầu
                self.log(f"❌ [Subscription] Hết credits! Gói {plan_name} còn {current_chars:,} ký tự")
                QtWidgets.QMessageBox.warning(
                    self, 
                    "Hết Credits Gói",
                    f"❌ Gói {plan_name} đã hết credits!\n\n"
                    f"Số ký tự còn lại: {current_chars:,}\n\n"
                    "Vui lòng nâng cấp gói hoặc liên hệ admin để nạp thêm."
                )
                return False
            
            # Còn credits - cho phép bắt đầu
            self.log(f"✅ [Subscription] Gói {plan_name} còn {current_chars:,} ký tự")
            return True
            
        except Exception as e:
            self.log(f"⚠️ [Subscription] Check credits error: {e}")
            return True  # Lỗi thì cho phép tiếp tục
    
    def _deduct_subscription_characters(self, chars_used: int):
        """
        Trừ số ký tự đã dùng từ subscription (count_characters).
        Gọi sau khi TTS thành công.
        
        Sử dụng optimistic locking với retry để tránh race condition
        (giống với AccurateCreditTracker trong ui/main_window.py).
        """
        if chars_used <= 0:
            return
        
        try:
            if not hasattr(self, 'current_user_id') or not self.current_user_id:
                return
            
            if not hasattr(self, 'supabase') or not self.supabase:
                return
            
            # Optimistic locking với retry (tối đa 3 lần)
            max_retries = 3
            for attempt in range(max_retries):
                # Get current subscription
                result = self.supabase.table('user_subscriptions')\
                    .select('id, count_characters')\
                    .eq('user_id', self.current_user_id)\
                    .eq('is_active', True)\
                    .order('created_at', desc=True)\
                    .limit(1)\
                    .execute()
                
                if not result.data:
                    return
                
                sub = result.data[0]
                sub_id = sub['id']
                current_chars = sub.get('count_characters')
                
                if current_chars is None:
                    # No character limit tracking
                    return
                
                # 🔧 NEW: Kiểm tra nếu hết credits
                if current_chars <= 0:
                    self.log(f"❌ [Subscription] Hết credits! (count_characters = {current_chars})")
                    # 🔧 FIX: Chỉ emit signal nếu chưa hiện popup
                    if not getattr(self, '_subscription_exhausted_shown', False):
                        QtCore.QTimer.singleShot(100, self._on_subscription_credits_exhausted)
                    return
                
                # Calculate new value
                new_chars = max(0, int(current_chars) - chars_used)
                
                # Update với điều kiện: chỉ update nếu count_characters vẫn còn giá trị cũ
                # (optimistic locking - tránh race condition)
                update_result = self.supabase.table('user_subscriptions')\
                    .update({'count_characters': new_chars})\
                    .eq('id', sub_id)\
                    .eq('count_characters', current_chars)\
                    .execute()
                
                # Kiểm tra xem có update thành công không
                if update_result.data:
                    # Update thành công
                    self.log(f"📝 [Subscription] Deducted {chars_used:,} chars → {new_chars:,} remaining")
                    # Update UI
                    QtCore.QTimer.singleShot(100, self._update_subscription_label)
                    
                    # 🔧 NEW: Kiểm tra nếu sắp hết credits (< 1000)
                    if new_chars <= 0:
                        self.log(f"❌ [Subscription] Đã hết credits sau khi trừ!")
                        # 🔧 FIX: Chỉ emit signal nếu chưa hiện popup
                        if not getattr(self, '_subscription_exhausted_shown', False):
                            QtCore.QTimer.singleShot(100, self._on_subscription_credits_exhausted)
                    elif new_chars < 1000:
                        self.log(f"⚠️ [Subscription] Sắp hết credits! Còn {new_chars:,} ký tự")
                    return
                else:
                    # Conflict - giá trị đã thay đổi, retry
                    if attempt < max_retries - 1:
                        self.log(f"⚠️ [Subscription] Retry {attempt + 1}/{max_retries} (conflict detected)")
                        time.sleep(0.1)  # Đợi ngắn trước khi retry
                        continue
                    else:
                        # Đã retry hết, log warning nhưng không throw error
                        self.log(f"⚠️ [Subscription] Failed to deduct after {max_retries} retries (concurrent update)")
                        return
            
        except Exception as e:
            err_str = str(e).lower()
            # 🔧 FIX: Retry nếu là connection error
            if "resource temporarily unavailable" in err_str or "connection" in err_str or "timeout" in err_str:
                # Retry 1 lần sau 0.5s
                try:
                    time.sleep(0.5)
                    # Simple retry - chỉ log, không block
                    self.log(f"⚠️ [Subscription] Deduct retry after connection error...")
                except:
                    pass
            else:
                self.log(f"⚠️ [Subscription] Deduct error: {e}")
    
    def _on_subscription_credits_exhausted(self):
        """🔧 NEW: Được gọi khi hết subscription credits từ DB - dừng tất cả và hiện popup"""
        # Tránh gọi nhiều lần - dùng lock để thread-safe
        if not hasattr(self, '_subscription_exhausted_lock'):
            self._subscription_exhausted_lock = threading.Lock()
        
        with self._subscription_exhausted_lock:
            if hasattr(self, '_subscription_exhausted_shown') and self._subscription_exhausted_shown:
                return
            self._subscription_exhausted_shown = True
        
        self.log("❌ HẾT SUBSCRIPTION CREDITS! Dừng tất cả workers...")
        
        # Dừng tất cả workers NGAY LẬP TỨC
        self.stop_requested = True
        if hasattr(self, '_active_workers'):
            for w in self._active_workers:
                try:
                    w.stop()
                except:
                    pass
        
        # Hiện popup thông báo (chỉ 1 lần)
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle("Hết Credits Gói")
        dlg.setIcon(QtWidgets.QMessageBox.Warning)
        dlg.setText("❌ Đã hết credits gói subscription!\n\nSố ký tự trong gói của bạn đã hết.\nVui lòng nâng cấp gói hoặc liên hệ admin để nạp thêm.")
        dlg.setStandardButtons(QtWidgets.QMessageBox.Ok)
        dlg.exec()
        
        # Reset UI
        self.bt_start.setEnabled(True)
        self.bt_start.setStyleSheet("")
        self.bt_stop.setEnabled(False)
        self.sb_thread.setEnabled(True)
        
        # KHÔNG reset flag ở đây - giữ True để tránh popup lại
        # self._subscription_exhausted_shown = False

    # ========== REMOVED: Menu bar methods ==========
    def _create_menu_bar_DISABLED(self):
        """Create menu bar với logout option"""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        # Logout action
        logout_action = QtGui.QAction("Đăng xuất", self)
        logout_action.setShortcut("Ctrl+L")
        logout_action.triggered.connect(self._logout)
        file_menu.addAction(logout_action)
        
        file_menu.addSeparator()
        
        # Exit action
        exit_action = QtGui.QAction("Thoát", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        # About action
        about_action = QtGui.QAction("Thông tin", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        
        # Show current user in status bar
        if self.current_user_id:
            self.lbl_status.setText(f"|User ID: {self.current_user_id}|")
    
    def _logout(self):
        """Logout và restart app"""
        reply = QtWidgets.QMessageBox.question(
            self,
            "Đăng xuất",
            "Bạn có chắc muốn đăng xuất?\nApp sẽ khởi động lại.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )
        
        if reply == QtWidgets.QMessageBox.Yes:
            self.log(f"👋 User {self.current_user_id} đăng xuất")
            # Restart app
            import sys
            QtWidgets.QApplication.quit()
            os.execv(sys.executable, ['python'] + sys.argv)
    
    def _show_about(self):
        """Show about dialog"""
        msg = (
            f"<h3>Huy Việt Elevenlabs v2.12</h3>"
            f"<p>ElevenLabs TTS Tool</p>"
            f"<p><b>User ID:</b> {self.current_user_id or 'Standalone'}</p>"
            f"<p><b>Proxy:</b> {self.proxies.enabled if hasattr(self.proxies, 'enabled') else 'N/A'}</p>"
        )
        QtWidgets.QMessageBox.about(self, "Thông tin", msg)

    # ---------- helpers ----------
    def log(self, text: str):
        if not getattr(sys, 'frozen', False):
            print(f"[LOG] {text}")
        log_to_file(text)

    def reset_all_settings(self):
        """Reset Options, Advance Settings, Change voice settings, Proxy Settings về mặc định.
        GIỮ NGUYÊN char_counter (Đếm Ký Tự Đã Dùng).
        """
        saved_char_counter = self.s.char_counter
        
        default = AppSettings()
        
        # === RESET CHANGE VOICE SETTINGS ===
        self.s.change_settings = default.change_settings
        self.s.speed = default.speed
        self.s.style = default.style
        self.s.stability = default.stability
        self.s.similarity = default.similarity
        self.s.speaker_boost = default.speaker_boost
        
        self.cb_change.setChecked(default.change_settings)
        self.sb_speed.setValue(default.speed)
        self.sb_style.setValue(default.style)
        self.cb_stab.setCurrentIndex(1)  # 50%
        self.sb_sim.setValue(default.similarity)
        self.cb_boost.setChecked(default.speaker_boost)
        
        # === RESET OPTIONS ===
        self.s.loop = default.loop
        self.s.auto_split = default.auto_split
        self.s.split_chars = default.split_chars
        self.s.thread_count = default.thread_count
        
        self.cb_loop.setChecked(default.loop)
        self.cb_split.setChecked(default.auto_split)
        self.ed_split.setText(default.split_chars)
        self.sb_thread.setValue(default.thread_count)
        
        # === RESET ADVANCE SETTINGS ===
        self.s.gap_segments_enabled = default.gap_segments_enabled
        self.s.gap_seconds = default.gap_seconds
        self.s.gap_every = default.gap_every
        self.s.gap_srt_enabled = default.gap_srt_enabled
        self.s.pause_char_enabled = default.pause_char_enabled
        self.s.char1 = default.char1
        self.s.char1_sec = default.char1_sec
        self.s.char2 = default.char2
        self.s.char2_sec = default.char2_sec
        self.s.sanitize = default.sanitize
        self.s.max_chars_per_line = default.max_chars_per_line
        
        # === RESET PROXY SETTINGS ===
        self.s.proxy_enabled = default.proxy_enabled
        self.s.proxy_sticky_enabled = default.proxy_sticky_enabled
        self.s.proxy_sticky_minutes = default.proxy_sticky_minutes
        self.s.request_delay = default.request_delay
        self.s.retry_401_count = default.retry_401_count
        self.s.error_400_retry_before_rotate = default.error_400_retry_before_rotate
        self.s.error_400_delay = default.error_400_delay
        
        self.s.char_counter = saved_char_counter
        
        save_settings(self.s)
        
        self.log("✅ Đã reset tất cả settings về mặc định (giữ nguyên Đếm Ký Tự)")
        QtWidgets.QMessageBox.information(self, "Reset", "Đã reset settings về mặc định!")
    
    def open_output_folder(self):
        """Mở thư mục chứa file/folder input"""
        import subprocess
        path = self.ed_folder.text().strip()
        if not path:
            QtWidgets.QMessageBox.warning(self, "Thông báo", "Chưa chọn file/folder!")
            return
        
        # File → mở thư mục chứa file, Folder → mở folder đó
        target = os.path.dirname(path) if os.path.isfile(path) else path
        if os.path.exists(target):
            subprocess.Popen(['explorer', os.path.abspath(target)] if os.name == 'nt' else ['xdg-open', target])
        else:
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"Thư mục không tồn tại:\n{target}")

    def pick_file(self):
        """Chọn 1 file .txt"""
        # 🐛 FIX: Dừng batch đang chạy trước khi chọn file mới
        if hasattr(self, 'stop_requested'):
            self.stop_requested = True
        if hasattr(self, '_active_workers') and self._active_workers:
            for worker in self._active_workers[:]:
                try:
                    if hasattr(worker, 'stop'):
                        worker.stop()
                except:
                    pass
            self._active_workers = []
        QtWidgets.QApplication.processEvents()
        
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chọn file .txt", self.s.last_folder or "",
            "Text Files (*.txt);;SRT Files (*.srt);;All Files (*)"
        )
        if path:
            self.ed_folder.setText(path)
            self._load_files_from_path()

    def pick_folder(self):
        """Chọn thư mục chứa nhiều file .txt"""
        # 🐛 FIX: Dừng batch đang chạy trước khi chọn folder mới
        if hasattr(self, 'stop_requested'):
            self.stop_requested = True
        if hasattr(self, '_active_workers') and self._active_workers:
            for worker in self._active_workers[:]:
                try:
                    if hasattr(worker, 'stop'):
                        worker.stop()
                except:
                    pass
            self._active_workers = []
        QtWidgets.QApplication.processEvents()
        
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục", "")
        if d:
            self.ed_folder.setText(d)
            self._load_files_from_path()
    
    def _centered_item(self, text: str) -> QtWidgets.QTableWidgetItem:
        """Tạo QTableWidgetItem với căn lề giữa"""
        item = QtWidgets.QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        return item
    
    def pick_srt(self):
        """Chọn file .srt để gen voice"""
        # 🐛 FIX: Dừng batch đang chạy trước khi chọn SRT mới
        if hasattr(self, 'stop_requested'):
            self.stop_requested = True
        if hasattr(self, '_active_workers') and self._active_workers:
            for worker in self._active_workers[:]:
                try:
                    if hasattr(worker, 'stop'):
                        worker.stop()
                except:
                    pass
            self._active_workers = []
        QtWidgets.QApplication.processEvents()
        
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chọn file SRT", self.s.last_folder or "",
            "SRT Files (*.srt);;All Files (*)"
        )
        if path:
            self.ed_folder.setText(path)
            self._load_files_from_path()
    
    def _on_path_text_changed(self):
        """Debounce khi text thay đổi - chờ 300ms trước khi load"""
        self._path_debounce_timer.stop()
        self._path_debounce_timer.start(300)  # 300ms debounce
    
    def _load_files_from_path(self):
        """Load và hiển thị danh sách file từ đường dẫn đã chọn"""
        path = self.ed_folder.text().strip()
        if not path or not os.path.exists(path):
            return
        
        # Clear hoàn toàn bảng và state cũ - QUAN TRỌNG: Clear TẤT CẢ state
        self.tbl_queue.setRowCount(0)
        self.tbl_sub.setRowCount(0)
        self._current_lines = []
        # 🔧 FIX: Reset counters với lock để tránh race condition
        with self._progress_lock:
            self._current_line_index = 0
            self._total_lines = 0
            self._completed_lines = 0
            self._failed_lines = 0
            self._active_worker_count = 0
        self._file_merging_in_progress = False
        self.stop_requested = False
        if hasattr(self, '_loaded_files'):
            self._loaded_files = []
        if hasattr(self, 'queue_files'):
            self.queue_files = []
        if hasattr(self, 'current_file_index'):
            self.current_file_index = 0
        if hasattr(self, '_active_workers'):
            self._active_workers = []
        if hasattr(self, '_line_retry_counts'):
            self._line_retry_counts = {}
        if hasattr(self, 'total_files'):
            self.total_files = 0
        if hasattr(self, 'completed_files'):
            self.completed_files = 0
        if hasattr(self, 'failed_files'):
            self.failed_files = 0
        if hasattr(self, '_last_highlighted_row'):
            self._last_highlighted_row = -1
        
        files = []
        if os.path.isfile(path):
            if path.lower().endswith(('.txt', '.srt')):
                files = [path]
        else:
            files = self.collect_files(path)
        total_files = len(files)
        valid_count = 0
        
        self._loaded_files = []
        
        for i, fpath in enumerate(files, start=1):
            r = self.tbl_queue.rowCount()
            self.tbl_queue.insertRow(r)
            self.tbl_queue.setItem(r, 0, self._centered_item(str(i)))
            self.tbl_queue.setItem(r, 1, QtWidgets.QTableWidgetItem(os.path.basename(fpath)))
            
            try:
                content = open(fpath, "r", encoding="utf-8", errors="ignore").read().strip()
                if not content:
                    self.tbl_queue.setItem(r, 2, self._centered_item("Skipped"))
                else:
                    self.tbl_queue.setItem(r, 2, self._centered_item("READY"))
                    valid_count += 1
                    self._loaded_files.append((fpath, content))
            except:
                self.tbl_queue.setItem(r, 2, self._centered_item("Skipped"))
            
            self.tbl_queue.setItem(r, 3, self._centered_item("0%"))
        
        self.log(f"Loaded {valid_count}/{total_files} file(s) từ: {os.path.basename(path)}")
        
        self._refresh_subtitles_table()
    
    def _refresh_subtitles_table(self, file_index: int = 0):
        """Hiển thị các dòng của 1 file TXT cụ thể trong Subtitles (theo index)"""
        self.tbl_sub.setRowCount(0)
        
        if not hasattr(self, '_loaded_files') or not self._loaded_files:
            return
        
        if file_index >= len(self._loaded_files):
            return
        
        fpath, content = self._loaded_files[file_index]
        base = os.path.splitext(os.path.basename(fpath))[0]
        
        lines = content.split('\n')
        
        line_id = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Sanitize
            if self.s.sanitize:
                line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", line)
                invalid_chars = r'[*~^#@$%&|\\<>{}[\]`]'
                line = re.sub(invalid_chars, " ", line)
                line = re.sub(r'[\U00010000-\U0010ffff]', '', line)
                line = re.sub(r' {2,}', ' ', line)
                line = line.strip()
            
            if not line:
                continue
            
            # 🔧 FIX: Set cứng max_len = 400 để tối ưu tốc độ
            max_len = 400  # Set cứng 400, không dùng self.s.max_chars_per_line
            parts = []
            remaining = line
            
            while remaining:
                if len(remaining) <= max_len:
                    parts.append(remaining)
                    break
                
                # Tìm vị trí chia tốt nhất <= max_len
                # Ưu tiên: dấu câu (. ! ?) -> dấu phẩy (,) -> space
                cut_pos = max_len
                
                # Tìm ngược từ max_len
                for i in range(max_len - 1, max(0, max_len - 100), -1):
                    if remaining[i] in '.!?':
                        cut_pos = i + 1
                        break
                else:
                    for i in range(max_len - 1, max(0, max_len - 100), -1):
                        if remaining[i] in ', ':
                            cut_pos = i + 1
                            break
                
                parts.append(remaining[:cut_pos].strip())
                remaining = remaining[cut_pos:].lstrip()
            
            # Hiển thị từng part
            for part in parts:
                line_id += 1
                r = self.tbl_sub.rowCount()
                self.tbl_sub.insertRow(r)
                
                self.tbl_sub.setItem(r, 0, self._centered_item(str(line_id)))
                self.tbl_sub.setItem(r, 1, self._centered_item(""))  # Output
                self.tbl_sub.setItem(r, 2, self._centered_item(""))  # Timing
                self.tbl_sub.setItem(r, 3, self._centered_item(str(len(part))))  # Chars
                self.tbl_sub.setItem(r, 4, QtWidgets.QTableWidgetItem(part[:200]))  # Content
                self.tbl_sub.setItem(r, 5, self._centered_item(""))  # Voice #
                self.tbl_sub.setItem(r, 6, self._centered_item("Queued"))  # Status
        
        self.log(f"Loaded {line_id} dòng từ {base}")

    def open_adv(self):
        d=AdvDialog(self)
        # prefill
        d.cb_gap_segments.setChecked(self.s.gap_segments_enabled); d.sb_gap.setValue(self.s.gap_seconds)
        d.sb_every.setValue(self.s.gap_every); d.cb_gap_srt.setChecked(self.s.gap_srt_enabled)
        d.cb_pause_char.setChecked(self.s.pause_char_enabled); d.ed_char1.setText(self.s.char1); d.sb_char1.setValue(self.s.char1_sec)
        d.ed_char2.setText(self.s.char2); d.sb_char2.setValue(self.s.char2_sec)
        d.cb_sanitize.setChecked(self.s.sanitize); d.cmb_dl.setCurrentIndex(max(0, d.cmb_dl.findText(self.s.download_type)))
        # Set giá trị max_chars_per_line (mặc định 1000)
        d.ed_max_chars.setText(str(self.s.max_chars_per_line))
        if self.s.keys_file: d.keys_path.setText(self.s.keys_file); d.keys_info.setText(f"Đã chọn: {len(self.keys.keys)} key(s)")
        if d.exec()==QtWidgets.QDialog.Accepted:
            self.s.gap_segments_enabled=d.cb_gap_segments.isChecked(); self.s.gap_seconds=float(d.sb_gap.value()); self.s.gap_every=int(d.sb_every.value())
            self.s.gap_srt_enabled=d.cb_gap_srt.isChecked(); self.s.pause_char_enabled=d.cb_pause_char.isChecked()
            self.s.char1=d.ed_char1.text() or ","; self.s.char1_sec=float(d.sb_char1.value()); self.s.char2=d.ed_char2.text() or "."; self.s.char2_sec=float(d.sb_char2.value())
            self.s.sanitize=d.cb_sanitize.isChecked(); self.s.download_type=d.cmb_dl.currentText()
            # Đọc giá trị từ QLineEdit
            try:
                max_chars_val = int(d.ed_max_chars.text() or "1000")
                self.s.max_chars_per_line = max(1, min(1000, max_chars_val))  # Clamp 1-1000
            except ValueError:
                self.s.max_chars_per_line = 1000
            if d.keys_path.text(): 
                self.s.keys_file=d.keys_path.text()
                self.s.keys_file_path=d.keys_path.text()
                n=self.keys.load(self.s.keys_file)
                self.log(f"Loaded {n} API key(s).")
            # 🐛 FIX: Chỉ update proxy settings khi user thực sự mở ProxyDialog
            # Trước đây điều kiện luôn đúng vì "_proxy_phase is not None" = True với False
            if getattr(d, '_proxy_changed', False):
                self.s.proxies_text=d._proxy_text; self.s.proxy_phase_log=bool(d._proxy_phase)
                self.s.proxy_enabled = d._proxy_enabled
                self.s.proxy_sticky_enabled = getattr(d, '_proxy_sticky_enabled', True)
                self.s.proxy_sticky_minutes = getattr(d, '_proxy_sticky_minutes', 3)
                self.s.request_delay = getattr(d, '_proxy_delay', 3.0)
                self.s.retry_401_count = getattr(d, '_proxy_retry_401', 1)
                self.s.error_400_retry_before_rotate = getattr(d, '_proxy_400_retry', 3)
                self.s.error_400_delay = getattr(d, '_proxy_400_delay', 2.0)
                self.proxies.set(self.s.proxies_text, self.s.proxy_phase_log)
                self.proxies.enabled = self.s.proxy_enabled
                status_str = "BẬT (2 Pha)" if self.s.proxy_enabled else "TẮT (Direct)"
                sticky_str = f"Sticky={self.s.proxy_sticky_enabled}/{self.s.proxy_sticky_minutes}m" if self.s.proxy_sticky_enabled else "Sticky=OFF"
                self.log(f"Proxy: {status_str} | {sticky_str} | Delay={self.s.request_delay}s | Retry401={self.s.retry_401_count} | 400_Retry={self.s.error_400_retry_before_rotate} | 400_Delay={self.s.error_400_delay}s | {len(self.proxies.list())} proxy(s)")
            save_settings(self.s)

    # ---------- ElevenLabs ----------
    def _auto_check_credits_on_start(self):
        """Tự động check credits và subscription khi mở app"""
        # Update subscription label
        if hasattr(self, 'lbl_subscription'):
            self._update_subscription_label()
        
        if self.keys.keys:
            self.log("Tự động check credits khi khởi động...")
            self.check_credits_async()
        else:
            self.log("Chưa có API keys - bỏ qua auto check credits")
    
    # ---------- Auto Update ----------
    def _auto_check_for_updates(self):
        """Check GitHub Releases cho bản cập nhật mới (chạy trong background thread)"""
        import threading
        def _check():
            try:
                from services.update_service import UpdateService
                svc = UpdateService()
                info = svc.check_for_updates()
                if info:
                    # Lưu tạm để main thread dùng
                    self._pending_update_svc = svc
                    self._pending_update_info = info
                    # Schedule trên main thread
                    QtCore.QTimer.singleShot(0, self._show_update_dialog)
            except Exception as e:
                print(f"⚠️ [UPDATE] Check error: {e}")
        threading.Thread(target=_check, daemon=True).start()
    
    def _show_update_dialog(self):
        """Hiện popup cập nhật (gọi từ main thread)"""
        try:
            svc = getattr(self, '_pending_update_svc', None)
            info = getattr(self, '_pending_update_info', None)
            if not svc or not info:
                return
            from ui.qt_update_dialog import QtUpdateDialog
            dlg = QtUpdateDialog(self, svc, info)
            dlg.exec()
        except Exception as e:
            print(f"❌ [UPDATE] Dialog error: {e}")
    
    def check_credits_async(self):
        """Check credits cho tất cả keys trong background"""
        # ========== NEW: Check from database if using DB keys ==========
        if self.current_user_id and self.key_pool_db:
            self.log(f"📊 Checking credits from database for user {self.current_user_id}...")
            
            # Get total credits từ LocalKeyPool
            try:
                with self.key_pool_db._lock:
                    total_credits = sum(k.credit_remaining for k in self.key_pool_db._keys if k.is_active)
                    active_keys = len([k for k in self.key_pool_db._keys if k.is_active])
                    total_keys = len(self.key_pool_db._keys)
                
                # Keys info removed - only show subscription
                self.log(f"✅ Credits checked: {active_keys} active keys, {total_credits:,} credits total")
                
                # Load models
                static_models = [
                    "eleven_v3",
                    "eleven_turbo_v2_5",
                    "eleven_flash_v2_5",
                    "eleven_multilingual_v2",
                    "eleven_turbo_v2",
                ]
                
                if self.cb_model.count() <= 1:
                    saved_model = self.s.last_model_id
                    self.cb_model.blockSignals(True)
                    self.cb_model.clear()
                    for mid in static_models:
                        self.cb_model.addItem(mid)
                    if saved_model:
                        idx = self.cb_model.findText(saved_model)
                        if idx >= 0:
                            self.cb_model.setCurrentIndex(idx)
                    self.cb_model.blockSignals(False)
                
                return
            except Exception as e:
                self.log(f"❌ Error checking DB credits: {e}")
                return
        
        # ========== OLD: Check from file-based keys ==========
        if not self.keys.keys:
            self.log("Chưa có API keys. Vui lòng chọn file keys trong Cài đặt nâng cao.")
            return
        
        static_models = [
            "eleven_v3",
            "eleven_turbo_v2_5",
            "eleven_flash_v2_5",
            "eleven_multilingual_v2",
            "eleven_multilingual_sts_v2",   # Speech-to-Speech multilingual
            "eleven_english_sts_v2",        # Speech-to-Speech English
            "eleven_turbo_v2",
            "eleven_flash_v2",
            "eleven_monolingual_v1",        # English only v1
            "eleven_multilingual_v1",       # Multilingual v1
        ]
        
        if self.cb_model.count() <= 1:
            saved_model = self.s.last_model_id
            self.cb_model.blockSignals(True)
            self.cb_model.clear()
            for mid in static_models:
                self.cb_model.addItem(mid)
            # Restore saved model selection
            if saved_model:
                idx = self.cb_model.findText(saved_model)
                if idx >= 0:
                    self.cb_model.setCurrentIndex(idx)
            self.cb_model.blockSignals(False)
        
        num_keys = len(self.keys.keys)
        self.log(f"Đang check credits cho {num_keys} keys (song song 20 workers)...")
        # Keys loading removed
        
        self.credits_sig = CreditsSig()
        self.credits_sig.progress.connect(self._on_credits_progress, QtCore.Qt.QueuedConnection)
        self.credits_sig.done.connect(self._on_credits_done, QtCore.Qt.QueuedConnection)
        self.credits_sig.error.connect(self._on_credits_error, QtCore.Qt.QueuedConnection)
        
        keys_path = self.s.keys_file_path if (hasattr(self.s, 'keys_file_path') and self.s.keys_file_path) else self.s.keys_file
        self.credits_worker = CreditsWorker(self.keys.keys.copy(), self.client, self.credits_sig, max_workers=20, keys_file_path=keys_path)
        
        self.pool.start(self.credits_worker)
    
    def _on_credits_progress(self, current: int, total: int, chars: int):
        self._total_credits_remaining = chars
        pass  # Keys progress removed
    
    def _on_credits_done(self, ok: int, total: int, chars: int):
        self._total_credits_remaining = chars
        pass  # Keys done removed
        self.log(f"Check xong: {ok}/{total} keys hoạt động")
    
    def _on_credits_error(self, msg: str):
        self.log(f"Credits error: {msg}")
    
    def _on_autosrt_changed(self, state: int):
        """Lưu trạng thái checkbox Tự động tạo SRT"""
        # 🔧 FIX: Reload từ file để preserve proxy settings
        temp_s = load_settings()
        temp_s.auto_srt_enabled = (state == QtCore.Qt.Checked)
        self.s.auto_srt_enabled = temp_s.auto_srt_enabled
        save_settings(temp_s)
    
    def _save_model_choice(self):
        """Lưu lựa chọn model khi thay đổi"""
        # 🔧 FIX: Reload từ file để preserve proxy settings
        temp_s = load_settings()
        temp_s.last_model_id = self.cb_model.currentText()
        self.s.last_model_id = temp_s.last_model_id
        save_settings(temp_s)
    
    def _save_voice_choice(self):
        """Lưu lựa chọn voice khi thay đổi và hiển thị voice_id lên textbox"""
        idx = self.cb_voice.currentIndex()
        if idx >= 0:
            voice_id = self.cb_voice.itemData(idx)
            display_text = self.cb_voice.currentText()
            
            print(f"[DEBUG] _save_voice_choice: idx={idx}, voice_id={voice_id}, display={display_text}")
            
            # Hiển thị voice_id lên textbox Name
            if voice_id:
                self.ed_name.setText(voice_id)
            
            # 🔧 FIX: Reload từ file để preserve proxy settings
            temp_s = load_settings()
            temp_s.last_voice_id = voice_id or ""
            temp_s.last_voice_display = display_text
            self.s.last_voice_id = temp_s.last_voice_id
            self.s.last_voice_display = temp_s.last_voice_display
            save_settings(temp_s)
    
    def _save_language_choice(self):
        """Lưu lựa chọn language khi thay đổi"""
        idx = self.cb_language.currentIndex()
        if idx >= 0:
            lang_code = self.cb_language.itemData(idx) or ""
            # 🔧 FIX: Reload từ file để preserve proxy settings
            temp_s = load_settings()
            temp_s.language_code = lang_code
            self.s.language_code = lang_code
            save_settings(temp_s)
            if lang_code:
                self.log(f"✅ Đã chọn ngôn ngữ: {self.cb_language.currentText()} (code: {lang_code})")
            else:
                self.log(f"✅ Chế độ Auto-detect ngôn ngữ")

    def search_voice(self):
        """Search voices - only default voices (no clone/shared to avoid 402 errors)"""
        q = (self.ed_name.text() or "").strip()
        
        is_voice_id = bool(q and re.fullmatch(r"[0-9a-zA-Z]{10,}", q))
        
        if not q:
            self.log("Đang tìm tất cả voice mặc định từ ElevenLabs...")
        elif is_voice_id:
            self.log(f"Đang tìm theo voice ID: {q}...")
        else:
            self.log(f"Đang tìm theo tên: {q}...")
        
        QtWidgets.QApplication.processEvents()
        
        all_found = []
        found_shared_or_clone = False
        shared_voice_name = ""
        
        try:
            self.log("Tìm trong thư viện voice mặc định...")
            QtWidgets.QApplication.processEvents()
            
            if is_voice_id:
                voices = self.client.search_voices(voice_id=q, page_size=100, max_pages=1)
            else:
                voices = self.client.search_voices(query=q, page_size=100, max_pages=3)
            
            # Check for shared/cloned voices and filter them out
            for v in voices:
                category = v.get("category", "").lower()
                voice_name = v.get("name", "?")
                
                # Check if this is a shared or cloned voice
                if category in ("cloned", "shared", "generated", "professional"):
                    found_shared_or_clone = True
                    shared_voice_name = voice_name
                    continue  # Skip this voice
                
                # Only allow premade/default voices
                if category in ("premade", "default", ""):
                    all_found.append({
                        "name": voice_name,
                        "voice_id": v.get("voice_id", ""),
                        "category": category or "default"
                    })
            
            if all_found:
                self.log(f"Tìm thấy {len(all_found)} voice mặc định")
        except Exception as e:
            self.log(f"Lỗi tìm voice: {e}")
        
        # If user searched for a specific voice ID but not found or is shared/clone
        if is_voice_id and not all_found:
            # Show warning popup
            QtWidgets.QMessageBox.warning(
                self, "Voice không hỗ trợ",
                f"Voice ID '{q}' không tìm thấy hoặc là voice shared/clone.\n\n"
                f"Loại voice này không thể sử dụng với API key thường.\n"
                f"Đã tự động chuyển sang voice mặc định 'Rachel'."
            )
            # Set default voice Rachel
            self._set_default_voice()
            return
        
        if is_voice_id and all_found:
            all_found = [v for v in all_found if v["voice_id"] == q]
            # If voice ID was found but filtered out (shared/clone)
            if not all_found:
                QtWidgets.QMessageBox.warning(
                    self, "Voice không hỗ trợ",
                    f"Voice ID '{q}' là voice shared/clone.\n\n"
                    f"Loại voice này không thể sử dụng với API key thường.\n"
                    f"Đã tự động chuyển sang voice mặc định 'Rachel'."
                )
                self._set_default_voice()
                return
        
        if all_found:
            self.cb_voice.clear()
            first_display = ""
            for i, v in enumerate(all_found):
                name = v['name'][:25] + "..." if len(v['name']) > 25 else v['name']
                vid_short = v['voice_id'][:8]
                display_text = f"{name} — {vid_short}"
                self.cb_voice.addItem(display_text, userData=v["voice_id"])
                if i == 0:
                    first_display = display_text
            
            self.log(f"Tổng cộng tìm thấy {len(all_found)} voice(s)")
            
            self.s.last_search_text = q
            if len(all_found) >= 1:
                self.s.last_voice_id = all_found[0]["voice_id"]
                self.s.last_voice_name = all_found[0]["name"]
                self.s.last_voice_display = first_display
            save_settings(self.s)
        else:
            self.log(f"Không tìm thấy voice nào với '{q}'")
    
    def _load_default_voices(self):
        """Load all default premade voices into combobox"""
        self.cb_voice.clear()
        for name, voice_id in DEFAULT_VOICES:
            display_text = f"{name} — {voice_id[:8]}"
            self.cb_voice.addItem(display_text, userData=voice_id)
    
    def _set_default_voice(self):
        """Set default voice Rachel"""
        default_voice_id = "EXAVITQu4vr4xnSDxMaL"  # Sarah
        default_voice_name = "Sarah"
        
        # Load all default voices first
        self._load_default_voices()
        
        # Select Sarah (index 1)
        self.cb_voice.setCurrentIndex(1)
        
        self.s.last_voice_id = default_voice_id
        self.s.last_voice_name = default_voice_name
        self.s.last_voice_display = f"{default_voice_name} — {default_voice_id[:8]}"
        save_settings(self.s)
        
        self.log(f"✅ Đã set voice mặc định: {default_voice_name}")

    def save_voice_choice(self):
        """Save current voice selection (called by Save button)"""
        i = self.cb_voice.currentIndex()
        if i >= 0:
            self.s.last_voice_id = self.cb_voice.itemData(i) or ""
            self.s.last_voice_name = self.cb_voice.currentText().split(" — ")[0]
            self.s.last_voice_display = self.cb_voice.currentText()
            save_settings(self.s)
            self.log(f"✅ Đã lưu voice: {self.s.last_voice_name}")
        else:
            self.log("⚠️ Chưa chọn voice để lưu")

    def _get_stability_value(self) -> int:
        """Get stability value from combo box (0, 50, or 100)."""
        idx = self.cb_stab.currentIndex()
        return [0, 50, 100][idx]

    def _save_all_settings(self):
        """Lưu tất cả settings từ UI widgets"""
        # Voice settings
        self.s.change_settings = self.cb_change.isChecked()
        self.s.speaker_boost = self.cb_boost.isChecked()
        self.s.speed = self.sb_speed.value()
        self.s.style = self.sb_style.value()
        self.s.stability = self._get_stability_value()
        self.s.similarity = self.sb_sim.value()
        
        # Options
        self.s.loop = self.cb_loop.isChecked()
        self.s.auto_split = self.cb_split.isChecked()
        self.s.split_chars = self.ed_split.text().strip() or ",.;!?"
        old_thread_count = self.s.thread_count
        self.s.thread_count = self.sb_thread.value()
        
        if old_thread_count != self.s.thread_count:
            self.log(f"[Config] Thread: {old_thread_count} → {self.s.thread_count}")
        
        save_settings(self.s)
    
    def _on_split_settings_changed(self):
        """Khi thay đổi Auto Split hoặc split_chars, lưu settings và refresh Subtitles"""
        self._save_all_settings()
        if hasattr(self, '_loaded_files') and self._loaded_files:
            self._refresh_subtitles_table()

    def generate_srt_from_mp3(self):
        """Tạo file SRT từ các file mp3 con bằng cách đo thời lượng từng file"""
        ffprobe = find_ffprobe()
        if not ffprobe:
            self.log("❌ Không tìm thấy FFprobe. Vui lòng cài FFmpeg đầy đủ.")
            return
        
        path = self.ed_folder.text().strip()
        if not path:
            self.log("❌ Chưa chọn file hoặc thư mục")
            return
        
        files_to_process = []
        if os.path.isfile(path):
            if path.lower().endswith(('.txt', '.srt')):
                files_to_process = [path]
        elif os.path.isdir(path):
            files_to_process = self.collect_files(path)
        
        if not files_to_process:
            self.log("❌ Không tìm thấy file .txt/.srt để tạo SRT")
            return
        
        srt_count = 0
        for txt_path in files_to_process:
            base = os.path.splitext(os.path.basename(txt_path))[0]
            out_dir = Path(os.path.dirname(txt_path)) / "_out" / base
            
            if not out_dir.exists():
                self.log(f"⚠ Bỏ qua {base}: không có thư mục _out/{base}")
                continue
            
            mp3_files = sorted(
                [f for f in out_dir.glob(f"{base}_*.mp3")],
                key=lambda x: int(x.stem.split("_")[-1])
            )
            
            if not mp3_files:
                self.log(f"⚠ Bỏ qua {base}: không có file mp3 con")
                continue
            
            try:
                text = read_text_or_srt(txt_path)
                text = ' '.join(text.split())
                if self.s.auto_split:
                    parts = split_by_chars(text, self.s.split_chars)
                else:
                    parts = smart_split_text(text, 400, tolerance=50)  # 🔧 FIX: Set cứng 400
            except Exception as e:
                self.log(f"⚠ Lỗi đọc file {base}: {e}")
                parts = [f"Đoạn {i+1}" for i in range(len(mp3_files))]
            
            durations = []
            for mp3 in mp3_files:
                dur = self._get_mp3_duration(ffprobe, str(mp3))
                durations.append(dur)
            
            srt_path = Path(os.path.dirname(txt_path)) / f"{base}.srt"
            self._write_srt(srt_path, parts, durations)
            
            self.log(f"✅ Đã tạo SRT: {srt_path.name} ({len(mp3_files)} đoạn)")
            srt_count += 1
            QtWidgets.QApplication.processEvents()
        
        if srt_count > 0:
            self.log(f"🎉 Hoàn thành: đã tạo {srt_count} file SRT")
        else:
            self.log("⚠ Không có file SRT nào được tạo")
    
    def _get_mp3_duration(self, ffprobe: str, mp3_path: str) -> float:
        """Lấy thời lượng file mp3 bằng ffprobe (đơn vị: giây)"""
        try:
            cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", mp3_path]
            result = run_hidden(cmd)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except:
            pass
        return 3.0
    
    def _write_srt(self, srt_path: Path, parts: List[str], durations: List[float]):
        """Ghi file SRT với timing dựa trên thời lượng thực tế"""
        import re as _re
        _AUDIO_TAG_RE = _re.compile(r'\[[^\[\]]{1,80}\]\s*')
        
        def _strip_tags(text: str) -> str:
            return _AUDIO_TAG_RE.sub('', text).strip()
        
        def format_time(seconds: float) -> str:
            """Chuyển giây thành định dạng SRT: HH:MM:SS,mmm"""
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            ms = int((seconds % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        
        current_time = 0.0
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, (part, dur) in enumerate(zip(parts, durations), start=1):
                start_time = current_time
                end_time = current_time + dur
                
                f.write(f"{i}\n")
                f.write(f"{format_time(start_time)} --> {format_time(end_time)}\n")
                f.write(f"{_strip_tags(part.strip())}\n")
                f.write("\n")
                
                current_time = end_time

    def join_selected_mp3(self):
        """Nối các file mp3 con từ thư mục _out với chèn silence mỗi N đoạn"""
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.log("❌ Không tìm thấy FFmpeg. Vui lòng cài FFmpeg hoặc đặt ffmpeg.exe vào thư mục ffmpeg_bin/")
            return
        
        path = self.ed_folder.text().strip()
        if not path:
            self.log("❌ Chưa chọn file hoặc thư mục")
            return
        
        files_to_join = []
        if os.path.isfile(path):
            files_to_join = [path]
        elif os.path.isdir(path):
            files_to_join = self.collect_files(path)
        
        if not files_to_join:
            self.log("❌ Không tìm thấy file .txt/.srt để join")
            return
        
        joined_count = 0
        for txt_path in files_to_join:
            base = os.path.splitext(os.path.basename(txt_path))[0]
            out_dir = Path(os.path.dirname(txt_path)) / "_out" / base
            
            if not out_dir.exists():
                self.log(f"⚠ Bỏ qua {base}: không có thư mục _out/{base}")
                continue
            
            mp3_files = sorted(
                [f for f in out_dir.glob("*.mp3") if f.stem.replace(base + "_", "").isdigit() or f.stem.isdigit()],
                key=lambda x: int(x.stem.replace(base + "_", "").lstrip("0") or "0")
            )
            
            if not mp3_files:
                mp3_files = sorted(
                    [f for f in out_dir.glob(f"{base}_*.mp3")],
                    key=lambda x: int(x.stem.split("_")[-1])
                )
            
            if not mp3_files:
                self.log(f"⚠ Bỏ qua {base}: không có file mp3 con")
                continue
            
            output = Path(txt_path).with_suffix(".mp3")
            
            self.log(f"🔄 Đang nối {len(mp3_files)} file → {output.name}...")
            QtWidgets.QApplication.processEvents()
            
            ok, msg = join_mp3_with_silence(
                ffmpeg=ffmpeg,
                mp3_files=mp3_files,
                output_path=output,
                gap_enabled=self.s.gap_segments_enabled,
                gap_seconds=self.s.gap_seconds,
                gap_every=self.s.gap_every
            )
            
            if ok:
                self.log(f"✅ Đã nối → {output.name}")
                joined_count += 1
            else:
                self.log(f"❌ Lỗi nối {base}: {msg}")
        
        if joined_count > 0:
            self.log(f"🎉 Hoàn thành: đã nối {joined_count} file mp3")
        else:
            self.log("⚠ Không có file nào được nối")


    def collect_files(self, folder: str) -> List[str]:
        """Thu thập các file .txt/.srt và sắp xếp theo thứ tự tự nhiên (natural sort)"""
        import re
        
        def natural_sort_key(path: str):
            """Key function cho natural sorting: 1.txt, 2.txt, 10.txt -> 1, 2, 10 (không phải 1, 10, 2)"""
            filename = os.path.basename(path)
            parts = re.split(r'(\d+)', filename)
            return [int(p) if p.isdigit() else p.lower() for p in parts]
        
        exts = (".txt", ".srt")
        items = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
        items.sort(key=natural_sort_key)
        return items

    def start_queue_sequential(self):
        path = self.ed_folder.text().strip()
        if not path or not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "Thiếu đường dẫn", "Chọn file hoặc thư mục hợp lệ.")
            return
        
        # ========== CHECK SUBSCRIPTION trước khi bắt đầu ==========
        if hasattr(self, 'current_user_id') and self.current_user_id:
            if not self._check_subscription_active():
                # Load subscription info để hiển thị chi tiết lỗi
                info = self._load_subscription_info()
                
                error_msg = "Gói của bạn không hợp lệ!\n\n"
                
                if info.get('status') == 'no_subscription':
                    error_msg += "❌ Bạn chưa có gói nào.\n"
                elif info.get('status') == 'expired':
                    error_msg += f"❌ Gói '{info.get('type', '')}' đã hết hạn.\n"
                    if info.get('end_date'):
                        error_msg += f"📅 Ngày hết hạn: {info['end_date'][:10]}\n"
                
                count_chars = info.get('count_characters')
                if count_chars is not None and count_chars <= 0:
                    error_msg += "❌ Đã hết số ký tự trong gói.\n"
                
                error_msg += "\nVui lòng liên hệ admin để gia hạn."
                
                QtWidgets.QMessageBox.critical(
                    self,
                    "⛔ Subscription không hợp lệ",
                    error_msg
                )
                self.log("⛔ [Start] Blocked - subscription không hợp lệ")
                self._update_subscription_label()  # Update UI
                return
            else:
                # Refresh subscription label
                self._update_subscription_label()
        
        # 🐛 FIX: Check proxy từ CẢ DB và settings file
        # Ưu tiên: DB proxy > Settings file proxy
        
        # Check proxy từ ProxyServiceDB trước
        has_db_proxy = False
        db_proxy_count = 0
        if hasattr(self, 'proxy_service_db') and self.proxy_service_db:
            proxy_info = self.proxy_service_db.get_proxy_info()
            db_proxy_count = proxy_info.get('total', 0)
            has_db_proxy = db_proxy_count > 0
        
        # Fallback: Check ProxyManager local
        if not has_db_proxy:
            has_db_proxy = hasattr(self, 'proxies') and self.proxies.enabled and len(getattr(self.proxies, '_proxies', [])) > 0
        
        proxy_text = (getattr(self.s, 'proxies_text', None) or "").strip()
        proxy_enabled = getattr(self.s, 'proxy_enabled', False) or has_db_proxy
        
        # Check nếu có proxy (từ DB hoặc từ file)
        has_any_proxy = has_db_proxy or bool(proxy_text)
        
        if not has_any_proxy:
            # KHÔNG CÓ PROXY (dù enabled hay disabled)
            reply = QtWidgets.QMessageBox.warning(
                self,
                "⚠️ Không có Proxy",
                "⚠️ BẠN CHƯA CẤU HÌNH PROXY!\n\n"
                "Chạy không proxy có thể gây:\n"
                "• Lỗi 429 (Too Many Requests)\n"
                "• IP bị block tạm thời\n"
                "• Giảm tốc độ xử lý\n\n"
                "Khuyến nghị:\n"
                "1. Vào 'Cài đặt nâng cao' → 'PVIP'\n"
                "2. Nhập proxy và BẬT checkbox\n\n"
                "Bạn có chắc muốn tiếp tục KHÔNG CÓ PROXY?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No  # Default = No
            )
            if reply == QtWidgets.QMessageBox.No:
                self.log("⛔ [Start] User hủy vì chưa có proxy")
                return
            self.log("⚠️ [Warning] User tiếp tục MÀ KHÔNG CÓ PROXY - tự chịu rủi ro!")
        else:
            # CÓ PROXY
            if has_db_proxy:
                # Proxy từ DB - ưu tiên
                if hasattr(self, 'proxy_service_db') and self.proxy_service_db:
                    proxy_info = self.proxy_service_db.get_proxy_info()
                    proxy_type = proxy_info.get('current_type', 'unknown')
                    proxy_label = proxy_info.get('current_label', '')
                    self.log(f"✅ [Proxy] Enabled from DB - {db_proxy_count} proxy(s), Type={proxy_type}, Label={proxy_label}")
                else:
                    proxy_count = len(self.proxies._proxies)
                    self.log(f"✅ [Proxy] Enabled from DB - {proxy_count} proxy(s) configured")
            elif proxy_enabled:
                self.log(f"✅ [Proxy] Enabled from config - {len(proxy_text.splitlines())} proxy(s) configured")
            else:
                # User có proxy nhưng TẮT enabled
                reply = QtWidgets.QMessageBox.warning(
                    self,
                    "⚠️ Proxy bị TẮT",
                    f"Bạn đã cấu hình {len(proxy_text.splitlines())} proxy\n"
                    "NHƯNG CHƯA BẬT checkbox 'Enable Proxy'!\n\n"
                    "Proxy sẽ KHÔNG được sử dụng.\n\n"
                    "Bạn có muốn:\n"
                    "• Click 'No' → Quay lại bật proxy\n"
                    "• Click 'Yes' → Tiếp tục không dùng proxy",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No
                )
                if reply == QtWidgets.QMessageBox.No:
                    self.log("⛔ [Start] User hủy để bật proxy")
                    return
                self.log("⚠️ [Warning] User tiếp tục với proxy bị TẮT")
        
        # 🔧 NEW: Quick verify key trước khi bắt đầu
        self.log("🔑 [Verify] Kiểm tra key hoạt động...")
        key = self.keys.cur()
        if key:
            try:
                sub = self.client.subscription_for_key_silent(key)
                if sub:
                    limit = sub.get("character_limit", 0) or 0
                    used = sub.get("character_count", 0) or 0
                    credits = max(0, int(limit) - int(used))
                    self.log(f"✅ [Verify] Key OK - còn {credits:,} credits")
                else:
                    self.log("⚠️ [Verify] Không thể verify key - tiếp tục...")
            except Exception as e:
                self.log(f"⚠️ [Verify] Lỗi verify key: {e}")
        
        if hasattr(self, 'queue_files') and self.queue_files and hasattr(self, 'current_file_index'):
            not_done_files = []
            for file_info in self.queue_files:
                row = file_info['row']
                status_item = self.tbl_queue.item(row, 2)  # Status column
                if status_item and status_item.text() not in ["DONE", "Done"]:
                    not_done_files.append(file_info)
            
            if not_done_files:
                self.log(f"[Continue] Tiếp tục {len(not_done_files)} file(s) chưa hoàn thành...")
                
                for i, file_info in enumerate(self.queue_files):
                    row = file_info['row']
                    status_item = self.tbl_queue.item(row, 2)
                    if status_item and status_item.text() not in ["DONE", "Done"]:
                        self.current_file_index = i
                        break
                
                self.stop_requested = False
                self.bt_start.setEnabled(False)
                self.bt_start.setStyleSheet("background-color: #28a745; color: white;")
                self.bt_stop.setEnabled(True)
                self.sb_thread.setEnabled(False)
                
                self._start_processing_file()
                return
        
        
        files = []
        if os.path.isfile(path):
            if path.lower().endswith(('.txt', '.srt')):
                files = [path]
        else:
            files = self.collect_files(path)
        
        if not files:
            QtWidgets.QMessageBox.warning(self, "Không có file", "Không tìm thấy file .txt hoặc .srt.")
            return
        
        # 🐛 FIX: Dừng tất cả workers đang chạy trước khi bắt đầu batch mới
        self.stop_requested = True
        if hasattr(self, '_active_workers') and self._active_workers:
            for worker in self._active_workers[:]:  # Copy list để tránh modification during iteration
                try:
                    if hasattr(worker, 'stop'):
                        worker.stop()
                except:
                    pass
            self._active_workers = []
        # Đợi một chút để workers dừng
        QtWidgets.QApplication.processEvents()
        time.sleep(0.2)
        
        # fill table Batch Job
        self.tbl_queue.setRowCount(0)
        self.queue_files = []
        
        # Clear hoàn toàn bảng và state cũ - QUAN TRỌNG: Clear TẤT CẢ state trước khi bắt đầu batch mới
        self.tbl_sub.setRowCount(0)
        self._current_lines = []
        self._current_line_index = 0
        self._total_lines = 0
        self._completed_lines = 0
        self._failed_lines = 0
        self._active_worker_count = 0
        self._file_merging_in_progress = False
        self.stop_requested = False
        if hasattr(self, '_active_workers'):
            self._active_workers = []
        if hasattr(self, '_line_retry_counts'):
            self._line_retry_counts = {}
        if hasattr(self, '_last_highlighted_row'):
            self._last_highlighted_row = -1
        
        # Reset tracking
        self.total_files = len(files)
        self.completed_files = 0
        self.failed_files = 0
        self.lbl_result.setText(f"Kết Quả: 0/{self.total_files}")
        
        for i, p in enumerate(files, start=1):
            r = self.tbl_queue.rowCount()
            self.tbl_queue.insertRow(r)
            self.tbl_queue.setItem(r, 0, self._centered_item(str(i)))
            self.tbl_queue.setItem(r, 1, QtWidgets.QTableWidgetItem(os.path.basename(p)))
            self.tbl_queue.setItem(r, 2, self._centered_item("Ready"))
            self.tbl_queue.setItem(r, 3, self._centered_item("0%"))
            self.queue_files.append({'path': p, 'row': r})

        # map UI -> settings
        self.s.change_settings=self.cb_change.isChecked()
        self.s.speed=float(self.sb_speed.value()); self.s.style=int(self.sb_style.value())
        self.s.stability=self._get_stability_value(); self.s.similarity=int(self.sb_sim.value()); self.s.speaker_boost=self.cb_boost.isChecked()
        self.s.loop=self.cb_loop.isChecked(); self.s.auto_split=self.cb_split.isChecked(); self.s.split_chars=self.ed_split.text() or ",.;!?"
        self.s.thread_count=int(self.sb_thread.value()); save_settings(self.s)

        if self.cb_voice.count()==0 or self.cb_model.count()==0:
            QtWidgets.QMessageBox.warning(self,"Thiếu dữ liệu","Bấm Load để tải Voices/Models."); return
        
        # 🔧 NEW: Kiểm tra subscription credits trước khi bắt đầu
        if not self._check_subscription_credits_before_start():
            return
        
        i=self.cb_voice.currentIndex(); voice_id=self.cb_voice.itemData(i) or self.cb_voice.currentText().split(" — ")[-1].strip()
        model_id=self.cb_model.currentText().strip()
        self.voice_model_tuple=(voice_id, model_id)

        self.stop_requested=False
        self._subscription_exhausted_shown = False  # 🔧 FIX: Reset flag khi bắt đầu TTS mới
        self.pool.setMaxThreadCount(self.sb_thread.value())
        
        self.bt_start.setEnabled(False)
        self.bt_start.setStyleSheet("background-color: #888888;")
        self.bt_stop.setEnabled(True)
        self.sb_thread.setEnabled(False)
        
        self.current_file_index = 0
        self._resume_mode = {}  # Track resume mode per file
        self._start_processing_file()
    
    def _check_existing_mp3_files(self, tts_mini_dir: str, total_paragraphs: int) -> dict:
        """
        Kiểm tra các file doan_X.mp3 và chunk_X_Y.mp3 đã tồn tại
        Returns: {
            'existing_ids': [1, 2, 3, ...],  # Các paragraph ID đã có file doan_X.mp3 hợp lệ
            'missing_ids': [4, 5, ...],       # Các paragraph ID còn thiếu
            'existing_chunks': {1: [1,2,3], 2: [1,2]},  # Các chunk đã tồn tại theo paragraph
            'total': total_paragraphs
        }
        """
        existing_ids = []
        existing_chunks = {}  # {para_idx: [chunk_idx, ...]}
        
        # 🔧 FIX: chunks_dir nằm trong tts_mini_dir (không phải file_output_dir)
        # ChunkWorker lưu file vào tts_mini_dir/_chunks
        chunks_dir = os.path.join(tts_mini_dir, "_chunks")
        
        self.log(f"[Cache] Scanning tts_mini_dir: {tts_mini_dir}")
        self.log(f"[Cache] Scanning chunks_dir: {chunks_dir}")
        
        # 1. Scan file doan_X.mp3 trong tts_mini_dir
        if os.path.exists(tts_mini_dir):
            for f in Path(tts_mini_dir).glob("doan_*.mp3"):
                try:
                    para_num = int(f.stem.split('_')[1])
                    if f.stat().st_size > 0:
                        existing_ids.append(para_num)
                except:
                    continue
            self.log(f"[Cache] Found {len(existing_ids)} completed paragraphs (doan_X.mp3)")
        
        # 2. Scan file X.Y.mp3 và X.mp3 trong _chunks folder
        # Format mới: 2.mp3 (1 chunk), 1.1.mp3, 1.2.mp3 (nhiều chunks)
        if os.path.exists(chunks_dir):
            # Support old format (*_chunk_*.mp3), new multi-chunk (X.Y.mp3), and single chunk (X.mp3)
            chunk_files_old = list(Path(chunks_dir).glob("*_chunk_*.mp3"))
            chunk_files_multi = list(Path(chunks_dir).glob("[0-9]*.[0-9]*.mp3"))  # 1.1.mp3, 1.2.mp3
            chunk_files_single = [f for f in Path(chunks_dir).glob("[0-9]*.mp3") if '.' not in f.stem or f.stem.count('.') == 0]  # 2.mp3
            chunk_files = chunk_files_old + chunk_files_multi + chunk_files_single
            self.log(f"[Cache] Found {len(chunk_files)} chunk files in {chunks_dir}")
            for f in chunk_files:
                try:
                    stem = f.stem
                    # New format multi-chunk: 1.1 -> para=1, chunk=1
                    if '.' in stem and '_chunk_' not in stem:
                        parts = stem.split('.')
                        if len(parts) >= 2:
                            para_num = int(parts[0])
                            chunk_num = int(parts[1])
                            if f.stat().st_size > 0:
                                if para_num not in existing_chunks:
                                    existing_chunks[para_num] = []
                                if chunk_num not in existing_chunks[para_num]:
                                    existing_chunks[para_num].append(chunk_num)
                    # New format single-chunk: 2.mp3 -> para=2, chunk=1
                    elif stem.isdigit():
                        para_num = int(stem)
                        if f.stat().st_size > 0:
                            if para_num not in existing_chunks:
                                existing_chunks[para_num] = []
                            if 1 not in existing_chunks[para_num]:
                                existing_chunks[para_num].append(1)
                    # Old format: 1_chunk_0001.mp3 -> para=1, chunk=1
                    elif '_chunk_' in stem:
                        parts = stem.split('_chunk_')
                        if len(parts) == 2 and f.stat().st_size > 0:
                            para_num = int(parts[0])
                            chunk_num = int(parts[1])
                            if para_num not in existing_chunks:
                                existing_chunks[para_num] = []
                            if chunk_num not in existing_chunks[para_num]:
                                existing_chunks[para_num].append(chunk_num)
                except:
                    continue
            # Log chi tiết chunks đã cache
            for para, chunks in existing_chunks.items():
                self.log(f"[Cache] Para {para}: {len(chunks)} chunks cached")
        else:
            self.log(f"[Cache] Chunks dir not found: {chunks_dir}")
        
        existing_ids.sort()
        all_ids = set(range(1, total_paragraphs + 1))
        missing_ids = sorted(list(all_ids - set(existing_ids)))
        
        return {
            'existing_ids': existing_ids,
            'missing_ids': missing_ids,
            'existing_chunks': existing_chunks,
            'total': total_paragraphs
        }
    
    def _show_resume_dialog(self, base: str, existing_count: int, total: int, missing_count: int, cached_chunks: int = 0) -> str:
        """
        Hiển thị dialog hỏi user khi phát hiện file đã tồn tại
        Returns: 'continue' | 'restart' | 'merge_only' | 'cancel'
        """
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Phát hiện file đã xử lý")
        dlg.setModal(True)
        dlg.setMinimumWidth(400)
        
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(12)
        
        # Icon và message
        if missing_count == 0:
            # Tất cả đã hoàn thành
            msg = f"📁 File: {base}\n\n✅ Tất cả {total} đoạn đã được tạo trước đó."
            lbl = QtWidgets.QLabel(msg)
            lbl.setStyleSheet("font-size: 11pt;")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            
            btn_merge = QtWidgets.QPushButton("🔗 Nối file MP3")
            btn_merge.setToolTip("Chỉ nối các file đã có thành MP3 hoàn chỉnh")
            btn_restart = QtWidgets.QPushButton("🔄 Làm lại từ đầu")
            btn_restart.setToolTip("Xóa hết và tạo lại tất cả")
            btn_cancel = QtWidgets.QPushButton("❌ Hủy")
            
            btn_merge.clicked.connect(lambda: dlg.done(1))
            btn_restart.clicked.connect(lambda: dlg.done(2))
            btn_cancel.clicked.connect(lambda: dlg.done(0))
            
            btn_layout = QtWidgets.QHBoxLayout()
            btn_layout.addWidget(btn_merge)
            btn_layout.addWidget(btn_restart)
            btn_layout.addWidget(btn_cancel)
            layout.addLayout(btn_layout)
            
            result = dlg.exec()
            if result == 1:
                return 'merge_only'
            elif result == 2:
                return 'restart'
            else:
                return 'cancel'
        else:
            # Còn một số đoạn chưa hoàn thành
            if cached_chunks > 0 and existing_count == 0:
                # Chỉ có chunks cached, không có paragraph hoàn chỉnh
                msg = f"📁 File: {base}\n\n⏸️ Phát hiện {cached_chunks} chunks đã được tạo.\n📝 Còn cần xử lý các chunks còn lại."
            else:
                msg = f"📁 File: {base}\n\n⏸️ Phát hiện {existing_count}/{total} đoạn đã được tạo."
                if cached_chunks > 0:
                    msg += f"\n🧩 Thêm {cached_chunks} chunks đang xử lý dở."
                msg += f"\n📝 Còn {missing_count} đoạn cần xử lý."
            lbl = QtWidgets.QLabel(msg)
            lbl.setStyleSheet("font-size: 11pt;")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            
            btn_continue = QtWidgets.QPushButton(f"▶️ Tiếp tục ({missing_count} đoạn)")
            btn_continue.setToolTip("Chỉ tạo các đoạn còn thiếu")
            btn_continue.setStyleSheet("background-color: #28a745; color: white; font-weight: bold;")
            btn_restart = QtWidgets.QPushButton("🔄 Làm lại từ đầu")
            btn_restart.setToolTip("Xóa hết và tạo lại tất cả")
            btn_cancel = QtWidgets.QPushButton("❌ Hủy")
            
            btn_continue.clicked.connect(lambda: dlg.done(1))
            btn_restart.clicked.connect(lambda: dlg.done(2))
            btn_cancel.clicked.connect(lambda: dlg.done(0))
            
            btn_layout = QtWidgets.QHBoxLayout()
            btn_layout.addWidget(btn_continue)
            btn_layout.addWidget(btn_restart)
            btn_layout.addWidget(btn_cancel)
            layout.addLayout(btn_layout)
            
            result = dlg.exec()
            if result == 1:
                return 'continue'
            elif result == 2:
                return 'restart'
            else:
                return 'cancel'
    
    def _delete_existing_mp3_files(self, tts_mini_dir: str):
        """Xóa tất cả file doan_*.mp3 trong folder tts_mini"""
        if not os.path.exists(tts_mini_dir):
            return
        
        for f in Path(tts_mini_dir).glob("doan_*.mp3"):
            try:
                f.unlink()
                self.log(f"[Resume] Đã xóa: {f.name}")
            except Exception as e:
                self.log(f"[Resume] Không thể xóa {f.name}: {e}")
    
    def _start_processing_file(self):
        """Bắt đầu xử lý file hiện tại - load subtitles và chạy từng dòng"""
        if self.stop_requested or self.current_file_index >= len(self.queue_files):
            self.bt_start.setEnabled(True)
            self.bt_start.setStyleSheet("")
            self.bt_stop.setEnabled(False)
            self.sb_thread.setEnabled(True)
            self.log(f"🎉 Hoàn thành {self.completed_files}/{self.total_files} file(s)")
            if hasattr(self, '_last_highlighted_row'):
                for j in range(self.tbl_queue.columnCount()):
                    item = self.tbl_queue.item(self._last_highlighted_row, j)
                    if item:
                        item.setBackground(QtGui.QBrush())
            
            if self.total_files > 0:
                # Log data usage summary
                self.client.log_data_summary()
                
                dlg = QtWidgets.QDialog(self)
                dlg.setWindowTitle("Hoàn Thành")
                dlg.setModal(True)
                dlg.setFixedSize(320, 130)
                layout = QtWidgets.QVBoxLayout(dlg)
                layout.setContentsMargins(15, 10, 15, 10)
                layout.setSpacing(8)
                
                lbl_success = QtWidgets.QLabel(f"✅ Hoàn thành: {self.completed_files}/{self.total_files} file(s)")
                lbl_success.setStyleSheet("font-size: 11pt; color: #28a745;")
                lbl_success.setAlignment(QtCore.Qt.AlignCenter)
                layout.addWidget(lbl_success)
                
                if self.failed_files > 0:
                    lbl_fail = QtWidgets.QLabel(f"❌ Thất bại: {self.failed_files} file(s)")
                    lbl_fail.setStyleSheet("font-size: 11pt; color: #dc3545;")
                    lbl_fail.setAlignment(QtCore.Qt.AlignCenter)
                    layout.addWidget(lbl_fail)
                
                btn_ok = QtWidgets.QPushButton("OK")
                btn_ok.setFixedWidth(60)
                btn_ok.clicked.connect(dlg.accept)
                layout.addWidget(btn_ok, alignment=QtCore.Qt.AlignCenter)
                
                # 🔔 Phát âm thanh thông báo
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)  # Windows notification sound
                except:
                    pass  # Nếu không có winsound (non-Windows), im lặng
                
                dlg.exec()
            return
        
        file_info = self.queue_files[self.current_file_index]
        fpath = file_info['path']
        batch_row = file_info['row']
        
        if hasattr(self, '_last_highlighted_row') and self._last_highlighted_row != batch_row:
            for j in range(self.tbl_queue.columnCount()):
                item = self.tbl_queue.item(self._last_highlighted_row, j)
                if item:
                    item.setBackground(QtGui.QBrush())
        
        for j in range(self.tbl_queue.columnCount()):
            item = self.tbl_queue.item(batch_row, j)
            if item:
                item.setBackground(QtGui.QColor(100, 180, 100))
        
        self._last_highlighted_row = batch_row
        
        self.tbl_queue.setItem(batch_row, 2, self._centered_item("RUNNING"))
        
        try:
            content = open(fpath, "r", encoding="utf-8", errors="ignore").read()
        except:
            self.tbl_queue.setItem(batch_row, 2, self._centered_item("Fail"))
            for j in range(self.tbl_queue.columnCount()):
                item = self.tbl_queue.item(batch_row, j)
                if item:
                    item.setBackground(QtGui.QBrush())
            self.current_file_index += 1
            QtCore.QTimer.singleShot(1000, self._start_processing_file)
            return
        
        # Sanitize filename để tránh lỗi với ffmpeg
        base_raw = os.path.splitext(os.path.basename(fpath))[0]
        base = sanitize_filename(base_raw)
        
        # Tạo cấu trúc folder mới: Folder cha → Folder cho mỗi file (1/, 2/) → tts_mini/
        parent_dir = os.path.dirname(fpath)
        file_output_dir = os.path.join(parent_dir, base)  # Folder cho file này (1/, 2/, ...)
        tts_mini_dir = os.path.join(file_output_dir, "tts_mini")  # Folder tts_mini/
        self.log(f"[DEBUG] Output path - fpath: {fpath}, parent_dir: {parent_dir}, file_output_dir: {file_output_dir}")
        ensure_dir(file_output_dir)
        ensure_dir(tts_mini_dir)
        
        # Sanitize toàn bộ content
        if self.s.sanitize:
            # 🔧 FIX: Xử lý từng bước với processEvents() để tránh UI freeze với file lớn
            QtWidgets.QApplication.processEvents()
            content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", content)
            QtWidgets.QApplication.processEvents()
            invalid_chars = r'[*~^#@$%&|\\<>{}[\]`]'
            content = re.sub(invalid_chars, " ", content)
            QtWidgets.QApplication.processEvents()
            content = re.sub(r'[\U00010000-\U0010ffff]', '', content)
            QtWidgets.QApplication.processEvents()
            content = re.sub(r' {2,}', ' ', content)
            content = content.strip()
        
        # 🔧 FIX: processEvents trước khi split file lớn
        QtWidgets.QApplication.processEvents()
        
        # 🔧 FIX: Set cứng max_chars = 300 để chunk nhỏ hơn, API response nhanh hơn
        max_chars = 300  # Giảm từ 400 xuống 300
        self.log(f"[DEBUG] Total content: {len(content):,} chars, max_chars_per_line: {max_chars}")
        
        paragraphs_data = split_by_paragraphs_then_chunks(content, max_chars)
        self.log(f"📝 Split thành {len(paragraphs_data)} đoạn (paragraphs)")
        
        # 🔧 FIX: processEvents sau khi split
        QtWidgets.QApplication.processEvents()
        
        # Tính tổng số chunks cần xử lý (để phân bổ luồng)
        total_chunks = sum(para['total_chunks'] for para in paragraphs_data)
        self.log(f"📊 Tổng số chunks cần xử lý: {total_chunks}")
        
        # 🔄 RESUME TTS: Kiểm tra file đã tồn tại
        total_paragraphs = len(paragraphs_data)
        resume_info = self._check_existing_mp3_files(tts_mini_dir, total_paragraphs)
        existing_count = len(resume_info['existing_ids'])
        missing_ids = resume_info['missing_ids']
        existing_chunks = resume_info.get('existing_chunks', {})  # 🔧 NEW: Chunk level cache
        
        resume_action = 'normal'  # Mặc định: chạy bình thường
        
        # 🔧 NEW: Tính số chunks đã cached
        cached_chunk_count = sum(len(chunks) for chunks in existing_chunks.values())
        if cached_chunk_count > 0:
            self.log(f"[Resume] Phát hiện {cached_chunk_count} chunks đã có file MP3")
        
        # 🔧 FIX: Hiển thị popup nếu có chunks cached HOẶC có paragraphs hoàn chỉnh
        if existing_count > 0 or cached_chunk_count > 0:
            self.log(f"[Resume] Phát hiện {existing_count}/{total_paragraphs} đoạn, {cached_chunk_count} chunks đã có file MP3")
            
            # Hiển thị dialog hỏi user
            resume_action = self._show_resume_dialog(
                base=base,
                existing_count=existing_count,
                total=total_paragraphs,
                missing_count=len(missing_ids),
                cached_chunks=cached_chunk_count
            )
            
            if resume_action == 'cancel':
                self.log(f"[Resume] User hủy xử lý file {base}")
                self.tbl_queue.setItem(batch_row, 2, self._centered_item("Skipped"))
                for j in range(self.tbl_queue.columnCount()):
                    item = self.tbl_queue.item(batch_row, j)
                    if item:
                        item.setBackground(QtGui.QBrush())
                self.current_file_index += 1
                QtCore.QTimer.singleShot(500, self._start_processing_file)
                return
            
            elif resume_action == 'restart':
                self.log(f"[Resume] User chọn làm lại từ đầu - xóa {existing_count} file cũ và {cached_chunk_count} chunks")
                self._delete_existing_mp3_files(tts_mini_dir)
                # 🔧 FIX: Xóa cả chunks trong folder _chunks (trong tts_mini_dir)
                chunks_dir = os.path.join(tts_mini_dir, "_chunks")
                if os.path.exists(chunks_dir):
                    # Delete both old format (*_chunk_*.mp3) and new format (X.Y.mp3)
                    for f in Path(chunks_dir).glob("*_chunk_*.mp3"):
                        try:
                            f.unlink()
                        except:
                            pass
                    for f in Path(chunks_dir).glob("[0-9]*.[0-9]*.mp3"):
                        try:
                            f.unlink()
                        except:
                            pass
                    self.log(f"[Resume] Đã xóa chunks trong {chunks_dir}")
                missing_ids = list(range(1, total_paragraphs + 1))  # Tạo lại tất cả
                existing_chunks = {}  # 🔧 NEW: Clear chunk cache
            
            elif resume_action == 'merge_only':
                self.log(f"[Resume] User chọn chỉ nối file - skip TTS")
                # Đánh dấu tất cả là cached, sau đó merge
                self._resume_mode[fpath] = 'merge_only'
            
            elif resume_action == 'continue':
                self.log(f"[Resume] User chọn tiếp tục - chỉ tạo {len(missing_ids)} đoạn còn thiếu")
                self._resume_mode[fpath] = {'missing_ids': set(missing_ids), 'existing_chunks': existing_chunks}
        
        self.tbl_sub.setRowCount(0)
        self._current_lines = []
        
        # Xác định các paragraph ID cần skip (đã cached)
        cached_para_ids = set()
        if fpath in self._resume_mode:
            mode = self._resume_mode[fpath]
            if mode == 'merge_only':
                # Tất cả đều cached
                cached_para_ids = set(range(1, total_paragraphs + 1))
            elif isinstance(mode, dict) and 'missing_ids' in mode:
                # Chỉ missing_ids cần xử lý, còn lại là cached
                cached_para_ids = set(range(1, total_paragraphs + 1)) - mode['missing_ids']
                # 🔧 NEW: Lấy existing_chunks từ resume_mode
                existing_chunks = mode.get('existing_chunks', existing_chunks)
        
        # 🔧 FIX: Xử lý theo CHUNK thay vì PARAGRAPH để tận dụng multi-thread
        # Mỗi chunk sẽ là 1 worker riêng, sau đó merge các chunks của cùng paragraph
        display_row_id = 0
        self._paragraph_info = {}  # Lưu thông tin paragraph để merge sau
        
        # 🔧 DEBUG: Log existing_chunks
        self.log(f"[Cache] existing_chunks keys: {list(existing_chunks.keys())}")
        self.log(f"[Cache] Total cached chunks: {sum(len(v) for v in existing_chunks.values())}")
        
        for para_data in paragraphs_data:
            para_idx = para_data['paragraph_idx']
            para_text = para_data['paragraph_text']
            chunks = para_data['chunks']
            num_chunks = len(chunks)
            
            # Check xem paragraph này đã cached chưa
            is_para_cached = para_idx in cached_para_ids
            
            # 🔧 NEW: Lấy danh sách chunks đã cached cho paragraph này
            para_cached_chunks = existing_chunks.get(para_idx, [])
            
            # Lưu thông tin paragraph để merge sau
            self._paragraph_info[para_idx] = {
                'total_chunks': num_chunks,
                'completed_chunks': 0,
                'chunk_files': {},  # {chunk_idx: file_path}
                'tts_mini_dir': tts_mini_dir,
                'outdir': file_output_dir,
                'base': base,
                'fpath': fpath,
                'batch_row': batch_row,
                'cached': is_para_cached
            }
            
            # Hiển thị và tạo worker cho mỗi chunk
            for chunk_idx, chunk_text in enumerate(chunks, start=1):
                display_row_id += 1
                r = self.tbl_sub.rowCount()
                self.tbl_sub.insertRow(r)
                
                # Hiển thị ID dạng "1.1", "1.2" nếu có nhiều chunks, hoặc "1" nếu chỉ có 1 chunk
                if num_chunks > 1:
                    display_id = f"{para_idx}.{chunk_idx}"
                else:
                    display_id = str(para_idx)
                
                # Hiển thị nội dung chunk (cắt ngắn nếu quá dài)
                display_text = chunk_text[:150] + ("..." if len(chunk_text) > 150 else "")
                
                # 🔧 NEW: Check chunk level cache
                is_chunk_cached = is_para_cached or (chunk_idx in para_cached_chunks)
                
                # 🔧 DEBUG: Log cache status
                if chunk_idx in para_cached_chunks:
                    self.log(f"[Cache] Para {para_idx} chunk {chunk_idx} found in para_cached_chunks")
                
                # 🔧 NEW: Nếu chunk đã cached, lấy file path
                chunk_file_path = ""
                if is_chunk_cached and not is_para_cached:
                    # Chunk cached nhưng paragraph chưa merge
                    chunk_filename = f"{para_idx}.{chunk_idx}.mp3"
                    # 🔧 FIX: Check vị trí tts_mini_dir/_chunks (vị trí thực tế của ChunkWorker)
                    chunks_dir = os.path.join(tts_mini_dir, "_chunks")
                    chunk_file_path = os.path.join(chunks_dir, chunk_filename)
                    
                    if os.path.exists(chunk_file_path):
                        # Lưu vào paragraph_info để merge sau
                        self._paragraph_info[para_idx]['chunk_files'][chunk_idx] = chunk_file_path
                        self._paragraph_info[para_idx]['completed_chunks'] += 1
                        self.log(f"[Cache] Found cached file: {chunk_file_path}")
                    else:
                        self.log(f"[Cache] File not found: {chunk_file_path}")
                        chunk_file_path = ""  # Reset nếu không tìm thấy
                
                self.tbl_sub.setItem(r, 0, self._centered_item(display_id))  # Id
                
                # 🔧 NEW: Hiển thị Output và Timing nếu chunk đã cached
                if is_chunk_cached and chunk_file_path and os.path.exists(chunk_file_path):
                    timing = get_mp3_duration_str(chunk_file_path)
                    output_item = self._centered_item(os.path.basename(chunk_file_path))
                    output_item.setData(QtCore.Qt.UserRole, chunk_file_path)
                    output_item.setToolTip(chunk_file_path)
                    self.tbl_sub.setItem(r, 1, output_item)  # Output
                    self.tbl_sub.setItem(r, 2, self._centered_item(timing))  # Timing
                else:
                    self.tbl_sub.setItem(r, 1, self._centered_item(""))  # Output
                    self.tbl_sub.setItem(r, 2, self._centered_item(""))  # Timing
                
                self.tbl_sub.setItem(r, 3, self._centered_item(str(len(chunk_text))))  # Chars
                self.tbl_sub.setItem(r, 4, QtWidgets.QTableWidgetItem(display_text))  # Content
                self.tbl_sub.setItem(r, 5, self._centered_item(""))  # Voice #
                
                if is_chunk_cached:
                    self.tbl_sub.setItem(r, 6, self._centered_item("✅ Cached"))
                    for col in range(self.tbl_sub.columnCount()):
                        item = self.tbl_sub.item(r, col)
                        if item:
                            item.setBackground(QtGui.QColor(200, 230, 200))  # Light green
                else:
                    self.tbl_sub.setItem(r, 6, self._centered_item("Ready"))
                
                # 🔧 NEW: Lưu mỗi CHUNK như 1 item riêng trong _current_lines
                self._current_lines.append({
                    'line_id': display_row_id,  # Unique ID cho mỗi chunk
                    'paragraph_idx': para_idx,
                    'chunk_idx': chunk_idx,
                    'content': chunk_text,  # Nội dung chunk (không phải paragraph)
                    'total_chunks': num_chunks,
                    'row': r,
                    'base': base,
                    'outdir': file_output_dir,
                    'tts_mini_dir': tts_mini_dir,
                    'fpath': fpath,
                    'batch_row': batch_row,
                    'cached': is_chunk_cached  # 🔧 FIX: Dùng chunk level cache
                })
                
                # processEvents mỗi 10 dòng để UI không freeze
                if display_row_id % 10 == 0:
                    QtWidgets.QApplication.processEvents()
        
        self.log(f"Loaded {len(self._current_lines)} chunks từ {base}")
        
        # 🔧 NEW: Log số chunks đã cached
        cached_chunks_count = sum(1 for line in self._current_lines if line.get('cached', False))
        if cached_chunks_count > 0:
            self.log(f"[Resume] {cached_chunks_count}/{len(self._current_lines)} chunks đã cached")
        
        if not self._current_lines:
            self.tbl_queue.setItem(batch_row, 2, self._centered_item("Skipped"))
            for j in range(self.tbl_queue.columnCount()):
                item = self.tbl_queue.item(batch_row, j)
                if item:
                    item.setBackground(QtGui.QBrush())
            self.current_file_index += 1
            QtCore.QTimer.singleShot(1000, self._start_processing_file)
            return
        
        # 🔄 Nếu merge_only, skip thẳng đến merge
        if fpath in self._resume_mode and self._resume_mode[fpath] == 'merge_only':
            self.log(f"[Resume] Merge only mode - chuyển thẳng đến nối file")
            with self._progress_lock:
                self._current_line_index = len(self._current_lines)  # Skip all
                self._total_lines = len(self._current_lines)
                self._completed_lines = len(self._current_lines)
                self._failed_lines = 0
            self._file_merging_in_progress = False  # 🔧 FIX: Khởi tạo trước khi merge
            self._active_workers = []
            # Trigger merge
            self._on_all_lines_completed()
            return
        
        # 🔧 FIX: Reset counters với lock để tránh race condition
        with self._progress_lock:
            self._current_line_index = 0
            self._total_lines = len(self._current_lines)
            self._completed_lines = 0
            self._failed_lines = 0
            self._active_worker_count = 0
        self._max_workers = self.sb_thread.value()
        self.log(f"[Config] Thread setting = {self._max_workers} workers (sẽ chạy {self._max_workers} dòng song song)")
        
        file_name = os.path.basename(fpath) if 'fpath' in dir() else "unknown"
        self.log(f"📋 File: {file_name} - Danh sách {len(self._current_lines)} dòng:")
        for line_info in self._current_lines:
            line_id = line_info.get('line_id', '?')
            content = line_info.get('content', '')[:50]
            if len(line_info.get('content', '')) > 50:
                content += "..."
            self.log(f"  ID {line_id}: {content}")
        
        self._file_merging_in_progress = False
        self._active_workers = []
        
        # 🔧 DISABLED: Warmup gây not responding - skip để tăng tốc
        # self._warmup_proxy_before_tts()
        
        self._start_next_single_line()
    
    def _warmup_proxy_before_tts(self):
        """
        Warm-up proxy bằng cách gửi 1 request nhỏ "Hello" để test proxy hoạt động.
        Nếu proxy fail, sẽ tự động xoay sang proxy khác.
        """
        if not self.client.proxies.enabled:
            self.log("[Warmup] Proxy disabled - skip warmup")
            return
        
        self.log("[Warmup] 🔥 Testing proxy với request nhỏ...")
        
        max_warmup_attempts = 3
        for attempt in range(max_warmup_attempts):
            try:
                # Lấy proxy hiện tại
                current_proxy = self.client.proxies.cur()
                if not current_proxy:
                    self.log("[Warmup] ⚠️ Không có proxy - skip warmup")
                    return
                
                # Tạo temp file cho warmup
                import tempfile
                temp_dir = tempfile.gettempdir()
                warmup_file = os.path.join(temp_dir, "tts_warmup.mp3")
                
                # Lấy key để test
                voice_id, model_id = self.voice_model_tuple
                key = self.client.keys.acquire_key(required_chars=10, line_id="warmup")
                if not key:
                    self.log("[Warmup] ⚠️ Không có key available - skip warmup")
                    return
                
                # Gửi request nhỏ "Hi"
                voice_settings = {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": False,
                    "speed": 1.0,
                }
                
                self.log(f"[Warmup] Attempt {attempt + 1}/{max_warmup_attempts} - proxy: {current_proxy[:30]}...")
                
                # Gọi tts_direct với text ngắn
                self.client.tts_direct(voice_id, "Hi", model_id, voice_settings, warmup_file, api_key=key)
                
                # Release key (success)
                self.client.keys.release_key(key, 2, True, None, "warmup")
                
                # Xóa file warmup
                if os.path.exists(warmup_file):
                    try:
                        os.remove(warmup_file)
                    except:
                        pass
                
                self.log("[Warmup] ✅ Proxy OK - sẵn sàng gen audio!")
                return
                
            except Exception as e:
                err_msg = str(e).lower()
                self.log(f"[Warmup] ❌ Attempt {attempt + 1} failed: {str(e)[:100]}")
                
                # Release key nếu có
                if 'key' in dir() and key:
                    error_type = "unknown"
                    if "401" in err_msg:
                        error_type = "401"
                    elif "timeout" in err_msg or "connection" in err_msg:
                        error_type = "connection_error"
                    self.client.keys.release_key(key, 0, False, error_type, "warmup")
                    key = None
                
                # Xoay proxy
                if hasattr(self.client.proxies, '_proxy_service_db') and self.client.proxies._proxy_service_db:
                    self.client.proxies._proxy_service_db.report_failure(is_rate_limited=False)
                    self.log(f"[Warmup] 🔄 Đã xoay proxy!")
                elif hasattr(self.client.proxies, 'rotate'):
                    self.client.proxies.rotate()
                    self.log(f"[Warmup] 🔄 Đã xoay proxy!")
                
                # Đợi 1s trước khi thử lại
                time.sleep(1)
        
        self.log("[Warmup] ⚠️ Warmup failed sau 3 lần - tiếp tục gen audio anyway")
    
    def _start_next_single_line(self):
        """Bắt đầu xử lý 1 dòng tiếp theo (nếu còn slot trống trong _max_workers luồng)"""
        if self.stop_requested:
            return
        
        # 🔧 FIX: Kiểm tra subscription exhausted
        if getattr(self, '_subscription_exhausted_shown', False):
            self.log("[Worker] Dừng - subscription credits đã hết")
            return
        
        if self._current_line_index >= len(self._current_lines):
            if self._active_worker_count == 0:
                # 🔧 FIX: Gọi _on_all_chunks_completed để merge chunks trước
                self._on_all_chunks_completed()
            return
        
        if self._active_worker_count >= self._max_workers:
            return
        
        line_info = self._current_lines[self._current_line_index]
        self._current_line_index += 1
        
        # 🔄 RESUME: Skip các đoạn đã cached
        if line_info.get('cached', False):
            self._completed_lines += 1
            para_idx = line_info.get('paragraph_idx', 1)
            chunk_idx = line_info.get('chunk_idx', 1)
            self.log(f"[Resume] Skip chunk {para_idx}.{chunk_idx} (đã cached)")
            
            # 🔧 FIX: Thêm cached chunk file vào _paragraph_info để merge có thể hoạt động
            # 🔧 FIX: Scan cả 2 vị trí để backward compatible
            if hasattr(self, '_paragraph_info') and para_idx in self._paragraph_info:
                tts_mini_dir = line_info.get('tts_mini_dir', line_info.get('outdir', ''))
                file_output_dir = os.path.dirname(tts_mini_dir) if tts_mini_dir else ''
                chunk_filename = sanitize_filename(f"{para_idx}.{chunk_idx}.mp3")
                
                # Thử vị trí mới trước (tts_mini_dir/_chunks)
                chunk_file_path_new = os.path.join(tts_mini_dir, "_chunks", chunk_filename)
                # Thử vị trí cũ (file_output_dir/_chunks)
                chunk_file_path_old = os.path.join(file_output_dir, "_chunks", chunk_filename)
                
                chunk_file_path = ""
                if os.path.exists(chunk_file_path_new) and os.path.getsize(chunk_file_path_new) > 0:
                    chunk_file_path = chunk_file_path_new
                elif os.path.exists(chunk_file_path_old) and os.path.getsize(chunk_file_path_old) > 0:
                    chunk_file_path = chunk_file_path_old
                
                if chunk_file_path:
                    self._paragraph_info[para_idx]['chunk_files'][chunk_idx] = chunk_file_path
                    self._paragraph_info[para_idx]['completed_chunks'] += 1
                    self.log(f"[Resume] Added cached chunk file: {chunk_filename}")
            
            # Cập nhật progress
            if self._total_lines > 0:
                progress = int((self._completed_lines / self._total_lines) * 100)
                batch_row = line_info.get('batch_row', 0)
                self.tbl_queue.setItem(batch_row, 3, self._centered_item(f"{progress}%"))
            # Tiếp tục với chunk tiếp theo
            QtCore.QTimer.singleShot(10, self._start_next_single_line)
            return
        
        self._active_worker_count += 1
        
        voice_id, model_id = self.voice_model_tuple
        
        sig = Sig()
        sig.chunk_status.connect(self._on_chunk_status, QtCore.Qt.QueuedConnection)
        sig.chunk_done.connect(self._on_chunk_done, QtCore.Qt.QueuedConnection)  # 🔧 NEW: Connect chunk_done signal
        sig.done.connect(self._on_chunk_worker_done, QtCore.Qt.QueuedConnection)  # 🔧 FIX: Direct connect + QueuedConnection
        
        # 🔧 NEW: Sử dụng ChunkWorker thay vì LineWorker
        w = ChunkWorker(
            row=line_info['row'],
            paragraph_idx=line_info.get('paragraph_idx', 1),
            chunk_idx=line_info.get('chunk_idx', 1),
            content=line_info['content'],
            file_base=line_info['base'],
            outdir=line_info['outdir'],
            tts_mini_dir=line_info.get('tts_mini_dir', line_info['outdir']),
            voice_id=voice_id,
            model_id=model_id,
            s=self.s,
            client=self.client,
            sig=sig,
            log=self.log,
            stop_flag_ref=lambda: self.stop_requested,
            total_chunks=line_info.get('total_chunks', 1)  # 🔧 NEW: Truyền total_chunks
        )
        
        w.setAutoDelete(False)
        w._sig_ref = sig  # CRITICAL: Keep strong reference to Sig to prevent GC
        self._active_workers.append(w)
        self.pool.start(w)
        
        para_idx = line_info.get('paragraph_idx', 1)
        chunk_idx = line_info.get('chunk_idx', 1)
        self.log(f"[Worker] Bắt đầu chunk {para_idx}.{chunk_idx} (active: {self._active_worker_count}/{self._max_workers})")
        
        QtCore.QTimer.singleShot(3000, self._check_line_completion)
        
        # 🔧 FIX: Bắt đầu nhiều workers cùng lúc (không chờ 2s)
        if self._current_line_index < len(self._current_lines) and self._active_worker_count < self._max_workers:
            QtCore.QTimer.singleShot(100, self._start_next_single_line)
    
    def _on_chunk_worker_done(self, row: int, status: str):
        """🔧 NEW: Được gọi khi 1 ChunkWorker hoàn thành"""
        # 🔧 FIX: Dùng lock đã khởi tạo trong __init__ để đảm bảo thread-safe
        with self._progress_lock:
            self._active_worker_count = max(0, self._active_worker_count - 1)
            self._completed_lines += 1
            current_completed = self._completed_lines
            current_total = self._total_lines
            current_active = self._active_worker_count
            current_failed = self._failed_lines
        
        # 🔧 DEBUG: Log progress update
        self.log(f"[Progress] _on_chunk_worker_done: row={row}, status={status}, completed={current_completed}/{current_total}")
        
        if status == "DONE":
            # Tính số ký tự
            chars_raw = 0
            for line_info in self._current_lines:
                if line_info.get('row') == row:
                    chars_raw = len(line_info.get('content', ''))
                    break
            
            if chars_raw > 0:
                billed_chars = max(1, int(math.ceil(chars_raw * 1.2)))
                self._deduct_subscription_characters(billed_chars)
        
        if status in ["TIMEOUT", "Stopped", "Fail"]:
            with self._progress_lock:
                self._failed_lines += 1
                current_failed = self._failed_lines
        
        # Cập nhật progress
        if current_total > 0 and self._current_lines:
            batch_row = self._current_lines[0]['batch_row']
            percent = int((current_completed / current_total) * 100)
            self.log(f"[Progress] Updating batch_row={batch_row} to {percent}%")  # 🔧 DEBUG
            if percent >= 100:
                if current_failed > 0:
                    item = self._centered_item("100% ❌")
                else:
                    item = self._centered_item("100% ✓")
            else:
                item = self._centered_item(f"{percent}%")
            item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
            self.tbl_queue.setItem(batch_row, 3, item)
        
        self.log(f"[Worker] Chunk hoàn thành (active: {current_active}/{self._max_workers}, done: {current_completed}/{current_total})")
        
        # Nếu fail, đánh dấu và tiếp tục
        if status in ["TIMEOUT", "Fail"]:
            self.log(f"⚠️ Chunk bị {status}!")
        
        # 🔧 FIX: Kiểm tra stop_requested và subscription_exhausted trước khi start next
        if self.stop_requested or getattr(self, '_subscription_exhausted_shown', False):
            self.log(f"[Worker] Dừng - không start chunk tiếp theo (stop_requested={self.stop_requested}, exhausted={getattr(self, '_subscription_exhausted_shown', False)})")
            if current_active == 0:
                # Tất cả workers đã dừng
                self._on_all_chunks_completed()
            return
        
        # Bắt đầu chunk tiếp theo
        if self._current_line_index < len(self._current_lines):
            QtCore.QTimer.singleShot(100, self._start_next_single_line)
        elif current_active == 0:
            # Tất cả chunks đã hoàn thành → merge
            self._on_all_chunks_completed()
    
    def _on_chunk_done(self, paragraph_idx: int, chunk_idx: int, row: int, output_path: str):
        """🔧 NEW: Được gọi khi 1 chunk hoàn thành - lưu thông tin để merge sau"""
        if hasattr(self, '_paragraph_info') and paragraph_idx in self._paragraph_info:
            para_info = self._paragraph_info[paragraph_idx]
            para_info['chunk_files'][chunk_idx] = output_path
            para_info['completed_chunks'] += 1
            
            self.log(f"[Chunk] Paragraph {paragraph_idx}: {para_info['completed_chunks']}/{para_info['total_chunks']} chunks done")
        
        # 🔧 FIX: Cập nhật Output và Timing vào bảng ngay khi chunk hoàn thành
        file_exists = output_path and os.path.exists(output_path)
        self.log(f"[Chunk {paragraph_idx}.{chunk_idx}] _on_chunk_done: path={output_path}, exists={file_exists}")
        
        if row < self.tbl_sub.rowCount() and file_exists:
            # Lấy timing từ file MP3
            timing = get_mp3_duration_str(output_path)
            output_name = os.path.basename(output_path)
            
            # Cập nhật cột Output
            output_item = self._centered_item(output_name)
            output_item.setData(QtCore.Qt.UserRole, output_path)
            output_item.setToolTip(output_path)
            self.tbl_sub.setItem(row, 1, output_item)
            
            # Cập nhật cột Timing
            self.tbl_sub.setItem(row, 2, self._centered_item(timing))
    
    def _on_all_chunks_completed(self):
        """🔧 NEW: Được gọi khi tất cả chunks hoàn thành - merge các chunks thành doan_X.mp3"""
        self.log(f"[Merge] Bắt đầu merge chunks thành doan_X.mp3...")
        
        if not hasattr(self, '_paragraph_info'):
            self._on_all_lines_completed()
            return
        
        # Merge chunks của từng paragraph
        for para_idx, para_info in self._paragraph_info.items():
            # 🔧 FIX: Chỉ skip nếu paragraph đã có file doan_X.mp3 (không phải chỉ cached flag)
            tts_mini_dir = para_info['tts_mini_dir']
            doan_filename = sanitize_filename(f"doan_{para_idx}.mp3")
            final_outpath = os.path.join(tts_mini_dir, doan_filename)
            
            if os.path.exists(final_outpath) and os.path.getsize(final_outpath) > 0:
                self.log(f"[Merge] Skip paragraph {para_idx} - đã có {doan_filename}")
                continue
            
            total_chunks = para_info['total_chunks']
            chunk_files = para_info['chunk_files']
            
            if len(chunk_files) < total_chunks:
                self.log(f"[Merge] ⚠️ Paragraph {para_idx}: Chỉ có {len(chunk_files)}/{total_chunks} chunks - skip merge")
                continue
            
            # Sắp xếp chunks theo thứ tự
            sorted_chunks = [chunk_files[i] for i in sorted(chunk_files.keys())]
            
            # Verify tất cả chunk files tồn tại
            missing_files = [f for f in sorted_chunks if not os.path.exists(f)]
            if missing_files:
                self.log(f"[Merge] ⚠️ Paragraph {para_idx}: Thiếu {len(missing_files)} chunk files - skip merge")
                continue
            
            ensure_dir(tts_mini_dir)
            
            if total_chunks > 1:
                self.log(f"[Merge] Ghép {total_chunks} chunks → {doan_filename}")
                try:
                    # Sử dụng join_mp3_simple hoặc ffmpeg
                    join_mp3_simple(sorted_chunks, final_outpath, crossfade_ms=50)
                    self.log(f"[Merge] ✅ Đã tạo: {doan_filename}")
                except Exception as e:
                    self.log(f"[Merge] ❌ Lỗi merge: {e}")
                    # Fallback: copy file đầu tiên
                    import shutil
                    shutil.copy2(sorted_chunks[0], final_outpath)
            else:
                # Chỉ có 1 chunk - copy trực tiếp
                import shutil
                shutil.copy2(sorted_chunks[0], final_outpath)
                self.log(f"[Merge] ✅ Đã tạo: {doan_filename} (1 chunk)")
        
        # 🔧 FIX: Giữ lại _chunks folder - không xóa
        # User có thể cần kiểm tra từng chunk riêng lẻ
        # if self._current_lines:
        #     tts_mini_dir = self._current_lines[0].get('tts_mini_dir', '')
        #     if tts_mini_dir:
        #         chunks_dir = os.path.join(tts_mini_dir, "_chunks")
        #         if os.path.exists(chunks_dir):
        #             try:
        #                 import shutil
        #                 shutil.rmtree(chunks_dir, ignore_errors=True)
        #                 self.log(f"[Cleanup] Đã xóa thư mục _chunks")
        #             except:
        #                 pass
        
        # Tiếp tục với merge file cuối cùng
        self._on_all_lines_completed()
    
    def _on_line_worker_done(self, subtitle_row: int, status: str):
        """Được gọi khi 1 LineWorker hoàn thành (legacy - giữ lại cho compatibility)"""
        self._active_worker_count = max(0, self._active_worker_count - 1)
        self._completed_lines += 1
        
        if status == "DONE":
            # 🐛 FIX: Tính số ký tự theo công thức: chars_raw * 1.2 (làm tròn lên)
            # Giống với AccurateCreditTracker: billed_chars = max(1, int(math.ceil(chars_raw * 1.2)))
            chars_raw = 0
            for line_info in self._current_lines:
                if line_info.get('row') == subtitle_row:
                    chars_raw = len(line_info.get('content', ''))
                    break
            
            if chars_raw > 0:
                # Công thức: nhân 1.2 và làm tròn lên (để lời ~20%)
                billed_chars = max(1, int(math.ceil(chars_raw * 1.2)))
                # Trừ ký tự đã dùng từ subscription trong DB
                self._deduct_subscription_characters(billed_chars)
        
        if status in ["TIMEOUT", "Stopped", "Fail"]:
            self._failed_lines += 1
        
        if self._total_lines > 0 and self._current_lines:
            batch_row = self._current_lines[0]['batch_row']
            
            done_count = 0
            for i in range(self.tbl_sub.rowCount()):
                item = self.tbl_sub.item(i, 5)
                if item and item.text() == "DONE":
                    done_count += 1
            
            percent = int((done_count / self._total_lines) * 100)
            if percent >= 100:
                if self._failed_lines > 0:
                    item = self._centered_item("100% ❌")
                    item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
                else:
                    item = self._centered_item("100% ✓")
                    item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
            else:
                item = self._centered_item(f"{percent}%")
                item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
            self.tbl_queue.setItem(batch_row, 3, item)
        
        self.log(f"[Worker] Dòng hoàn thành (active: {self._active_worker_count}/{self._max_workers}, done: {self._completed_lines}/{self._total_lines})")
        
        if status in ["TIMEOUT", "Fail"]:
            status_msg = "TIMEOUT" if status == "TIMEOUT" else "Fail"
            self.log(f"⛔ Dòng bị {status_msg}! Dừng xử lý file này và không ghép file.")
            self.stop_requested = True
            
            if self._current_lines:
                batch_row = self._current_lines[0]['batch_row']
                fail_item = self._centered_item("Fail")
                fail_item.setBackground(QtGui.QBrush(QtGui.QColor("#dc3545")))
                fail_item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
                self.tbl_queue.setItem(batch_row, 2, fail_item)
                
                current_percent = int((self._completed_lines / self._total_lines) * 100) if self._total_lines > 0 else 0
                progress_item = self._centered_item(f"{current_percent}%")
                progress_item.setBackground(QtGui.QBrush(QtGui.QColor("#dc3545")))
                progress_item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
                self.tbl_queue.setItem(batch_row, 3, progress_item)
            
            self.failed_files += 1
            
            self._file_merging_in_progress = True
            self._cleanup_and_next_file()
            return
        
        QtCore.QTimer.singleShot(2000, self._start_next_single_line)
    
    def _check_line_completion(self):
        """Kiểm tra các dòng đã hoàn thành dựa trên Status trong bảng"""
        if self.stop_requested:
            return
        
        # 🔧 FIX: Không ghi đè _completed_lines từ UI nữa
        # Vì _on_chunk_worker_done() đã cập nhật chính xác với lock
        # Chỉ đọc giá trị hiện tại để cập nhật UI
        
        # Sử dụng lock để đọc an toàn
        if hasattr(self, '_progress_lock'):
            with self._progress_lock:
                completed = self._completed_lines
        else:
            completed = self._completed_lines
        
        if self._total_lines > 0 and self._current_lines:
            batch_row = self._current_lines[0]['batch_row']
            percent = int((completed / self._total_lines) * 100)
            item = self._centered_item(f"{percent}%")
            item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
            if percent >= 100:
                item.setBackground(QtGui.QBrush(QtGui.QColor("#28a745")))
            self.tbl_queue.setItem(batch_row, 3, item)
        
        if completed >= self._total_lines:
            self._on_all_lines_completed()
        else:
            if self._current_line_index < len(self._current_lines) or self._active_worker_count > 0:
                QtCore.QTimer.singleShot(2000, self._check_line_completion)
    
    def _on_all_lines_completed(self):
        """Được gọi khi tất cả các dòng của file hiện tại đã hoàn thành"""
        if not self._current_lines or self._file_merging_in_progress:
            return
        
        self._file_merging_in_progress = True
        
        batch_row = self._current_lines[0]['batch_row']
        fpath = self._current_lines[0]['fpath']
        base = self._current_lines[0]['base']
        outdir = self._current_lines[0]['outdir']
        
        merge_success = False
        
        if self._failed_lines > 0:
            self.log(f"[Fail] {base} có {self._failed_lines} dòng bị lỗi/timeout. Không ghép file!")
            self.tbl_queue.setItem(batch_row, 2, self._centered_item("Fail"))
            fail_item = self.tbl_queue.item(batch_row, 2)
            if fail_item:
                fail_item.setBackground(QtGui.QBrush(QtGui.QColor("#dc3545")))
                fail_item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
            self.failed_files += 1
            merge_success = False
        else:
            self.log(f"[Merge] Bắt đầu nối các file MP3 cho {base}...")
            
            time.sleep(0.5)
            
            try:
                self._merge_mp3_and_cleanup(fpath, base, outdir)
                self.tbl_queue.setItem(batch_row, 2, self._centered_item("Done"))
                progress_item = self._centered_item("100% ✓")
                progress_item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
                self.tbl_queue.setItem(batch_row, 3, progress_item)
                self.completed_files += 1
                merge_success = True
                
                # 🔧 FIX: Chỉ cộng ký tự cho các đoạn KHÔNG phải cached (đã thực sự TTS)
                total_chars_in_file = sum(
                    len(line['content']) 
                    for line in self._current_lines 
                    if not line.get('cached', False)
                )
                if total_chars_in_file > 0:
                    self.s.char_counter += total_chars_in_file
                    save_settings(self.s)
                    if hasattr(self, 'lbl_char_counter'):
                        self.lbl_char_counter.setText(f"Ký Tự Đã Dùng: {self.s.char_counter:,}")
                    self.log(f"[Counter] +{total_chars_in_file:,} ký tự → Tổng: {self.s.char_counter:,}")
                else:
                    self.log(f"[Counter] Không có ký tự mới (tất cả đã cached)")
                
                # 🐛 FIX: Cập nhật subscription label sau khi merge xong để credits được cập nhật đúng
                QtCore.QTimer.singleShot(500, self._update_subscription_label)
                
                if self.cb_autosrt.isChecked():
                    try:
                        self._generate_srt_for_file(fpath, outdir)
                    except Exception as srt_err:
                        self.log(f"[SRT] Lỗi tạo SRT: {srt_err}")
                
            except Exception as e:
                self.log(f"❌ [Merge FAIL] Không thể nối file MP3 cho {base}: {e}")
                self.log(f"❌ Kiểm tra FFmpeg có được cài đặt và hoạt động không!")
                self.tbl_queue.setItem(batch_row, 2, self._centered_item("Fail"))
                fail_item = self.tbl_queue.item(batch_row, 2)
                if fail_item:
                    fail_item.setBackground(QtGui.QBrush(QtGui.QColor("#dc3545")))
                    fail_item.setForeground(QtGui.QBrush(QtGui.QColor("#000000")))
                self.failed_files += 1
                merge_success = False
        
        for j in range(self.tbl_queue.columnCount()):
            if j == 3:
                continue
            item = self.tbl_queue.item(batch_row, j)
            if item:
                item.setBackground(QtGui.QBrush())
        
        self.lbl_result.setText(f"Kết Quả: {self.completed_files}/{self.total_files}")
        
        if not merge_success:
            self.log(f"⛔ DỪNG XỬ LÝ: File {base} không thể ghép MP3 thành công!")
            self.log(f"⛔ Đã dừng lại. Vui lòng kiểm tra lỗi và chạy lại.")
            self.stop_requested = True
            self.bt_start.setEnabled(True)
            self.bt_stop.setEnabled(False)
            self.sb_thread.setEnabled(True)
            return
        
        self.current_file_index += 1
        QtCore.QTimer.singleShot(2000, self._start_processing_file)
    
    def _cleanup_and_next_file(self):
        """Dọn dẹp và chuyển sang file tiếp theo (khi TIMEOUT, không merge)"""
        if self._current_lines:
            batch_row = self._current_lines[0]['batch_row']
            
            for j in [0, 1]:
                item = self.tbl_queue.item(batch_row, j)
                if item:
                    item.setBackground(QtGui.QBrush())
        
        self.lbl_result.setText(f"Kết Quả: {self.completed_files}/{self.total_files}")
        
        self.current_file_index += 1
        self.stop_requested = False
        QtCore.QTimer.singleShot(2000, self._start_processing_file)
    
    def _merge_mp3_and_cleanup(self, fpath: str, base: str, outdir: str):
        """Nối các file doan_X.mp3 từ tts_mini thành file tổng hợp"""
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            import sys
            self.log(f"[Merge] ❌ FFmpeg không tìm thấy!")
            self.log(f"[Debug] sys.frozen={getattr(sys, 'frozen', False)}")
            self.log(f"[Debug] sys.executable={sys.executable}")
            self.log(f"[Debug] sys.argv[0]={sys.argv[0]}")
            raise RuntimeError("FFmpeg không tìm thấy - Kiểm tra thư mục ffmpeg_bin/")
        
        self.log(f"[Merge] FFmpeg: {ffmpeg}")
        
        # Tìm các file doan_X.mp3 trong tts_mini folder
        tts_mini_dir = os.path.join(outdir, "tts_mini")
        if not os.path.exists(tts_mini_dir):
            self.log(f"[Merge] Không tìm thấy folder tts_mini trong {outdir}")
            return
        
        # Tìm tất cả file doan_*.mp3 và sắp xếp theo số thứ tự
        mp3_files = []
        for f in Path(tts_mini_dir).glob("doan_*.mp3"):
            try:
                # Extract số từ tên file: doan_1.mp3 -> 1
                para_num = int(f.stem.split('_')[1])
                mp3_files.append((para_num, f))
            except (ValueError, IndexError):
                continue
        
        # Sắp xếp theo số thứ tự paragraph
        mp3_files.sort(key=lambda x: x[0])
        mp3_files = [f for _, f in mp3_files]
        
        if not mp3_files:
            self.log(f"[Merge] Không tìm thấy file doan_*.mp3 trong {tts_mini_dir}")
            return
        
        # File tổng hợp được lưu trong folder của file (1/, 2/, ...)
        output_filename = sanitize_filename(f"{base}.mp3")
        output_path = Path(outdir) / output_filename
        
        self.log(f"[Merge] Nối {len(mp3_files)} file → {output_path.name}")
        
        srt_gaps = None
        if self.s.gap_srt_enabled and fpath.lower().endswith('.srt'):
            srt_entries = parse_srt_timings(fpath)
            if srt_entries and len(srt_entries) == len(mp3_files):
                srt_gaps = []
                for i in range(len(srt_entries) - 1):
                    end_current = srt_entries[i][1]
                    start_next = srt_entries[i + 1][0]
                    gap = max(0, start_next - end_current)
                    srt_gaps.append(gap)
                self.log(f"[SRT Gap] Phát hiện {len(srt_gaps)} khoảng cách từ SRT timing")
        
        ok, msg = join_mp3_with_silence(
            ffmpeg=ffmpeg,
            mp3_files=mp3_files,
            output_path=output_path,
            gap_enabled=self.s.gap_segments_enabled,
            gap_seconds=self.s.gap_seconds,
            gap_every=self.s.gap_every,
            srt_gaps=srt_gaps
        )
        
        if not ok:
            raise RuntimeError(f"Nối file thất bại: {msg}")
        
        # 🔧 Move file MP3 ra cùng vị trí với file TXT gốc
        import shutil
        txt_dir = os.path.dirname(fpath)  # Folder chứa file TXT gốc
        final_mp3_path = os.path.join(txt_dir, output_path.name)
        
        self.log(f"[Merge] Moving: {output_path} → {final_mp3_path}")
        
        try:
            # Xóa file cũ nếu tồn tại
            if os.path.exists(final_mp3_path):
                os.remove(final_mp3_path)
            shutil.move(str(output_path), final_mp3_path)
            self.log(f"[Merge] ✅ Đã tạo: {final_mp3_path}")
            self._last_merged_mp3 = final_mp3_path
        except Exception as e:
            self.log(f"[Merge] ⚠️ Không thể move file: {e}, giữ nguyên tại {output_path}")
            self._last_merged_mp3 = str(output_path)
        
        # 🔧 FIX: Giữ lại _chunks folder - không xóa
        # User có thể cần kiểm tra từng chunk riêng lẻ
        # chunks_dir = Path(tts_mini_dir) / "_chunks"
        # if chunks_dir.exists():
        #     try:
        #         shutil.rmtree(chunks_dir, ignore_errors=True)
        #         self.log(f"[Cleanup] Đã xóa thư mục _chunks")
        #     except Exception as e:
        #         self.log(f"[Cleanup] Không thể xóa _chunks: {e}")
        
        try:
            outdir_path = Path(outdir)
            silence_files = list(outdir_path.glob("_silence_*.mp3")) + list(outdir_path.glob("silent_*.mp3"))
            for sf in silence_files:
                try:
                    sf.unlink()
                    self.log(f"[Cleanup] Đã xóa file silence: {sf.name}")
                except:
                    pass
        except Exception as e:
            self.log(f"[Cleanup] Lỗi xóa silence files: {e}")
    
    def _generate_srt_for_file(self, fpath: str, outdir: str):
        """Tạo file SRT với timing CHÍNH XÁC + Scale Factor
        
        Pipeline 6 bước:
        1. Tìm ffprobe
        2. Đọc RAW durations từ từng MP3 segment
        3. Đo duration THỰC của silence file (nếu có gap)
        4. Tính tổng Gap duration
        5. Calculate Scale Factor từ merged MP3
        6. Ghi file SRT
        """
        ffprobe = find_ffprobe()
        if not ffprobe:
            self.log("[SRT] Không tìm thấy ffprobe, bỏ qua tạo SRT")
            return
        
        # Step 2: Tìm file doan_X.mp3 trong tts_mini folder
        tts_mini_dir = outdir
        if self._current_lines:
            tts_mini_dir = self._current_lines[0].get('tts_mini_dir', outdir)
        
        mp3_files = sorted(
            [f for f in Path(tts_mini_dir).glob("doan_*.mp3")],
            key=lambda x: int(x.stem.replace('doan_', ''))
        )
        
        if not mp3_files:
            self.log(f"[SRT] Không có file doan_X.mp3 trong {tts_mini_dir}")
            return
        
        parts = [line['content'] for line in self._current_lines]
        
        if len(parts) != len(mp3_files):
            self.log(f"[SRT] Cảnh báo: {len(parts)} parts nhưng {len(mp3_files)} mp3")
        
        # Đọc RAW duration từng segment
        raw_durations = []
        for mp3 in mp3_files:
            dur = self._get_mp3_duration(ffprobe, str(mp3))
            raw_durations.append(dur)
        raw_total = sum(raw_durations)
        
        # Step 3: Đo duration THỰC của silence file (nếu có gap)
        gap_enabled = self.s.gap_segments_enabled
        gap_seconds = self.s.gap_seconds
        gap_every = self.s.gap_every
        actual_gap_seconds = gap_seconds
        
        if gap_enabled and gap_seconds > 0:
            silence_path = Path(tts_mini_dir) / f"_silence_{gap_seconds:.1f}s.mp3"
            if silence_path.exists():
                actual_gap_seconds = self._get_mp3_duration(ffprobe, str(silence_path))
        
        # Step 4: Tính tổng Gap duration
        total_gaps = 0
        if gap_enabled and gap_every > 0:
            num_parts = len(mp3_files)
            for seg_idx in range(1, num_parts + 1):
                if seg_idx % gap_every == 0 and seg_idx < num_parts:
                    total_gaps += 1
        total_gaps_duration = total_gaps * actual_gap_seconds
        
        # Step 5: Calculate Scale Factor
        scale = 1.0
        durations = raw_durations
        merged_total = 0.0
        
        # Tìm file MP3 đã merge
        merged_mp3 = Path(fpath).with_suffix(".mp3")
        if not merged_mp3.exists():
            base_name = Path(fpath).stem
            candidates = [
                Path(outdir).parent / f"{base_name}.mp3",
                Path(outdir) / f"{base_name}.mp3",
            ]
            for cand in candidates:
                if cand.exists():
                    merged_mp3 = cand
                    break
        
        if merged_mp3.exists() and raw_total > 0:
            merged_total = self._get_mp3_duration(ffprobe, str(merged_mp3))
            target_content = merged_total - total_gaps_duration
            if target_content > 0:
                scale = target_content / raw_total
                if scale < 0.5 or scale > 2.0:
                    self.log(f"[SRT] ⚠️ Scale {scale:.4f} bất thường (merged={merged_total:.2f}s, raw={raw_total:.2f}s, gaps={total_gaps_duration:.2f}s)")
                durations = [d * scale for d in raw_durations]
                self.log(f"[SRT] Scale factor: {scale:.4f} (merged={merged_total:.2f}s, raw={raw_total:.2f}s, gaps={total_gaps_duration:.2f}s)")
        else:
            self.log(f"[SRT] ⚠️ Không tìm thấy merged MP3, bỏ qua scale factor")
        
        # Step 6: Ghi file SRT
        srt_path = Path(fpath).with_suffix(".srt")
        self._write_srt(
            srt_path, 
            parts, 
            durations,
            gap_enabled=gap_enabled,
            gap_seconds=actual_gap_seconds,
            gap_every=gap_every,
            merged_duration=merged_total
        )
        
        total_dur = sum(durations)
        self.log(f"[SRT] ✅ Đã tạo: {srt_path.name} ({total_dur:.2f}s, {len(parts)} parts, scale={scale:.4f})")
    
    def _write_srt(self, srt_path: Path, parts: List[str], durations: List[float],
                    gap_enabled: bool = False, gap_seconds: float = 0, gap_every: int = 5,
                    merged_duration: float = 0.0):
        """Ghi file SRT với Smart Split + timing chính xác
        
        Logic: 
        - Chia text thành subtitle ≤ 80 ký tự (char-based split)
        - Duration mỗi subtitle = tỉ lệ theo ký tự, tối thiểu 0.3s
        - THÊM gaps vào timeline sau mỗi gap_every segments
        - Subtitle cuối kết thúc đúng merged_duration
        """
        MAX_CHARS_PER_SUBTITLE = 100
        BREAK_CHARS = '.!?。！？,;:，；：、'
        
        import re as _re
        _AUDIO_TAG_RE = _re.compile(r'\[[^\[\]]{1,80}\]\s*')
        
        def _strip_audio_tags(text: str) -> str:
            """Loại bỏ audio tags [tag] khỏi text SRT — chỉ hiển thị nội dung"""
            return _AUDIO_TAG_RE.sub('', text).strip()
        
        def format_time(seconds: float) -> str:
            """Convert seconds to SRT time format: HH:MM:SS,mmm"""
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            millis = int((seconds - int(seconds)) * 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        
        def split_by_max_chars(text: str, max_chars: int) -> List[str]:
            """Chia text thành chunks ≤ max_chars.
            Ưu tiên cắt:
            1. Dấu câu (tìm ngược 50 ký tự từ max_chars)
            2. Space (tìm ngược 30 ký tự)
            3. Cắt cứng tại max_chars
            """
            text = text.strip().replace('\r\n', ' ').replace('\n', ' ')
            if len(text) <= max_chars:
                return [text] if text else []
            
            chunks = []
            remaining = text
            while remaining:
                if len(remaining) <= max_chars:
                    chunks.append(remaining.strip())
                    break
                
                cut_pos = max_chars
                # Tìm ngược 50 ký tự để tìm dấu câu
                for j in range(max_chars - 1, max(0, max_chars - 50), -1):
                    if remaining[j] in BREAK_CHARS:
                        cut_pos = j + 1  # Cắt SAU dấu câu
                        break
                
                # Nếu không có dấu câu → tìm space
                if cut_pos == max_chars:
                    for j in range(max_chars - 1, max(0, max_chars - 30), -1):
                        if remaining[j] == ' ':
                            cut_pos = j
                            break
                
                chunks.append(remaining[:cut_pos].strip())
                remaining = remaining[cut_pos:].lstrip()
            
            return chunks
        
        current_time = 0.0
        lines = []
        actual_idx = 0
        segment_idx = 0
        total_parts = len(parts)
        
        for part_text, part_duration in zip(parts, durations):
            if not part_text.strip():
                continue
            
            segment_idx += 1
            
            # Chia text thành subtitle ngắn
            sub_chunks = split_by_max_chars(part_text, MAX_CHARS_PER_SUBTITLE)
            
            if not sub_chunks:
                continue
            
            # Phân bổ duration theo tỷ lệ ký tự (two-pass)
            total_chars = sum(len(s) for s in sub_chunks)
            
            # Pass 1: Tính proportional durations
            chunk_infos = []
            for chunk in sub_chunks:
                chunk_chars = len(chunk)
                if total_chars > 0:
                    prop_dur = (chunk_chars / total_chars) * part_duration
                else:
                    prop_dur = part_duration / len(sub_chunks)
                chunk_infos.append((chunk, prop_dur))
            
            # Pass 2: Ghi subtitles
            segment_start = current_time
            for i, (chunk, prop_dur) in enumerate(chunk_infos):
                actual_idx += 1
                
                # Duration tối thiểu 0.3s
                chunk_duration = max(0.3, prop_dur)
                
                # Chunk cuối: điều chỉnh end = segment_start + part_duration
                if i == len(chunk_infos) - 1:
                    expected_end = segment_start + part_duration
                    chunk_duration = expected_end - current_time
                    if chunk_duration < 0.3:
                        chunk_duration = 0.3
                
                start = current_time
                end = current_time + chunk_duration
                
                lines.append(str(actual_idx))
                lines.append(f"{format_time(start)} --> {format_time(end)}")
                lines.append(_strip_audio_tags(chunk.strip()))
                lines.append("")
                
                current_time = end
            
            # Sửa current_time nếu chưa đến expected_end
            expected_end = segment_start + part_duration
            if current_time < expected_end:
                current_time = expected_end
            
            # Chèn gap sau segment
            if gap_enabled and gap_seconds > 0 and gap_every > 0:
                if segment_idx % gap_every == 0 and segment_idx < total_parts:
                    current_time += gap_seconds
        
        # Điều chỉnh subtitle cuối = merged_duration (v3.8.4)
        if merged_duration > 0 and len(lines) >= 4:
            last_ts_idx = len(lines) - 3  # Dòng timestamp cuối
            if '-->' in lines[last_ts_idx]:
                parts_ts = lines[last_ts_idx].split(' --> ')
                start_ts = parts_ts[0]
                new_end_ts = format_time(merged_duration)
                lines[last_ts_idx] = f"{start_ts} --> {new_end_ts}"
        
        # Ghi ra file
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    
    def start_next_in_queue(self):
        pass

    def _on_progress(self, row:int, current:int, total:int):
        """Cập nhật % tiến độ của file"""
        if total > 0:
            percent = int((current / total) * 100)
            self.tbl_queue.setItem(row, 3, self._centered_item(f"{percent}%"))

    def _on_status(self, row:int, st:str):
        self.tbl_queue.setItem(row,2, self._centered_item(st))
    
    def _on_subtitle_status(self, subtitle_row: int, status: str):
        """Cập nhật Status trong bảng Subtitles"""
        if subtitle_row < self.tbl_sub.rowCount():
            self.tbl_sub.setItem(subtitle_row, 6, self._centered_item(status))  # Status
    
    def _on_chunk_status(self, chunk_row: int, status: str):
        """🔧 NEW: Cập nhật Status cho từng chunk row riêng biệt"""
        # 🔧 FIX: Throttle UI updates để tránh "not responding"
        # Chỉ update ngay lập tức cho status quan trọng (Done, Fail, Processing)
        important_statuses = ["Done", "✅", "Fail", "TIMEOUT", "Processing", "Stopped"]
        is_important = any(s in status for s in important_statuses)
        
        if not is_important:
            # Throttle: chỉ update mỗi 200ms cho status không quan trọng
            now = time.time()
            if not hasattr(self, '_last_status_update'):
                self._last_status_update = {}
            
            last_update = self._last_status_update.get(chunk_row, 0)
            if now - last_update < 0.2:  # 200ms throttle
                return
            self._last_status_update[chunk_row] = now
        
        if chunk_row < self.tbl_sub.rowCount():
            status_item = self._centered_item(status)
            
            # Đổi màu nền nếu Done
            if "Done" in status or "✅" in status:
                status_item.setBackground(QtGui.QColor(200, 230, 200))  # Light green
            
            self.tbl_sub.setItem(chunk_row, 6, status_item)
    
    def _on_subtitle_output(self, subtitle_row: int, output_path: str, timing: str):
        """Cập nhật Output và Timing trong bảng Subtitles sau khi gen xong"""
        if subtitle_row < self.tbl_sub.rowCount():
            # Hiển thị tên file ngắn gọn
            output_name = os.path.basename(output_path) if output_path else ""
            output_item = self._centered_item(output_name)
            # Lưu đường dẫn đầy đủ vào UserRole để có thể mở khi click
            output_item.setData(QtCore.Qt.UserRole, output_path)
            output_item.setToolTip(output_path)  # Hiển thị full path khi hover
            self.tbl_sub.setItem(subtitle_row, 1, output_item)  # Output
            self.tbl_sub.setItem(subtitle_row, 2, self._centered_item(timing))  # Timing
    
    def _on_subtitle_cell_double_clicked(self, row: int, col: int):
        """Xử lý double-click trên bảng Subtitles - mở file MP3 khi click vào cột Output"""
        if col == 1:  # Cột Output
            item = self.tbl_sub.item(row, col)
            if item:
                file_path = item.data(QtCore.Qt.UserRole)
                if file_path and os.path.exists(file_path):
                    # Mở Explorer và select file
                    try:
                        import subprocess
                        if os.name == 'nt':  # Windows
                            subprocess.Popen(['explorer', '/select,', os.path.abspath(file_path)])
                            self.log(f"📁 Đã mở thư mục chứa: {file_path}")
                        else:
                            # Linux/Mac - mở thư mục chứa file
                            folder = os.path.dirname(file_path)
                            subprocess.Popen(['xdg-open', folder])
                    except Exception as e:
                        self.log(f"❌ Không thể mở file: {e}")
                elif file_path:
                    self.log(f"⚠ File chưa tồn tại: {file_path}")
                else:
                    self.log(f"⚠ Chưa có file output cho dòng này")

    def _on_done_then_next(self, row:int, st:str):
        self.tbl_queue.setItem(row,2, self._centered_item(st))
        
        if st in ("Fail", "Stopped"):
            self.tbl_queue.setItem(row,3, self._centered_item("0%"))
        else:
            self.tbl_queue.setItem(row,3, self._centered_item("100%"))
        
        self.current_worker=None
        
        if st == "Done":
            self.completed_files += 1
        self.lbl_result.setText(f"Kết Quả: {self.completed_files}/{self.total_files}")
        
        if not self.stop_requested and self.queue_paths:
            QtCore.QTimer.singleShot(200, self.start_next_in_queue)
        else:
            self.bt_start.setEnabled(True)
            self.bt_stop.setEnabled(False)

    def stop_all(self):
        self.stop_requested=True
        
        if hasattr(self, '_active_workers'):
            for worker in self._active_workers:
                if worker:
                    worker.stop()
        
        if self.current_worker:
            self.current_worker.stop()
        
        self.log("⛔ Stop signal sent - Đang dừng tất cả workers...")
        
        self.bt_start.setEnabled(True)
        self.bt_start.setStyleSheet("")
        self.bt_stop.setEnabled(False)
        self.sb_thread.setEnabled(True)
    
    def closeEvent(self, event):
        """Xử lý đóng app - hiện popup xác nhận trước khi thoát"""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Xác Nhận Thoát")
        dlg.setModal(True)
        dlg.setFixedSize(300, 120)
        
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(15)
        
        lbl = QtWidgets.QLabel("👋 Hẹn gặp lại!")
        lbl.setStyleSheet("font-size: 14pt; font-weight: bold; color: #2c3e50;")
        lbl.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(lbl)
        
        # Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(20)
        
        btn_exit = QtWidgets.QPushButton("Thoát")
        btn_exit.setFixedSize(80, 30)
        btn_exit.setStyleSheet("background-color: #e74c3c; color: black; font-weight: bold;")
        btn_exit.clicked.connect(dlg.accept)
        
        btn_cancel = QtWidgets.QPushButton("Không")
        btn_cancel.setFixedSize(80, 30)
        btn_cancel.setStyleSheet("background-color: #3498db; color: black; font-weight: bold;")
        btn_cancel.clicked.connect(dlg.reject)
        
        btn_layout.addWidget(btn_exit)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)
        
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            event.ignore()
            return
        
        self.stop_requested = True
        
        if hasattr(self, '_active_workers'):
            for worker in self._active_workers:
                if worker:
                    try:
                        worker.stop()
                    except:
                        pass
        
        if self.current_worker:
            try:
                self.current_worker.stop()
            except:
                pass
        
        # Clear thread pool (only if initialized)
        if hasattr(self, 'pool') and self.pool:
            self.pool.clear()
        if not self.pool.waitForDone(2000):
            print("[App] Một số threads chưa kết thúc, force quit...")
        
        # Flush logs
        flush_all_logs()
        
        # 🔧 FIX: Lưu settings trước khi đóng app
        try:
            if hasattr(self, 's') and self.s:
                save_settings(self.s)
        except Exception as e:
            print(f"⚠️ Error saving settings on close: {e}")
        
        event.accept()
        print("[App] Đóng app hoàn tất")

# ---- run ----

def main():
    app = QtWidgets.QApplication([])
    
    w = MainWindow()
    w.show()
    
    exit_code = app.exec()
    
    # Flush logs
    flush_all_logs()
    
    os._exit(exit_code)

if __name__=="__main__":
    main()
