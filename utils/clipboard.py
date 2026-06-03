"""
州州語音 - 剪貼板操作

使用 Win32 API 直接操作 Windows 剪貼板。
無需額外依賴（只用 ctypes）。

功能：
- 讀取/寫入 Unicode 文字
- 粘貼文字（寫入剪貼板 + Ctrl+V）
- 粘貼後恢復原始剪貼板內容
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("clipboard")


# ─── Win32 常數和函數 ─────────────────────────────────────

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# 貼上後等幾耐先還原剪貼板（僅 restore=True 時）。
# 由 0.15s 提升到 0.4s：俾慢應用（Electron/瀏覽器/遠端桌面）足夠時間讀取，
# 避免未貼完就被還原成舊內容。
_RESTORE_DELAY = 0.4

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# Clipboard
_OpenClipboard = _user32.OpenClipboard
_OpenClipboard.argtypes = [wt.HWND]
_OpenClipboard.restype = wt.BOOL

_CloseClipboard = _user32.CloseClipboard
_CloseClipboard.restype = wt.BOOL

_EmptyClipboard = _user32.EmptyClipboard
_EmptyClipboard.restype = wt.BOOL

_GetClipboardData = _user32.GetClipboardData
_GetClipboardData.argtypes = [wt.UINT]
_GetClipboardData.restype = wt.HANDLE

_SetClipboardData = _user32.SetClipboardData
_SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
_SetClipboardData.restype = wt.HANDLE

# Memory
_GlobalAlloc = _kernel32.GlobalAlloc
_GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_GlobalAlloc.restype = wt.HANDLE

_GlobalLock = _kernel32.GlobalLock
_GlobalLock.argtypes = [wt.HANDLE]
_GlobalLock.restype = ctypes.c_void_p

_GlobalUnlock = _kernel32.GlobalUnlock
_GlobalUnlock.argtypes = [wt.HANDLE]
_GlobalUnlock.restype = wt.BOOL


# ─── 低階剪貼板操作 ──────────────────────────────────────

def _open_clipboard(retries: int = 3, delay: float = 0.05) -> bool:
    """
    開啟剪貼板（帶重試）。

    其他應用可能正在使用剪貼板，所以需要重試機制。
    """
    for i in range(retries):
        if _OpenClipboard(None):
            return True
        if i < retries - 1:
            time.sleep(delay)
    logger.warning("無法開啟剪貼板（重試 %d 次後失敗）", retries)
    return False


def _read_text() -> Optional[str]:
    """從剪貼板讀取 Unicode 文字（需先 OpenClipboard）。"""
    handle = _GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return None
    ptr = _GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.wstring_at(ptr)
    finally:
        _GlobalUnlock(handle)


def _write_text(text: str) -> bool:
    """將 Unicode 文字寫入剪貼板（需先 OpenClipboard + EmptyClipboard）。"""
    # UTF-16LE 編碼 + null 終止符
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    size = len(encoded)

    handle = _GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False

    ptr = _GlobalLock(handle)
    if not ptr:
        return False

    try:
        ctypes.memmove(ptr, encoded, size)
    finally:
        _GlobalUnlock(handle)

    result = _SetClipboardData(CF_UNICODETEXT, handle)
    return bool(result)


# ─── 公開 API ─────────────────────────────────────────────

class ClipboardManager:
    """
    Windows 剪貼板管理器。

    所有方法都是類方法，無需實例化。
    自動處理剪貼板的開啟/關閉和重試。
    """

    @classmethod
    def get_text(cls) -> Optional[str]:
        """
        讀取剪貼板中的文字。

        Returns:
            剪貼板文字，無文字時返回 None
        """
        if not _open_clipboard():
            return None
        try:
            return _read_text()
        finally:
            _CloseClipboard()

    @classmethod
    def set_text(cls, text: str) -> bool:
        """
        將文字寫入剪貼板。

        Args:
            text: 要寫入的文字

        Returns:
            是否成功
        """
        if not _open_clipboard():
            return False
        try:
            _EmptyClipboard()
            success = _write_text(text)
            if success:
                logger.debug("已寫入剪貼板: %d 個字元", len(text))
            return success
        finally:
            _CloseClipboard()

    @classmethod
    def paste_text(
        cls,
        text: str,
        restore: bool = False,
    ) -> bool:
        """
        透過剪貼板粘貼文字到當前應用。

        流程：
        1. 備份原有剪貼板內容（若 restore=True）
        2. 寫入新文字到剪貼板
        3. 模擬 Ctrl+V
        4. 等待粘貼完成
        5. 恢復原有剪貼板內容（若 restore=True）

        Args:
            text: 要粘貼的文字
            restore: 粘貼後是否恢復原始剪貼板內容（預設 False，結果留喺剪貼板）

        Returns:
            是否成功貼上（寫入剪貼板或 Ctrl+V 失敗時回 False，且不冒泡異常）
        """
        from utils.keyboard import KeyboardSimulator

        try:
            # 1. 備份
            original = None
            if restore:
                original = cls.get_text()

            # 2. 寫入
            if not cls.set_text(text):
                logger.error("寫入剪貼板失敗，無法粘貼")
                return False

            # 3. 粘貼
            time.sleep(0.02)
            if not KeyboardSimulator.press_ctrl_v():
                logger.error("模擬 Ctrl+V 失敗，文字仍保留喺剪貼板")
                return False

            # 4. 恢復（成功貼上後才還原；失敗時保留結果俾用戶手動 Ctrl+V）
            if restore and original is not None:
                time.sleep(_RESTORE_DELAY)  # 等待粘貼完成再恢復
                if cls.set_text(original):
                    logger.debug("剪貼板已恢復原始內容")
                else:
                    logger.warning("還原剪貼板失敗，剪貼板留有識別結果")

            return True
        except Exception as err:  # noqa: BLE001 — 任何失敗都回報，避免被外層靜默吞掉
            logger.error("粘貼流程異常: %s", err, exc_info=True)
            return False

    @classmethod
    def clear(cls) -> None:
        """清空剪貼板。"""
        if _open_clipboard():
            _EmptyClipboard()
            _CloseClipboard()
            logger.debug("剪貼板已清空")
