"""
潤色逾時（polish_timeout）設定頁 UI 綁定測試。

polish_timeout 一直存在於 LLMConfig，app/app.py 亦已實作逾時保護（超時就
丟棄半截潤色、貼 ASR 原文、彈托盤警告），但從未接上設定頁 —— 用戶改唔到，
只能手改 config.json。本測試鎖住 load_config → UI → get_config 完整往返，
防止日後重構把接線改斷（改斷了只會靜默沿用 10s，肉眼看不出）。

範圍 3–120 秒：下限防止填太細導致每次都逾時貼原文；上限容納慢模型／長文。
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


def _with_timeout(seconds: float) -> AppConfig:
    cfg = AppConfig()
    return dataclasses.replace(
        cfg, llm=dataclasses.replace(cfg.llm, polish_timeout=seconds)
    )


def test_spin_shows_config_value(panel):
    panel.load_config(_with_timeout(25.0))
    assert panel._polish_timeout_spin.value() == 25


def test_get_config_returns_spin_value(panel):
    panel._polish_timeout_spin.setValue(45)
    assert panel.get_config().llm.polish_timeout == 45.0


def test_roundtrip_preserves_value(panel):
    panel.load_config(_with_timeout(60.0))
    assert panel.get_config().llm.polish_timeout == 60.0


def test_default_is_ten_seconds(panel):
    panel.load_config(AppConfig())
    assert panel._polish_timeout_spin.value() == 10


def test_range_is_three_to_onetwenty(panel):
    spin = panel._polish_timeout_spin
    assert (spin.minimum(), spin.maximum()) == (3, 120)


def test_legacy_zero_clamps_to_minimum(panel):
    """舊 config 的 0（不限制）現已不支援 —— 應收斂到下限而非留 0。"""
    panel.load_config(_with_timeout(0.0))
    assert panel.get_config().llm.polish_timeout == 3.0
