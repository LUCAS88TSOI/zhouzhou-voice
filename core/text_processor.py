"""
州州語音 - 文字後處理

處理 ASR 識別結果的後處理流程：
- 中英文間加空格
- 中文數字 → 阿拉伯數字（ITN）
- 末尾標點移除
- 簡體 → 繁體轉換
- 重疊段落文字合併
"""

from __future__ import annotations

import re
from typing import Optional

from utils.config import OutputConfig
from utils.logger import get_logger

logger = get_logger("text_proc")


# ═══════════════════════════════════════════════════════════
# 中英文空格
# ═══════════════════════════════════════════════════════════

_CJK = r"[\u4e00-\u9fff\u3400-\u4dbf]"
_LATIN = r"[A-Za-z0-9]"
_RE_CJK_LATIN = re.compile(f"({_CJK})({_LATIN})")
_RE_LATIN_CJK = re.compile(f"({_LATIN})({_CJK})")


def add_cjk_spacing(text: str) -> str:
    """在中文和英文/數字之間加空格。

    例：「這是hello世界」→「這是 hello 世界」
    """
    text = _RE_CJK_LATIN.sub(r"\1 \2", text)
    text = _RE_LATIN_CJK.sub(r"\1 \2", text)
    return text


# ═══════════════════════════════════════════════════════════
# 末尾標點移除
# ═══════════════════════════════════════════════════════════

def strip_trailing_punc(text: str, chars: str = "，。,.") -> str:
    """移除指定的末尾標點符號。"""
    return text.rstrip(chars)


# ═══════════════════════════════════════════════════════════
# 中文數字 → 阿拉伯數字（ITN）
# ═══════════════════════════════════════════════════════════

_CN_DIGITS = {
    "零": 0, "〇": 0,
    "一": 1, "壹": 1, "幺": 1,
    "二": 2, "貳": 2, "贰": 2, "兩": 2, "两": 2,
    "三": 3, "參": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陸": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

_CN_UNITS = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
    "萬": 10000, "万": 10000,
    "億": 100000000, "亿": 100000000,
}

# 所有中文數字字元（用於正則）
_CN_ALL_CHARS = "".join(
    re.escape(c) for c in list(_CN_DIGITS) + list(_CN_UNITS)
)

# 純數字序列：幺九二 → 192
_RE_DIGIT_SEQ = re.compile(
    "[" + "".join(re.escape(c) for c in _CN_DIGITS) + "]{2,}"
)

# 百分之X
_RE_PERCENT = re.compile(r"百分之([" + _CN_ALL_CHARS + r"]+)")

# 帶量詞的數值：三百五十、一千二
_RE_VALUE = re.compile(
    r"([" + _CN_ALL_CHARS + r"]{2,})"
    r"(?=[個件台次天年月日分秒塊元]|$|\s|[，。,.！？!?])"
)

# 常見慣用語（不轉換）
_IDIOM_BLACKLIST = frozenset(
    {
        "一切", "一般", "一樣", "一样", "一直", "一起",
        "一些", "一定", "一次", "一下", "一個", "一个",
        "一種", "一种", "一點", "一点", "一天", "一年",
        "一月", "一心", "一路", "一邊", "一边", "一半",
        "七上八下", "亂七八糟", "乱七八糟", "四面八方",
        "三心二意", "五花八門", "五花八门", "九牛一毛",
    }
)


def _digits_to_arabic(match: re.Match) -> str:
    """純數字序列轉阿拉伯數字：幺九二 → 192"""
    text = match.group()
    return "".join(str(_CN_DIGITS.get(c, c)) for c in text)


def _value_to_number(text: str) -> Optional[int]:
    """
    中文數值轉阿拉伯數字。

    支援：三百五十→350, 一千二→1200, 十五→15
    """
    result = 0
    current = 0

    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit >= 10000:
                # 萬/億級：累積結果乘以大單位
                result = (result + max(current, 1)) * unit
                current = 0
            else:
                # 十/百/千級
                result += max(current, 1) * unit
                current = 0

    result += current
    return result if result > 0 else None


def _percent_replace(match: re.Match) -> str:
    """百分之X → X%"""
    inner = match.group(1)
    num = _value_to_number(inner)
    if num is not None:
        return f"{num}%"
    return match.group()


def _value_replace(match: re.Match) -> str:
    """帶量詞的數值轉換。"""
    text = match.group(1)
    # 跳過慣用語
    if text in _IDIOM_BLACKLIST:
        return text
    # 至少包含一個量詞才轉換
    if not any(c in _CN_UNITS for c in text):
        return text
    num = _value_to_number(text)
    if num is not None:
        return str(num)
    return text


def chinese_to_number(text: str) -> str:
    """
    中文數字轉阿拉伯數字（ITN）。

    處理：
    - 純序列：幺九二 → 192
    - 百分比：百分之五十 → 50%
    - 數值+量詞：三百五十個 → 350個
    - 跳過慣用語
    """
    # 先跳過包含慣用語的片段
    for idiom in _IDIOM_BLACKLIST:
        if idiom in text:
            # 暫時替換慣用語，處理完再還原
            pass

    text = _RE_PERCENT.sub(_percent_replace, text)
    text = _RE_VALUE.sub(_value_replace, text)
    text = _RE_DIGIT_SEQ.sub(_digits_to_arabic, text)

    return text


# ═══════════════════════════════════════════════════════════
# 繁體中文轉換
# ═══════════════════════════════════════════════════════════

def to_traditional(text: str, locale: str = "zh-hk") -> str:
    """
    簡體 → 繁體轉換。

    Args:
        text: 輸入文字
        locale: 目標地區 (zh-tw / zh-hk / zh-hant)

    若 opencc 未安裝則原樣返回（不報錯）。
    """
    try:
        from opencc import OpenCC
    except ImportError:
        logger.warning("opencc 未安裝，無法進行繁體轉換（pip install opencc-python-reimplemented）")
        return text

    locale_map = {
        "zh-tw": "s2twp",   # 簡體→臺灣繁體（含慣用詞）
        "zh-hk": "s2hk",    # 簡體→香港繁體
        "zh-hant": "s2t",   # 簡體→標準繁體
        "zh-hans": "t2s",   # 繁體→簡體
    }

    config = locale_map.get(locale, "s2hk")
    try:
        converter = OpenCC(config)
        return converter.convert(text)
    except Exception as err:
        logger.warning("繁體轉換失敗（字典文件可能缺失），跳過: %s", err)
        return text


# ═══════════════════════════════════════════════════════════
# 重疊段落文字合併
# ═══════════════════════════════════════════════════════════

_OVERLAP_CHARS = 5
_MAX_SKIP = 10
_PUNC_CHARS = "，。,.、！？!?；;：:"


def merge_text_segments(prev_text: str, new_text: str) -> str:
    """
    合併兩段有重疊的識別文字。

    用於串流識別：每段音頻有 1 秒重疊，識別出的文字
    在重疊區會有重複，此函數去除重複部分。

    算法：
    1. 取 prev 末尾的搜尋窗口
    2. 在 new 開頭尋找精確匹配
    3. 找到則去重拼接，否則直接串接

    Args:
        prev_text: 前一段文字
        new_text: 新一段文字

    Returns:
        合併後的文字
    """
    if not prev_text:
        return new_text
    if not new_text:
        return prev_text

    # 去掉邊界標點以便匹配
    prev_clean = prev_text.rstrip(_PUNC_CHARS)
    new_clean = new_text.lstrip(_PUNC_CHARS)

    if not prev_clean or not new_clean:
        return prev_text + new_text

    # 搜尋窗口：prev 末尾 N 個字元
    window_size = min(_OVERLAP_CHARS, len(prev_clean))
    search_window = prev_clean[-window_size:]

    # 從最長到最短嘗試匹配
    best_len = 0
    best_skip = 0

    for length in range(len(search_window), 1, -1):
        pattern = search_window[-length:]
        for skip in range(min(_MAX_SKIP, len(new_clean) - length + 1)):
            if new_clean[skip : skip + length] == pattern:
                best_len = length
                best_skip = skip
                break
        if best_len > 0:
            break

    if best_len >= 2:
        # 找到重疊：保留 prev 全部 + new 去掉重疊部分
        # new_clean 的 leading_punc 長度
        leading_punc_len = len(new_text) - len(new_clean)
        merge_start = leading_punc_len + best_skip + best_len
        return prev_text + new_text[merge_start:]

    # 無重疊：直接串接
    return prev_text + new_text


# ═══════════════════════════════════════════════════════════
# 處理管線
# ═══════════════════════════════════════════════════════════

class TextProcessor:
    """
    文字後處理管線。

    按順序執行：中英空格 → ITN → 標點移除 → 繁體轉換。
    每個步驟都可透過配置獨立開關。
    """

    def __init__(self, config: Optional[OutputConfig] = None) -> None:
        if config is None:
            config = OutputConfig()
        self._config = config

    def process(self, text: str) -> str:
        """
        執行完整的後處理管線。

        Args:
            text: 原始識別文字

        Returns:
            處理後的文字
        """
        if not text:
            return text

        if self._config.format_spell:
            text = add_cjk_spacing(text)

        if self._config.format_num:
            text = chinese_to_number(text)

        if self._config.trash_punc:
            text = strip_trailing_punc(text, self._config.trash_punc)

        if self._config.traditional_convert:
            text = to_traditional(text, self._config.traditional_locale)

        return text

    @staticmethod
    def merge(prev_text: str, new_text: str) -> str:
        """合併重疊的文字段落。"""
        return merge_text_segments(prev_text, new_text)
