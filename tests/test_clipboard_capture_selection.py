"""utils/clipboard.py::ClipboardManager.capture_selection() 單元測試。

情境：有選取／無選取（逾時）／Ctrl+C 模擬失敗／選取內容僅空白。

註：v3.6.4（Bug R15）起改用序號法偵測 Ctrl+C 有冇生效，唔再清空剪貼板，
所以呢度要 mock `_clipboard_sequence`；「唔清空」嘅保護行為見
tests/test_audit_r15_clipboard.py。
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.clipboard import ClipboardManager


def _sequence_feed(*values):
    """製造 _clipboard_sequence 的 side_effect：值用完之後永遠回最後一個。"""
    remaining = list(values)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


def test_returns_selection_text_when_clipboard_changes():
    """Ctrl+C 後剪貼板出現新文字 → 回傳該文字，並還原備份的原剪貼板內容。"""
    get_text_results = iter(["原本剪貼板內容", "選取的文字"])
    with patch.object(ClipboardManager, "get_text", side_effect=lambda: next(get_text_results)), \
            patch("utils.clipboard._clipboard_sequence", side_effect=_sequence_feed(1, 2)), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text", return_value=True) as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result == "選取的文字"
    mock_set.assert_called_once_with("原本剪貼板內容")
    mock_clear.assert_not_called()  # R15：序號法唔會清空剪貼板


def test_returns_none_when_no_selection_detected():
    """Ctrl+C 後剪貼板持續空白（逾時）→ 回傳 None；原本亦無內容 → 用 clear 收尾。"""
    with patch.object(ClipboardManager, "get_text", return_value=None), \
            patch("utils.clipboard._clipboard_sequence", side_effect=_sequence_feed(1, 2)), \
            patch.object(ClipboardManager, "clear") as mock_clear, \
            patch.object(ClipboardManager, "set_text") as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result is None
    mock_set.assert_not_called()
    mock_clear.assert_called_once()  # 序號變過（Ctrl+C 已覆蓋內容）且原本無文字 → 清走殘留


def test_returns_none_when_ctrl_c_simulation_fails():
    """模擬 Ctrl+C 失敗 → 直接回傳 None，唔會輪詢剪貼板。"""
    with patch.object(ClipboardManager, "get_text", return_value=None) as mock_get, \
            patch.object(ClipboardManager, "clear"), \
            patch.object(ClipboardManager, "set_text") as mock_set, \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=False):
        result = ClipboardManager.capture_selection(timeout=0.05, poll_interval=0.01)

    assert result is None
    mock_set.assert_not_called()
    assert mock_get.call_count == 1  # 只有備份嗰次，冇輪詢


def test_whitespace_only_selection_treated_as_no_selection():
    """剪貼板變成純空白 → 視為無選取，回傳 None。"""
    with patch.object(ClipboardManager, "get_text", side_effect=lambda: "   ") as mock_get, \
            patch("utils.clipboard._clipboard_sequence", side_effect=_sequence_feed(1, 2)), \
            patch.object(ClipboardManager, "clear"), \
            patch.object(ClipboardManager, "set_text"), \
            patch("utils.keyboard.KeyboardSimulator.press_ctrl_c", return_value=True):
        result = ClipboardManager.capture_selection(timeout=0.03, poll_interval=0.01)

    assert result is None
    assert mock_get.call_count >= 2
