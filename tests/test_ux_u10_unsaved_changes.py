"""
U10：未儲存的設定與角色提示詞會靜默蒸發，兩處都無確認

(a) 設定頁按「← 返回」或關窗直接 setCurrentIndex，零 dirty 檢查；
    下次進入 load_config() 把 UI 全部刷回舊值。那顆按鈕看起來就像瀏覽器
    返回，很多人以為等同「完成」—— 花五分鐘貼 API Key、調參數，全部歸零。
(b) 角色分頁切換角色時 _on_role_selected 直接 setPlainText 蓋掉編輯框。
    角色提示詞是這產品的核心賣點，動輒幾百字，切個角色作對照就沒了。

鎖住的行為：
- is_dirty() 用 baseline snapshot 比對，且 baseline 必須深拷貝
  （role_tab 是就地改 dict 的，共用參考會讓比對永遠相等）
- 有未儲存變更時返回要三選一，「取消」必須留在設定頁
- 角色切換前先問，「取消」要把下拉選單彈回原本那個角色
- 儲存後髒標記清除，程式化重填不得觸發詢問（會無限遞歸）
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config import AppConfig


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("未安裝 PySide6")
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def panel(qapp, tmp_path, monkeypatch):
    from core.recording_db import RecordingDatabase
    from gui.settings_panel import SettingsPanel

    monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "h.db")
    p = SettingsPanel(AppConfig())
    yield p
    p.deleteLater()


# ─────────────────────── (a) 設定頁髒檢查 ───────────────────────

class TestSettingsPanelDirtyDetection:
    def test_freshly_loaded_panel_is_clean(self, panel) -> None:
        assert panel.is_dirty() is False

    def test_editing_a_field_marks_dirty(self, panel) -> None:
        panel._max_tokens_spin.setValue(panel._max_tokens_spin.value() + 128)
        assert panel.is_dirty() is True

    def test_toggling_a_checkbox_marks_dirty(self, panel) -> None:
        panel._llm_enabled_check.setChecked(
            not panel._llm_enabled_check.isChecked()
        )
        assert panel.is_dirty() is True

    def test_load_config_resets_the_baseline(self, panel) -> None:
        panel._max_tokens_spin.setValue(panel._max_tokens_spin.value() + 128)
        assert panel.is_dirty() is True

        panel.load_config(AppConfig())
        assert panel.is_dirty() is False

    def test_baseline_is_deep_copied(self, qapp, tmp_path, monkeypatch) -> None:
        """role_tab 是就地改 dict 的 —— 共用參考會讓髒檢查永遠說「乾淨」。"""
        from core.recording_db import RecordingDatabase
        from gui.settings_panel import SettingsPanel

        monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "h2.db")
        cfg = AppConfig()
        cfg = replace(cfg, llm=replace(
            cfg.llm,
            custom_roles=[{"id": "r", "name": "R", "system_prompt": "原始"}],
        ))
        p = SettingsPanel(cfg)
        try:
            assert p.is_dirty() is False

            # get_custom_roles() 會先把編輯框內容寫回同一批 dict。基準若
            # 共用這些 dict，就會被一起改掉 → 比對永遠相等。
            tab = p._tab_role
            for i in range(tab._role_combo.count()):
                if tab._role_combo.itemData(i) == "r":
                    tab._role_combo.setCurrentIndex(i)
                    break
            tab._prompt_edit.setPlainText("改過的三百字")

            assert p.is_dirty() is True
        finally:
            p.deleteLater()

    def test_dirty_check_never_raises(self, panel, monkeypatch) -> None:
        """髒檢查壞掉不可以擋住用戶離開設定頁。"""
        monkeypatch.setattr(
            panel, "get_config",
            lambda: (_ for _ in ()).throw(RuntimeError("壞了")),
        )
        assert panel.is_dirty() is False


class TestSettingsBackButtonAsks:
    @pytest.fixture
    def win(self, qapp, panel, monkeypatch):
        from gui.main_window import MainWindow

        w = MainWindow.__new__(MainWindow)
        w._settings_panel = panel
        w.navigated: list[str] = []
        w.saved: list[str] = []
        monkeypatch.setattr(
            w, "_navigate_to_voice", lambda: w.navigated.append("voice"),
        )
        monkeypatch.setattr(
            w, "_on_settings_save", lambda: w.saved.append("save"),
        )
        return w

    def test_clean_panel_leaves_without_asking(self, win, monkeypatch) -> None:
        asked: list[int] = []
        monkeypatch.setattr(win, "_ask_unsaved_settings", lambda: asked.append(1))
        win._on_settings_back()
        assert win.navigated == ["voice"] and not asked

    def test_dirty_panel_asks(self, win, monkeypatch) -> None:
        win._settings_panel._max_tokens_spin.setValue(999)
        monkeypatch.setattr(win, "_ask_unsaved_settings", lambda: "discard")
        win._on_settings_back()
        assert win.navigated == ["voice"]

    def test_save_choice_saves_then_leaves(self, win, monkeypatch) -> None:
        win._settings_panel._max_tokens_spin.setValue(999)
        monkeypatch.setattr(win, "_ask_unsaved_settings", lambda: "save")
        win._on_settings_back()
        assert win.saved == ["save"]

    def test_cancel_choice_stays_on_the_settings_page(self, win, monkeypatch) -> None:
        """按錯了要能反悔 —— 這是三選一存在的唯一理由。"""
        win._settings_panel._max_tokens_spin.setValue(999)
        monkeypatch.setattr(win, "_ask_unsaved_settings", lambda: "cancel")
        win._on_settings_back()
        assert win.navigated == [] and win.saved == []

    def test_missing_panel_just_leaves(self, win) -> None:
        win._settings_panel = None
        win._on_settings_back()
        assert win.navigated == ["voice"]


# ─────────────────────── (b) 角色提示詞 ───────────────────────

@pytest.fixture
def role_tab(qapp):
    from gui.widgets.role_tab import RoleTab

    t = RoleTab(
        active_role_id="default",
        custom_roles=[
            {"id": "alpha", "name": "阿爾法", "system_prompt": "甲的提示詞"},
            {"id": "beta", "name": "貝塔", "system_prompt": "乙的提示詞"},
        ],
        builtin_overrides={},
    )
    yield t
    t.deleteLater()


def _select(tab, role_id: str) -> None:
    for i in range(tab._role_combo.count()):
        if tab._role_combo.itemData(i) == role_id:
            tab._role_combo.setCurrentIndex(i)
            return
    raise AssertionError(f"找不到角色 {role_id}")


class TestRoleTabDirtyDetection:
    def test_freshly_loaded_role_is_clean(self, role_tab) -> None:
        _select(role_tab, "alpha")
        assert role_tab.is_dirty() is False

    def test_editing_the_prompt_marks_dirty(self, role_tab) -> None:
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("我改了三百字")
        assert role_tab.is_dirty() is True

    def test_editing_the_name_marks_dirty(self, role_tab) -> None:
        _select(role_tab, "alpha")
        role_tab._name_input.setText("新名字")
        assert role_tab.is_dirty() is True

    def test_saving_clears_dirty(self, role_tab) -> None:
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("改過的提示詞")
        role_tab._on_save_edits()
        assert role_tab.is_dirty() is False

    def test_save_button_shows_an_unsaved_marker(self, role_tab) -> None:
        _select(role_tab, "alpha")
        clean_text = role_tab._btn_save.text()
        role_tab._prompt_edit.setPlainText("改了")
        assert role_tab._btn_save.text() != clean_text


class TestRoleSwitchAsksBeforeDiscarding:
    def test_clean_switch_does_not_ask(self, role_tab, monkeypatch) -> None:
        _select(role_tab, "alpha")
        asked: list[int] = []
        monkeypatch.setattr(role_tab, "_ask_unsaved_role", lambda: asked.append(1))
        _select(role_tab, "beta")
        assert not asked

    def test_dirty_switch_asks(self, role_tab, monkeypatch) -> None:
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("三百字的心血")
        asked: list[int] = []
        monkeypatch.setattr(
            role_tab, "_ask_unsaved_role",
            lambda: asked.append(1) or "discard",
        )
        _select(role_tab, "beta")
        assert asked == [1]

    def test_discard_switches_and_loses_the_edit(self, role_tab, monkeypatch) -> None:
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("不要了")
        monkeypatch.setattr(role_tab, "_ask_unsaved_role", lambda: "discard")

        _select(role_tab, "beta")
        assert role_tab._prompt_edit.toPlainText() == "乙的提示詞"

    def test_save_choice_persists_before_switching(self, role_tab, monkeypatch) -> None:
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("要留住的三百字")
        monkeypatch.setattr(role_tab, "_ask_unsaved_role", lambda: "save")

        _select(role_tab, "beta")

        alpha = next(r for r in role_tab.get_custom_roles() if r["id"] == "alpha")
        assert alpha["system_prompt"] == "要留住的三百字"

    def test_cancel_snaps_the_combo_back(self, role_tab, monkeypatch) -> None:
        """取消卻停在新角色上，等於騙人 —— 編輯框內容跟選單對不上。"""
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("還在寫")
        monkeypatch.setattr(role_tab, "_ask_unsaved_role", lambda: "cancel")

        _select(role_tab, "beta")

        assert role_tab._role_combo.currentData() == "alpha"
        assert role_tab._prompt_edit.toPlainText() == "還在寫"

    def test_programmatic_reload_does_not_ask(self, role_tab, monkeypatch) -> None:
        """儲存後會重建下拉選單並重選 —— 這裡再問一次就會無限遞歸。"""
        _select(role_tab, "alpha")
        role_tab._prompt_edit.setPlainText("改了")

        asked: list[int] = []
        monkeypatch.setattr(
            role_tab, "_ask_unsaved_role", lambda: asked.append(1) or "discard",
        )
        role_tab._on_save_edits()

        assert not asked
