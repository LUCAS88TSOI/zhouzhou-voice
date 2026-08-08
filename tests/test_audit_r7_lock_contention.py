"""
審查修復 R7：_processing_lock 跨 LLM I/O 持鎖，把 pynput 鉤子線程卡住數十秒

三個機制疊在一起：
(a) _run_repolish / _run_polish_selection 用同一把 _processing_lock 包住整個
    LLM 往返 + paste_text（含 0.4s restore sleep）
(b) 這兩條路徑沒傳 enforce_timeout=True，client 走預設 30s read timeout，
    失敗後 _failover 再無截止時間逐一試完所有 provider，最壞 30–120 秒
(c) _on_recording_stop 用阻塞式 `with self._processing_lock:`，而它是由
    hotkey.py 在 pynput listener thread（擁有 WH_KEYBOARD_LL 鉤子那條）同步呼叫

repolish_key 預設就是 "f2" 且 repolish_instant=True，開箱即用。按完 F2 之後
全域快捷鍵偵測失靈（Windows LowLevelHooksTimeout 預設 300ms 就開始略過鉤子），
使用者感受是「CapsLock 突然沒反應了」。
"""

from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from utils.config import AppConfig


def _app():
    from app.app import VoiceApp

    app = object.__new__(VoiceApp)
    app._config = AppConfig()
    app._processing_lock = threading.Lock()
    app._is_processing = False
    app._is_repolishing = False
    app._recorder = None
    app._last_result = "上一次的結果"
    app._last_pre_llm_text = "上一次的結果"
    app._invoke_gui = lambda *a: None
    app._shutting_down = False
    return app


class TestLockNotHeldAcrossIo:
    def test_recording_stop_not_blocked_by_repolish(self) -> None:
        """核心 bug：repolish 進行中，_on_recording_stop 不得被阻塞住。"""
        app = _app()

        io_started = threading.Event()
        io_release = threading.Event()

        def slow_polish(*args, **kwargs):
            io_started.set()
            io_release.wait(timeout=5.0)
            from llm.processor import LLMResultStatus
            return LLMResultStatus(
                success=True, text="潤色後", was_processed=True,
            )

        app._try_llm_polish = slow_polish
        app._build_repolish_processor = lambda: (mock.MagicMock(), "")

        worker = threading.Thread(target=app._run_repolish, daemon=True)
        worker.start()
        assert io_started.wait(timeout=3.0), "repolish 未進入 LLM 階段"

        # 此刻 LLM I/O 正在進行 —— 鉤子線程必須能立刻拿到鎖
        acquired = app._processing_lock.acquire(timeout=0.5)
        try:
            assert acquired, (
                "R7: LLM I/O 期間 _processing_lock 仍被持有，"
                "pynput 鉤子線程會被卡住數十秒"
            )
        finally:
            if acquired:
                app._processing_lock.release()
            io_release.set()
            worker.join(timeout=5.0)

    def test_second_repolish_is_rejected_while_first_runs(self) -> None:
        """單次執行保證不能因為改用短鎖而失效。"""
        app = _app()

        io_started = threading.Event()
        io_release = threading.Event()
        calls: list[int] = []

        def slow_polish(*args, **kwargs):
            calls.append(1)
            io_started.set()
            io_release.wait(timeout=5.0)
            from llm.processor import LLMResultStatus
            return LLMResultStatus(success=True, text="x", was_processed=True)

        app._try_llm_polish = slow_polish
        app._build_repolish_processor = lambda: (mock.MagicMock(), "")

        first = threading.Thread(target=app._run_repolish, daemon=True)
        first.start()
        assert io_started.wait(timeout=3.0)

        app._run_repolish()  # 第二次應立刻被拒，不進 LLM
        assert len(calls) == 1, "R7: 重新潤色必須維持 single-flight"

        io_release.set()
        first.join(timeout=5.0)

    def test_repolish_rejected_while_voice_pipeline_running(self) -> None:
        """語音管線處理中不得同時 repolish（原本靠同一把鎖保證）。"""
        app = _app()
        app._is_processing = True

        calls: list[int] = []
        app._try_llm_polish = lambda *a, **k: calls.append(1)
        app._build_repolish_processor = lambda: (mock.MagicMock(), "")

        app._run_repolish()
        assert not calls, "R7: 語音處理中不該啟動重新潤色"

    def test_recording_stop_rejected_while_repolishing(self) -> None:
        """反向：repolish 進行中，錄音處理應被拒（而非阻塞等待）。"""
        app = _app()
        app._is_repolishing = True

        fake_recorder = mock.MagicMock()
        fake_recorder.stop_recording.return_value = b"\x00" * 64000
        app._recorder = fake_recorder

        spawned: list[str] = []
        app._spawn_worker = lambda fn, **kw: spawned.append(kw.get("name", ""))

        start = time.monotonic()
        app._on_recording_stop()
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"R7: 不得阻塞等待，耗時 {elapsed:.2f}s"
        assert not spawned, "repolish 進行中不該啟動語音管線"

    def test_flag_released_even_when_polish_raises(self) -> None:
        """例外路徑也要把旗標放掉，否則之後永久卡住。"""
        app = _app()

        def boom(*args, **kwargs):
            raise RuntimeError("網路炸了")

        app._try_llm_polish = boom
        app._build_repolish_processor = lambda: (mock.MagicMock(), "")

        app._run_repolish()

        assert app._is_repolishing is False
        assert not app._processing_lock.locked()


class TestFailoverDeadline:
    def test_failover_stops_after_deadline_even_without_should_stop(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """should_stop 為 None 時 _failover 也必須自己算截止時間。

        正式預算是 15 秒（測試不可能真等），這裡把常數壓小驗證機制本身。
        """
        from llm import processor as proc_mod
        from llm.processor import LLMProcessor, LLMResult
        from llm.provider import ProviderInfo

        class _LLM:
            active_provider = ""
            providers: dict = {}
            enabled = True

        class _Cfg:
            llm = _LLM()

        p = LLMProcessor(_Cfg())
        monkeypatch.setattr(proc_mod, "_FAILOVER_BUDGET", 0.3)

        provs = [
            ProviderInfo(key=k, name=k, api_url=f"https://{k}.test/v1",
                         api_key="k", model="m", enabled=True)
            for k in ("a", "b", "c", "d", "e")
        ]

        with mock.patch.object(proc_mod, "list_available_providers", lambda c: provs):
            with mock.patch.object(
                p, "_build_client", lambda *a, **k: object(),
            ):
                calls: list[int] = []

                def slow_stream(**kwargs):
                    calls.append(1)
                    time.sleep(0.15)
                    return LLMResult(error="連線逾時")

                with mock.patch.object(p, "_stream_chat", slow_stream):
                    p._failover(
                        messages=[], failed_provider=None,
                        first_error="連線逾時", on_token=None,
                        should_stop=None, request_timeout=0.1,
                    )

        assert len(calls) < len(provs), (
            f"R7: _failover 應在截止後中止，但試了全部 {len(calls)} 個 provider"
        )
