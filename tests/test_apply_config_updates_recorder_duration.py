"""
HIGH-1 修復（v3.9.3 code review 發現）：

錄音時長上限（audio.max_recording_seconds）只喺 AudioRecorder 建構期烘一次
（app.py:_init_recorder），_apply_config() 移除 audio_changed 分支後，
儲存設定完全冇任何路徑會碰到已存在嘅 recorder 實例——用戶調高上限，當次
session 錄音仍會俾舊上限靜默截斷，屬內容遺失。

core/audio_recorder.py 已有 set_max_duration() 但全 repo 零呼叫點。

Fix: _apply_config() 偵測 audio.max_recording_seconds 改變時，呼叫
self._recorder.set_max_duration(new_value)。
"""
from __future__ import annotations

import dataclasses
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.app import VoiceApp
from utils.config import AppConfig


def _make_va(old_config: AppConfig, recorder) -> VoiceApp:
    va = object.__new__(VoiceApp)
    VoiceApp.__init__(va)
    va._config = old_config
    va._hotkey = None
    va._llm = None
    va._hotword = None
    va._asr_process = None
    va._text_processor = None
    va._invoke_gui = MagicMock()
    va._recorder = recorder
    return va


def test_recorder_max_duration_updated_when_audio_changed():
    old_config = AppConfig()
    new_config = dataclasses.replace(
        old_config,
        audio=dataclasses.replace(old_config.audio, max_recording_seconds=7200),
    )
    mock_recorder = MagicMock()
    va = _make_va(old_config, mock_recorder)

    with patch("app.app.ConfigManager"):
        va._apply_config(new_config)

    mock_recorder.set_max_duration.assert_called_once_with(7200)


def test_recorder_not_touched_when_audio_unchanged():
    old_config = AppConfig()
    new_config = dataclasses.replace(old_config)  # 其餘欄位相同，audio 不變
    mock_recorder = MagicMock()
    va = _make_va(old_config, mock_recorder)

    with patch("app.app.ConfigManager"):
        va._apply_config(new_config)

    mock_recorder.set_max_duration.assert_not_called()


def test_no_crash_when_recorder_is_none():
    """錄音器初始化失敗時 self._recorder 可能係 None，唔應該炸。"""
    old_config = AppConfig()
    new_config = dataclasses.replace(
        old_config,
        audio=dataclasses.replace(old_config.audio, max_recording_seconds=600),
    )
    va = _make_va(old_config, None)

    with patch("app.app.ConfigManager"):
        va._apply_config(new_config)  # 不應拋出例外
