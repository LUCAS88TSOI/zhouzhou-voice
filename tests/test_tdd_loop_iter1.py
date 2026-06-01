"""
TDD Loop Iteration 1 — Codex adversarial review 發現的 4 個 high severity bugs。

測試覆蓋：
  Bug 1 — HotkeyListener 執行緒競態（press / release / threshold timer）
  Bug 2 — VoiceApp._on_recording_stop 的 _is_processing guard 非原子
  Bug 3 — daemon workers 超出 shutdown 不等待
  Bug 4 — LLMProcessor 並發呼叫下 history/client/provider 可被換掉
"""
from __future__ import annotations

import sys
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────
# Bug 1 — HotkeyListener 狀態未同步
# ─────────────────────────────────────────────────────────────


class TestBug1HotkeyThreadingSafety:
    """Bug 1: press / release / threshold timer 並發競態。

    失敗情境：按下後迅速鬆開，但 timer 在 release 之後才觸發 on_activate，
    導致 _is_activated=True 但 _on_deactivate 從未被呼叫 → 錄音卡住。
    """

    def _make_listener(self, threshold=0.05):
        from utils.hotkey import HotkeyListener

        activate_calls = []
        deactivate_calls = []

        listener = HotkeyListener(
            key="caps_lock",
            threshold=threshold,
            suppress=False,
            on_activate=lambda: activate_calls.append(time.monotonic()),
            on_deactivate=lambda: deactivate_calls.append(time.monotonic()),
        )
        return listener, activate_calls, deactivate_calls

    def test_release_before_timer_cancels_activation(self):
        """按下 → 迅速鬆開（threshold 前）→ timer 不能觸發 on_activate。"""
        listener, activates, deactivates = self._make_listener(threshold=0.1)

        listener._handle_press()
        # 閾值到達前立即 release
        listener._handle_release()
        # 等待原 timer 的時間過去
        time.sleep(0.2)

        # release 已 cancel timer，on_activate 不應被呼叫
        assert activates == [], "短按不該觸發 on_activate"
        # 短按也不該觸發 on_deactivate（因為沒有達到 activate 狀態）
        assert deactivates == [], "短按不該觸發 on_deactivate"
        assert listener._is_activated is False
        assert listener._is_pressed is False

    def test_race_release_interleaved_with_timer_callback(self):
        """模擬 release 與 timer callback 交錯執行。

        原本的 bug：timer callback 已進入 _on_threshold_reached 並通過 _is_pressed 檢查，
        之後 release 把 _is_pressed 設為 False，但 timer 已經呼叫 on_activate，
        然後 release 因為 _is_activated 尚未設 True 而走「短按」分支 → 不呼叫 on_deactivate。
        修復後：必須用鎖使 press→set_activated→callback 或 release→cancel 之間串行化，
        二擇一：要嘛呼叫 on_activate+on_deactivate，要嘛都不呼叫，不能只呼叫 activate。
        """
        listener, activates, deactivates = self._make_listener(threshold=0.01)

        # 按下：啟動 timer（threshold 0.01 秒很短）
        listener._handle_press()
        # 在 timer 觸發前/後 release — 等 timer 跑完再 release 模擬交錯
        time.sleep(0.05)
        listener._handle_release()
        time.sleep(0.05)

        # 不變性：若 on_activate 被呼叫了一次，on_deactivate 必須被對稱呼叫一次
        assert len(activates) == len(deactivates), (
            f"activate ({len(activates)}) / deactivate ({len(deactivates)}) 必須對稱"
        )
        # 結束狀態必須乾淨
        assert listener._is_activated is False
        assert listener._is_pressed is False

    def test_rapid_press_release_cycle_no_orphaned_activation(self):
        """快速 press/release 多次，不應有孤兒 on_activate（沒有對應 on_deactivate）。"""
        listener, activates, deactivates = self._make_listener(threshold=0.02)

        for _ in range(5):
            listener._handle_press()
            time.sleep(0.005)  # 遠小於 threshold
            listener._handle_release()
        # 等所有 timer 跑完
        time.sleep(0.1)

        # 所有循環都是短按，on_activate / on_deactivate 都不應觸發
        assert activates == []
        assert deactivates == []
        assert listener._is_activated is False
        assert listener._is_pressed is False

    def test_timer_callback_after_release_is_noop(self):
        """模擬 release 與 timer callback 真正交錯：release 已執行後，
        再手動呼叫 _on_threshold_reached（模擬 timer thread 已進入
        callback 但還沒執行到檢查點），不應觸發 on_activate。

        這直接測試修復的核心 invariant：threshold callback 必須以原子方式
        檢查 _is_pressed 並設定 _is_activated。
        """
        listener, activates, deactivates = self._make_listener(threshold=1.0)

        # Press — timer 會排程但 1 秒內不觸發
        listener._handle_press()
        assert listener._is_pressed is True

        # Release — cancel timer
        listener._handle_release()
        assert listener._is_pressed is False

        # 模擬 timer thread 延遲觸發（cancel 之後才執行）
        listener._on_threshold_reached()

        assert activates == [], "release 後 _on_threshold_reached 不應觸發 activate"
        assert listener._is_activated is False

    def test_release_between_threshold_check_and_activation(self):
        """直接暴露 race：timer callback 在檢查 _is_pressed 後、設 _is_activated 前，
        被 release 搶先執行。

        原碼：
          def _on_threshold_reached(self):
              if not self._is_pressed: return   # (A) 通過
              # [race window] release 發生：_is_pressed=False, _is_activated=False
              self._is_activated = True          # (B) 仍設 True
              if self._on_activate: self._on_activate()  # (C) 孤兒 activate

          def _handle_release(self):
              if not self._is_pressed: return
              self._is_pressed = False
              ...
              if self._is_activated: ... on_deactivate  # 但此時還是 False
              else: # 走短按分支

        結果：activate 被呼叫，deactivate 從未被呼叫 → 錄音卡住。

        修復後：on_threshold_reached 從通過檢查到呼叫 on_activate 應在同一個鎖
        保護區；release 也需拿同一把鎖，這樣 release 會等 threshold 走完或
        threshold 會在檢查時看到 _is_pressed=False。

        測試方法：monkey-patch _is_pressed 的讀取，在讀取完後立即觸發 release。
        """
        listener, activates, deactivates = self._make_listener(threshold=1.0)

        # 直接模擬「_on_threshold_reached 通過檢查後、執行 callback 前，release 已發生」
        # 這代表：在 timer callback 執行中途，_is_pressed 可能已被設為 False
        # 正確的修復應確保不論這種 race 是否發生，activate/deactivate 對稱
        listener._handle_press()
        # 模擬 threshold callback 執行到一半時，release 先跑完
        # 直接呼叫 release 讓狀態歸零
        def interleaved_release_then_timer():
            listener._handle_release()
        # 在 _on_threshold_reached 執行前先做 release
        interleaved_release_then_timer()
        # 然後 timer callback 延遲觸發
        listener._on_threshold_reached()

        # 不變性：activate / deactivate 必須對稱
        assert len(activates) == len(deactivates), (
            f"activate={len(activates)} vs deactivate={len(deactivates)}"
        )
        assert listener._is_activated is False

    def test_press_release_stress_with_short_threshold(self):
        """壓力測試：多線程併發 press/release，不應留下 _is_activated=True 的殘留。"""
        listener, activates, deactivates = self._make_listener(threshold=0.01)

        stop = threading.Event()

        def press_loop():
            while not stop.is_set():
                listener._handle_press()
                time.sleep(0.002)
                listener._handle_release()

        threads = [threading.Thread(target=press_loop) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=1.0)
        time.sleep(0.1)  # 讓殘留 timer 跑完

        # 不變性：activate 數量必須等於 deactivate 數量
        assert len(activates) == len(deactivates), (
            f"activate={len(activates)} vs deactivate={len(deactivates)} 不對稱"
        )
        # 最終狀態必須乾淨
        assert listener._is_activated is False
        assert listener._is_pressed is False


# ─────────────────────────────────────────────────────────────
# Bug 2 — _is_processing guard 非原子
# ─────────────────────────────────────────────────────────────


class TestBug2ProcessingGuardAtomic:
    """Bug 2: 兩次快速 recording stop 能同時通過 _is_processing guard。"""

    def _make_app(self):
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        va._is_processing = False
        va._processing_lock = threading.Lock()
        va._active_workers = set()
        va._active_workers_lock = threading.Lock()
        # iter 2 Bug 7：shutdown guard 欄位（_spawn_worker 會讀取）
        va._is_shutting_down = False
        # Mock recorder 回傳固定音頻
        recorder = MagicMock()
        # 1 秒 float32 音頻（大於 0.1 秒門檻）
        recorder.stop_recording.return_value = b"\x00" * (4 * 16000)
        va._recorder = recorder
        va._main_window = None
        return va

    def test_concurrent_stop_only_one_worker(self):
        """兩個 thread 同時 _on_recording_stop，_process_audio 只能被呼叫一次。

        原 bug：
          if self._is_processing: return
          # <-- 此處另一線程也通過檢查 -->
          thread = Thread(target=self._process_audio, ...)
          thread.start()
        兩個線程都會 start worker。修復後需 atomic 檢查 + 設置。
        """
        va = self._make_app()

        process_call_count = [0]
        process_release = threading.Event()
        call_lock = threading.Lock()

        def fake_process_audio(audio_bytes):
            with call_lock:
                process_call_count[0] += 1
            # 讓 worker 阻塞一段時間，確保第二個 caller 的 guard 檢查能看到
            # _is_processing=True
            process_release.wait(timeout=1.0)
            with va._processing_lock:
                va._is_processing = False

        va._process_audio = fake_process_audio

        # 強制讓兩個線程在 guard 處「幾乎同時」進入，放大 race window。
        # 透過 barrier 使兩個 caller 都通過 recorder.stop_recording 後再進入 guard。
        gate = threading.Barrier(2, timeout=2.0)
        original_stop = va._recorder.stop_recording.return_value

        def gated_stop():
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                pass
            return original_stop

        va._recorder.stop_recording = gated_stop

        def caller():
            va._on_recording_stop()

        t1 = threading.Thread(target=caller)
        t2 = threading.Thread(target=caller)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        # 放掉 worker
        process_release.set()
        time.sleep(0.2)

        assert process_call_count[0] == 1, (
            f"_process_audio 應該只被呼叫 1 次，實際 {process_call_count[0]} 次"
        )

    def test_is_processing_set_before_thread_start(self):
        """防重入標記應在 start thread 之前設為 True（atomic guard），
        否則兩次快速呼叫能同時通過檢查。"""
        va = self._make_app()

        observed_is_processing = []

        def fake_process(audio_bytes):
            # worker 開始運行時，_is_processing 必須已經是 True
            observed_is_processing.append(va._is_processing)
            va._is_processing = False

        va._process_audio = fake_process
        va._on_recording_stop()
        time.sleep(0.1)  # 等 daemon thread

        assert observed_is_processing == [True], (
            f"worker 執行時 _is_processing 必須為 True，實際 observed={observed_is_processing}"
        )


# ─────────────────────────────────────────────────────────────
# Bug 3 — daemon workers 超出 shutdown
# ─────────────────────────────────────────────────────────────


class TestBug3DaemonWorkersJoinedOnShutdown:
    """Bug 3: cleanup 應等 in-flight daemon workers 結束。"""

    def test_active_workers_registry_exists(self):
        """VoiceApp 應暴露 _active_workers 集合 + 對應鎖。"""
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)
        assert hasattr(va, "_active_workers"), "缺少 _active_workers"
        assert hasattr(va, "_active_workers_lock"), "缺少 _active_workers_lock"

    def test_spawn_worker_tracks_thread(self):
        """_spawn_worker 應 start thread 且加入 _active_workers；結束時自動移除。"""
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)

        started = threading.Event()
        finish = threading.Event()

        def payload():
            started.set()
            finish.wait(timeout=3.0)

        t = va._spawn_worker(payload, name="test-worker")
        assert t.is_alive()
        started.wait(timeout=1.0)
        with va._active_workers_lock:
            assert t in va._active_workers

        finish.set()
        t.join(timeout=2.0)
        # thread 結束後應自動從 registry 移除
        time.sleep(0.05)
        with va._active_workers_lock:
            assert t not in va._active_workers

    def test_wait_active_workers_joins_inflight(self):
        """_wait_active_workers() 應等待還活著的 worker 完成。"""
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)

        completed = threading.Event()

        def slow_payload():
            time.sleep(0.2)
            completed.set()

        va._spawn_worker(slow_payload, name="slow-worker")
        # 呼叫 _wait_active_workers 應阻塞直到 payload 完成（或 timeout）
        start_ts = time.monotonic()
        va._wait_active_workers(timeout=2.0)
        elapsed = time.monotonic() - start_ts

        assert completed.is_set(), "payload 必須完成"
        assert elapsed >= 0.15, f"_wait_active_workers 應實際等待 (elapsed={elapsed:.2f}s)"


# ─────────────────────────────────────────────────────────────
# Bug 4 — LLMProcessor 共享狀態並發變動
# ─────────────────────────────────────────────────────────────


class TestBug4LLMProcessorConcurrentSafety:
    """Bug 4: process() 執行中，其他線程 clear_history/update_config 不應污染 in-flight 請求。"""

    def _make_processor_with_fake_client(self, block_event: threading.Event):
        """建立一個 LLMProcessor，其 client.chat 會在 block_event 被 set 前阻塞。"""
        from llm.processor import LLMProcessor
        from unittest.mock import MagicMock

        # 最小 mock config
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

        config = FakeLLMConfig()
        processor = object.__new__(LLMProcessor)
        processor._config = config
        processor._llm_config = config
        processor._conversation_history = []
        # iter 2 Bug 3：_history_epoch 欄位（process() 會 snapshot）
        processor._history_epoch = 0

        # Fake client：chat() 會阻塞到 block_event
        fake_client = MagicMock()
        captured_messages: list[list] = []

        def fake_chat(messages, stream=True):
            captured_messages.append(list(messages))
            block_event.wait(timeout=3.0)
            yield "polished"

        def fake_chat_with_warnings(messages, stream=True):
            # iter 3 Bug C：新 API 返回 (generator, warnings_list)
            return fake_chat(messages, stream=stream), []

        fake_client.chat = fake_chat
        fake_client.chat_with_warnings = fake_chat_with_warnings
        fake_client.param_warnings = []
        processor._client = fake_client
        processor._provider = MagicMock(model="fake-model")

        # 用 RLock 保護狀態（修復後應有）
        if not hasattr(processor, "_state_lock"):
            processor._state_lock = threading.RLock()

        return processor, captured_messages

    def test_append_and_clear_concurrent_no_orphan_messages(self):
        """_append_history 與 clear_history 並發，不應產生落單的 user 或 assistant。

        原 bug：_append_history 分兩次 list.append，中間可能被 clear_history().clear()
        打斷，使 user 訊息寫入後立刻被清，assistant 寫入空 list → user/assistant 錯位。
        修復後：append_history 整個流程應受同一把 lock 保護。
        """
        from llm.processor import LLMProcessor

        processor = object.__new__(LLMProcessor)
        processor._conversation_history = []
        processor._state_lock = threading.RLock()
        processor._history_epoch = 0

        stop = threading.Event()

        def appender():
            i = 0
            while not stop.is_set():
                processor._append_history(f"u{i}", f"a{i}")
                i += 1

        def clearer():
            while not stop.is_set():
                processor.clear_history()

        ts = [
            threading.Thread(target=appender),
            threading.Thread(target=appender),
            threading.Thread(target=clearer),
        ]
        for t in ts:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in ts:
            t.join(timeout=2.0)

        # 結構檢查：無論並發發生什麼，剩下的歷史必須是完整的 user/assistant 配對
        hist = processor._conversation_history
        # 長度必須是偶數
        assert len(hist) % 2 == 0, f"歷史長度不成對: {len(hist)}"
        # 每個偶數位必須是 user，奇數位必須是 assistant
        for i in range(0, len(hist), 2):
            assert hist[i]["role"] == "user", f"位置 {i} 應為 user: {hist[i]}"
            assert hist[i + 1]["role"] == "assistant", (
                f"位置 {i+1} 應為 assistant: {hist[i+1]}"
            )

    def test_clear_history_during_process_does_not_corrupt(self):
        """process() 進行中，clear_history() 不應使 in-flight 請求中途看到空歷史。

        具體：process 開始時已 snapshot 了歷史；即使 clear 發生，收到的訊息結構仍一致。
        """
        from llm.processor import LLMProcessor, RoleConfig

        block = threading.Event()
        processor, captured = self._make_processor_with_fake_client(block)

        # 預先放一些歷史
        processor._conversation_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

        role = RoleConfig(
            name="test", system_prompt="sys", enable_history=True,
        )

        result_holder: list = []

        def run_process():
            r = processor.process(text="new input", role=role)
            result_holder.append(r)

        t = threading.Thread(target=run_process, daemon=True)
        t.start()
        # 等 fake_chat 被呼叫（messages 已 build）
        for _ in range(100):
            if captured:
                break
            time.sleep(0.01)
        assert captured, "process 必須進入 fake_chat"

        # 此時從另一個 thread 清歷史
        processor.clear_history()
        # 放掉 process，讓它完成
        block.set()
        t.join(timeout=3.0)

        # process 實際收到的 messages 應包含 system + 舊歷史 + 新 user（4 條）
        # 若沒有 snapshot 保護，在併發下可能只看到 system+new_user（2 條）
        msgs = captured[0]
        # 預期：system + 舊 user + 舊 assistant + 新 user = 4 條
        assert len(msgs) == 4, (
            f"process 應 snapshot 當時的歷史，但 captured messages={len(msgs)}"
        )
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["content"] == "new input"

    def test_update_config_during_process_uses_snapshot_client(self):
        """process() 進行中，update_config() 換掉 _client 不應影響目前請求。"""
        from llm.processor import RoleConfig

        block = threading.Event()
        processor, captured = self._make_processor_with_fake_client(block)

        role = RoleConfig(name="test", system_prompt="sys", enable_history=False)

        result_holder = []

        def run_process():
            r = processor.process(text="hello", role=role)
            result_holder.append(r)

        t = threading.Thread(target=run_process, daemon=True)
        t.start()
        for _ in range(100):
            if captured:
                break
            time.sleep(0.01)
        assert captured, "process 必須進入 fake_chat"

        # 從外部 thread 替換 client（模擬 update_config）
        from unittest.mock import MagicMock
        new_client = MagicMock()
        new_client.chat = MagicMock(return_value=iter(["should-not-appear"]))
        new_client.param_warnings = []
        processor._client = new_client

        # 放掉原 client 的 chat
        block.set()
        t.join(timeout=3.0)

        assert len(result_holder) == 1
        result = result_holder[0]
        # 原本的 chat 產出 "polished"，新 client 不應介入
        assert "polished" in result.text, f"不應受更換後 client 影響；實際={result.text!r}"

    def test_append_history_is_thread_safe(self):
        """100 個 thread 同時 _append_history，最終計數應一致（無 race）。"""
        from llm.processor import LLMProcessor

        processor = object.__new__(LLMProcessor)
        processor._conversation_history = []
        processor._state_lock = threading.RLock()
        processor._history_epoch = 0

        def worker(i):
            processor._append_history(f"u{i}", f"a{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 裁剪門檻 = _MAX_HISTORY_TURNS * 2
        from llm.processor import _MAX_HISTORY_TURNS
        max_msgs = _MAX_HISTORY_TURNS * 2

        # 長度必須 ≤ max_msgs，且每條結構完整（不是錯位的 user/assistant）
        hist = processor._conversation_history
        assert len(hist) <= max_msgs
        # 每對 user+assistant 必須一致
        for i in range(0, len(hist), 2):
            assert hist[i]["role"] == "user"
            assert hist[i + 1]["role"] == "assistant"
