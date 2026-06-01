"""
TDD Loop Iteration 2 — Codex re-review 發現的 4 個 high + 1 個 dead-code medium。

測試覆蓋：
  Bug 3 — LLMProcessor.clear_history() 期間 in-flight process() 仍寫回舊歷史
  Bug 4 — VoiceApp._spawn_worker thread.start 失敗留下殭屍 registry + stuck flag
  Bug 6 — HotkeyListener callback 交錯（deactivate 先於 activate）
  Bug 7 — shutdown 期間新 worker 仍被 spawn（iter 1 regression）
  Bug 2 — LLMProcessor._build_messages dead code（無測試，只驗證刪除）
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────
# Bug 3 — clear_history epoch race
# ─────────────────────────────────────────────────────────────


class TestBug3ClearHistoryEpochRace:
    """Bug 3: process() snapshot 後在鎖外做 HTTP，成功回寫時若 clear_history 已
    被呼叫，使用者看到清空後下一輪仍帶回舊對話 = 隱性上下文洩漏。"""

    def _make_processor(self, block: threading.Event):
        """建立 processor，其 client.chat 會在 block 被 set 前阻塞。"""
        from llm.processor import LLMProcessor

        class FakeLLMConfig:
            enabled = True
            temperature = 0.3
            max_tokens = 256
            top_p = 1.0
            frequency_penalty = 0.0
            presence_penalty = 0.0
            do_sample = True
            active_provider = "fake"
            providers = {}
            custom_roles = []
            builtin_overrides = {}
            active_role = "default"

        processor = object.__new__(LLMProcessor)
        processor._config = FakeLLMConfig()
        processor._llm_config = processor._config
        processor._conversation_history = []
        processor._state_lock = threading.RLock()
        processor._history_epoch = 0

        fake_client = MagicMock()

        def fake_chat(messages, stream=True):
            block.wait(timeout=3.0)
            yield "new-assistant-reply"

        def fake_chat_with_warnings(messages, stream=True):
            # iter 3 Bug C：新 per-call API 回傳 (generator, warnings_list)
            return fake_chat(messages, stream=stream), []

        fake_client.chat = fake_chat
        fake_client.chat_with_warnings = fake_chat_with_warnings
        fake_client.param_warnings = []
        processor._client = fake_client
        processor._provider = MagicMock(model="fake-model")
        return processor

    def test_history_epoch_attribute_exists(self):
        """LLMProcessor 應有 _history_epoch 計數器。"""
        from llm.processor import LLMProcessor

        p = object.__new__(LLMProcessor)
        LLMProcessor.__init__(p, _FakeLLM())
        assert hasattr(p, "_history_epoch"), "缺少 _history_epoch 欄位"
        assert isinstance(p._history_epoch, int)

    def test_clear_history_increments_epoch(self):
        """clear_history() 應遞增 epoch，使 in-flight append 失效。"""
        from llm.processor import LLMProcessor

        p = object.__new__(LLMProcessor)
        LLMProcessor.__init__(p, _FakeLLM())
        before = p._history_epoch
        p.clear_history()
        assert p._history_epoch == before + 1, "clear_history 必須遞增 epoch"

    def test_append_history_with_stale_epoch_is_noop(self):
        """_append_history 收到過期 epoch 時，不寫入歷史。"""
        from llm.processor import LLMProcessor

        p = object.__new__(LLMProcessor)
        LLMProcessor.__init__(p, _FakeLLM())
        stale_epoch = p._history_epoch
        p.clear_history()  # 現在 epoch = stale_epoch + 1
        p._append_history("u", "a", expected_epoch=stale_epoch)
        assert p._conversation_history == [], (
            "expected_epoch 過期時 _append_history 應 skip"
        )

    def test_append_history_with_current_epoch_writes(self):
        """expected_epoch 等於當前 epoch 時，正常寫入。"""
        from llm.processor import LLMProcessor

        p = object.__new__(LLMProcessor)
        LLMProcessor.__init__(p, _FakeLLM())
        p._append_history("u", "a", expected_epoch=p._history_epoch)
        assert len(p._conversation_history) == 2

    def test_clear_during_process_skips_history_writeback(self):
        """process() 執行中，clear_history 被呼叫 → process 完成後不應寫回。

        具體劇本：
          1. 歷史先有 [user/hi, assistant/hello]
          2. process(text="new", enable_history=True) 進入、snapshot、卡在 HTTP
          3. 主線程 clear_history() → 歷史清空 + epoch+1
          4. block.set()，process 完成
          5. 檢查 _conversation_history 應仍為空，不應有 "new input/reply" 混入
        """
        from llm.processor import RoleConfig

        block = threading.Event()
        processor = self._make_processor(block)
        processor._conversation_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        role = RoleConfig(name="test", system_prompt="sys", enable_history=True)

        holder = []

        def run():
            holder.append(processor.process(text="new input", role=role))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # 等 chat 進入阻塞
        time.sleep(0.1)

        # 另一線程清空歷史
        processor.clear_history()
        assert processor._conversation_history == []

        # 放行 process
        block.set()
        t.join(timeout=3.0)

        assert len(holder) == 1
        # 核心不變性：clear 後歷史仍為空，未被 in-flight process 汙染
        assert processor._conversation_history == [], (
            f"clear 後歷史應保持為空，實際: {processor._conversation_history}"
        )


class _FakeLLM:
    """最小 fake LLMConfig-like object，支援 LLMProcessor.__init__ 讀取。"""
    enabled = False  # 關掉 LLM 使 _init_client 走空路徑
    temperature = 0.3
    max_tokens = 256
    top_p = 1.0
    frequency_penalty = 0.0
    presence_penalty = 0.0
    do_sample = True
    active_provider = ""
    providers = {}
    custom_roles = []
    builtin_overrides = {}
    active_role = "default"


# ─────────────────────────────────────────────────────────────
# Bug 4 — _spawn_worker thread.start 失敗的 registry/flag 清理
# ─────────────────────────────────────────────────────────────


class TestBug4SpawnWorkerStartFailure:
    """Bug 4: 若 thread.start() 拋例外，原實作已 add() 到 _active_workers，
    使 registry 留殭屍。呼叫端也因此假設 worker 會跑而未 reset flag。"""

    def _make_app(self):
        from app.app import VoiceApp
        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)
        return va

    def test_spawn_worker_start_exception_does_not_leave_zombie(self):
        """thread.start() 拋例外時，_active_workers 必不含該 thread。"""
        va = self._make_app()

        real_thread_cls = threading.Thread

        class FailingThread(real_thread_cls):
            def start(self):
                raise RuntimeError("模擬 start 失敗")

        import app.app as app_mod
        orig = app_mod.threading.Thread
        app_mod.threading.Thread = FailingThread
        try:
            try:
                va._spawn_worker(lambda: None, name="will-fail")
            except RuntimeError:
                pass
            # 關鍵檢查：registry 不該留殭屍 thread
            with va._active_workers_lock:
                names = [t.name for t in va._active_workers]
            assert "will-fail" not in names, (
                f"start 失敗的 thread 不該留在 registry：{names}"
            )
        finally:
            app_mod.threading.Thread = orig

    def test_recording_stop_resets_flag_when_spawn_fails(self):
        """_on_recording_stop 中若 _spawn_worker 拋例外，_is_processing 必須被 reset。"""
        va = self._make_app()
        recorder = MagicMock()
        recorder.stop_recording.return_value = b"\x00" * (4 * 16000)
        va._recorder = recorder

        # Patch _spawn_worker 模擬失敗
        def failing_spawn(*args, **kwargs):
            raise RuntimeError("模擬 spawn 失敗")
        va._spawn_worker = failing_spawn

        try:
            va._on_recording_stop()
        except RuntimeError:
            pass  # 可接受 propagate 或 silent

        assert va._is_processing is False, (
            "_spawn_worker 失敗後 _is_processing 必須 reset"
        )

    def test_transcribe_resets_flag_when_spawn_fails(self):
        """_on_files_dropped 中若 _spawn_worker 拋例外，_is_transcribing 必須被 reset。"""
        va = self._make_app()
        # Mock asr_process ready
        va._asr_process = MagicMock(is_running=True)
        va._config = MagicMock()
        va._config.file = MagicMock()
        va._main_window = None

        def failing_spawn(*args, **kwargs):
            raise RuntimeError("模擬 spawn 失敗")
        va._spawn_worker = failing_spawn

        try:
            va._on_files_dropped(["fake.mp3"])
        except RuntimeError:
            pass

        assert va._is_transcribing is False, (
            "_spawn_worker 失敗後 _is_transcribing 必須 reset"
        )


# ─────────────────────────────────────────────────────────────
# Bug 6 — HotkeyListener callback 排序
# ─────────────────────────────────────────────────────────────


class TestBug6HotkeyCallbackOrdering:
    """Bug 6: threshold timer thread 與 release thread 並發時，
    deactivate 可能先於 activate 被呼叫或兩者交錯。"""

    def _make_listener(self, threshold=0.01):
        from utils.hotkey import HotkeyListener

        order: list[str] = []
        order_lock = threading.Lock()

        def act():
            with order_lock:
                order.append("A:enter")
            time.sleep(0.02)  # 模擬 callback 非即刻結束
            with order_lock:
                order.append("A:exit")

        def deact():
            with order_lock:
                order.append("D:enter")
            time.sleep(0.02)
            with order_lock:
                order.append("D:exit")

        listener = HotkeyListener(
            key="caps_lock",
            threshold=threshold,
            suppress=False,
            on_activate=act,
            on_deactivate=deact,
        )
        return listener, order

    def test_callback_lock_attribute_exists(self):
        """HotkeyListener 應有專屬的 _callback_lock（不是 _state_lock，
        避免持 state lock 期間執行慢 callback 阻塞 press/release）。"""
        from utils.hotkey import HotkeyListener
        listener = HotkeyListener(key="caps_lock", threshold=0.1, suppress=False)
        assert hasattr(listener, "_callback_lock"), "缺少 _callback_lock 欄位"

    def test_callbacks_are_serialized_not_interleaved(self):
        """activate 與 deactivate callback 不可交錯執行。

        模擬：press → 等 timer 觸發 activate 進入 → 同時 release 觸發 deactivate。
        序列必須是：A:enter → A:exit → D:enter → D:exit（或全 D 後 A，但不能交錯）。
        """
        listener, order = self._make_listener(threshold=0.01)

        listener._handle_press()
        # 等 timer 觸發 activate
        time.sleep(0.015)
        # 此時 activate callback 可能仍在 sleep 中（20ms）
        listener._handle_release()
        # 等 callbacks 都跑完
        time.sleep(0.1)

        # 檢查：找到 A:enter/exit 和 D:enter/exit 的相對位置
        # 不該有 A:enter → D:enter → A:exit 這種交錯
        if "A:enter" in order and "D:enter" in order:
            a_enter = order.index("A:enter")
            a_exit = order.index("A:exit") if "A:exit" in order else -1
            d_enter = order.index("D:enter")
            # A 必須完整結束才能開始 D（或反之）
            assert a_exit < d_enter or a_enter > order.index("D:exit") if "D:exit" in order else True, (
                f"callback 序列交錯: {order}"
            )

    def test_concurrent_callback_invocation_stays_ordered(self):
        """高壓力下多次 press/release，callback 不可交錯執行。"""
        listener, order = self._make_listener(threshold=0.005)

        def cycle():
            for _ in range(5):
                listener._handle_press()
                time.sleep(0.01)  # 讓 timer 觸發 activate
                listener._handle_release()
                time.sleep(0.005)

        threads = [threading.Thread(target=cycle) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)
        time.sleep(0.2)  # 等所有 timer callback 跑完

        # 把 enter/exit 配對：每個 A:enter 後必須是對應的 A:exit（中間無其他 callback）
        # 同理 D:enter 後必須是 D:exit
        i = 0
        while i < len(order):
            if order[i] == "A:enter":
                assert i + 1 < len(order) and order[i + 1] == "A:exit", (
                    f"A:enter 後未立即接 A:exit，位置 {i}: {order[i:i+3]}"
                )
                i += 2
            elif order[i] == "D:enter":
                assert i + 1 < len(order) and order[i + 1] == "D:exit", (
                    f"D:enter 後未立即接 D:exit，位置 {i}: {order[i:i+3]}"
                )
                i += 2
            else:
                i += 1


# ─────────────────────────────────────────────────────────────
# Bug 7 — shutdown 期間 spawn race
# ─────────────────────────────────────────────────────────────


class TestBug7ShutdownSpawnRace:
    """Bug 7: shutdown 流程調 _wait_active_workers 時，hotkey listener 還沒停，
    仍可新 spawn worker → 錯過等待。"""

    def _make_app(self):
        from app.app import VoiceApp
        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)
        return va

    def test_is_shutting_down_attribute_exists(self):
        """VoiceApp 應有 _is_shutting_down 欄位。"""
        va = self._make_app()
        assert hasattr(va, "_is_shutting_down"), "缺少 _is_shutting_down 欄位"
        assert va._is_shutting_down is False

    def test_spawn_worker_rejected_during_shutdown(self):
        """_is_shutting_down=True 時，_spawn_worker 不應啟動新 thread。"""
        va = self._make_app()
        va._is_shutting_down = True

        ran = threading.Event()

        def payload():
            ran.set()

        # 可接受兩種行為：silent skip（返回 None）或 raise RuntimeError
        try:
            result = va._spawn_worker(payload, name="post-shutdown")
        except RuntimeError:
            result = None

        time.sleep(0.1)
        assert not ran.is_set(), "shutdown 期間 payload 不該執行"
        with va._active_workers_lock:
            names = [t.name for t in va._active_workers]
        assert "post-shutdown" not in names, (
            f"shutdown 期間不該將 worker 加入 registry：{names}"
        )

    def test_spawn_worker_works_before_shutdown(self):
        """正常狀態下 _spawn_worker 應照常運作（regression guard）。"""
        va = self._make_app()
        assert va._is_shutting_down is False

        ran = threading.Event()
        t = va._spawn_worker(lambda: ran.set(), name="normal-worker")
        assert t is not None, "正常狀態下應返回 thread"
        ran.wait(timeout=1.0)
        assert ran.is_set()
        t.join(timeout=1.0)


# ─────────────────────────────────────────────────────────────
# Bug 2 — dead code removal verification
# ─────────────────────────────────────────────────────────────


class TestBug2DeadCodeRemoval:
    """Bug 2: `_build_messages` 已無呼叫點，只該保留 `_build_messages_with_history`。"""

    def test_build_messages_removed(self):
        """_build_messages 方法應已被刪除。"""
        from llm.processor import LLMProcessor
        assert not hasattr(LLMProcessor, "_build_messages"), (
            "_build_messages 是 dead code，應已刪除"
        )

    def test_build_messages_with_history_still_present(self):
        """_build_messages_with_history 仍應存在（process() 使用）。"""
        from llm.processor import LLMProcessor
        assert hasattr(LLMProcessor, "_build_messages_with_history")
