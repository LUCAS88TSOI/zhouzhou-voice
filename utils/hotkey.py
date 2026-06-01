"""
州州語音 - 快捷鍵監聽

使用 pynput 監聽鍵盤和滑鼠按鍵。
支援長按偵測、短按穿透（按鍵補發）和選擇性按鍵阻塞。

用法：
    def on_start():
        print("開始錄音")
    def on_stop():
        print("停止錄音")

    listener = HotkeyListener(
        key="caps_lock", threshold=0.3, suppress=True,
        on_activate=on_start, on_deactivate=on_stop,
    )
    listener.start()
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from utils.logger import get_logger

logger = get_logger("hotkey")

# 滑鼠側鍵名稱
_MOUSE_KEYS = frozenset({"x1", "x2"})

# Windows 低階鍵盤鉤子：注入事件標記
_LLKHF_INJECTED = 0x10


# ─── 按鍵名稱 → pynput 物件 ──────────────────────────────

def _resolve_keyboard_key(name):
    """將按鍵名稱轉換為 pynput Key 或 KeyCode。非字串輸入返回 None。"""
    if not isinstance(name, str):
        return None
    from pynput.keyboard import Key, KeyCode

    special = {
        "caps_lock": Key.caps_lock,
        "space": Key.space,
        "insert": Key.insert,
        "shift": Key.shift, "shift_l": Key.shift_l, "shift_r": Key.shift_r,
        "ctrl": Key.ctrl_l, "ctrl_l": Key.ctrl_l, "ctrl_r": Key.ctrl_r,
        "alt": Key.alt_l, "alt_l": Key.alt_l, "alt_r": Key.alt_r,
        "esc": Key.esc,
        "tab": Key.tab,
        "enter": Key.enter,
    }

    if name in special:
        return special[name]

    # 功能鍵 f1-f24
    if name.startswith("f") and name[1:].isdigit():
        return getattr(Key, name, None)

    # 單字母
    if len(name) == 1 and name.isalpha():
        return KeyCode.from_char(name.lower())

    return None


def _resolve_mouse_button(name: str):
    """將滑鼠按鍵名稱轉換為 pynput Button。"""
    from pynput.mouse import Button
    return {"x1": Button.x1, "x2": Button.x2}.get(name)


def _get_vk_code(name: str) -> Optional[int]:
    """取得 Windows 虛擬鍵碼（用於 win32_event_filter）。"""
    vk_map = {
        "caps_lock": 0x14, "space": 0x20, "insert": 0x2D,
        "shift": 0xA0, "shift_l": 0xA0, "shift_r": 0xA1,
        "ctrl": 0xA2, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
        "alt": 0xA4, "alt_l": 0xA4, "alt_r": 0xA5,
        "esc": 0x1B, "tab": 0x09, "enter": 0x0D,
    }
    if name in vk_map:
        return vk_map[name]
    # 功能鍵 F1=0x70 ...
    if name.startswith("f") and name[1:].isdigit():
        return 0x6F + int(name[1:])
    # 字母
    if len(name) == 1 and name.isalpha():
        return ord(name.upper())
    return None


def _key_matches(pressed, target, key_name: str) -> bool:
    """判斷按下的鍵是否匹配目標鍵。"""
    if pressed == target:
        return True
    # KeyCode 比較 vk
    if hasattr(pressed, "vk") and hasattr(target, "vk"):
        if pressed.vk is not None and pressed.vk == target.vk:
            return True
    return False


# ─── 快捷鍵監聽器 ─────────────────────────────────────────

class HotkeyListener:
    """
    快捷鍵監聽器。

    支援鍵盤按鍵和滑鼠側鍵。
    長按超過 threshold 觸發 on_activate，鬆開觸發 on_deactivate。
    短按（低於 threshold）時若 suppress=True，會自動補發按鍵。
    """

    def __init__(
        self,
        key: str = "caps_lock",
        threshold: float = 0.3,
        suppress: bool = True,
        on_activate: Optional[Callable] = None,
        on_deactivate: Optional[Callable] = None,
    ) -> None:
        self._key_name = key
        self._threshold = threshold
        self._suppress = suppress
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate

        self._press_time: float = 0.0
        self._is_pressed: bool = False
        self._is_activated: bool = False
        self._threshold_timer: Optional[threading.Timer] = None
        # 按鍵世代計數：每次 press 遞增，timer callback 需驗證世代一致才觸發，
        # 防止「release → 新 press 前的舊 timer 觸發」的競態。
        self._press_generation: int = 0
        # 保護 _is_pressed / _is_activated / _threshold_timer / _press_generation
        # 的所有狀態轉換。pynput listener thread 與 threading.Timer callback thread
        # 可能同時進入此類方法。
        self._state_lock = threading.RLock()
        # 專屬 lock 序列化 on_activate / on_deactivate 呼叫。兩個 callback
        # 可能被不同 thread（timer 觸發 vs release 觸發）在重疊時間點呼叫，
        # 造成 activate/deactivate 交錯（例如 deactivate 進入後 activate 才執行）。
        # 使用獨立 lock（非 _state_lock）：避免持 state lock 期間執行慢 callback
        # 阻塞其他 press/release 的狀態轉換。
        self._callback_lock = threading.Lock()
        # iter 3 Bug B：callback_lock 僅保證「不交錯」，但不保證「順序」——
        # deactivate 可能先拿到鎖跑完、activate 隨後才跑，違反錄音 lifecycle
        # invariant（stop 必須在 start 後）。此 Event 讓 release 路徑能等待
        # threshold-callback 中的 on_activate 完成後才呼叫 on_deactivate。
        # - 每次 press 清空
        # - on_threshold_reached 執行 on_activate 後 set()
        # - _handle_release 若 was_activated=True 則 wait()（超時 2 秒）
        self._activate_completed = threading.Event()

        self._kb_listener = None
        self._mouse_listener = None
        self._running = False

    # ─── 公開 API ──────────────────────────────────────

    @property
    def is_activated(self) -> bool:
        """是否處於啟動狀態（正在錄音）。"""
        return self._is_activated

    def start(self) -> None:
        """開始監聽快捷鍵。"""
        if self._running:
            return
        self._running = True

        if self._key_name in _MOUSE_KEYS:
            self._start_mouse_listener()
        else:
            self._start_keyboard_listener()

        logger.info(
            "快捷鍵監聽已啟動: %s (閾值=%.1fs, 阻塞=%s)",
            self._key_name, self._threshold, self._suppress,
        )

    def stop(self) -> None:
        """停止監聽。"""
        self._running = False
        with self._state_lock:
            self._cancel_timer()
            # 取消任何在途的 threshold callback（提高世代使其 no-op）
            self._press_generation += 1
            self._is_pressed = False
            self._is_activated = False
        # iter 3 Bug B：set 避免任何正在 wait 的 release 路徑永久阻塞。
        self._activate_completed.set()

        if self._kb_listener is not None:
            self._kb_listener.stop()
            self._kb_listener = None
        if self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None

        logger.info("快捷鍵監聽已停止")

    def update_key(self, key: str) -> None:
        """更換監聽的快捷鍵（重啟監聽器）。"""
        self.update_config(key=key)

    def update_config(
        self,
        key: str | None = None,
        threshold: float | None = None,
        suppress: bool | None = None,
    ) -> None:
        """更新監聽配置（重啟監聽器）。"""
        was_running = self._running
        self.stop()
        if key is not None:
            self._key_name = key
        if threshold is not None:
            self._threshold = threshold
        if suppress is not None:
            self._suppress = suppress
        if was_running:
            self.start()

    # ─── 鍵盤監聽 ──────────────────────────────────────

    def _start_keyboard_listener(self) -> None:
        from pynput import keyboard

        target_key = _resolve_keyboard_key(self._key_name)

        def on_press(key):
            if _key_matches(key, target_key, self._key_name):
                self._handle_press()

        def on_release(key):
            if _key_matches(key, target_key, self._key_name):
                self._handle_release()

        kwargs = {"on_press": on_press, "on_release": on_release}

        # 選擇性阻塞：只阻塞我們的快捷鍵，放行其他按鍵
        # 注意：pynput 1.8+ 中 event_filter 返回 False 會同時跳過
        # on_press/on_release 回調，所以必須在 filter 內手動觸發
        if self._suppress:
            vk = _get_vk_code(self._key_name)
            if vk is not None:
                def event_filter(msg, data):
                    # 放行注入的事件（我們補發的按鍵）
                    if hasattr(data, "flags") and data.flags & _LLKHF_INJECTED:
                        return True
                    # 攔截我們的快捷鍵：手動觸發回調再阻塞
                    if hasattr(data, "vkCode") and data.vkCode == vk:
                        # WM_KEYDOWN=0x100, WM_SYSKEYDOWN=0x104
                        if msg in (0x100, 0x104):
                            self._handle_press()
                        # WM_KEYUP=0x101, WM_SYSKEYUP=0x105
                        elif msg in (0x101, 0x105):
                            self._handle_release()
                        return False
                    return True
                kwargs["win32_event_filter"] = event_filter
                # event_filter 已完整處理所有非注入事件。
                # 注入事件（_schedule_reemit 補發的按鍵）通過 event_filter 讓 OS 切換 LED，
                # 但不應再被 on_press/on_release 重複處理（否則形成無限循環）。
                kwargs["on_press"] = lambda key: None
                kwargs["on_release"] = lambda key: None

        self._kb_listener = keyboard.Listener(**kwargs)
        self._kb_listener.start()

    # ─── 滑鼠監聽 ──────────────────────────────────────

    def _start_mouse_listener(self) -> None:
        from pynput import mouse

        target = _resolve_mouse_button(self._key_name)

        def on_click(x, y, button, pressed):
            if button == target:
                if pressed:
                    self._handle_press()
                else:
                    self._handle_release()

        self._mouse_listener = mouse.Listener(on_click=on_click)
        self._mouse_listener.start()

    # ─── 按鍵邏輯 ──────────────────────────────────────

    def _handle_press(self) -> None:
        """處理按鍵按下。忽略重複按下事件。

        整段受 _state_lock 保護：避免 timer callback 或 release 在狀態轉換中途
        插入。遞增 _press_generation 使尚未執行的舊 timer callback 失效。
        """
        with self._state_lock:
            if self._is_pressed:
                return
            self._is_pressed = True
            self._press_time = time.monotonic()
            self._press_generation += 1
            my_gen = self._press_generation
            # iter 3 Bug B：每次新 press 清空 activate_completed，確保下次
            # release 路徑不會誤認為已完成（讀到上一輪的 set 狀態）。
            self._activate_completed.clear()

            # 啟動閾值計時器（傳入當前世代，callback 會比對）
            self._threshold_timer = threading.Timer(
                self._threshold,
                self._on_threshold_reached,
                args=(my_gen,),
            )
            self._threshold_timer.daemon = True
            self._threshold_timer.start()

        # 補發在鎖外執行，避免長時間持鎖（_schedule_reemit 會 spawn thread）
        if self._suppress and self._key_name == "caps_lock":
            self._schedule_reemit()

    def _handle_release(self) -> None:
        """處理按鍵鬆開。

        使用 lock snapshot 狀態後再執行回調，避免回調與下一次 press 產生
        狀態競爭。遞增 generation 使尚未觸發的舊 timer callback 變成 no-op。
        """
        with self._state_lock:
            if not self._is_pressed:
                return
            self._is_pressed = False
            self._cancel_timer()
            # 世代遞增 → 任何還沒執行到 callback 中間「已通過 gen 檢查」之前的
            # timer callback 都會被 invalidated（與 _handle_press 同機制）
            self._press_generation += 1
            was_activated = self._is_activated
            self._is_activated = False
            hold_time = time.monotonic() - self._press_time

        # 回調在 state lock 外觸發，但用專屬 _callback_lock 序列化，
        # 避免 on_activate / on_deactivate 被交錯執行。
        if was_activated:
            logger.debug("快捷鍵鬆開 (長按 %.2fs)", hold_time)
            # iter 3 Bug B：等待 threshold callback 中的 on_activate 完成，
            # 確保 on_deactivate 不會先於 on_activate 執行（lifecycle invariant）。
            # 2 秒超時避免 activate callback 卡死時整個 listener 永久阻塞。
            if not self._activate_completed.wait(timeout=2.0):
                logger.warning("等待 on_activate 完成超時 (2.0s)，強制繼續 deactivate")
            if self._on_deactivate:
                with self._callback_lock:
                    self._on_deactivate()
        else:
            logger.debug("短按 (%.2fs)，補發按鍵", hold_time)
            if self._suppress and self._key_name != "caps_lock":
                self._schedule_reemit()
            # 速發模式：短按鬆開時也觸發 on_deactivate
            # 短按路徑未觸發 on_activate，不需 wait activate_completed
            if self._on_deactivate and self._on_activate is None:
                with self._callback_lock:
                    self._on_deactivate()

    def _on_threshold_reached(self, generation: int | None = None) -> None:
        """閾值計時器觸發：開始錄音。

        Args:
            generation: 排程 timer 時的 press 世代；若 None（手動呼叫）則跳過世代檢查

        用 lock + generation 檢查防止競態：
        - generation 不一致代表 release 或新 press 已發生，此 callback 作廢
        - lock 保證「通過檢查 → 設 _is_activated = True」之間不可被 release 插隊
        """
        with self._state_lock:
            # 世代不一致（或已 release）→ 此 timer 作廢
            if generation is not None and generation != self._press_generation:
                return
            if not self._is_pressed:
                return
            self._is_activated = True

        logger.debug("閾值 %.1fs 達到，啟動錄音", self._threshold)

        # CapsLock suppress=True：補發一次取消 _handle_press 的切換，保持大小寫不變
        if self._suppress and self._key_name == "caps_lock":
            self._schedule_reemit()

        # 用 _callback_lock 序列化，避免與 _handle_release 觸發的 on_deactivate 交錯
        if self._on_activate:
            try:
                with self._callback_lock:
                    self._on_activate()
            finally:
                # iter 3 Bug B：即使 on_activate 拋例外也要 set，否則 release
                # 路徑會 wait 到超時才繼續。callback 只執行一次，set 不會衝突。
                self._activate_completed.set()
        else:
            # 無 on_activate callback 也要 set，避免 release 路徑乾等。
            self._activate_completed.set()

    def _cancel_timer(self) -> None:
        """取消待觸發的 threshold timer。呼叫者應持有 _state_lock。"""
        if self._threshold_timer is not None:
            self._threshold_timer.cancel()
            self._threshold_timer = None

    def _schedule_reemit(self) -> None:
        """延遲補發按鍵（避免在鉤子回調中直接發送）。"""
        def _do_reemit():
            time.sleep(0.01)
            from utils.keyboard import KeyboardSimulator
            KeyboardSimulator.tap_key(self._key_name)

        threading.Thread(target=_do_reemit, daemon=True).start()
