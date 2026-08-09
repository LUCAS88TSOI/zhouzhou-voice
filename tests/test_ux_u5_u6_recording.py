"""
U5 + U6：錄音狀態機的兩個「白講一場」

U5 上一段還在處理時，第二段錄音照常開始，狀態列還變「錄音中...」——
   用戶完全相信系統在收音。鬆手才被靜默丟棄，音頻蒸發。期間浮窗還會顯示
   「潤色中...」→「完成」並貼上第一句，用戶會以為第二句識別錯了。
U6 key-up 遺失（UAC 提權彈窗切安全桌面、Win+L、全螢幕獨佔遊戲）後，
   _is_pressed 永遠是 True，之後每次按快捷鍵都無反應，只能重啟程式。
   錄音會一路跑到 1800 秒上限，而那段音頻永不識別、永不入歷史。

鎖住的行為：
- 忙碌時不開錄音器，並且第一時間告訴用戶
- 真的走到丟棄分支時也要有 UI 回饋，不可只寫 log
- watchdog 用實體按鍵狀態驗證，偵測到 key-up 遺失就強制復位
- 查不到實體狀態時保守處理（不誤砍正在進行的錄音）
- 錄音達上限時強制釋放，讓已錄到的音頻走完識別流程
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.hotkey import HotkeyListener


# ─────────────────────── U5：忙碌時別讓用戶白講 ───────────────────────

@pytest.fixture
def app(monkeypatch):
    from app.app import VoiceApp
    from utils.config import AppConfig

    a = VoiceApp.__new__(VoiceApp)
    a._config = AppConfig()
    a._is_processing = False
    a._is_repolishing = False
    a._processing_lock = threading.Lock()
    a._recorder = None

    a.gui_calls: list[tuple] = []
    monkeypatch.setattr(
        a, "_invoke_gui",
        lambda method, *args: a.gui_calls.append(
            (method, args[0][1] if args else "")
        ),
    )

    class _Recorder:
        def __init__(self) -> None:
            self.started = 0

        def start_recording(self) -> None:
            self.started += 1

        def stop_recording(self) -> bytes:
            return b"\x00" * (4 * 16000)      # 1 秒

    a._recorder = _Recorder()
    monkeypatch.setattr(a, "_ensure_recorder", lambda: True)
    return a


def _messages(app) -> str:
    return " | ".join(str(m) for _, m in app.gui_calls)


class TestU5BusyRefusesToRecord:
    def test_busy_does_not_start_the_recorder(self, app) -> None:
        app._is_processing = True
        app._on_recording_start()
        assert app._recorder.started == 0, "忙碌時不該開始收音"

    def test_busy_tells_the_user_immediately(self, app) -> None:
        app._is_processing = True
        app._on_recording_start()
        assert any(m == "notify_warning" for m, _ in app.gui_calls)
        assert "處理" in _messages(app)

    def test_busy_never_shows_recording_status(self, app) -> None:
        """「錄音中...」是最誤導的一句 —— 用戶就是看著它開口的。"""
        app._is_processing = True
        app._on_recording_start()
        assert "錄音中" not in _messages(app)

    def test_repolish_in_flight_also_blocks(self, app) -> None:
        app._is_repolishing = True
        app._on_recording_start()
        assert app._recorder.started == 0

    def test_idle_records_normally(self, app) -> None:
        app._on_recording_start()
        assert app._recorder.started == 1
        assert "錄音中" in _messages(app)


class TestU5DiscardBranchIsNotSilent:
    def test_discarded_audio_produces_feedback(self, app, monkeypatch) -> None:
        """就算走到這裡（競態），也不可以只寫一行 log 就吃掉整段錄音。"""
        app._is_processing = True
        app._on_recording_stop()

        assert any(m == "notify_warning" for m, _ in app.gui_calls), (
            "音頻被丟棄必須有 UI 回饋"
        )

    def test_normal_stop_spawns_worker(self, app, monkeypatch) -> None:
        spawned: list[str] = []
        monkeypatch.setattr(
            app, "_spawn_worker",
            lambda fn, name="", args=(): spawned.append(name) or object(),
        )
        app._on_recording_stop()
        assert spawned == ["voice-worker"]


# ─────────────────────── U6：卡住要能自救 ───────────────────────

class TestU6Watchdog:
    def test_force_release_resets_a_stuck_state(self) -> None:
        released: list[int] = []
        hk = HotkeyListener(
            key="f2", threshold=0.1,
            on_activate=lambda: None,
            on_deactivate=lambda: released.append(1),
        )
        hk._is_pressed = True
        hk._is_activated = True
        hk._activate_completed.set()

        hk.force_release()

        assert hk._is_pressed is False
        assert hk._is_activated is False
        assert released == [1], "強制復位要走完 on_deactivate，音頻才不會白錄"

    def test_force_release_is_a_noop_when_not_pressed(self) -> None:
        released: list[int] = []
        hk = HotkeyListener(key="f2", on_deactivate=lambda: released.append(1))
        hk.force_release()
        assert released == []

    def test_watchdog_releases_when_physical_key_is_up(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """key-up 遺失的核心情境：我們以為按住，實體鍵早就鬆了。"""
        import utils.hotkey as hk_mod

        monkeypatch.setattr(hk_mod, "_physical_key_down", lambda vk: False)

        released: list[int] = []
        hk = HotkeyListener(key="f2", on_deactivate=lambda: released.append(1))
        hk._is_pressed = True
        hk._is_activated = True
        hk._activate_completed.set()

        hk._watchdog_tick(hk._press_generation)

        assert hk._is_pressed is False
        assert released == [1]

    def test_watchdog_keeps_waiting_while_key_is_held(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """真的按住 30 分鐘也不能被 watchdog 砍掉。"""
        import utils.hotkey as hk_mod

        monkeypatch.setattr(hk_mod, "_physical_key_down", lambda vk: True)
        rearmed: list[int] = []

        hk = HotkeyListener(key="f2")
        monkeypatch.setattr(hk, "_start_watchdog", lambda gen: rearmed.append(gen))
        hk._is_pressed = True

        hk._watchdog_tick(hk._press_generation)

        assert hk._is_pressed is True
        assert rearmed, "仍按住時要繼續守望，不是就此收工"

    def test_unknown_physical_state_does_not_cut_the_recording(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查不到就當還按著 —— 誤砍正在進行的錄音比卡住更糟。"""
        import ctypes

        import utils.hotkey as hk_mod

        monkeypatch.setattr(
            ctypes, "windll",
            property(lambda self: (_ for _ in ()).throw(AttributeError())),
            raising=False,
        )
        assert hk_mod._physical_key_down(0x71) is True

    def test_stale_generation_tick_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已經放開又重新按下時，舊的 watchdog 不可以砍掉新的錄音。"""
        import utils.hotkey as hk_mod

        monkeypatch.setattr(hk_mod, "_physical_key_down", lambda vk: False)
        released: list[int] = []

        hk = HotkeyListener(key="f2", on_deactivate=lambda: released.append(1))
        hk._is_pressed = True
        hk._press_generation = 7

        hk._watchdog_tick(3)          # 上一輪的 tick

        assert hk._is_pressed is True
        assert released == []

    def test_press_arms_the_watchdog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        armed: list[int] = []
        hk = HotkeyListener(key="f2", suppress=False)
        monkeypatch.setattr(hk, "_start_watchdog", lambda gen: armed.append(gen))

        hk._handle_press()
        try:
            assert armed, "按下時就要開始守望"
        finally:
            hk._handle_release()

    def test_watchdog_does_not_arm_before_start(self) -> None:
        """未 start() 就沒有 pynput 回調，排 timer 只會留下背景執行緒。"""
        hk = HotkeyListener(key="f2", suppress=False)
        hk._is_pressed = True
        hk._start_watchdog(hk._press_generation)
        assert hk._watchdog_timer is None

    def test_watchdog_arms_once_running(self) -> None:
        hk = HotkeyListener(key="f2", suppress=False)
        hk._running = True
        hk._is_pressed = True
        try:
            hk._start_watchdog(hk._press_generation)
            assert hk._watchdog_timer is not None
        finally:
            if hk._watchdog_timer is not None:
                hk._watchdog_timer.cancel()

    def test_stop_cancels_the_watchdog(self) -> None:
        hk = HotkeyListener(key="f2", suppress=False)
        hk._handle_press()
        hk.stop()
        assert hk._watchdog_timer is None or not hk._watchdog_timer.is_alive()

    def test_mouse_side_buttons_have_a_vk_code(self) -> None:
        """滑鼠側鍵一樣會遺失 key-up，不能沒有 watchdog。"""
        from utils.hotkey import _get_vk_code

        assert _get_vk_code("x1") is not None
        assert _get_vk_code("x2") is not None


class TestU6RecordingLimitDoesNotStrandAudio:
    @pytest.fixture
    def limit_app(self, app, monkeypatch):
        """本回調跑在音頻線程上，release 會被丟去背景；測試同步執行它。"""
        app.spawned: list[str] = []

        def _inline(fn, name="", args=()):
            app.spawned.append(name)
            fn(*args)
            return object()

        monkeypatch.setattr(app, "_spawn_worker", _inline)
        return app

    def test_limit_forces_hotkey_release(self, limit_app) -> None:
        """錄到 30 分鐘上限卻永遠等不到 key-up，那段音頻就白錄了。"""
        forced: list[int] = []

        class _HK:
            def force_release(self) -> None:
                forced.append(1)

        limit_app._hotkey = _HK()
        limit_app._on_recording_limit_reached()

        assert forced == [1]

    def test_release_runs_off_the_audio_thread(self, limit_app) -> None:
        """force_release 會做 30 分鐘音頻的 concatenate，不能卡住音頻回調。"""
        class _HK:
            def force_release(self) -> None: ...

        limit_app._hotkey = _HK()
        limit_app._on_recording_limit_reached()

        assert limit_app.spawned == ["hotkey-force-release"]

    def test_release_falls_back_to_inline_when_spawn_refused(
        self, app, monkeypatch,
    ) -> None:
        """關機中 spawn 會被拒 —— 寧可同步做，也不要丟掉已錄到的音頻。"""
        forced: list[int] = []

        class _HK:
            def force_release(self) -> None:
                forced.append(1)

        app._hotkey = _HK()
        monkeypatch.setattr(app, "_spawn_worker", lambda *a, **k: None)
        app._on_recording_limit_reached()

        assert forced == [1]

    def test_release_failure_is_contained(self, limit_app) -> None:
        class _HK:
            def force_release(self) -> None:
                raise RuntimeError("快捷鍵已停止")

        limit_app._hotkey = _HK()
        limit_app._on_recording_limit_reached()      # 不拋例外即通過

    def test_limit_survives_missing_hotkey(self, app) -> None:
        app._hotkey = None
        app._on_recording_limit_reached()      # 不拋例外即通過
