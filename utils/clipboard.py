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

# 一旦清空就無法復原嘅非文字格式（圖片／複製嘅檔案／音訊／中繼檔）。
# capture_selection() 走後備路徑時見到任何一個，就寧可放棄擷取都唔清空剪貼板。
CF_TEXT, CF_BITMAP, CF_METAFILEPICT, CF_OEMTEXT = 1, 2, 3, 7
CF_TIFF, CF_DIB = 6, 8
CF_PALETTE, CF_RIFF, CF_WAVE, CF_ENHMETAFILE, CF_HDROP, CF_DIBV5 = 9, 11, 12, 14, 15, 17
CF_LOCALE = 16

# 白名單而非黑名單：實務上不可復原的內容常用 >= 0xC000 的註冊格式
#（"PNG"、"FileGroupDescriptorW"+"FileContents"、"Preferred DropEffect"），
# 黑名單一定漏。只有確定是純文字的格式才算「清空無損失」。
_TEXT_FORMATS = frozenset({CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE})

# 密碼管理員／瀏覽器用這些註冊格式標記「此內容勿讀取、勿進剪貼簿歷史」。
# 見到任何一個就放棄擷取——絕不能把別人的密碼送去雲端 LLM。
_SENSITIVE_FORMAT_NAMES = (
    "Clipboard Viewer Ignore",
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)

# 貼上後等幾耐先還原剪貼板（僅 restore=True 時）。
# 由 0.15s 提升到 0.4s：俾慢應用（Electron/瀏覽器/遠端桌面）足夠時間讀取，
# 避免未貼完就被還原成舊內容。
_RESTORE_DELAY = 0.4

# use_last_error=True 是必要的：ctypes.windll.X 建立的函式不會把 Win32
# 的 LastError 存進 ctypes 的 thread-local，get_last_error() 會恆回 0 ——
# 於是 _enum_formats() 那個「列舉中途失敗」的判斷永遠不成立，退回成
# fail-open（把失敗當成「純文字」），正是 M2 要防的情況。
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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

_EnumClipboardFormats = _user32.EnumClipboardFormats
_EnumClipboardFormats.argtypes = [wt.UINT]
_EnumClipboardFormats.restype = wt.UINT

# 剪貼板內容每次變更都會遞增嘅全局序號；用嚟非破壞性偵測 Ctrl+C 有冇生效。
_GetClipboardSequenceNumber = getattr(_user32, "GetClipboardSequenceNumber", None)
if _GetClipboardSequenceNumber is not None:
    _GetClipboardSequenceNumber.argtypes = []
    _GetClipboardSequenceNumber.restype = wt.DWORD

_RegisterClipboardFormatW = _user32.RegisterClipboardFormatW
_RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
_RegisterClipboardFormatW.restype = wt.UINT

_GetForegroundWindow = _user32.GetForegroundWindow
_GetForegroundWindow.argtypes = []
_GetForegroundWindow.restype = wt.HWND

# _sensitive_format_ids() 的快取（延遲註冊，避免 import 期做 Win32 呼叫）
_SENSITIVE_FORMAT_IDS: Optional[frozenset] = None

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

_GlobalFree = _kernel32.GlobalFree
_GlobalFree.argtypes = [wt.HANDLE]
_GlobalFree.restype = wt.HANDLE


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


def _clipboard_sequence() -> Optional[int]:
    """
    讀取剪貼板序號（內容每次變更都遞增），用嚟非破壞性偵測剪貼板有冇被改動。

    Returns:
        序號；API 不存在、呼叫失敗或無 WINSTA_ACCESSCLIPBOARD 權限（回 0）時為 None
    """
    if _GetClipboardSequenceNumber is None:
        return None
    try:
        seq = int(_GetClipboardSequenceNumber())
    except OSError as err:
        logger.warning("讀取剪貼板序號失敗: %s", err)
        return None
    return seq if seq else None  # 0 = 冇存取權限，當作取唔到


def _sensitive_format_ids() -> frozenset[int]:
    """密碼管理員用嚟標記「勿讀取」嘅註冊格式 ID（延遲註冊 + 快取）。"""
    global _SENSITIVE_FORMAT_IDS
    if _SENSITIVE_FORMAT_IDS is None:
        ids = set()
        for name in _SENSITIVE_FORMAT_NAMES:
            try:
                fmt = int(_RegisterClipboardFormatW(name))
            except OSError as err:
                logger.warning("註冊剪貼板格式 %s 失敗: %s", name, err)
                continue
            if fmt:
                ids.add(fmt)
        _SENSITIVE_FORMAT_IDS = frozenset(ids)
    return _SENSITIVE_FORMAT_IDS


def _enum_formats() -> Optional[frozenset[int]]:
    """
    列舉剪貼板現有格式。

    Returns:
        格式 ID 集合；開唔到剪貼板或列舉中途失敗（無法判斷）時回 None。
        注意 EnumClipboardFormats 回 0 同時代表「列舉完畢」同「失敗」，
        必須用 GetLastError() 分辨——當成「完畢」會 fail-open 毀掉圖片。
    """
    if not _open_clipboard():
        return None
    try:
        formats: set[int] = set()
        ctypes.set_last_error(0)
        fmt = _EnumClipboardFormats(0)
        while fmt:
            formats.add(int(fmt))
            ctypes.set_last_error(0)
            fmt = _EnumClipboardFormats(fmt)
        if ctypes.get_last_error() != 0:
            logger.warning("列舉剪貼板格式中途失敗: %d", ctypes.get_last_error())
            return None
        return frozenset(formats)
    finally:
        _CloseClipboard()


def foreground_window() -> int:
    """取得當前前景視窗 handle；拿不到時回 0（代表「未知」）。"""
    try:
        return int(_GetForegroundWindow() or 0)
    except Exception as err:  # noqa: BLE001 — 偵測失敗不得癱瘓貼上主功能
        logger.warning("取得前景視窗失敗: %s", err)
        return 0


def _has_non_text_formats() -> Optional[bool]:
    """
    檢查剪貼板是否存在清空後無法復原嘅非文字內容（圖片／複製嘅檔案等）。

    用白名單判斷：只要有任何一個唔喺文字白名單內嘅格式就當非文字。
    黑名單一定漏掉 "PNG"、"FileGroupDescriptorW" 呢類註冊格式。

    Returns:
        True/False；無法判斷時回 None，呼叫端應保守處理
    """
    formats = _enum_formats()
    if formats is None:
        return None
    return any(fmt not in _TEXT_FORMATS for fmt in formats)


def _is_sensitive_clipboard() -> Optional[bool]:
    """
    剪貼板有冇被標記為敏感（密碼管理員／勿進剪貼簿歷史）。

    Returns:
        True/False；無法判斷時回 None，呼叫端應保守處理（當成敏感）
    """
    formats = _enum_formats()
    if formats is None:
        return None
    return bool(formats & _sensitive_format_ids())


def _write_text(text: str) -> bool:
    """將 Unicode 文字寫入剪貼板（需先 OpenClipboard + EmptyClipboard）。"""
    # UTF-16LE 編碼 + null 終止符
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    size = len(encoded)

    handle = _GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False

    # GMEM_MOVEABLE 區塊的所有權只在 SetClipboardData 成功後才移交系統，
    # 所以每條失敗路徑都必須自己 GlobalFree，否則常駐程式會慢慢漏記憶體。
    ptr = _GlobalLock(handle)
    if not ptr:
        _GlobalFree(handle)
        return False

    try:
        ctypes.memmove(ptr, encoded, size)
    finally:
        _GlobalUnlock(handle)

    if not _SetClipboardData(CF_UNICODETEXT, handle):
        _GlobalFree(handle)
        return False
    return True


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
        expect_hwnd: int = 0,
    ) -> bool:
        """
        透過剪貼板粘貼文字到當前應用。

        流程：
        1. 備份原有剪貼板內容（若 restore=True）
        2. 寫入新文字到剪貼板
        3. 確認前景視窗仍是預期目標（若有給 expect_hwnd）
        4. 模擬 Ctrl+V 並校驗注入結果
        5. 恢復原有剪貼板內容（若 restore=True）

        Args:
            text: 要粘貼的文字
            restore: 粘貼後是否恢復原始剪貼板內容（預設 False，結果留喺剪貼板）
            expect_hwnd: 錄音當下記下的目標視窗 handle。0 = 不檢查。
                開了 LLM 潤色時管線可耗 10-30 秒，期間用戶早就切走視窗，
                盲貼會把逐字稿送進聊天室輸入框、密碼欄或程式碼中間（U9）。

        Returns:
            是否成功貼上。視窗已切換或注入被擋時回 False，
            但文字一定已在剪貼簿，呼叫端可以誠實地提示「手動 Ctrl+V」。
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

            # 3. 目標視窗校驗（handle 為 0 代表未知，一律放行）
            current = foreground_window()
            if expect_hwnd and current and current != expect_hwnd:
                logger.warning(
                    "目標視窗已切換（%s → %s），不貼上；文字留喺剪貼板",
                    expect_hwnd, current,
                )
                return False

            # 4. 粘貼
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
    def capture_selection(
        cls, timeout: float = 0.5, poll_interval: float = 0.03,
    ) -> Optional[str]:
        """
        模擬 Ctrl+C 讀取當前選取文字（非破壞性）。

        主流程（序號法）：備份原文字 → 記下剪貼板序號 → 模擬 Ctrl+C → 輪詢至序號
        改變先讀取 → 還原原文字。全程唔清空剪貼板，所以原本嘅圖片／複製嘅檔案
        唔會被毀（Bug R15）。

        後備流程（序號 API 取唔到時）：先用 EnumClipboardFormats 檢查有冇非文字
        格式，有（或無法判斷）就直接放棄擷取回傳 None；確認純文字先沿用舊嘅
        「清空 → Ctrl+C → 輪詢」做法。

        Args:
            timeout: 最長等待秒數
            poll_interval: 輪詢間隔秒數

        Returns:
            選取的文字；偵測不到選取（含僅空白、Ctrl+C 模擬失敗、逾時、為保護
            非文字剪貼板而放棄）時回傳 None
        """
        from utils.keyboard import KeyboardSimulator

        # 敏感內容（密碼管理員標記）一律唔掂——序號變更只證明剪貼板被改過，
        # 唔證明係我哋嘅 Ctrl+C 改嘅；讀錯咗會把別人嘅密碼送去雲端 LLM。
        if _is_sensitive_clipboard() is not False:
            logger.warning("剪貼板已標記為敏感內容（或無法判斷），放棄擷取選取文字")
            return None

        # 記下焦點視窗：Ctrl+C 送去邊個視窗、之後讀返嚟嘅內容應該同源。
        # 期間焦點被搶走代表寫入者好可能係其他應用。
        try:
            before_hwnd = _GetForegroundWindow()
        except OSError:
            before_hwnd = None

        original = cls.get_text()
        before_seq = _clipboard_sequence()
        dirtied = False  # 剪貼板有冇被我哋改動過，決定收尾使唔使還原

        if before_seq is None:
            # 後備路徑：冇序號可用，只能靠清空辨識新內容——但唔可以毀掉非文字內容
            if _has_non_text_formats() is not False:  # True 或 None（判斷唔到）都放棄
                logger.warning("剪貼板含非文字內容（或無法判斷），放棄擷取選取文字以免毀掉")
                return None
            cls.clear()
            dirtied = True

        selected: Optional[str] = None
        if KeyboardSimulator.press_ctrl_c():
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                if before_seq is not None:
                    current = _clipboard_sequence()
                    if current is None or current == before_seq:
                        continue  # 序號未變＝Ctrl+C 未生效，唔好讀（否則會誤判舊內容）
                    dirtied = True
                # 寫入者可能係其他應用（密碼管理員、瀏覽器擴充）——內容變咗
                # 但焦點視窗換咗，或者新內容被標記敏感，一律唔讀。
                if _is_sensitive_clipboard() is not False:
                    logger.warning("擷取期間剪貼板出現敏感標記，放棄讀取")
                    break
                if before_hwnd is not None:
                    try:
                        if _GetForegroundWindow() != before_hwnd:
                            logger.warning("擷取期間焦點視窗已變更，放棄讀取以免讀到其他應用的內容")
                            break
                    except OSError:
                        pass
                text = cls.get_text()
                if text and text.strip():
                    selected = text
                    break

        if dirtied:  # 冇改動過就唔好郁，避免無謂寫入
            if original is not None:
                cls.set_text(original)
            else:
                cls.clear()

        if selected:
            logger.debug("已讀取選取文字: %d 個字元", len(selected))
        else:
            logger.debug("未偵測到選取文字")
        return selected

    @classmethod
    def clear(cls) -> None:
        """清空剪貼板。"""
        if _open_clipboard():
            _EmptyClipboard()
            _CloseClipboard()
            logger.debug("剪貼板已清空")
