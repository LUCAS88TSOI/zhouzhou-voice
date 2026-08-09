"""
州州語音 - 鍵盤模擬

使用 pynput 模擬鍵盤操作：
- 按鍵補發（短按快捷鍵時補回原始按鍵）
- Ctrl+V 粘貼
- 逐字打字（備援方案）
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger("keyboard")


# ─── Win32 SendInput（貼上結果校驗，U9） ─────────────────────
#
# pynput 送出按鍵後不回報結果：非提權程序向提權視窗（工作管理員、admin
# cmd）注入按鍵會被 UIPI **靜默丟棄**，於是「貼上失敗」的提示永遠不觸發，
# 狀態列寫「完成」而目標視窗一個字都沒有。SendInput 會回報實際插入的
# 事件數，是唯一能分辨「送出去了」與「被擋下」的方法。

VK_CONTROL = 0x11
VK_V = 0x56
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_ERROR_ACCESS_DENIED = 5

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# 必須 use_last_error=True：ctypes.windll.user32 建立的函式不會把 LastError
# 存進 ctypes 的 thread-local，get_last_error() 恆回 0，就分辨不出
# 「被 UIPI 擋下（ERROR_ACCESS_DENIED）」與其他失敗。
_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # 必須把三個成員都列出來：INPUT 的大小由最大成員決定（MOUSEINPUT），
    # 只放 KEYBDINPUT 會讓 cbSize 對不上，SendInput 直接回 0。
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_input(events: List[Tuple[int, bool]]) -> int:
    """送出鍵盤事件序列，回傳實際插入的事件數。

    events: [(virtual_key, is_keyup), ...]
    """
    buf = (_INPUT * len(events))()
    for i, (vk, keyup) in enumerate(events):
        buf[i].type = _INPUT_KEYBOARD
        buf[i].ki = _KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=_KEYEVENTF_KEYUP if keyup else 0,
            time=0,
            dwExtraInfo=0,
        )
    return int(
        _user32.SendInput(len(events), ctypes.byref(buf), ctypes.sizeof(_INPUT))
    )


def _last_error() -> int:
    """取得最後一次 Win32 錯誤碼。"""
    return int(ctypes.get_last_error())


# ─── 按鍵名稱轉換 ─────────────────────────────────────────

def _name_to_pynput_key(name: str):
    """將按鍵名稱轉換為 pynput Key 或 KeyCode。"""
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
        "backspace": Key.backspace,
        "delete": Key.delete,
    }

    if name in special:
        return special[name]

    # 功能鍵 f1-f24
    if name.startswith("f") and name[1:].isdigit():
        return getattr(Key, name, None)

    # 單字元
    if len(name) == 1:
        return KeyCode.from_char(name)

    return None


# ─── 鍵盤模擬器 ──────────────────────────────────────────

class KeyboardSimulator:
    """
    鍵盤模擬器。

    所有方法都是類方法（classmethod），無需實例化。
    內部使用 pynput.keyboard.Controller，延遲初始化。
    """

    _controller = None

    @classmethod
    def _get_controller(cls):
        """取得 pynput 鍵盤控制器（延遲初始化）。"""
        if cls._controller is None:
            from pynput.keyboard import Controller
            cls._controller = Controller()
        return cls._controller

    @classmethod
    def tap_key(cls, key_name: str) -> None:
        """
        模擬單次按鍵（按下 + 鬆開）。

        用於短按快捷鍵時補發原始按鍵。

        Args:
            key_name: 按鍵名稱（如 "caps_lock"、"a"、"f1"）
        """
        pynput_key = _name_to_pynput_key(key_name)
        if pynput_key is None:
            logger.warning("無法補發未知按鍵: %s", key_name)
            return

        ctrl = cls._get_controller()
        ctrl.press(pynput_key)
        time.sleep(0.01)
        ctrl.release(pynput_key)
        logger.debug("已補發按鍵: %s", key_name)

    @classmethod
    def press_key(cls, key_name: str) -> None:
        """模擬按住按鍵（不鬆開）。"""
        pynput_key = _name_to_pynput_key(key_name)
        if pynput_key is not None:
            cls._get_controller().press(pynput_key)

    @classmethod
    def release_key(cls, key_name: str) -> None:
        """模擬鬆開按鍵。"""
        pynput_key = _name_to_pynput_key(key_name)
        if pynput_key is not None:
            cls._get_controller().release(pynput_key)

    @classmethod
    def press_ctrl_v(cls) -> bool:
        """
        模擬 Ctrl+V（粘貼），並校驗注入結果。

        用 SendInput 而非 pynput：只有它會回報實際插入的事件數，讓被 UIPI
        擋下（提權視窗）的情況回報成失敗，而不是假裝貼上成功（U9）。

        Returns:
            四個事件全部插入才算成功；被阻擋或部分插入都回 False
        """
        events = [
            (VK_CONTROL, False),
            (VK_V, False),
            (VK_V, True),
            (VK_CONTROL, True),
        ]
        try:
            ctypes.set_last_error(0)
            sent = _send_input(events)
        except Exception as err:  # noqa: BLE001 — 任何失敗都回報，避免靜默冒泡
            logger.error("模擬 Ctrl+V 失敗: %s", err, exc_info=True)
            return False

        if sent == len(events):
            time.sleep(0.05)  # 等待粘貼完成
            logger.debug("已送出 Ctrl+V（%d 個事件）", sent)
            return True

        err_code = _last_error()
        if err_code == _ERROR_ACCESS_DENIED:
            logger.error(
                "Ctrl+V 被系統阻擋：目標視窗權限較高（UIPI），"
                "請以相同權限執行本程式或手動貼上",
            )
        else:
            logger.error(
                "Ctrl+V 注入不完整：僅送出 %d/%d 個事件（GetLastError=%d）",
                sent, len(events), err_code,
            )
        return False

    @classmethod
    def press_ctrl_c(cls) -> bool:
        """
        模擬 Ctrl+C（複製）。

        用於讀取當前選取文字，配合 ClipboardManager.capture_selection() 使用。

        Returns:
            是否成功（pynput 初始化或按鍵模擬失敗時回 False，不冒泡）
        """
        from pynput.keyboard import Key

        try:
            ctrl = cls._get_controller()
            ctrl.press(Key.ctrl_l)
            time.sleep(0.01)
            ctrl.press("c")
            time.sleep(0.01)
            ctrl.release("c")
            ctrl.release(Key.ctrl_l)
            time.sleep(0.05)  # 等待複製完成
            logger.debug("已模擬 Ctrl+C")
            return True
        except Exception as err:  # noqa: BLE001 — 任何失敗都回報，避免靜默冒泡
            logger.error("模擬 Ctrl+C 失敗: %s", err, exc_info=True)
            return False

    @classmethod
    def type_text(cls, text: str, interval: float = 0.01) -> None:
        """
        逐字打字（備援方案）。

        不經過剪貼板，直接模擬鍵盤逐字輸入。
        速度較慢，適用於不支持 Ctrl+V 的場景。

        Args:
            text: 要打字的文字
            interval: 每個字元間的延遲（秒）
        """
        ctrl = cls._get_controller()
        for char in text:
            ctrl.type(char)
            if interval > 0:
                time.sleep(interval)
        logger.debug("已打字 %d 個字元", len(text))
