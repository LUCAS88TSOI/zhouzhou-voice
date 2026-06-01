"""
音頻播放器 Widget

使用 QMediaPlayer 播放 WAV 格式的錄音。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from utils.logger import get_logger

logger = get_logger("audio_player")


class AudioPlayerWidget(QWidget):
    """音頻播放器 Widget"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._player: Optional[QMediaPlayer] = None
        self._audio_output: Optional[QAudioOutput] = None
        self._temp_file: Optional[Path] = None

        self._build_ui()
        self._setup_player()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedSize(32, 32)
        self._play_btn.setEnabled(False)
        self._play_btn.clicked.connect(self._toggle_play)
        layout.addWidget(self._play_btn)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.setValue(0)
        self._slider.setEnabled(False)
        self._slider.sliderMoved.connect(self._on_seek)
        layout.addWidget(self._slider, stretch=1)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setMinimumWidth(80)
        layout.addWidget(self._time_label)

    def _setup_player(self) -> None:
        self._audio_output = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_output)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)

    def load_wav(self, wav_bytes: bytes) -> None:
        """載入 WAV 音頻數據"""
        self.stop()

        # 清理舊的臨時文件
        if self._temp_file and self._temp_file.exists():
            try:
                self._temp_file.unlink()
            except Exception:
                pass

        # 寫入新的臨時文件
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        Path(path).write_bytes(wav_bytes)
        self._temp_file = Path(path)

        self._player.setSource(QUrl.fromLocalFile(str(self._temp_file)))
        self._play_btn.setEnabled(True)
        self._slider.setEnabled(True)
        logger.debug("已載入音頻: %d bytes", len(wav_bytes))

    @Slot()
    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def stop(self) -> None:
        if self._player:
            self._player.stop()

    @Slot(int)
    def _on_position_changed(self, position: int) -> None:
        duration = self._player.duration()
        if duration > 0:
            self._slider.blockSignals(True)
            self._slider.setValue(int(position / duration * 1000))
            self._slider.blockSignals(False)
            self._update_time_label(position, duration)

    @Slot(int)
    def _on_duration_changed(self, duration: int) -> None:
        self._update_time_label(self._player.position(), duration)

    @Slot()
    def _on_state_changed(self) -> None:
        state = self._player.playbackState()
        if state == QMediaPlayer.PlayingState:
            self._play_btn.setText("⏸")
        else:
            self._play_btn.setText("▶")

    @Slot(int)
    def _on_seek(self, value: int) -> None:
        duration = self._player.duration()
        if duration > 0:
            self._player.setPosition(int(value / 1000 * duration))

    def _update_time_label(self, position: int, duration: int) -> None:
        pos_str = self._format_time(position)
        dur_str = self._format_time(duration)
        self._time_label.setText(f"{pos_str} / {dur_str}")

    @staticmethod
    def _format_time(ms: int) -> str:
        seconds = ms // 1000
        mins, secs = divmod(seconds, 60)
        return f"{mins}:{secs:02d}"

    def cleanup(self) -> None:
        """清理資源"""
        self.stop()
        if self._temp_file and self._temp_file.exists():
            try:
                self._temp_file.unlink()
            except Exception:
                pass
            self._temp_file = None
