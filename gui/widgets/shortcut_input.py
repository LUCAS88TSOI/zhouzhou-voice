"""
州州語音 - 快捷鍵選擇器

提供一個 QComboBox 封裝，列出常用的快捷鍵並映射內部名稱與顯示名稱。
用法：
    widget = ShortcutInput()
    widget.set_key("caps_lock")
    key = widget.get_key()  # "caps_lock"
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtWidgets import QComboBox

from utils.logger import get_logger

logger = get_logger("shortcut_input")


# ─── 按鍵映射表 ──────────────────────────────────────────

# (內部名稱, 顯示名稱) — 順序即下拉選單的排列順序
_KEY_MAP: List[Tuple[str, str]] = [
    ("", "（停用）"),
    ("caps_lock", "Caps Lock"),
    ("esc", "Esc"),
    ("f1", "F1"),
    ("f2", "F2"),
    ("f3", "F3"),
    ("f4", "F4"),
    ("f5", "F5"),
    ("f6", "F6"),
    ("f7", "F7"),
    ("f8", "F8"),
    ("f9", "F9"),
    ("f10", "F10"),
    ("f11", "F11"),
    ("f12", "F12"),
    ("shift_l", "Left Shift"),
    ("shift_r", "Right Shift"),
    ("ctrl_l", "Left Ctrl"),
    ("ctrl_r", "Right Ctrl"),
    ("space", "Space"),
    ("insert", "Insert"),
    ("x1", "Mouse X1"),
    ("x2", "Mouse X2"),
]

# 反向查找：內部名稱 → 在列表中的索引
_KEY_TO_INDEX: Dict[str, int] = {key: idx for idx, (key, _) in enumerate(_KEY_MAP)}


class ShortcutInput(QComboBox):
    """
    快捷鍵選擇器 — QComboBox 封裝。

    將內部按鍵名稱（如 ``caps_lock``）映射為人類可讀的
    顯示名稱（如 ``Caps Lock``），供設定介面使用。
    """

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)

        for _internal, display in _KEY_MAP:
            self.addItem(display)

        logger.debug("ShortcutInput 初始化完成，共 %d 個選項", len(_KEY_MAP))

    # ─── 公開 API ──────────────────────────────────────

    def get_key(self) -> str:
        """
        取得目前選中的按鍵內部名稱。

        Returns:
            內部名稱字串，例如 ``"caps_lock"``
        """
        idx = self.currentIndex()
        if 0 <= idx < len(_KEY_MAP):
            return _KEY_MAP[idx][0]
        return ""

    def set_key(self, key: str) -> None:
        """
        依據內部名稱設定選中項。

        若傳入的 key 不在映射表中，預設選取第一項並記錄警告。

        Args:
            key: 內部名稱，例如 ``"f1"``、``"caps_lock"``
        """
        idx = _KEY_TO_INDEX.get(key)
        if idx is not None:
            self.setCurrentIndex(idx)
        else:
            logger.warning("未知的快捷鍵名稱 '%s'，回退到預設值", key)
            self.setCurrentIndex(0)
