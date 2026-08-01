"""
麥克風不可用時的靜默失敗修復。

現場 bug：系統無預設輸入裝置（`sd.default.device = [-1, 1]`）時，
`AudioRecorder.open()` 拋 `Error querying device -1`，`_init_recorder` 只寫日誌，
`self._recorder` 留在 None。使用者長按快捷鍵 → `_on_recording_start` 直接
`return`，UI 零反饋，表現為「狂按都沒法錄音」。

修復要求：
  1. `_init_recorder` 失敗時保存錯誤原因到 `_recorder_error`
  2. `_on_recording_start` 在錄音器不可用時必須給 UI 反饋，不可靜默 return
  3. 按鍵時嘗試重建錄音器（支援麥克風熱插拔，插上即用，不需重啟程式）
  4. 重試與托盤通知都要節流，狂按時不會反覆卡住鍵盤線程或刷屏
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_app():
    from app.app import VoiceApp
    va = object.__new__(VoiceApp)
    VoiceApp.__init__(va)
    va._invoke_gui = MagicMock()
    return va


class TestInitRecorderRecordsError:
    """`_init_recorder` 失敗時要留下可讀的失敗原因，而非只丟進日誌。"""

    def test_failure_stores_error_message(self, monkeypatch):
        from utils.config import AppConfig

        va = _make_app()
        va._config = AppConfig()

        import core.audio_recorder as ar_mod

        class BoomRecorder:
            def __init__(self, max_duration: float = 0.0) -> None:
                raise RuntimeError("Error querying device -1")

        monkeypatch.setattr(ar_mod, "AudioRecorder", BoomRecorder)

        va._init_recorder()

        assert va._recorder is None
        assert va._recorder_error is not None
        assert "device -1" in va._recorder_error

    def test_success_clears_previous_error(self, monkeypatch):
        from utils.config import AppConfig

        va = _make_app()
        va._config = AppConfig()
        va._recorder_error = "舊的失敗訊息"

        import core.audio_recorder as ar_mod

        class OkRecorder:
            def __init__(self, max_duration: float = 0.0) -> None:
                self.is_open = True

            def set_limit_callback(self, cb) -> None:
                pass

            def open(self) -> None:
                pass

            def close(self) -> None:
                pass

        monkeypatch.setattr(ar_mod, "AudioRecorder", OkRecorder)

        va._init_recorder()

        assert va._recorder is not None
        assert va._recorder_error is None


class TestRecorderRebuildCleanup:
    """重建錄音器時要回收舊資源，否則熱插拔幾次就洩漏 PortAudio stream。"""

    def _patch_recorder(self, monkeypatch, created: list):
        import core.audio_recorder as ar_mod

        class FakeRecorder:
            def __init__(self, max_duration: float = 0.0) -> None:
                self.is_open = False
                self.closed = False
                created.append(self)

            def set_limit_callback(self, cb) -> None:
                pass

            def open(self) -> None:
                self.is_open = True

            def close(self) -> None:
                self.closed = True
                self.is_open = False

        monkeypatch.setattr(ar_mod, "AudioRecorder", FakeRecorder)

    def test_rebuild_closes_old_recorder(self, monkeypatch):
        from utils.config import AppConfig

        va = _make_app()
        va._config = AppConfig()
        created: list = []
        self._patch_recorder(monkeypatch, created)

        va._init_recorder()
        va._init_recorder()

        assert len(created) == 2
        assert created[0].closed is True, "重建時舊錄音器未關閉，PortAudio stream 洩漏"
        assert va._recorder is created[1]

    def test_rebuild_does_not_accumulate_shutdown_callbacks(self, monkeypatch):
        from utils.config import AppConfig

        va = _make_app()
        va._config = AppConfig()
        created: list = []
        self._patch_recorder(monkeypatch, created)

        for _ in range(3):
            va._init_recorder()

        callbacks = va._lifecycle._shutdown_callbacks
        assert len(callbacks) == 1, f"shutdown 回調累積了 {len(callbacks)} 個"


class TestRecordingStartFeedback:
    """錄音器不可用時，按快捷鍵必須有 UI 反饋。"""

    def test_no_recorder_updates_status(self):
        va = _make_app()
        va._recorder = None
        va._ensure_recorder = MagicMock(return_value=False)

        va._on_recording_start()

        methods = [c.args[0] for c in va._invoke_gui.call_args_list]
        assert "set_status" in methods, "錄音器不可用時應更新狀態列，不可靜默 return"
        status_text = next(
            c.args[1][1] for c in va._invoke_gui.call_args_list if c.args[0] == "set_status"
        )
        assert "麥克風" in status_text

    def test_available_recorder_starts_normally(self):
        va = _make_app()
        recorder = MagicMock()
        recorder.is_open = True
        va._recorder = recorder

        va._on_recording_start()

        recorder.start_recording.assert_called_once()
        status_text = next(
            c.args[1][1] for c in va._invoke_gui.call_args_list if c.args[0] == "set_status"
        )
        assert status_text == "錄音中..."


class TestMicTestDialogGuard:
    """沒有麥克風時點「麥克風測試」不可開對話框（內部 `_rec.start_recording()`
    無 None guard，會拋 AttributeError）。"""

    def test_no_dialog_when_recorder_unavailable(self, monkeypatch):
        va = _make_app()
        va._recorder = None
        va._ensure_recorder = MagicMock(return_value=False)

        opened = []
        import gui.mic_test_dialog as dlg_mod
        monkeypatch.setattr(
            dlg_mod, "MicTestDialog", lambda *a, **k: opened.append(a) or MagicMock()
        )

        va._show_mic_test()

        assert not opened, "錄音器不可用時不應開啟麥克風測試對話框"
        warnings = [c for c in va._invoke_gui.call_args_list if c.args[0] == "notify_warning"]
        assert warnings, "應提示使用者麥克風不可用"


class TestEnsureRecorderHotPlug:
    """`_ensure_recorder` 支援熱插拔重建 + 節流。"""

    def test_retries_init_when_recorder_missing(self):
        va = _make_app()
        va._recorder = None

        def fake_init():
            recorder = MagicMock()
            recorder.is_open = True
            va._recorder = recorder

        va._init_recorder = MagicMock(side_effect=fake_init)

        assert va._ensure_recorder() is True
        va._init_recorder.assert_called_once()

    def test_no_retry_when_already_open(self):
        va = _make_app()
        recorder = MagicMock()
        recorder.is_open = True
        va._recorder = recorder
        va._init_recorder = MagicMock()

        assert va._ensure_recorder() is True
        va._init_recorder.assert_not_called()

    def test_retry_is_throttled_on_rapid_presses(self):
        """狂按時不可每次都去 probe 音訊裝置（PortAudio 查詢會阻塞鍵盤線程）。"""
        va = _make_app()
        va._recorder = None
        va._init_recorder = MagicMock()  # 一直失敗，_recorder 保持 None

        results = [va._ensure_recorder() for _ in range(10)]

        assert all(r is False for r in results)
        assert va._init_recorder.call_count == 1, (
            f"節流失效：10 次快速按鍵觸發了 {va._init_recorder.call_count} 次裝置重試"
        )

    def test_notifies_user_once_per_throttle_window(self):
        """重試失敗要彈托盤警告，但狂按只彈一次（不刷屏）。"""
        va = _make_app()
        va._recorder = None
        va._init_recorder = MagicMock()

        for _ in range(10):
            va._ensure_recorder()

        warnings = [c for c in va._invoke_gui.call_args_list if c.args[0] == "notify_warning"]
        assert len(warnings) == 1, f"托盤通知應節流為 1 次，實際 {len(warnings)} 次"
        assert "麥克風" in warnings[0].args[1][1]
