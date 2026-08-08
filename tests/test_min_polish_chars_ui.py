"""
字數門檻（min_polish_chars）設定頁 UI 綁定測試。

模式仿 test_polish_timeout_ui.py：鎖住 load_config -> UI -> get_config
完整往返，防止日後重構把接線改斷。
"""

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config import AppConfig


@pytest.fixture(scope="module")
def qapp():
    """取得（或建立）QApplication；無 PySide6 或建不起時跳過整個模組。"""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("未安裝 PySide6")
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture
def panel(qapp, tmp_path, monkeypatch):
    """建立隔離的 SettingsPanel（錄音 DB 導向 tmp，唔掂用戶真實資料）。"""
    from core.recording_db import RecordingDatabase
    from gui.settings_panel import SettingsPanel

    monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "recordings.db")
    p = SettingsPanel(AppConfig())
    yield p
    p.deleteLater()


def _with_min_chars(chars: int) -> AppConfig:
    cfg = AppConfig()
    return dataclasses.replace(cfg, llm=dataclasses.replace(cfg.llm, min_polish_chars=chars))


def test_spin_shows_config_value(panel):
    panel.load_config(_with_min_chars(10))
    assert panel._min_polish_chars_spin.value() == 10


def test_get_config_returns_spin_value(panel):
    panel._min_polish_chars_spin.setValue(20)
    assert panel.get_config().llm.min_polish_chars == 20


def test_roundtrip_preserves_value(panel):
    panel.load_config(_with_min_chars(15))
    assert panel.get_config().llm.min_polish_chars == 15


def test_default_is_four(panel):
    panel.load_config(AppConfig())
    assert panel._min_polish_chars_spin.value() == 4


def test_no_artificial_upper_cap(panel):
    spin = panel._min_polish_chars_spin
    assert spin.minimum() == 1
    assert spin.maximum() >= 999999
