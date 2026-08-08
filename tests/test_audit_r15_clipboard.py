"""Bug R15 迴歸測試：capture_selection() 唔可以為咗偵測而毀掉剪貼簿內容。

災情重現：使用者喺 Explorer 複製咗幾個檔案（CF_HDROP），中途撳「潤色選取文字」
熱鍵，舊實作無條件 EmptyClipboard 清走全部格式 → 檔案／圖片無聲無息消失，零提示。

覆蓋範圍：
- 序號法（主流程）：全程唔清空，序號變咗先讀取
- 序號一直冇變（使用者根本冇選中文字）→ 逾時回 None，且冇留下破壞
- 後備流程遇上 CF_HDROP／點陣圖 → 放棄擷取回 None，堅決唔清空
- 後備流程確認純文字先沿用舊嘅「清空 → Ctrl+C」做法
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import utils.clipboard as clipboard_mod
from utils.clipboard import ClipboardManager

CF_BITMAP, CF_DIB, CF_HDROP, CF_DIBV5 = 2, 8, 15, 17
CF_UNICODETEXT = 13


def _sequence_feed(*values):
    """製造 _clipboard_sequence 的 side_effect：值用完之後永遠回最後一個。"""
    remaining = list(values)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


# ─── 主流程：序號法完全唔清空 ─────────────────────────────

def test_sequence_change_triggers_read_without_any_clear():
    """序號改變先讀取剪貼板，全程唔會呼叫 clear（非破壞性）。"""
    texts = iter(["原本剪貼板內容", "選取的文字"])
    with patch.object(ClipboardManager, "get_text", side_effect=lambda: next(texts)), \
            patch("utils.clipboard._clipboard_sequence",
                  side_effect=_sequence_feed(100, 100, 101)), \
            patch("utils.clipboard._has_non_text_formats") as mock_enum, \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text", return_value=True) as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.5, poll_interval=0.01)

    assert result == "選取的文字"
    mock_clear.assert_not_called()                      # R15 核心：絕不清空
    mock_set.assert_called_once_with("原本剪貼板內容")  # 擷取後還原原文字
    mock_enum.assert_not_called()                        # 有序號就唔使行後備路徑


def test_sequence_unchanged_times_out_without_damage():
    """序號一直冇變（使用者冇選中任何文字）→ 逾時回 None，且完全冇改動剪貼板。"""
    with patch.object(ClipboardManager, "get_text", return_value="使用者原本嘅內容") as mock_get, \
            patch("utils.clipboard._clipboard_sequence", side_effect=_sequence_feed(77)), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text") as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result is None
    mock_clear.assert_not_called()
    mock_set.assert_not_called()      # 冇改動就唔使還原，避免無謂寫入
    assert mock_get.call_count == 1   # 序號未變就唔應該讀，只有備份嗰次


def test_sequence_mode_does_not_touch_clipboard_when_ctrl_c_fails():
    """模擬 Ctrl+C 失敗 → 剪貼板原封不動。"""
    with patch.object(ClipboardManager, "get_text", return_value="原本內容"), \
            patch("utils.clipboard._clipboard_sequence", side_effect=_sequence_feed(5)), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text") as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=False):
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result is None
    mock_clear.assert_not_called()
    mock_set.assert_not_called()


def test_sequence_mode_tolerates_delayed_data_after_bump():
    """序號先跳、資料稍後先寫入（EmptyClipboard 與 SetClipboardData 分兩步）→ 繼續輪詢直到讀到。"""
    texts = iter(["原本內容", None, "遲到嘅選取文字"])
    with patch.object(ClipboardManager, "get_text", side_effect=lambda: next(texts)), \
            patch("utils.clipboard._clipboard_sequence", side_effect=_sequence_feed(1, 2)), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text", return_value=True), \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.5, poll_interval=0.01)

    assert result == "遲到嘅選取文字"
    mock_clear.assert_not_called()


# ─── 後備流程：偵測到非文字格式就放棄 ─────────────────────

@pytest.mark.parametrize("fmt_name,fmt", [
    ("CF_HDROP（複製的檔案）", CF_HDROP),
    ("CF_BITMAP", CF_BITMAP),
    ("CF_DIB", CF_DIB),
    ("CF_DIBV5", CF_DIBV5),
])
def test_non_text_clipboard_is_never_cleared(fmt_name, fmt):
    """後備流程下剪貼板含非文字格式 → 放棄擷取，絕不清空（R15 主災情）。"""
    # capture_selection 會列舉多次（敏感標記檢查 + 非文字格式檢查），
    # 每次都要從頭重放同一份格式清單
    sequence = [CF_UNICODETEXT, fmt, 0]

    def _enum(prev):
        return sequence[0] if prev == 0 else sequence[sequence.index(prev) + 1]

    with patch.object(ClipboardManager, "get_text", return_value=None), \
            patch("utils.clipboard._clipboard_sequence", return_value=None), \
            patch("utils.clipboard._open_clipboard", return_value=True), \
            patch("utils.clipboard._CloseClipboard"), \
            patch("utils.clipboard._EnumClipboardFormats", side_effect=_enum), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text") as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c") as mock_ctrl_c:
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result is None, f"{fmt_name} 應該放棄擷取"
    mock_clear.assert_not_called()
    mock_set.assert_not_called()
    mock_ctrl_c.assert_not_called()  # 連 Ctrl+C 都唔應該送，免得覆蓋原內容


def test_fallback_aborts_when_formats_cannot_be_inspected():
    """後備流程下無法判斷格式（開唔到剪貼板）→ 寧可唔做，都唔清空。"""
    with patch.object(ClipboardManager, "get_text", return_value=None), \
            patch("utils.clipboard._clipboard_sequence", return_value=None), \
            patch("utils.clipboard._has_non_text_formats", return_value=None), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c") as mock_ctrl_c:
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result is None
    mock_clear.assert_not_called()
    mock_ctrl_c.assert_not_called()


def test_fallback_still_works_for_text_only_clipboard():
    """後備流程確認淨係文字 → 沿用舊嘅「清空 → Ctrl+C → 輪詢 → 還原」做法。"""
    texts = iter(["原本文字", "選取的文字"])
    with patch.object(ClipboardManager, "get_text", side_effect=lambda: next(texts)), \
            patch("utils.clipboard._clipboard_sequence", return_value=None), \
            patch("utils.clipboard._has_non_text_formats", return_value=False), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text", return_value=True) as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.5, poll_interval=0.01)

    assert result == "選取的文字"
    mock_clear.assert_called_once()
    mock_set.assert_called_once_with("原本文字")


# ─── 低階輔助函數 ────────────────────────────────────────

@pytest.mark.parametrize("fmt", [CF_BITMAP, CF_DIB, CF_HDROP, CF_DIBV5])
def test_has_non_text_formats_detects_binary_formats(fmt):
    """EnumClipboardFormats 列舉到圖片／檔案格式 → True。"""
    enum_feed = iter([fmt, 0])
    with patch("utils.clipboard._open_clipboard", return_value=True), \
            patch("utils.clipboard._CloseClipboard"), \
            patch("utils.clipboard._EnumClipboardFormats", side_effect=lambda prev: next(enum_feed)):
        assert clipboard_mod._has_non_text_formats() is True


def test_has_non_text_formats_false_for_text_only():
    """只有文字相關格式 → False。"""
    enum_feed = iter([CF_UNICODETEXT, 1, 0])  # 1 = CF_TEXT
    with patch("utils.clipboard._open_clipboard", return_value=True), \
            patch("utils.clipboard._CloseClipboard"), \
            patch("utils.clipboard._EnumClipboardFormats", side_effect=lambda prev: next(enum_feed)):
        assert clipboard_mod._has_non_text_formats() is False


def test_has_non_text_formats_none_when_clipboard_unavailable():
    """開唔到剪貼板 → None（無法判斷，交由呼叫端保守處理）。"""
    with patch("utils.clipboard._open_clipboard", return_value=False):
        assert clipboard_mod._has_non_text_formats() is None


def test_clipboard_sequence_none_when_access_denied():
    """GetClipboardSequenceNumber 回 0（無 WINSTA_ACCESSCLIPBOARD 權限）→ None。"""
    with patch("utils.clipboard._GetClipboardSequenceNumber", return_value=0):
        assert clipboard_mod._clipboard_sequence() is None


def test_clipboard_sequence_returns_int():
    """正常取得序號 → int。"""
    with patch("utils.clipboard._GetClipboardSequenceNumber", return_value=4242):
        assert clipboard_mod._clipboard_sequence() == 4242
