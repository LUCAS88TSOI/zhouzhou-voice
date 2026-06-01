"""
州州語音 - 麥克風測試對話框

三步驟語音質量檢測：環境噪音 → 說話測試 → ASR 識別。
首次啟動自動彈出，也可從設定頁手動開啟。
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from utils.logger import get_logger

if TYPE_CHECKING:
    from core.asr_process import ASRProcess
    from core.audio_recorder import AudioRecorder

logger = get_logger("mic_test")

_DB_MIN, _DB_MAX = -60, 0


def _db_pct(db: float) -> int:
    """dB → 0-100 百分比。"""
    return max(0, min(100, int((db - _DB_MIN) / (_DB_MAX - _DB_MIN) * 100)))


class _ASRRelay(QObject):
    result_ready = Signal(str)


class MicTestDialog(QDialog):
    """麥克風三步驟測試。"""

    NOISE_SEC = 3.0
    VOICE_SEC = 5.0
    ASR_MAX_SEC = 6.0

    def __init__(
        self,
        recorder: AudioRecorder,
        asr_process: ASRProcess | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("麥克風測試")
        self.setFixedSize(420, 360)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self._rec = recorder
        self._asr = asr_process

        # 測試數據
        self._noise_samples: list[float] = []
        self._voice_samples: list[float] = []
        self._noise_db = -100.0
        self._voice_db = -100.0
        self._snr = 0.0
        self._asr_text = ""
        self._step = 0          # 0=noise 1=voice 2=asr 3=summary
        self._step_t0 = 0.0
        self._asr_recording = False
        self._asr_done = False

        # ASR relay
        self._relay = _ASRRelay()
        self._relay.result_ready.connect(self._on_asr_result)

        # ── UI ──
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        self._title = QLabel()
        self._title.setStyleSheet("font-size: 16px; font-weight: bold;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._title)

        self._info = QLabel()
        self._info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info.setWordWrap(True)
        lay.addWidget(self._info)

        self._vu = QProgressBar()
        self._vu.setRange(0, 100)
        self._vu.setTextVisible(False)
        self._vu.setFixedHeight(22)
        lay.addWidget(self._vu)

        self._db_label = QLabel()
        self._db_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._db_label)

        self._result = QLabel()
        self._result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._result.setWordWrap(True)
        self._result.setStyleSheet("font-size: 13px; padding: 4px;")
        lay.addWidget(self._result)

        lay.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        self._skip_btn = QPushButton("跳過")
        self._skip_btn.clicked.connect(self._on_skip)
        self._act_btn = QPushButton()
        self._act_btn.clicked.connect(self._on_action)
        btn_row.addWidget(self._skip_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._act_btn)
        lay.addLayout(btn_row)

        # Timer (30ms ≈ 33fps)
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)

        self._go(0)

    # ── 步驟切換 ──

    def _go(self, step: int) -> None:
        self._step = step
        self._step_t0 = time.monotonic()
        self._result.clear()

        if step == 0:
            self._title.setText("步驟 1/3 — 環境噪音檢測")
            self._info.setText(f"請保持安靜 {self.NOISE_SEC:.0f} 秒...")
            self._noise_samples.clear()
            self._vu.show(); self._db_label.show()
            self._act_btn.hide()
            self._skip_btn.setText("跳過")
            self._timer.start()

        elif step == 1:
            self._title.setText("步驟 2/3 — 說話測試")
            self._info.setText(f"請對麥克風說幾句話（{self.VOICE_SEC:.0f} 秒）...")
            self._voice_samples.clear()
            self._act_btn.hide()
            self._timer.start()

        elif step == 2:
            self._title.setText("步驟 3/3 — 語音識別測試")
            self._info.setText("請說：「今天天氣真不錯」")
            self._asr_recording = False
            self._asr_done = False
            self._act_btn.show()
            if self._asr and self._asr.is_running:
                self._act_btn.setText("開始錄音")
                self._act_btn.setEnabled(True)
            else:
                self._result.setText("（ASR 未就緒，可跳過）")
                self._act_btn.setText("下一步")
                self._act_btn.setEnabled(True)
                self._asr_done = True

        elif step == 3:
            self._timer.stop()
            self._title.setText("測試結果")
            self._info.clear()
            self._vu.hide(); self._db_label.hide()
            self._skip_btn.setText("重新測試")
            self._act_btn.setText("開始使用")
            self._act_btn.show()
            self._act_btn.setEnabled(True)
            self._show_summary()

    # ── Timer tick ──

    def _tick(self) -> None:
        db = self._rec.level_db if self._rec else -100.0
        pct = _db_pct(db)
        self._vu.setValue(pct)
        self._db_label.setText(f"{db:.0f} dB")

        # VU bar color
        color = "#e74c3c" if pct > 85 else "#2ecc71" if pct > 30 else "#f39c12"
        self._vu.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")

        elapsed = time.monotonic() - self._step_t0

        if self._step == 0:
            self._noise_samples.append(db)
            rem = max(0, self.NOISE_SEC - elapsed)
            self._info.setText(f"請保持安靜 {rem:.0f} 秒...")
            if elapsed >= self.NOISE_SEC:
                self._finish_noise()

        elif self._step == 1:
            self._voice_samples.append(db)
            rem = max(0, self.VOICE_SEC - elapsed)
            self._info.setText(f"請對麥克風說幾句話（{rem:.0f} 秒）...")
            if elapsed >= self.VOICE_SEC:
                self._finish_voice()

    # ── 噪音完成 ──

    def _finish_noise(self) -> None:
        self._timer.stop()
        if self._noise_samples:
            self._noise_db = sum(self._noise_samples) / len(self._noise_samples)
        ok = self._noise_db < -30
        tag = "✅ 安靜" if ok else "⚠️ 環境較吵"
        self._result.setText(f"環境噪音: {self._noise_db:.0f} dB — {tag}")
        logger.info("噪音檢測: %.0f dB", self._noise_db)
        QTimer.singleShot(1200, lambda: self._go(1))

    # ── 說話完成 ──

    def _finish_voice(self) -> None:
        self._timer.stop()
        if self._voice_samples:
            self._voice_db = max(self._voice_samples)
        self._snr = self._voice_db - self._noise_db
        v_ok = self._voice_db > -35
        s_ok = self._snr > 10
        self._result.setText(
            f"說話音量: {self._voice_db:.0f} dB — {'✅ 清晰' if v_ok else '⚠️ 太小聲'}\n"
            f"信噪比: {self._snr:.0f} dB — {'✅ 優秀' if s_ok else '⚠️ 偏低'}"
        )
        logger.info("說話測試: peak=%.0f dB, SNR=%.0f dB", self._voice_db, self._snr)
        QTimer.singleShot(1200, lambda: self._go(2))

    # ── 按鈕 ──

    def _on_skip(self) -> None:
        if self._step == 3:
            self._go(0)  # 重新測試
        else:
            self._timer.stop()
            if self._asr_recording and self._rec:
                self._rec.stop_recording()
                self._asr_recording = False
            self.accept()

    def _on_action(self) -> None:
        if self._step == 2:
            if self._asr_done:
                self._go(3)
            elif not self._asr_recording:
                self._start_asr_rec()
            else:
                self._stop_asr_rec()
        elif self._step == 3:
            self.accept()

    # ── ASR 錄音 ──

    def _start_asr_rec(self) -> None:
        self._asr_recording = True
        self._act_btn.setText("停止錄音")
        self._result.setText("🔴 錄音中...")
        self._rec.start_recording()
        self._step_t0 = time.monotonic()
        QTimer.singleShot(int(self.ASR_MAX_SEC * 1000), self._auto_stop_asr)

    def _auto_stop_asr(self) -> None:
        if self._asr_recording:
            self._stop_asr_rec()

    def _stop_asr_rec(self) -> None:
        if not self._asr_recording:
            return
        self._asr_recording = False
        audio = self._rec.stop_recording()
        self._act_btn.setEnabled(False)
        self._act_btn.setText("識別中...")
        self._result.setText("正在識別...")

        if not audio or len(audio) < 16000:
            self._on_asr_result("（錄音太短，請重試）")
            return

        threading.Thread(
            target=self._asr_worker,
            args=(self._asr, audio, self._relay),
            daemon=True,
            name="mic-test-asr",
        ).start()

    @staticmethod
    def _asr_worker(asr, audio_bytes: bytes, relay: _ASRRelay) -> None:
        try:
            from core.asr_process import ASRRequest, new_task_id
            tid = new_task_id()
            req = ASRRequest(task_id=tid, audio_data=audio_bytes)
            timeout = max(30, len(audio_bytes) / 4 / 16000 * 1.5)
            resp = asr.send_and_wait(req, timeout=timeout)
            relay.result_ready.emit(resp.text if not resp.error else f"錯誤: {resp.error}")
        except Exception as e:
            relay.result_ready.emit(f"錯誤: {e}")

    def _on_asr_result(self, text: str) -> None:
        self._asr_text = text
        self._asr_done = True
        self._result.setText(f"識別結果：{text}")
        self._act_btn.setText("下一步")
        self._act_btn.setEnabled(True)
        logger.info("ASR 測試: %s", text)

    # ── 結果摘要 ──

    def _show_summary(self) -> None:
        n_ok = self._noise_db < -30
        v_ok = self._voice_db > -35
        s_ok = self._snr > 10
        lines = [
            f"{'✅' if n_ok else '❌'} 環境噪音: {self._noise_db:.0f} dB",
            f"{'✅' if v_ok else '❌'} 說話音量: {self._voice_db:.0f} dB",
            f"{'✅' if s_ok else '❌'} 信噪比: {self._snr:.0f} dB",
        ]
        if self._asr_text:
            lines.append(f"\n識別結果：{self._asr_text}")
        all_ok = n_ok and v_ok and s_ok
        lines.append(f"\n{'✅ 麥克風狀態良好！' if all_ok else '⚠️ 建議調整麥克風或環境'}")
        self._result.setText("\n".join(lines))
