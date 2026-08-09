"""
U9：貼上成功與否從未驗證

press_ctrl_v 只要 pynput 不拋例外就 return True，沒有任何注入結果校驗。
非提權程序向提權視窗（工作管理員、admin cmd）注入 Ctrl+V 會被 Windows
**靜默丟棄** —— 於是三處「⚠ 貼上失敗」通知永遠不會觸發，狀態列顯示
「完成」但目標視窗一個字都沒有。

另一半：開了 LLM 潤色時管線可以耗 10-30 秒，期間用戶早就切走視窗。
全 codebase 沒有任何 GetForegroundWindow 比對，於是逐字稿會被貼進
聊天室輸入框（可能被 Enter 送出）、密碼欄或程式碼中間。

鎖住的行為：
- SendInput 的回傳值決定成敗，被 UIPI 阻擋（回 0 / GetLastError()==5）要回 False
- 部分注入（少於 4 個事件）也算失敗
- 目標視窗換了就不貼，但文字必須留在剪貼簿讓用戶手動貼
- 拿不到視窗 handle 時不阻擋正常貼上（不可因為偵測失敗就癱瘓主功能）
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.keyboard import KeyboardSimulator

ERROR_ACCESS_DENIED = 5


# ─────────────────── SendInput 的回傳值必須被檢查 ───────────────────

class TestCtrlVChecksInjectionResult:
    def test_all_events_injected_is_success(self, monkeypatch) -> None:
        import utils.keyboard as kb

        monkeypatch.setattr(kb, "_send_input", lambda inputs: len(inputs))
        assert KeyboardSimulator.press_ctrl_v() is True

    def test_uipi_block_is_reported_as_failure(self, monkeypatch) -> None:
        """提權視窗會靜默吞掉注入 —— 這正是「顯示完成但一個字都沒有」的成因。"""
        import utils.keyboard as kb

        monkeypatch.setattr(kb, "_send_input", lambda inputs: 0)
        monkeypatch.setattr(kb, "_last_error", lambda: ERROR_ACCESS_DENIED)

        assert KeyboardSimulator.press_ctrl_v() is False

    def test_partial_injection_is_failure(self, monkeypatch) -> None:
        """只送進去一半的按鍵組合，等於留下一個按住的 Ctrl。"""
        import utils.keyboard as kb

        monkeypatch.setattr(kb, "_send_input", lambda inputs: 1)
        monkeypatch.setattr(kb, "_last_error", lambda: 0)
        assert KeyboardSimulator.press_ctrl_v() is False

    def test_sends_the_full_ctrl_v_sequence(self, monkeypatch) -> None:
        """press ctrl → press v → release v → release ctrl，順序不能亂。"""
        import utils.keyboard as kb

        captured: list = []

        def _capture(inputs):
            captured.extend(inputs)
            return len(inputs)

        monkeypatch.setattr(kb, "_send_input", _capture)
        KeyboardSimulator.press_ctrl_v()

        assert [(vk, up) for vk, up in captured] == [
            (kb.VK_CONTROL, False),
            (kb.VK_V, False),
            (kb.VK_V, True),
            (kb.VK_CONTROL, True),
        ]

    def test_exception_is_contained(self, monkeypatch) -> None:
        import utils.keyboard as kb

        def _boom(inputs):
            raise OSError("SendInput 掛了")

        monkeypatch.setattr(kb, "_send_input", _boom)
        assert KeyboardSimulator.press_ctrl_v() is False


# ─────────────────── 目標視窗比對 ───────────────────

class TestPasteChecksTargetWindow:
    @pytest.fixture
    def clip(self, monkeypatch):
        from utils.clipboard import ClipboardManager

        written: list[str] = []
        pasted: list[int] = []
        monkeypatch.setattr(
            ClipboardManager, "set_text",
            classmethod(lambda cls, t: written.append(t) or True),
        )
        monkeypatch.setattr(
            ClipboardManager, "get_text", classmethod(lambda cls: "原本內容"),
        )
        monkeypatch.setattr(
            KeyboardSimulator, "press_ctrl_v",
            classmethod(lambda cls: pasted.append(1) or True),
        )
        ClipboardManager._written = written
        ClipboardManager._pasted = pasted
        return ClipboardManager

    def test_same_window_pastes_normally(self, clip, monkeypatch) -> None:
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 1234)
        assert clip.paste_text("識別結果", expect_hwnd=1234) is True
        assert clip._pasted == [1]

    def test_switched_window_does_not_paste(self, clip, monkeypatch) -> None:
        """貼進聊天室輸入框可能直接被 Enter 送出 —— 寧可不貼。"""
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 9999)
        assert clip.paste_text("客戶合約金額", expect_hwnd=1234) is False
        assert clip._pasted == []

    def test_switched_window_still_leaves_text_on_clipboard(
        self, clip, monkeypatch,
    ) -> None:
        """UI 會說「結果已在剪貼簿，可手動 Ctrl+V」—— 這句話必須是真的。"""
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 9999)
        clip.paste_text("識別結果", expect_hwnd=1234)
        assert "識別結果" in clip._written

    def test_no_expectation_means_no_check(self, clip, monkeypatch) -> None:
        """檔案轉錄等路徑沒有「目標視窗」概念，不該被擋。"""
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 9999)
        assert clip.paste_text("結果") is True

    def test_unknown_current_window_does_not_block(self, clip, monkeypatch) -> None:
        """偵測失敗就放行 —— 不能因為 API 拿不到 handle 就癱瘓主功能。"""
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 0)
        assert clip.paste_text("結果", expect_hwnd=1234) is True

    def test_unknown_expected_window_does_not_block(self, clip, monkeypatch) -> None:
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 5678)
        assert clip.paste_text("結果", expect_hwnd=0) is True


class TestForegroundWindowHelper:
    def test_returns_zero_when_api_fails(self, monkeypatch) -> None:
        import utils.clipboard as cb

        monkeypatch.setattr(
            cb, "_GetForegroundWindow",
            lambda: (_ for _ in ()).throw(OSError("boom")),
        )
        assert cb.foreground_window() == 0

    def test_returns_int_handle(self, monkeypatch) -> None:
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "_GetForegroundWindow", lambda: 4321)
        assert cb.foreground_window() == 4321


# ─────────────────── app 層：錄音當下記下目標視窗 ───────────────────

class TestAppRemembersTargetWindow:
    @pytest.fixture
    def app(self, monkeypatch):
        import threading

        from app.app import VoiceApp
        from utils.config import AppConfig

        a = VoiceApp.__new__(VoiceApp)
        a._config = AppConfig()
        a._is_processing = False
        a._is_repolishing = False
        a._processing_lock = threading.Lock()
        a._target_hwnd = 0
        monkeypatch.setattr(a, "_invoke_gui", lambda *a_, **k: None)
        monkeypatch.setattr(a, "_ensure_recorder", lambda: True)

        class _Rec:
            def start_recording(self) -> None: ...
        a._recorder = _Rec()
        return a

    def test_recording_start_captures_the_window(self, app, monkeypatch) -> None:
        import utils.clipboard as cb

        monkeypatch.setattr(cb, "foreground_window", lambda: 777)
        app._on_recording_start()
        assert app._target_hwnd == 777

    def test_capture_failure_stores_zero_not_crash(self, app, monkeypatch) -> None:
        import utils.clipboard as cb

        monkeypatch.setattr(
            cb, "foreground_window", lambda: (_ for _ in ()).throw(OSError()),
        )
        app._on_recording_start()
        assert app._target_hwnd == 0
