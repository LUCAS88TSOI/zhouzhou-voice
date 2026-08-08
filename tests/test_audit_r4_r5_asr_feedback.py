"""
審查修復 R4 / R5：ASR 失敗完全靜默、載入誤判成功、崩潰後永不重啟

R4：模型未載入 / 子進程崩潰 / 識別逾時 / response.error —— 四條失敗路徑
    全部只 logger 後 return None，狀態列還被 finally 設回「就緒」。
    使用者按住 CapsLock 講完一段，游標處零個字，唯一線索埋在 app.log。

R5：(a) start() 用 is_alive() 判斷載入結果，子進程送出 __load_error__ 後
    還在 bootstrap 收尾，is_alive() 幾乎必然 True → 直接印「載入完成」。
    (b) restart() 生產代碼零呼叫點，崩潰後永久失效直到使用者自己重啟。
"""

from __future__ import annotations

import queue as _queue
from unittest import mock

import pytest

from app.app import AsrFailure, AsrOutcome


class TestAsrOutcome:
    """_try_recognize 要回傳可辨識的失敗原因，而非一律 None。"""

    def test_success_carries_text(self) -> None:
        outcome = AsrOutcome(text="你好")
        assert outcome.text == "你好"
        assert outcome.failure is None
        assert outcome.ok is True

    def test_empty_result_is_not_a_failure(self) -> None:
        """使用者沒出聲 → 空結果但不是錯誤，不該報警。"""
        outcome = AsrOutcome(text="")
        assert outcome.ok is True
        assert outcome.failure is None

    @pytest.mark.parametrize(
        "failure",
        [
            AsrFailure.NOT_READY,
            AsrFailure.TIMEOUT,
            AsrFailure.ERROR,
        ],
    )
    def test_failures_are_distinguishable(self, failure: AsrFailure) -> None:
        outcome = AsrOutcome(failure=failure)
        assert outcome.ok is False
        assert outcome.message, "每種失敗都要有給使用者看的訊息"

    def test_not_ready_message_tells_user_what_to_do(self) -> None:
        msg = AsrOutcome(failure=AsrFailure.NOT_READY).message
        assert "設定" in msg or "模型" in msg


class TestTryRecognizeReportsFailure:
    def _app(self):
        from app.app import VoiceApp

        app = object.__new__(VoiceApp)
        app._asr_process = None
        app._config = None
        return app

    def test_not_ready_returns_not_ready_failure(self) -> None:
        app = self._app()
        outcome = app._try_recognize(b"\x00" * 64000)
        assert outcome.failure is AsrFailure.NOT_READY

    def test_timeout_returns_timeout_failure(self) -> None:
        app = self._app()
        fake = mock.MagicMock()
        fake.is_running = True
        fake.send_and_wait.side_effect = TimeoutError()
        app._asr_process = fake

        outcome = app._try_recognize(b"\x00" * 64000)
        assert outcome.failure is AsrFailure.TIMEOUT

    def test_response_error_returns_error_failure(self) -> None:
        app = self._app()
        fake = mock.MagicMock()
        fake.is_running = True
        fake.send_and_wait.return_value = mock.MagicMock(
            error="模型崩潰", text="",
        )
        app._asr_process = fake

        outcome = app._try_recognize(b"\x00" * 64000)
        assert outcome.failure is AsrFailure.ERROR
        assert "模型崩潰" in outcome.message

    def test_success_path_returns_text(self) -> None:
        app = self._app()
        fake = mock.MagicMock()
        fake.is_running = True
        fake.send_and_wait.return_value = mock.MagicMock(error="", text="今日天氣")
        app._asr_process = fake

        outcome = app._try_recognize(b"\x00" * 64000)
        assert outcome.text == "今日天氣"
        assert outcome.ok is True


class TestProcessAudioSurfacesAsrFailure:
    def test_asr_failure_notifies_and_sets_failed_status(self) -> None:
        """核心 bug：ASR 全掛掉但狀態列顯示「就緒」、零通知。"""
        from app.app import VoiceApp

        from utils.config import AppConfig

        app = object.__new__(VoiceApp)
        app._config = AppConfig()
        app._asr_process = None
        app._text_processor = None
        app._hotword = None
        app._recording_db = None
        app._last_result = ""
        app._last_pre_llm_text = ""
        app._is_processing = True
        import threading
        app._processing_lock = threading.Lock()

        calls: list[tuple] = []
        app._invoke_gui = lambda method, *a: calls.append((method, a))
        app._try_recognize = lambda b: AsrOutcome(failure=AsrFailure.NOT_READY)

        app._process_audio(b"\x00" * 64000)

        warned = [c for c in calls if c[0] == "notify_warning"]
        assert warned, "R4: ASR 失敗必須彈托盤通知"

        statuses = [c[1][0][1] for c in calls if c[0] == "set_status"]
        assert statuses[-1] == "失敗", f"R4: 最終狀態應為「失敗」，實際 {statuses[-1]}"

    def test_empty_speech_stays_quiet(self) -> None:
        """使用者沒出聲 → 不該彈通知，狀態回「就緒」。"""
        from app.app import VoiceApp

        from utils.config import AppConfig

        app = object.__new__(VoiceApp)
        app._config = AppConfig()
        app._asr_process = None
        app._text_processor = None
        app._hotword = None
        app._recording_db = None
        app._last_result = ""
        app._last_pre_llm_text = ""
        app._is_processing = True
        import threading
        app._processing_lock = threading.Lock()

        calls: list[tuple] = []
        app._invoke_gui = lambda method, *a: calls.append((method, a))
        app._try_recognize = lambda b: AsrOutcome(text="")

        app._process_audio(b"\x00" * 64000)

        assert not [c for c in calls if c[0] == "notify_warning"]
        statuses = [c[1][0][1] for c in calls if c[0] == "set_status"]
        assert statuses[-1] == "就緒"


class TestAsrProcessLoadDetection:
    """R5(a)：載入失敗不得被 is_alive() 誤判成成功。"""

    def test_load_error_raises_with_original_message(self) -> None:
        from core.asr_process import ASRProcess, ASRResponse

        proc = object.__new__(ASRProcess)
        proc._model_dir = "fake"
        proc._model_info = None

        fake_process = mock.MagicMock()
        fake_process.is_alive.return_value = True  # 關鍵：子進程還活著
        fake_process.pid = 1234

        fake_event = mock.MagicMock()
        fake_event.wait.return_value = True

        proc._process = None
        proc._ready_event = fake_event
        proc._queue_in = mock.MagicMock()
        proc._queue_out = mock.MagicMock()
        proc._queue_out.get_nowait.side_effect = _queue.Empty()
        proc._queue_out.get.return_value = ASRResponse(
            task_id="__load_error__", error="模型檔案損毀",
        )

        with mock.patch("core.asr_process.Process", return_value=fake_process):
            with pytest.raises(RuntimeError) as exc:
                proc.start()

        assert "模型檔案損毀" in str(exc.value)

    def test_load_ok_sentinel_accepted(self) -> None:
        from core.asr_process import ASRProcess, ASRResponse

        proc = object.__new__(ASRProcess)
        proc._model_dir = "fake"
        proc._model_info = None

        fake_process = mock.MagicMock()
        fake_process.is_alive.return_value = True
        fake_process.pid = 1234

        fake_event = mock.MagicMock()
        fake_event.wait.return_value = True

        proc._process = None
        proc._ready_event = fake_event
        proc._queue_in = mock.MagicMock()
        proc._queue_out = mock.MagicMock()
        proc._queue_out.get_nowait.side_effect = _queue.Empty()
        proc._queue_out.get.return_value = ASRResponse(task_id="__load_ok__")

        with mock.patch("core.asr_process.Process", return_value=fake_process):
            proc.start()  # 不應拋出

    def test_stale_response_does_not_break_restart(self) -> None:
        """restart() 時 queue 裡的舊識別回應不得讓載入判定誤判為失敗。"""
        from core.asr_process import ASRProcess, ASRResponse

        proc = object.__new__(ASRProcess)
        proc._model_dir = "fake"
        proc._model_info = None
        proc._process = None

        fake_process = mock.MagicMock()
        fake_process.is_alive.return_value = True
        fake_process.pid = 1234

        fake_event = mock.MagicMock()
        fake_event.wait.return_value = True
        proc._ready_event = fake_event
        proc._queue_in = mock.MagicMock()

        # 崩潰前殘留的舊回應排在載入哨兵前面
        pending = [
            ASRResponse(task_id="stale-abc", text="上一次沒收走的結果"),
            ASRResponse(task_id="__load_ok__"),
        ]

        fake_queue = mock.MagicMock()
        fake_queue.get_nowait.side_effect = _queue.Empty()
        fake_queue.get.side_effect = lambda *a, **k: pending.pop(0)
        proc._queue_out = fake_queue

        with mock.patch("core.asr_process.Process", return_value=fake_process):
            proc.start()  # 不應因殘留回應而拋出


class TestEnsureAsrRestarts:
    """R5(b)：子進程死掉時要自動重啟，而非永久失效。"""

    def test_ensure_asr_restarts_dead_process(self) -> None:
        from app.app import VoiceApp

        app = object.__new__(VoiceApp)
        fake = mock.MagicMock()
        fake.is_running = False
        app._asr_process = fake
        app._asr_restart_attempts = 0
        app._asr_last_restart = 0.0
        app._invoke_gui = lambda *a: None

        assert app._ensure_asr() is True or fake.restart.called
        assert fake.restart.called, "R5: 子進程已死必須嘗試 restart()"

    def test_ensure_asr_noop_when_running(self) -> None:
        from app.app import VoiceApp

        app = object.__new__(VoiceApp)
        fake = mock.MagicMock()
        fake.is_running = True
        app._asr_process = fake
        app._asr_restart_attempts = 0
        app._asr_last_restart = 0.0
        app._invoke_gui = lambda *a: None

        assert app._ensure_asr() is True
        assert not fake.restart.called

    def test_restart_attempts_are_capped(self) -> None:
        """避免無限重啟風暴。"""
        from app.app import VoiceApp

        app = object.__new__(VoiceApp)
        fake = mock.MagicMock()
        fake.is_running = False
        fake.restart.side_effect = RuntimeError("still broken")
        app._asr_process = fake
        app._asr_restart_attempts = 99
        app._asr_last_restart = 0.0
        app._invoke_gui = lambda *a: None

        assert app._ensure_asr() is False
        assert not fake.restart.called, "已達上限不應再重啟"
