"""VoiceApp._make_polish_selection_hotkey() / _init_polish_selection_hotkey() 測試。

補齊覆蓋率缺口：呢兩個函式喺 Task 4 引入後從未被任何測試直接執行過
（`polish_selection_key` 預設空字串停用，一般全量 VoiceApp() 構造測試
永遠行早退分支，`_make_polish_selection_hotkey()` 本身亦冇被直接呼叫）。
私有屬性斷言風格（`_key_name`/`_threshold`/`_suppress` 等）仿
`tests/test_discovery2_fixes.py` 對 `HotkeyListener` 嘅既有測試寫法。
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch


def _make_app(shortcut=None):
    from app.app import VoiceApp
    from utils.config import AppConfig

    va = object.__new__(VoiceApp)
    cfg = AppConfig()
    va._config = replace(cfg, shortcut=shortcut) if shortcut is not None else cfg
    va._polish_selection_hotkey = None
    return va


class TestMakePolishSelectionHotkey:
    def test_instant_mode_wires_on_deactivate(self):
        from utils.config import ShortcutConfig

        va = _make_app()
        sc = ShortcutConfig(polish_selection_key="f3", polish_selection_instant=True)

        listener = va._make_polish_selection_hotkey(sc)

        assert listener._key_name == "f3"
        assert listener._on_activate is None
        assert listener._on_deactivate == va._on_polish_selection_activate
        assert listener._suppress is False  # 唔阻塞用戶按鍵傳去目標應用
        assert listener._threshold == 0.05  # 速發模式最小閾值防抖（app.py 硬編碼常數）

    def test_long_press_mode_wires_on_activate(self):
        from utils.config import ShortcutConfig

        va = _make_app()
        sc = ShortcutConfig(
            polish_selection_key="f3", polish_selection_instant=False, threshold=0.4,
        )

        listener = va._make_polish_selection_hotkey(sc)

        assert listener._key_name == "f3"
        assert listener._on_deactivate is None
        assert listener._on_activate == va._on_polish_selection_activate
        assert listener._suppress is False  # 唔阻塞用戶按鍵傳去目標應用
        assert listener._threshold == 0.4


class TestInitPolishSelectionHotkey:
    def test_disabled_key_skips_listener_creation(self):
        from utils.config import ShortcutConfig

        va = _make_app(shortcut=ShortcutConfig(polish_selection_key=""))

        with patch.object(va, "_make_polish_selection_hotkey") as mock_make:
            va._init_polish_selection_hotkey()

        mock_make.assert_not_called()
        assert va._polish_selection_hotkey is None

    def test_enabled_key_creates_and_starts_listener(self):
        from utils.config import ShortcutConfig

        va = _make_app(shortcut=ShortcutConfig(polish_selection_key="f3"))
        mock_listener = MagicMock()

        with patch.object(va, "_make_polish_selection_hotkey", return_value=mock_listener) as mock_make:
            va._init_polish_selection_hotkey()

        mock_make.assert_called_once_with(va._config.shortcut)
        mock_listener.start.assert_called_once()
        assert va._polish_selection_hotkey is mock_listener


class TestApplyConfigRebuildsPolishSelectionHotkey:
    """_apply_config() 熱重載分支：polish_selection_key/instant/threshold 任一變更都要重建
    listener（app/app.py:1795-1800）。仿 tests/test_discovery3_fixes.py::TestF6RepolishHotkeyRebuild
    對應風格。"""

    def test_key_change_rebuilds_listener(self):
        from utils.config import AppConfig, ShortcutConfig
        from app.app import VoiceApp

        old_sc = ShortcutConfig(polish_selection_key="")
        new_sc = ShortcutConfig(polish_selection_key="f3")
        old_cfg = replace(AppConfig(), shortcut=old_sc)
        new_cfg = replace(AppConfig(), shortcut=new_sc)

        va = object.__new__(VoiceApp)
        va._config = old_cfg
        va._hotkey = MagicMock()
        va._repolish_hotkey = MagicMock()
        va._polish_selection_hotkey = MagicMock()
        va._llm = None
        va._main_window = None
        va._recording_db = None
        va._text_processor = None

        with patch("utils.config.ConfigManager.save"):
            with patch.object(va, "_make_polish_selection_hotkey", return_value=MagicMock()) as mock_make:
                va._apply_config(new_cfg)

        mock_make.assert_called_once_with(new_sc)
