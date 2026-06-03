"""
州州語音 - 鍵盤模擬

使用 pynput 模擬鍵盤操作：
- 按鍵補發（短按快捷鍵時補回原始按鍵）
- Ctrl+V 粘貼
- 逐字打字（備援方案）
"""

from __future__ import annotations

import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("keyboard")


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
        模擬 Ctrl+V（粘貼）。

        用於將剪貼板內容粘貼到當前焦點應用。

        Returns:
            是否成功（pynput 初始化或按鍵模擬失敗時回 False，不冒泡）
        """
        from pynput.keyboard import Key

        try:
            ctrl = cls._get_controller()
            ctrl.press(Key.ctrl_l)
            time.sleep(0.01)
            ctrl.press("v")
            time.sleep(0.01)
            ctrl.release("v")
            ctrl.release(Key.ctrl_l)
            time.sleep(0.05)  # 等待粘貼完成
            logger.debug("已模擬 Ctrl+V")
            return True
        except Exception as err:  # noqa: BLE001 — 任何失敗都回報，避免靜默冒泡
            logger.error("模擬 Ctrl+V 失敗: %s", err, exc_info=True)
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
