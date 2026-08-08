"""
錄音時長上限（audio.max_recording_seconds）設定頁 UI 綁定測試。

AudioConfig.max_recording_seconds 一直存在，錄音器（core/audio_recorder.py）
亦已支援動態帶入使用，但從未接上設定頁 —— 用戶改唔到，只能手改
config.json。本測試鎖住 load_config -> UI -> get_config 完整往返。
"""

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config import AppConfig


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("未安裝 PySide6")
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture
def panel(qapp, tmp_path, monkeypatch):
    from core.recording_db import RecordingDatabase
    from gui.settings_panel import SettingsPanel

    monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "recordings.db")
    p = SettingsPanel(AppConfig())
    yield p
    p.deleteLater()


def _with_max_seconds(seconds: int) -> AppConfig:
    cfg = AppConfig()
    return dataclasses.replace(cfg, audio=dataclasses.replace(cfg.audio, max_recording_seconds=seconds))


def test_spin_shows_config_value(panel):
    panel.load_config(_with_max_seconds(600))
    assert panel._max_recording_spin.value() == 600


def test_get_config_returns_spin_value(panel):
    panel._max_recording_spin.setValue(3600)
    assert panel.get_config().audio.max_recording_seconds == 3600


def test_roundtrip_preserves_value(panel):
    panel.load_config(_with_max_seconds(900))
    assert panel.get_config().audio.max_recording_seconds == 900


def test_default_is_1800(panel):
    panel.load_config(AppConfig())
    assert panel._max_recording_spin.value() == 1800


def test_no_artificial_upper_cap(panel):
    spin = panel._max_recording_spin
    assert spin.minimum() == 1
    assert spin.maximum() >= 999999


def test_asr_model_selection_still_works_after_wrapping(panel):
    """ASRModelTab wrapper 化後，get_selected_model_key() 仍要可用，
    get_config() 讀返嘅 asr.model 唔應該係空字串。"""
    cfg = panel.get_config()
    assert cfg.asr.model
