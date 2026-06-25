"""
TextProcessor 標點移除測試。

涵蓋 punc_strip_mode 三態：
  off       — 不移除任何標點
  trailing  — 只移除末尾標點（沿用舊行為）
  all       — 移除全文中所有指定標點
以及自訂字元集 trash_punc 與向後相容預設值。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config import OutputConfig
from core.text_processor import strip_all_punc, strip_trailing_punc, TextProcessor


# ─── strip_all_punc 純函數 ────────────────────────────────

def test_strip_all_punc_removes_every_occurrence():
    assert strip_all_punc("你好，世界。再見！", "，。！") == "你好世界再見"


def test_strip_all_punc_keeps_unlisted_chars():
    # 只移除指定字元，未列出的標點保留
    assert strip_all_punc("a, b. c!", "，。") == "a, b. c!"


def test_strip_all_punc_empty_chars_is_noop():
    assert strip_all_punc("你好，世界。", "") == "你好，世界。"


def test_strip_all_punc_empty_text():
    assert strip_all_punc("", "，。") == ""


# ─── strip_trailing_punc 舊行為不變 ───────────────────────

def test_strip_trailing_only_removes_tail():
    assert strip_trailing_punc("你好，世界。", "，。") == "你好，世界"


# ─── process() 三態分派 ───────────────────────────────────

def _proc(mode: str, chars: str = "，。") -> TextProcessor:
    cfg = OutputConfig(
        traditional_convert=False,
        format_num=False,
        format_spell=False,
        trash_punc=chars,
        punc_strip_mode=mode,
    )
    return TextProcessor(cfg)


def test_process_mode_off_keeps_all_punc():
    assert _proc("off").process("你好，世界。") == "你好，世界。"


def test_process_mode_trailing_strips_tail_only():
    assert _proc("trailing").process("你好，世界。") == "你好，世界"


def test_process_mode_all_strips_everywhere():
    assert _proc("all").process("你好，世界。") == "你好世界"


def test_process_default_mode_is_trailing():
    # 不顯式設定 punc_strip_mode → 預設 trailing（向後相容舊 config）
    cfg = OutputConfig(
        traditional_convert=False,
        format_num=False,
        format_spell=False,
        trash_punc="，。",
    )
    assert TextProcessor(cfg).process("你好，世界。") == "你好，世界"


def test_process_empty_text_returns_empty():
    assert _proc("all").process("") == ""
