"""
U19：更新對話框每次啟動都彈 modal 搶焦點，「稍後提醒」什麼都不記

on_update_result 先 tray.show_update_available(info)（非侵入式），接著又
dialog.exec() 開 modal。「稍後提醒」只是 self.reject()，config 裡沒有任何
skipped/remind 欄位 —— 所以明天、後天、每次開機都再彈同一個框。

這是開機自啟的托盤常駐工具：用戶開機坐下開始打信，modal 憑空跳出搶走
鍵盤焦點，正在輸入的字可能被吃掉。

鎖住的行為：
- 預設路徑完全不彈 modal，只留托盤入口 + 氣泡
- 「跳過此版本」與「稍後提醒（24 小時）」都要寫進 config
- 被跳過／未到提醒時間的版本，連托盤入口都不插
- 出現更新版本時，舊的跳過記錄不能繼續生效
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config import AppConfig


class _Info:
    def __init__(self, version: str = "9.9.9", available: bool = True) -> None:
        self.remote_version = version
        self.available = available
        self.download_url = "https://example.test/x.zip"
        self.release_notes: list[str] = ["修了一些東西"]
        self.local_version = "1.0.0"
        self.size_mb = 12.3
        self.notes = ""


@pytest.fixture
def app(monkeypatch):
    from app.app import VoiceApp

    a = VoiceApp.__new__(VoiceApp)
    a._config = AppConfig()

    a.tray_calls: list[str] = []
    a.balloons: list[str] = []
    a.modals: list[str] = []

    class _Tray:
        @staticmethod
        def show_update_available(info) -> None:
            a.tray_calls.append(info.remote_version)

    class _Win:
        _tray = _Tray()

    a._main_window = _Win()
    monkeypatch.setattr(
        a, "_invoke_gui",
        lambda method, *args: a.balloons.append(args[0][1] if args else method),
    )
    monkeypatch.setattr(
        a, "_show_update_dialog", lambda info: a.modals.append(info.remote_version),
    )
    return a


# ─────────────────── 預設路徑不搶焦點 ───────────────────

class TestNoModalOnStartup:
    def test_new_version_does_not_open_a_modal(self, app) -> None:
        """開機坐下打信時，不該有東西憑空搶走鍵盤焦點。"""
        app._announce_update(_Info())
        assert app.modals == []

    def test_new_version_still_reaches_the_tray(self, app) -> None:
        app._announce_update(_Info("9.9.9"))
        assert app.tray_calls == ["9.9.9"]

    def test_new_version_shows_a_non_intrusive_balloon(self, app) -> None:
        """拿掉 modal 之後，氣泡是唯一的發現路徑，不能也一起拿掉。"""
        app._announce_update(_Info("9.9.9"))
        assert any("9.9.9" in b for b in app.balloons)


# ─────────────────── 跳過／稍後提醒要留下記錄 ───────────────────

class TestSkipAndSnoozeArePersisted:
    def test_skipped_version_is_not_announced(self, app) -> None:
        app._config = replace(app._config, skipped_update_version="9.9.9")
        app._announce_update(_Info("9.9.9"))
        assert app.tray_calls == [] and app.balloons == []

    def test_a_newer_version_ignores_the_old_skip(self, app) -> None:
        """跳過 9.9.9 不代表以後所有版本都不想知道。"""
        app._config = replace(app._config, skipped_update_version="9.9.9")
        app._announce_update(_Info("10.0.0"))
        assert app.tray_calls == ["10.0.0"]

    def test_snooze_window_suppresses_the_announcement(self, app) -> None:
        app._config = replace(
            app._config, update_remind_after=time.time() + 3600,
        )
        app._announce_update(_Info())
        assert app.tray_calls == []

    def test_expired_snooze_announces_again(self, app) -> None:
        app._config = replace(
            app._config, update_remind_after=time.time() - 1,
        )
        app._announce_update(_Info())
        assert app.tray_calls == ["9.9.9"]

    def test_config_defaults_are_empty(self) -> None:
        cfg = AppConfig()
        assert cfg.skipped_update_version == ""
        assert cfg.update_remind_after == 0.0


class TestDecisionIsWrittenBack:
    @pytest.fixture
    def app_with_save(self, app, monkeypatch):
        saved: list[AppConfig] = []
        monkeypatch.setattr(
            "utils.config.ConfigManager.save", staticmethod(saved.append),
        )
        app.saved = saved
        return app

    def test_skip_records_the_version(self, app_with_save) -> None:
        app_with_save._apply_update_decision("skip", _Info("9.9.9"))
        assert app_with_save._config.skipped_update_version == "9.9.9"
        assert app_with_save.saved, "決定必須落盤，否則重開又彈"

    def test_later_sets_a_24h_window(self, app_with_save) -> None:
        before = time.time()
        app_with_save._apply_update_decision("later", _Info())
        delta = app_with_save._config.update_remind_after - before
        assert 23 * 3600 < delta <= 24 * 3600 + 5

    def test_later_does_not_skip_the_version(self, app_with_save) -> None:
        app_with_save._apply_update_decision("later", _Info("9.9.9"))
        assert app_with_save._config.skipped_update_version == ""

    def test_no_decision_changes_nothing(self, app_with_save) -> None:
        app_with_save._apply_update_decision("", _Info())
        assert app_with_save.saved == []


# ─────────────────── 對話框本身 ───────────────────

class TestUpdateDialogButtons:
    @pytest.fixture
    def dialog(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("未安裝 PySide6")
        QApplication.instance() or QApplication(sys.argv)

        from gui.update_dialog import UpdateDialog

        d = UpdateDialog(_Info(), parent=None)
        yield d
        d.deleteLater()

    def test_starts_with_no_decision(self, dialog) -> None:
        assert dialog.decision == ""

    def test_skip_button_records_skip(self, dialog) -> None:
        dialog._btn_skip.click()
        assert dialog.decision == "skip"

    def test_later_button_records_later(self, dialog) -> None:
        dialog._btn_later.click()
        assert dialog.decision == "later"

    def test_later_button_says_how_long(self, dialog) -> None:
        """「稍後提醒」不講多久，用戶會以為是「這次先不要」。"""
        assert "24" in dialog._btn_later.text()
