"""
審查修復 R14：熱詞停用後再啟用不會重建 manager，熱詞分頁變成死頁

_apply_config 第 6 步只有 `if self._hotword: reload(...)`，缺了
`elif new_config.hotword.enabled: self._init_hotword()` 分支 —— 同一函式
第 4 步的 LLM 就有這個對稱處理，證明是遺漏不是刻意。

_hotword 為 None 時 hotword_tab 六個 CRUD 全部靜默 return：按「新增」
連輸入框的字都不會被清掉，沒彈窗、沒 log、零回饋。使用者關掉熱詞、
重啟後想開回來，勾選 → 儲存 → 還是全空，怎麼弄都沒反應。
"""

from __future__ import annotations

import dataclasses
from unittest import mock

import pytest

from utils.config import AppConfig, HotwordConfig


def _app(hotword_enabled: bool, manager=None):
    from app.app import VoiceApp

    app = object.__new__(VoiceApp)
    app._config = dataclasses.replace(
        AppConfig(), hotword=HotwordConfig(enabled=hotword_enabled),
    )
    app._hotword = manager
    app._llm = None
    app._recorder = None
    app._hotkey = None
    app._repolish_hotkey = None
    app._polish_selection_hotkey = None
    app._text_processor = None
    app._main_window = None
    app._invoke_gui = lambda *a: None
    app._refresh_tray_roles = lambda: None
    return app


class TestApplyConfigRebuildsHotword:
    def test_reenabling_creates_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """關掉熱詞 → 勾回來 → 儲存，必須重建 manager（原本永遠是 None）。"""
        app = _app(hotword_enabled=False, manager=None)
        new_config = dataclasses.replace(
            app._config, hotword=HotwordConfig(enabled=True),
        )

        created: list[bool] = []

        def fake_init():
            created.append(True)
            app._hotword = mock.MagicMock()

        app._init_hotword = fake_init
        monkeypatch.setattr("utils.config.ConfigManager.save", lambda cfg: None)

        app._apply_config(new_config)

        assert created, "R14: 重新啟用熱詞時必須呼叫 _init_hotword()"

    def test_existing_manager_is_reloaded_not_recreated(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = mock.MagicMock()
        app = _app(hotword_enabled=True, manager=manager)
        new_config = dataclasses.replace(
            app._config, hotword=HotwordConfig(enabled=True, threshold=0.9),
        )

        app._init_hotword = lambda: pytest.fail("不該重建既有 manager")
        monkeypatch.setattr("utils.config.ConfigManager.save", lambda cfg: None)

        app._apply_config(new_config)

        assert manager.reload.called

    def test_disabled_stays_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """維持停用時不該憑空建出 manager。"""
        app = _app(hotword_enabled=True, manager=None)
        new_config = dataclasses.replace(
            app._config, hotword=HotwordConfig(enabled=False),
        )

        app._init_hotword = lambda: pytest.fail("停用時不該建立 manager")
        monkeypatch.setattr("utils.config.ConfigManager.save", lambda cfg: None)

        app._apply_config(new_config)

        assert app._hotword is None

    def test_new_manager_is_pushed_to_tab(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """重建後要回填給熱詞分頁，否則 UI 仍是死的。"""
        app = _app(hotword_enabled=False, manager=None)
        new_config = dataclasses.replace(
            app._config, hotword=HotwordConfig(enabled=True),
        )

        manager = mock.MagicMock()

        def fake_init():
            app._hotword = manager

        app._init_hotword = fake_init
        pushed: list[tuple] = []
        app._invoke_gui = lambda method, *a: pushed.append((method, a))
        monkeypatch.setattr("utils.config.ConfigManager.save", lambda cfg: None)

        app._apply_config(new_config)

        assert any(m == "set_hotword_manager" for m, _ in pushed), (
            f"R14: 未把新 manager 推給 UI，實際呼叫 {[m for m, _ in pushed]}"
        )


class TestHotwordTabShowsDeadState:
    """manager 為 None 時 UI 必須明講，而非靜默吞掉每一次操作。"""

    @pytest.fixture(autouse=True)
    def _qt(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        yield app

    def _tab(self):
        from gui.widgets.hotword_tab import HotwordTab

        tab = HotwordTab(HotwordConfig())
        tab.set_manager(None)
        return tab

    def test_stats_label_explains_the_problem(self) -> None:
        tab = self._tab()
        text = tab._stats_label.text()
        assert "熱詞" in text and ("啟用" in text or "未連接" in text)

    def test_inputs_disabled_without_manager(self) -> None:
        tab = self._tab()
        assert not tab._hotword_input.isEnabled(), (
            "R14: manager 為 None 時輸入框應 disable，讓死狀態一眼可見"
        )

    def test_inputs_reenabled_with_manager(self) -> None:
        tab = self._tab()
        tab.set_manager(mock.MagicMock(
            get_hotwords=lambda: [],
            get_rules=lambda: [],
            get_rectify_pairs=lambda: [],
            hotword_count=0, rule_count=0, rectify_count=0,
        ))
        assert tab._hotword_input.isEnabled()
