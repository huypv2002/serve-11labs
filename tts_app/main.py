#!/usr/bin/env python
"""
ElevenLabs Preview TTS App - Camoufox + HSW
Auto resolve captcha, token pool, proxy management, TTS generation.
"""
import asyncio
import os
import sys
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread

# ============================================================
# Async Bridge - chạy asyncio event loop trong thread riêng
# ============================================================

class AsyncBridge(QObject):
    """Chạy asyncio event loop trong QThread riêng, giao tiếp qua signals."""
    log_signal = Signal(str)
    stats_signal = Signal(dict)
    tts_done_signal = Signal(int, bool, str, bytes)  # job_idx, success, msg, audio_bytes

    def __init__(self):
        super().__init__()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._token_pool = None
        self._proxy_pool = None

    def start_loop(self):
        """Khởi động asyncio loop trong thread riêng."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_coro(self, coro):
        """Schedule coroutine trên async loop."""
        if self._loop and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        return None

    async def init_pools(self, proxy_keys: list, pool_size: int):
        """Khởi tạo ProxyPool + TokenPool."""
        from proxy_pool import ProxyPool
        from token_solver import TokenPool

        self._proxy_pool = ProxyPool(proxy_keys)
        self._token_pool = TokenPool(self._proxy_pool, target_size=pool_size)
        self._token_pool.set_log_callback(lambda msg: self.log_signal.emit(msg))
        await self._token_pool.start()

    async def stop_pools(self):
        if self._token_pool:
            await self._token_pool.stop()

    async def add_proxy_key(self, key: str) -> bool:
        if self._proxy_pool:
            result = await self._proxy_pool.add_key(key)
            if result and self._token_pool:
                await self._token_pool.restart_solvers()
            return result
        return False

    async def remove_proxy_key(self, key: str) -> bool:
        if self._proxy_pool:
            result = await self._proxy_pool.remove_key(key)
            if result and self._token_pool:
                await self._token_pool.restart_solvers()
            return result
        return False

    async def do_tts(self, job_idx: int, text: str, voice_id: str, model_id: str,
                     speed: float, language: str, stability: float, similarity: float):
        """Lấy token từ pool và gọi TTS."""
        from tts_engine import tts_preview
        try:
            self.log_signal.emit(f"[tts-{job_idx}] Đang lấy token...")
            token, proxy = await self._token_pool.get_token(timeout=90.0)
            self.log_signal.emit(f"[tts-{job_idx}] Có token, gọi API...")

            audio = await tts_preview(
                text=text,
                hcaptcha_token=token,
                proxy_http=proxy["http"],
                voice_id=voice_id,
                model_id=model_id,
                speed=speed,
                language_code=language,
                stability=stability,
                similarity_boost=similarity,
            )
            self.tts_done_signal.emit(job_idx, True, f"OK ({len(audio)//1024}KB)", audio)
        except Exception as e:
            self.tts_done_signal.emit(job_idx, False, str(e)[:100], b"")

    def get_stats(self) -> dict:
        if self._token_pool:
            return self._token_pool.stats
        return {}


# ============================================================
# Default Voices
# ============================================================

DEFAULT_VOICES = [
    ("Aria", "NOpBlnGInO9m6vDvFkFC"),
    ("Roger", "CwhRBWXzGAHq8TQ4Fs17"),
    ("Sarah", "EXAVITQu4vr4xnSDxMaL"),
    ("Laura", "FGY2WhTYpPnrIDTdsKH5"),
    ("Charlie", "IKne3meq5aSn9XLyUdCD"),
    ("George", "JBFqnCBsd6RMkjVDRZzb"),
    ("Callum", "N2lVS1w4EtoT3dr4eOWO"),
    ("River", "SAz9YHcvj6GT2YYXdXww"),
    ("Liam", "TX3LPaxmHKxFdv7VOQHJ"),
    ("Alice", "Xb7hH8MSUJpSbSDYk0k2"),
    ("Matilda", "XrExE9yKIg1WjnnlVkGX"),
    ("Will", "bIHbv24MWmeRgasZH58o"),
    ("Jessica", "cgSgspJ2msm6clMCkdW9"),
    ("Eric", "cjVigY5qzO86Huf0OWal"),
    ("Chris", "iP95p4xoKVk53GoZ742B"),
    ("Brian", "nPczCjzI2devNBz1zQrb"),
    ("Daniel", "onwK4e9ZLuTAKqWW03F9"),
    ("Lily", "pFZP5JQG7iQjIQuC4Bku"),
]

MODELS = [
    ("eleven_v3", "Eleven V3 (mới nhất)"),
    ("eleven_multilingual_v2", "Multilingual V2"),
    ("eleven_turbo_v2_5", "Turbo V2.5"),
    ("eleven_turbo_v2", "Turbo V2"),
]


# ============================================================
# Main Window
# ============================================================

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ElevenLabs Preview TTS - HSW Auto")
        self.resize(1300, 850)

        self._bridge = AsyncBridge()
        self._bridge.log_signal.connect(self._append_log)
        self._bridge.stats_signal.connect(self._poll_stats)
        self._bridge.tts_done_signal.connect(self._on_tts_done)
        self._bridge.start_loop()

        self._jobs = []  # list of (text, status)
        self._output_dir = Path.home() / "Desktop" / "tts_output"

        self._build_ui()
        self._apply_style()

        # Timer cập nhật stats
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._poll_stats)
        self._stats_timer.start(2000)

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(12)

        # Header
        header = QtWidgets.QLabel("ElevenLabs Preview TTS - Camoufox HSW")
        header.setObjectName("Header")
        main_layout.addWidget(header)

        # Tabs
        self.tabs = QtWidgets.QTabWidget()
        main_layout.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_tts_tab(), "TTS")
        self.tabs.addTab(self._build_pool_tab(), "Token Pool")
        self.tabs.addTab(self._build_proxy_tab(), "Proxy")
        self.tabs.addTab(self._build_settings_tab(), "Cài đặt")

        # Log
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(180)
        self.log_box.setPlaceholderText("Log...")
        main_layout.addWidget(self.log_box)

    # ---- Tab TTS ----
    def _build_tts_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(widget)
        layout.setSpacing(12)

        # Left: input
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(10)

        # Voice
        voice_row = QtWidgets.QHBoxLayout()
        voice_row.addWidget(QtWidgets.QLabel("Giọng:"))
        self.voice_combo = QtWidgets.QComboBox()
        for name, vid in DEFAULT_VOICES:
            self.voice_combo.addItem(f"{name} ({vid[:8]}...)", vid)
        voice_row.addWidget(self.voice_combo, 1)
        left.addLayout(voice_row)

        # Model
        model_row = QtWidgets.QHBoxLayout()
        model_row.addWidget(QtWidgets.QLabel("Model:"))
        self.model_combo = QtWidgets.QComboBox()
        for mid, label in MODELS:
            self.model_combo.addItem(label, mid)
        model_row.addWidget(self.model_combo, 1)
        left.addLayout(model_row)

        # Language
        lang_row = QtWidgets.QHBoxLayout()
        lang_row.addWidget(QtWidgets.QLabel("Ngôn ngữ:"))
        self.lang_input = QtWidgets.QLineEdit("vi")
        self.lang_input.setMaximumWidth(60)
        lang_row.addWidget(self.lang_input)
        lang_row.addStretch()
        left.addLayout(lang_row)

        # Text input
        left.addWidget(QtWidgets.QLabel("Nội dung (mỗi dòng = 1 job):"))
        self.text_input = QtWidgets.QPlainTextEdit()
        self.text_input.setPlaceholderText("Nhập text ở đây...\nMỗi dòng sẽ thành 1 file audio.")
        left.addWidget(self.text_input, 1)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Bắt đầu TTS")
        self.btn_start.setObjectName("PrimaryBtn")
        self.btn_start.clicked.connect(self._start_tts)
        self.btn_stop = QtWidgets.QPushButton("Dừng")
        self.btn_stop.setObjectName("DangerBtn")
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        left.addLayout(btn_row)

        layout.addLayout(left, 1)

        # Right: results table
        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Kết quả:"))
        self.result_table = QtWidgets.QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["#", "Nội dung", "Trạng thái", "File"])
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.result_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        right.addWidget(self.result_table, 1)

        # Output dir
        dir_row = QtWidgets.QHBoxLayout()
        dir_row.addWidget(QtWidgets.QLabel("Thư mục:"))
        self.dir_input = QtWidgets.QLineEdit(str(self._output_dir))
        dir_row.addWidget(self.dir_input, 1)
        btn_dir = QtWidgets.QPushButton("Chọn")
        btn_dir.clicked.connect(self._choose_dir)
        dir_row.addWidget(btn_dir)
        btn_open = QtWidgets.QPushButton("Mở")
        btn_open.clicked.connect(self._open_dir)
        dir_row.addWidget(btn_open)
        right.addLayout(dir_row)

        layout.addLayout(right, 1)
        return widget

    # ---- Tab Token Pool ----
    def _build_pool_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setSpacing(12)

        # Stats
        stats_group = QtWidgets.QGroupBox("Trạng thái Token Pool")
        stats_layout = QtWidgets.QFormLayout(stats_group)
        self.lbl_pool_size = QtWidgets.QLabel("0")
        self.lbl_pool_target = QtWidgets.QLabel("5")
        self.lbl_solving = QtWidgets.QLabel("0")
        self.lbl_total_solved = QtWidgets.QLabel("0")
        self.lbl_total_served = QtWidgets.QLabel("0")
        self.lbl_total_expired = QtWidgets.QLabel("0")
        self.lbl_total_failed = QtWidgets.QLabel("0")
        stats_layout.addRow("Token sẵn sàng:", self.lbl_pool_size)
        stats_layout.addRow("Mục tiêu:", self.lbl_pool_target)
        stats_layout.addRow("Đang solve:", self.lbl_solving)
        stats_layout.addRow("Tổng đã solve:", self.lbl_total_solved)
        stats_layout.addRow("Tổng đã dùng:", self.lbl_total_served)
        stats_layout.addRow("Tổng hết hạn:", self.lbl_total_expired)
        stats_layout.addRow("Tổng lỗi:", self.lbl_total_failed)
        layout.addWidget(stats_group)

        # Controls
        ctrl_row = QtWidgets.QHBoxLayout()
        self.btn_start_pool = QtWidgets.QPushButton("Khởi động Pool")
        self.btn_start_pool.setObjectName("PrimaryBtn")
        self.btn_start_pool.clicked.connect(self._start_pool)
        self.btn_stop_pool = QtWidgets.QPushButton("Dừng Pool")
        self.btn_stop_pool.setObjectName("DangerBtn")
        self.btn_stop_pool.clicked.connect(self._stop_pool)
        ctrl_row.addWidget(self.btn_start_pool)
        ctrl_row.addWidget(self.btn_stop_pool)
        layout.addLayout(ctrl_row)

        # Pool size setting
        size_row = QtWidgets.QHBoxLayout()
        size_row.addWidget(QtWidgets.QLabel("Pool size:"))
        self.pool_size_spin = QtWidgets.QSpinBox()
        self.pool_size_spin.setRange(1, 20)
        self.pool_size_spin.setValue(5)
        size_row.addWidget(self.pool_size_spin)
        size_row.addStretch()
        layout.addLayout(size_row)

        layout.addStretch()
        return widget

    # ---- Tab Proxy ----
    def _build_proxy_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setSpacing(12)

        layout.addWidget(QtWidgets.QLabel("Proxy Keys (proxyxoay.shop):"))

        self.proxy_list = QtWidgets.QListWidget()
        layout.addWidget(self.proxy_list, 1)

        # Add/Remove
        row = QtWidgets.QHBoxLayout()
        self.proxy_input = QtWidgets.QLineEdit()
        self.proxy_input.setPlaceholderText("Nhập proxy key...")
        row.addWidget(self.proxy_input, 1)
        btn_add = QtWidgets.QPushButton("Thêm")
        btn_add.setObjectName("PrimaryBtn")
        btn_add.clicked.connect(self._add_proxy_key)
        row.addWidget(btn_add)
        btn_remove = QtWidgets.QPushButton("Xóa chọn")
        btn_remove.setObjectName("DangerBtn")
        btn_remove.clicked.connect(self._remove_proxy_key)
        row.addWidget(btn_remove)
        layout.addLayout(row)

        # Bulk add
        layout.addWidget(QtWidgets.QLabel("Thêm nhiều key (mỗi dòng 1 key):"))
        self.proxy_bulk = QtWidgets.QPlainTextEdit()
        self.proxy_bulk.setMaximumHeight(100)
        layout.addWidget(self.proxy_bulk)
        btn_bulk = QtWidgets.QPushButton("Thêm tất cả")
        btn_bulk.clicked.connect(self._add_bulk_keys)
        layout.addWidget(btn_bulk)

        return widget

    # ---- Tab Settings ----
    def _build_settings_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(widget)
        layout.setSpacing(12)

        self.speed_spin = QtWidgets.QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 2.0)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSingleStep(0.1)
        layout.addRow("Tốc độ:", self.speed_spin)

        self.stability_spin = QtWidgets.QSpinBox()
        self.stability_spin.setRange(0, 100)
        self.stability_spin.setValue(50)
        layout.addRow("Stability (%):", self.stability_spin)

        self.similarity_spin = QtWidgets.QSpinBox()
        self.similarity_spin.setRange(0, 100)
        self.similarity_spin.setValue(75)
        layout.addRow("Similarity (%):", self.similarity_spin)

        self.thread_spin = QtWidgets.QSpinBox()
        self.thread_spin.setRange(1, 10)
        self.thread_spin.setValue(3)
        layout.addRow("Số job song song:", self.thread_spin)

        return widget

    # ---- Style ----
    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #1a1a2e;
                color: #e0e0e0;
                font-family: "SF Pro Display", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            #Header {
                font-size: 20px;
                font-weight: 700;
                color: #00d4aa;
                padding: 8px 0;
            }
            QTabWidget::pane {
                border: 1px solid #2d2d44;
                border-radius: 8px;
                background: #16213e;
            }
            QTabBar::tab {
                background: #1a1a2e;
                border: 1px solid #2d2d44;
                padding: 8px 16px;
                margin: 0 2px;
                border-radius: 6px 6px 0 0;
                color: #8888aa;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #16213e;
                color: #00d4aa;
                border-bottom: none;
            }
            QGroupBox {
                border: 1px solid #2d2d44;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 20px;
                font-weight: 700;
                color: #00d4aa;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background: #0f3460;
                border: 1px solid #2d2d44;
                border-radius: 6px;
                padding: 6px 10px;
                color: #e0e0e0;
            }
            QListWidget {
                background: #0f3460;
                border: 1px solid #2d2d44;
                border-radius: 6px;
            }
            QTableWidget {
                background: #0f3460;
                border: 1px solid #2d2d44;
                border-radius: 6px;
                gridline-color: #2d2d44;
            }
            QHeaderView::section {
                background: #1a1a2e;
                color: #00d4aa;
                border: none;
                border-bottom: 1px solid #2d2d44;
                padding: 8px;
                font-weight: 700;
            }
            QPushButton {
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 700;
                border: 1px solid #2d2d44;
                background: #0f3460;
                color: #e0e0e0;
            }
            QPushButton:hover {
                background: #1a4a7a;
            }
            #PrimaryBtn {
                background: #00d4aa;
                color: #0a0a1a;
                border: none;
            }
            #PrimaryBtn:hover {
                background: #00b894;
            }
            #DangerBtn {
                background: #e74c3c;
                color: white;
                border: none;
            }
            #DangerBtn:hover {
                background: #c0392b;
            }
            QLabel {
                color: #c0c0d0;
            }
        """)

    # ---- Actions ----
    def _append_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"[{ts}] {msg}")

    def _poll_stats(self):
        stats = self._bridge.get_stats()
        if stats:
            self.lbl_pool_size.setText(str(stats.get("pool_size", 0)))
            self.lbl_pool_target.setText(str(stats.get("pool_target", 0)))
            self.lbl_solving.setText(str(stats.get("solving_now", 0)))
            self.lbl_total_solved.setText(str(stats.get("total_solved", 0)))
            self.lbl_total_served.setText(str(stats.get("total_served", 0)))
            self.lbl_total_expired.setText(str(stats.get("total_expired", 0)))
            self.lbl_total_failed.setText(str(stats.get("total_failed", 0)))

    def _start_pool(self):
        keys = [self.proxy_list.item(i).data(Qt.UserRole) for i in range(self.proxy_list.count())]
        if not keys:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Thêm ít nhất 1 proxy key trước!")
            return
        pool_size = self.pool_size_spin.value()
        self._bridge.run_coro(self._bridge.init_pools(keys, pool_size))
        self._append_log(f"Khởi động pool: {len(keys)} key, target={pool_size}")

    def _stop_pool(self):
        self._bridge.run_coro(self._bridge.stop_pools())
        self._append_log("Đã dừng pool")

    def _add_proxy_key(self):
        key = self.proxy_input.text().strip()
        if not key or len(key) < 10:
            return
        item = QtWidgets.QListWidgetItem(f"{key[:8]}...{key[-4:]}")
        item.setData(Qt.UserRole, key)
        self.proxy_list.addItem(item)
        self.proxy_input.clear()
        self._bridge.run_coro(self._bridge.add_proxy_key(key))
        self._append_log(f"Đã thêm proxy key: {key[:8]}...")

    def _remove_proxy_key(self):
        current = self.proxy_list.currentItem()
        if not current:
            return
        key = current.data(Qt.UserRole)
        self.proxy_list.takeItem(self.proxy_list.row(current))
        self._bridge.run_coro(self._bridge.remove_proxy_key(key))
        self._append_log(f"Đã xóa proxy key: {key[:8]}...")

    def _add_bulk_keys(self):
        text = self.proxy_bulk.toPlainText().strip()
        if not text:
            return
        count = 0
        for line in text.splitlines():
            key = line.strip()
            if key and len(key) >= 10:
                item = QtWidgets.QListWidgetItem(f"{key[:8]}...{key[-4:]}")
                item.setData(Qt.UserRole, key)
                self.proxy_list.addItem(item)
                self._bridge.run_coro(self._bridge.add_proxy_key(key))
                count += 1
        self.proxy_bulk.clear()
        self._append_log(f"Đã thêm {count} proxy key")

    def _choose_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục output")
        if path:
            self.dir_input.setText(path)
            self._output_dir = Path(path)

    def _open_dir(self):
        path = Path(self.dir_input.text().strip())
        path.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def _start_tts(self):
        text = self.text_input.toPlainText().strip()
        if not text:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Nhập nội dung text!")
            return

        if self.proxy_list.count() == 0:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Thêm proxy key trước!")
            return

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        voice_id = self.voice_combo.currentData()
        model_id = self.model_combo.currentData()
        speed = self.speed_spin.value()
        language = self.lang_input.text().strip() or "vi"
        stability = self.stability_spin.value() / 100.0
        similarity = self.similarity_spin.value() / 100.0

        self._output_dir = Path(self.dir_input.text().strip())
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Setup table
        self.result_table.setRowCount(len(lines))
        self._jobs = lines

        for i, line in enumerate(lines):
            self.result_table.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i + 1)))
            self.result_table.setItem(i, 1, QtWidgets.QTableWidgetItem(line[:60]))
            self.result_table.setItem(i, 2, QtWidgets.QTableWidgetItem("Đang chờ..."))
            self.result_table.setItem(i, 3, QtWidgets.QTableWidgetItem(""))

        # Dispatch jobs
        max_concurrent = self.thread_spin.value()
        self._append_log(f"Bắt đầu {len(lines)} job, {max_concurrent} song song")

        for i, line in enumerate(lines):
            self._bridge.run_coro(
                self._bridge.do_tts(i, line, voice_id, model_id, speed, language, stability, similarity)
            )

    def _on_tts_done(self, job_idx: int, success: bool, msg: str, audio: bytes):
        if success and audio:
            # Save file
            text = self._jobs[job_idx] if job_idx < len(self._jobs) else "job"
            safe_name = "".join(c if c.isalnum() or c in " _-" else "" for c in text[:30]).strip() or "audio"
            filename = f"{job_idx+1:03d}_{safe_name}.mp3"
            filepath = self._output_dir / filename
            filepath.write_bytes(audio)

            self.result_table.setItem(job_idx, 2, QtWidgets.QTableWidgetItem("✓ Xong"))
            self.result_table.setItem(job_idx, 3, QtWidgets.QTableWidgetItem(str(filepath.name)))
            self._append_log(f"[tts-{job_idx}] ✓ {filepath.name} ({len(audio)//1024}KB)")
        else:
            self.result_table.setItem(job_idx, 2, QtWidgets.QTableWidgetItem(f"✗ Lỗi"))
            self.result_table.setItem(job_idx, 3, QtWidgets.QTableWidgetItem(msg))
            self._append_log(f"[tts-{job_idx}] ✗ {msg}")


# ============================================================
# Entry point
# ============================================================

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ElevenLabs Preview TTS")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
