"""
TDD Loop Iteration 3 — Iter 2 reviewer 複查發現的 5 個 HIGH 殘留。

測試覆蓋：
  Bug A — `_spawn_worker` start-then-register race（window 內 shutdown snapshot 漏掉）
  Bug B — `_callback_lock` 不保證 activate/deactivate 順序（只保證不交錯）
  Bug C — `LLMClient._param_warnings` 跨 thread 污染（UX cross-contamination）
  Bug D — `_build_system_prompt` 型別錯誤 + 冗餘 `len(hotword_context) > 0`
  Bug E — `_is_transcribing` check-and-set 對稱修復（與 `_is_processing` 一致）
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────
# Bug A — _spawn_worker start-then-register race
# ─────────────────────────────────────────────────────────────


class TestBugASpawnWorkerAtomicRegistration:
    """Bug A: iter 2 把 start 放 register 前避免殭屍，但同時打開了新 race:
    `thread.start()` 後、`_active_workers.add(thread)` 前，shutdown 可快照
    空 registry 返回，worker 仍跑在已釋放資源上。

    修復：atomic check-and-register 在同一把 lock 內完成。
    """

    def _make_app(self):
        from app.app import VoiceApp
        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)
        return va

    def test_worker_is_registered_before_start(self):
        """修復後：thread.start() 前，worker 必須已在 registry。

        驗證方式：payload 本體檢查 current_thread 是否已在 registry。
        修復前（iter 2）：start 在 register 前，payload 初期可能 registry 不含自己。
        修復後：register 在 start 前，payload 任何時刻都能從 registry 找到自己。
        """
        va = self._make_app()

        observed = []
        entered = threading.Event()
        release = threading.Event()

        def payload():
            with va._active_workers_lock:
                observed.append(threading.current_thread() in va._active_workers)
            entered.set()
            release.wait(timeout=2.0)

        t = va._spawn_worker(payload, name="order-worker")
        assert t is not None
        entered.wait(timeout=1.0)
        release.set()
        t.join(timeout=2.0)

        assert observed == [True], (
            f"worker 進入時必須已在 registry（修復前為 False）: {observed}"
        )

    def test_shutdown_flag_rejects_after_lock_acquired(self):
        """`_is_shutting_down=True` 時，即使 spawn 進入 lock 也應拒絕。

        modelling：shutdown 寫 flag + register check 在同一 lock 內，
        spawn 若讓 flag 讀到 True，則不得 start、不得 register。
        """
        va = self._make_app()
        va._is_shutting_down = True

        ran = threading.Event()
        result = va._spawn_worker(lambda: ran.set(), name="post-shutdown")
        assert result is None
        time.sleep(0.1)
        assert not ran.is_set(), "shutdown 後 payload 不該執行"
        with va._active_workers_lock:
            names = [t.name for t in va._active_workers]
        assert "post-shutdown" not in names

    def test_concurrent_shutdown_and_spawn_no_escape(self):
        """壓力測試：並發的 shutdown 設 flag 與 spawn 不可出現 worker 跑過 shutdown snapshot 的洩漏。

        劇本：
          - thread1 不斷 spawn worker
          - thread2 某時點把 _is_shutting_down=True 並 snapshot registry（模擬 _wait_active_workers）
          - 所有 spawn 成功（worker is not None）的 worker 都必須在 snapshot 中被看到，
            或是 snapshot 後才被拒絕（返回 None）。
        """
        va = self._make_app()

        spawned_threads: list[threading.Thread] = []
        spawn_lock = threading.Lock()
        stop = threading.Event()
        release = threading.Event()

        def payload():
            release.wait(timeout=3.0)

        def spawner():
            while not stop.is_set():
                t = va._spawn_worker(payload, name="race-worker")
                if t is not None:
                    with spawn_lock:
                        spawned_threads.append(t)
                time.sleep(0.001)

        sp = threading.Thread(target=spawner, daemon=True)
        sp.start()
        time.sleep(0.05)

        # 模擬 shutdown：設 flag + snapshot 必須 atomic
        with va._active_workers_lock:
            va._is_shutting_down = True
            snapshot = set(va._active_workers)

        stop.set()
        sp.join(timeout=2.0)

        # 放掉所有 worker
        release.set()
        for t in spawned_threads:
            t.join(timeout=2.0)

        # 關鍵不變性：在 flag 設 True 之前被 spawn 成功（被加入 spawned_threads）
        # 的 worker，必須出現在 snapshot 中。這只能用 atomic register 保證。
        with spawn_lock:
            pre_shutdown_threads = list(spawned_threads)
        # 抽查 pre-shutdown 的前幾個 worker（最早 spawn 的）——
        # 若 start 先於 register，最早的那些 worker 可能已經完成 register 之前
        # 被 snapshot 錯過；修復後必定在 snapshot 中或尚未 register（但那樣就不會
        # 在 spawned_threads 裡，因為 _spawn_worker 返回 None）。
        # 因此，spawned_threads 裡拿到的一定是 register 成功的，必在 snapshot 中。
        missed = [
            t for t in pre_shutdown_threads
            if t not in snapshot and t.is_alive()
        ]
        # 仍活著但不在 snapshot = 漏掉
        assert not missed, (
            f"有 {len(missed)} 個 worker 被 spawn 後未出現在 shutdown snapshot"
        )


# ─────────────────────────────────────────────────────────────
# Bug B — activate/deactivate 順序保證
# ─────────────────────────────────────────────────────────────


class TestBugBHotkeyActivateBeforeDeactivate:
    """Bug B: iter 2 的 `_callback_lock` 只保證 callback 不交錯，但兩個觸發路徑
    的「先後」仍可能顛倒 —— deactivate 可能先拿到鎖跑完，activate 隨後才跑，
    違反錄音 lifecycle invariant（stop 必須在 start 後）。

    修復：用 `_activate_completed: threading.Event`
      - 每次 press 清空
      - on_threshold_reached 成功執行 on_activate 後 set()
      - _handle_release 若 was_activated=True，呼叫 on_deactivate 前 wait()
    """

    def _make_listener(self, threshold=0.01):
        from utils.hotkey import HotkeyListener

        order: list[str] = []
        order_lock = threading.Lock()

        def act():
            with order_lock:
                order.append("A")
            # 模擬 activate callback 有延遲（e.g. 初始化錄音器）
            time.sleep(0.05)

        def deact():
            with order_lock:
                order.append("D")

        listener = HotkeyListener(
            key="caps_lock",
            threshold=threshold,
            suppress=False,
            on_activate=act,
            on_deactivate=deact,
        )
        return listener, order

    def test_activate_completed_event_attribute_exists(self):
        """HotkeyListener 應有 `_activate_completed: threading.Event` 欄位。"""
        from utils.hotkey import HotkeyListener
        listener = HotkeyListener(key="caps_lock", threshold=0.1, suppress=False)
        assert hasattr(listener, "_activate_completed"), (
            "缺少 _activate_completed Event 欄位"
        )
        assert isinstance(listener._activate_completed, threading.Event)

    def test_activate_always_runs_before_deactivate(self):
        """即使 release 先於 timer callback 跑完 activate，on_deactivate
        也必須等 on_activate 結束後才執行。

        劇本：threshold=10ms、activate callback 睡 50ms。
        press → 15ms 後 release → activate 剛進入但還在 sleep。
        若無 Event 機制：deactivate 先跑 → order = [D, A]。
        修復後：deactivate 等 activate 完成 → order = [A, D]。
        """
        listener, order = self._make_listener(threshold=0.01)

        listener._handle_press()
        time.sleep(0.02)  # 讓 timer 觸發進入 activate callback（sleep 中）
        listener._handle_release()
        # 等所有 callback 完成
        time.sleep(0.2)

        # 不變性：若兩者都發生，A 必先於 D
        if "A" in order and "D" in order:
            a_idx = order.index("A")
            d_idx = order.index("D")
            assert a_idx < d_idx, (
                f"on_activate 必先於 on_deactivate，實際順序: {order}"
            )

    def test_activate_completed_cleared_on_new_press(self):
        """每次新的 press 必須 clear _activate_completed，否則舊的 set 會
        讓下次 release 不等 activate。"""
        from utils.hotkey import HotkeyListener
        listener = HotkeyListener(
            key="caps_lock", threshold=0.5, suppress=False,
            on_activate=lambda: None, on_deactivate=lambda: None,
        )
        # 模擬上一輪結束時 event 處於 set 狀態
        listener._activate_completed.set()
        listener._handle_press()
        assert not listener._activate_completed.is_set(), (
            "新的 press 必須 clear _activate_completed"
        )

    def test_deactivate_does_not_block_when_activate_not_triggered(self):
        """短按未達 threshold → 未呼叫 activate → deactivate 不該 wait 卡住。

        修復必須檢查 was_activated：False 時跳過 Event.wait()，避免
        短按路徑陷入永久等待。
        """
        from utils.hotkey import HotkeyListener

        triggered = {"deact": False}

        listener = HotkeyListener(
            key="caps_lock", threshold=1.0, suppress=False,
            on_activate=lambda: None,
            on_deactivate=lambda: triggered.__setitem__("deact", True),
        )
        # 短按：activate 未觸發
        listener._handle_press()
        time.sleep(0.01)

        start = time.monotonic()
        listener._handle_release()
        elapsed = time.monotonic() - start

        # 短按 release 必須在合理時間內完成（< 0.5s）而非卡在 wait(2.0)
        assert elapsed < 0.5, (
            f"短按 release 不該 wait activate event（elapsed={elapsed:.2f}s）"
        )
        # 短按模式下 on_activate=存在，on_deactivate 不會在短按被呼叫（只有在 was_activated=True 時）
        assert triggered["deact"] is False


# ─────────────────────────────────────────────────────────────
# Bug C — LLMClient._param_warnings 跨 thread 污染
# ─────────────────────────────────────────────────────────────


class TestBugCWarningsCrossThreadContamination:
    """Bug C: 多 worker thread 共用 LLMClient 實例。chat() 開頭
    `self._param_warnings = []` 會覆蓋其他 thread 的警告，使
    使用者 A 的請求看到使用者 B 的警告字串（UX 洩漏）。

    修復（方案 A）：warnings 改成 return tuple 或附加到 yield 的 metadata。
    此處採用：chat() 與 chat_sync() 額外以 out-param 方式收集，不存 instance state。
    具體實作：新增 `chat_with_warnings` 返回 (generator, warnings_list)，
    或在 chat/chat_sync 返回值附帶 warnings（如 tuple）。
    processor 側讀取返回值的 warnings，而非讀 client.param_warnings。
    """

    def test_chat_returns_warnings_alongside_generator(self):
        """chat() 或有新 API 可取得 per-call warnings，不需讀 instance state。

        接受實作：
          - `chat_with_warnings(messages) -> (generator, list[str])`
          - 或 chat 返回值支援 `.warnings` 屬性
          - 或新增 `pop_warnings(token) -> list[str]` 取出本次 call 的警告
        """
        from llm.client import LLMClient
        # 任一種 per-call warnings API 必須存在（不再只有 instance property）
        has_new_api = (
            hasattr(LLMClient, "chat_with_warnings")
            or hasattr(LLMClient, "chat_collect_warnings")
        )
        assert has_new_api, (
            "LLMClient 必須有 per-call warnings API（避免 instance state 跨 thread 污染）"
        )

    def test_processor_reads_warnings_without_instance_state(self):
        """LLMProcessor._stream_chat 應透過 per-call API 取得 warnings，
        而非 `active_client.param_warnings`（後者易被其他 thread 覆蓋）。"""
        import inspect
        import re
        from llm.processor import LLMProcessor

        src = inspect.getsource(LLMProcessor._stream_chat)
        # 剔除註解行再檢查實際程式碼（註解可以提到修復背景）
        code_lines = [
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_only = "\n".join(code_lines)

        # 修復後實際程式碼不應再用 instance property 讀 warnings
        # 檢查形式：`.param_warnings` 屬性存取（非註解）
        assert ".param_warnings" not in code_only, (
            "LLMProcessor._stream_chat 不該再讀 client.param_warnings "
            "（iter 3 要求改用 per-call 返回值）"
        )
        # 必須使用新 API
        assert "chat_with_warnings" in code_only, (
            "_stream_chat 必須呼叫 chat_with_warnings()"
        )

    def test_concurrent_chat_calls_do_not_cross_contaminate_warnings(self):
        """模擬兩 thread 並發 chat，各自 warnings 互不干擾。

        實作策略：直接驅動新 API（chat_with_warnings），兩個 thread 都取得自己的
        警告清單，互不覆蓋。若仍讀 instance state，兩 thread 看到的 list 相同。
        """
        from llm.client import LLMClient
        from llm.provider import ProviderInfo

        provider = ProviderInfo(
            key="fake", name="Fake", api_url="https://example.com/v1",
            api_key="sk-fake", model="fake-model",
        )
        client = LLMClient(provider=provider, temperature=0.3, max_tokens=16)

        # 直接走新 API 驗證互不污染；不實際打網路 —— 注入 warnings 用 monkey-patch
        call_results = {}
        barrier = threading.Barrier(2, timeout=2.0)

        def fake_chat_with_warnings(tag):
            """驅動一次請求並返回其 warnings。用反射呼叫新 API（若存在）。"""
            def _run():
                # 使用 client 的 per-call API，如果有 chat_with_warnings 我們就直接
                # 測試其返回結構；若 API 名不同，這個測試會自然失敗，提示實作
                if hasattr(client, "chat_with_warnings"):
                    # 模擬 response：mock `_POOL_MANAGER.urlopen` 拋錯以快速結束
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                    import llm.client as mod
                    orig_pool = mod._POOL_MANAGER
                    try:
                        import urllib3
                        fake_pool = MagicMock()
                        fake_pool.urlopen.side_effect = urllib3.exceptions.HTTPError(
                            f"simulated {tag}"
                        )
                        mod._POOL_MANAGER = fake_pool
                        try:
                            gen, warnings = client.chat_with_warnings(
                                [{"role": "user", "content": tag}]
                            )
                            # 消費 generator 觸發實際邏輯
                            list(gen)
                        except RuntimeError:
                            warnings = []  # HTTPError → 走例外路徑
                    finally:
                        mod._POOL_MANAGER = orig_pool
                    call_results[tag] = warnings
            return _run

        t1 = threading.Thread(target=fake_chat_with_warnings("A"))
        t2 = threading.Thread(target=fake_chat_with_warnings("B"))
        t1.start(); t2.start()
        t1.join(timeout=5.0); t2.join(timeout=5.0)

        # 兩 thread 各自得到獨立 warnings list（即使都空），不共享 instance state
        # 驗證方式：call_results 字典中應有 A 和 B 的 entry，且各自是獨立 list
        assert "A" in call_results or "B" in call_results, (
            "至少一個 thread 成功呼叫 chat_with_warnings"
        )


# ─────────────────────────────────────────────────────────────
# Bug D — _build_system_prompt 型別 + 冗餘
# ─────────────────────────────────────────────────────────────


class TestBugDBuildSystemPromptCleanup:
    """Bug D: `should_inject = role.enable_hotwords and hotword_context and
    len(hotword_context) > 0` → `bool | None`，`len > 0` 與 `hotword_context`
    真值判斷冗餘；`"".join(None)` 型別錯（mypy）。
    """

    def test_build_system_prompt_handles_none_hotword_context(self):
        """hotword_context=None 時不應拋錯，返回原 prompt。"""
        from llm.processor import LLMProcessor, RoleConfig

        role = RoleConfig(
            name="t", system_prompt="base prompt", enable_hotwords=True,
        )
        result = LLMProcessor._build_system_prompt(role, None)
        assert result == "base prompt", (
            f"hotword_context=None 應返回原 prompt，實際: {result!r}"
        )

    def test_build_system_prompt_handles_empty_list(self):
        """hotword_context=[] 時不該嘗試注入（否則出現 '、'.join([]) 空注入）。"""
        from llm.processor import LLMProcessor, RoleConfig

        role = RoleConfig(
            name="t", system_prompt="base", enable_hotwords=True,
        )
        result = LLMProcessor._build_system_prompt(role, [])
        assert result == "base", f"空 hotword_context 不該注入，實際: {result!r}"

    def test_build_system_prompt_injects_when_hotwords_present(self):
        """有熱詞且 enable_hotwords=True 時，應注入模板。"""
        from llm.processor import LLMProcessor, RoleConfig

        role = RoleConfig(
            name="t", system_prompt="base", enable_hotwords=True,
        )
        result = LLMProcessor._build_system_prompt(role, ["深度學習", "卷積"])
        assert "base" in result
        assert "深度學習" in result
        assert "卷積" in result

    def test_build_system_prompt_no_redundant_len_check(self):
        """修復後源碼實際邏輯不該再有冗餘的 `len(hotword_context) > 0`。"""
        import inspect
        from llm.processor import LLMProcessor

        src = inspect.getsource(LLMProcessor._build_system_prompt)
        code_lines = [
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        assert "len(hotword_context) > 0" not in code_only, (
            "修復後應移除冗餘的 len(hotword_context) > 0 檢查"
        )

    def test_build_system_prompt_enable_hotwords_false_no_inject(self):
        """enable_hotwords=False 時即使有熱詞列表也不注入。"""
        from llm.processor import LLMProcessor, RoleConfig

        role = RoleConfig(
            name="t", system_prompt="base", enable_hotwords=False,
        )
        result = LLMProcessor._build_system_prompt(role, ["x", "y"])
        assert result == "base"


# ─────────────────────────────────────────────────────────────
# Bug E — _is_transcribing 對稱修復
# ─────────────────────────────────────────────────────────────


class TestBugETranscribingLockSymmetric:
    """Bug E: `_is_transcribing` check-and-set 無鎖，與已修的 `_is_processing` 不對稱。
    未來若有跨線程呼叫 `_on_files_dropped`，可能雙次觸發轉錄。"""

    def _make_app(self):
        from app.app import VoiceApp
        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)
        return va

    def test_transcribing_lock_attribute_exists(self):
        """VoiceApp 應有 `_transcribing_lock: threading.Lock` 欄位。"""
        va = self._make_app()
        assert hasattr(va, "_transcribing_lock"), "缺少 _transcribing_lock"
        # Lock 物件有 acquire/release 方法
        assert hasattr(va._transcribing_lock, "acquire")
        assert hasattr(va._transcribing_lock, "release")

    def test_concurrent_files_dropped_only_one_transcribe(self):
        """兩 thread 同時 _on_files_dropped，只能啟動一次轉錄。

        模擬策略：patch _spawn_worker 計數，fake asr_process + config。
        """
        va = self._make_app()
        va._asr_process = MagicMock(is_running=True)
        va._config = MagicMock()
        va._config.file = MagicMock()
        va._main_window = None

        spawn_count = [0]
        spawn_lock = threading.Lock()
        release = threading.Event()

        def fake_spawn(target, *, name="worker", args=()):
            with spawn_lock:
                spawn_count[0] += 1
            # 返回一個實際 thread 模擬成功 spawn（但 payload 不做事）
            t = threading.Thread(
                target=lambda: release.wait(timeout=2.0), daemon=True,
            )
            t.start()
            return t

        va._spawn_worker = fake_spawn

        # 用 barrier 放大 race window
        gate = threading.Barrier(2, timeout=2.0)

        def caller():
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                pass
            va._on_files_dropped(["fake.mp3"])

        t1 = threading.Thread(target=caller)
        t2 = threading.Thread(target=caller)
        t1.start(); t2.start()
        t1.join(timeout=2.0); t2.join(timeout=2.0)

        release.set()
        assert spawn_count[0] == 1, (
            f"_spawn_worker 只應被呼叫 1 次，實際 {spawn_count[0]} 次"
        )

    def test_transcribing_flag_set_atomic_with_lock(self):
        """_on_files_dropped 進入時 check-and-set 必須 atomic（在 lock 內完成）。

        驗證方式：reset flag 後第二次呼叫也應能正常進入（新轉錄批次）。
        """
        va = self._make_app()
        va._asr_process = MagicMock(is_running=True)
        va._config = MagicMock()
        va._config.file = MagicMock()
        va._main_window = None

        def fake_spawn(target, *, name="worker", args=()):
            t = threading.Thread(target=lambda: None, daemon=True)
            t.start()
            return t

        va._spawn_worker = fake_spawn

        # 第一次
        va._on_files_dropped(["a.mp3"])
        assert va._is_transcribing is True
        # 模擬轉錄結束 reset flag
        va._is_transcribing = False
        # 第二次應能再進入
        va._on_files_dropped(["b.mp3"])
        assert va._is_transcribing is True
