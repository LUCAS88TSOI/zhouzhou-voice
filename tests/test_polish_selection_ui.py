"""潤色選取文字快捷鍵設定頁 UI 綁定測試（仿 test_polish_timeout_ui.py）。"""
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


def _with_polish_selection(key: str, instant: bool) -> AppConfig:
    cfg = AppConfig()
    return dataclasses.replace(
        cfg,
        shortcut=dataclasses.replace(
            cfg.shortcut, polish_selection_key=key, polish_selection_instant=instant,
        ),
    )


def test_default_is_disabled(panel):
    panel.load_config(AppConfig())
    assert panel._polish_selection_key_input.get_key() == ""


def test_load_config_shows_key(panel):
    panel.load_config(_with_polish_selection("f3", True))
    assert panel._polish_selection_key_input.get_key() == "f3"
    assert panel._polish_selection_instant_combo.currentIndex() == 0


def test_load_config_shows_long_press_mode(panel):
    panel.load_config(_with_polish_selection("f3", False))
    assert panel._polish_selection_instant_combo.currentIndex() == 1


def test_get_config_returns_ui_value(panel):
    panel._polish_selection_key_input.set_key("f4")
    panel._polish_selection_instant_combo.setCurrentIndex(1)

    cfg = panel.get_config()

    assert cfg.shortcut.polish_selection_key == "f4"
    assert cfg.shortcut.polish_selection_instant is False


def test_roundtrip_preserves_value(panel):
    panel.load_config(_with_polish_selection("f5", True))
    cfg = panel.get_config()
    assert cfg.shortcut.polish_selection_key == "f5"
    assert cfg.shortcut.polish_selection_instant is True
