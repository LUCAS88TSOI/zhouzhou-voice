"""
州州語音 - 輸出文件寫入器

將轉錄結果寫入 SRT/TXT/JSON 格式文件。

SRT 生成算法：
1. 將識別 token 組成 word 列表（帶 start/end 時間）
2. 用標點符號智慧分行
3. 用 SequenceMatcher 對齊 token 到文字行
4. 生成帶時間戳的 SRT 字幕

用法：
    writer = OutputWriter(tokens, timestamps)
    writer.save_srt("output.srt")
    writer.save_txt("output.txt")
    writer.save_json("output.json")
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger("srt_writer")


# ─── 常量 ──────────────────────────────────────────────────

# 強分句標點（總是觸發換行）
_STRONG_PUNC = set("。？！.?!")

# 弱分句標點（超過閾值才觸發換行）
_WEAK_PUNC = set("，,、；;")

# 行字數閾值：弱標點超過此長度才換行
_LINE_THRESHOLD = 15

# 用於清洗 token 的標點（對齊用）
_STRIP_CHARS = "，。？！,.?!、；;：:—…「」『』（）《》【】\u3000 "

# SRT 時間格式
_SRT_TIME_FMT = "{:02d}:{:02d}:{:02d},{:03d}"


# ─── Word 結構 ─────────────────────────────────────────────

@dataclass
class Word:
    """一個帶時間信息的詞元。"""
    text: str
    start: float
    end: float


# ─── 智慧分行 ──────────────────────────────────────────────

def smart_split(text: str) -> List[str]:
    """
    按標點符號將文字智慧分成多行。

    規則：
    - 強標點（。？！.?!）：總是換行
    - 弱標點（，,）：累積超過閾值才換行
    - 去除每行末尾標點

    Args:
        text: 合併後的識別文字

    Returns:
        分行後的文字列表
    """
    if not text:
        return []

    # 用正則切割，保留分隔符
    parts = re.split(r"([，。？！,.?!、；;])", text)

    lines: List[str] = []
    buffer = ""

    for part in parts:
        if not part:
            continue

        if part in _STRONG_PUNC:
            # 強標點：立即換行
            buffer += part
            line = buffer.rstrip("".join(_STRONG_PUNC | _WEAK_PUNC)).strip()
            if line:
                lines.append(line)
            buffer = ""

        elif part in _WEAK_PUNC:
            # 弱標點：視長度決定是否換行
            buffer += part
            if len(buffer) > _LINE_THRESHOLD:
                line = buffer.rstrip(
                    "".join(_STRONG_PUNC | _WEAK_PUNC)
                ).strip()
                if line:
                    lines.append(line)
                buffer = ""

        else:
            buffer += part

    # 處理剩餘
    remainder = buffer.rstrip("".join(_STRONG_PUNC | _WEAK_PUNC)).strip()
    if remainder:
        lines.append(remainder)

    return lines


# ─── Token → Word 轉換 ────────────────────────────────────

def build_words(
    tokens: List[str], timestamps: List[float],
) -> List[Word]:
    """
    將 token + timestamp 轉換為 Word 列表。

    每個 Word 有 start 和 end 時間：
    - start = 該 token 的時間戳
    - end = 下一個 token 的時間戳（最後一個預設 +0.2s）

    Args:
        tokens: 識別 token 列表
        timestamps: 對應的時間戳列表

    Returns:
        Word 列表
    """
    if not tokens or not timestamps:
        return []

    n = min(len(tokens), len(timestamps))
    words: List[Word] = []

    for i in range(n):
        clean_text = tokens[i].replace("@", "").replace("@@", "")
        if not clean_text:
            continue

        start = timestamps[i]
        # end = 下一個 token 的 start，或 +0.2s
        if i + 1 < n:
            end = min(timestamps[i + 1], start + 0.5)
        else:
            end = start + 0.2

        words.append(Word(text=clean_text, start=start, end=end))

    return words


# ─── 文字行 → 時間戳對齊 ──────────────────────────────────

def _clean_for_align(text: str) -> str:
    """去除標點和空格，用於對齊比較。"""
    return "".join(c for c in text if c not in _STRIP_CHARS)


def align_lines_to_words(
    lines: List[str], words: List[Word],
) -> List[Tuple[float, float, str]]:
    """
    將分好的文字行對齊到 Word 列表的時間戳。

    算法：
    1. 建立 token 的純文字索引（去標點），每個字元映射回 Word 索引
    2. 建立所有行合併的純文字
    3. SequenceMatcher 全域對齊
    4. 根據對齊結果，為每行找到 start/end 時間

    Args:
        lines: 分行後的文字列表
        words: Word 列表（帶時間戳）

    Returns:
        [(start, end, text), ...] 每行帶時間戳
    """
    if not lines or not words:
        return []

    # 1. 建立 word 的純文字索引
    word_chars = ""
    char_to_word_idx: List[int] = []

    for idx, word in enumerate(words):
        clean = _clean_for_align(word.text)
        for c in clean:
            word_chars += c
            char_to_word_idx.append(idx)

    # 2. 建立所有行的合併純文字
    line_texts = [_clean_for_align(line) for line in lines]
    all_lines_text = "".join(line_texts)

    if not word_chars or not all_lines_text:
        return [(0.0, 0.2, line) for line in lines]

    # 3. SequenceMatcher 全域對齊
    matcher = SequenceMatcher(None, word_chars, all_lines_text)
    matching_blocks = matcher.get_matching_blocks()

    # 建立 lines_text 中每個字元 → word 索引的映射
    line_char_to_word: Dict[int, int] = {}
    for block in matching_blocks:
        word_start, line_start, size = block
        for k in range(size):
            word_char_idx = word_start + k
            line_char_idx = line_start + k
            if word_char_idx < len(char_to_word_idx):
                line_char_to_word[line_char_idx] = (
                    char_to_word_idx[word_char_idx]
                )

    # 4. 為每行計算時間範圍
    result: List[Tuple[float, float, str]] = []
    char_offset = 0

    for i, line in enumerate(lines):
        line_clean_len = len(line_texts[i])

        # 收集此行對應的 word 索引
        found_indices: List[int] = []
        for j in range(line_clean_len):
            global_idx = char_offset + j
            if global_idx in line_char_to_word:
                found_indices.append(line_char_to_word[global_idx])

        if found_indices:
            min_idx = min(found_indices)
            max_idx = max(found_indices)
            start = words[min_idx].start
            end = words[max_idx].end
        else:
            # 回退：用前一行的 end 或 0
            if result:
                start = result[-1][1]
            else:
                start = 0.0
            end = start + 0.5

        result.append((start, end, line))
        char_offset += line_clean_len

    return result


# ─── SRT 格式化 ────────────────────────────────────────────

def format_srt_time(seconds: float) -> str:
    """
    將秒數格式化為 SRT 時間碼。

    格式：HH:MM:SS,mmm

    Args:
        seconds: 秒數

    Returns:
        SRT 時間碼字串
    """
    if seconds < 0:
        seconds = 0.0

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds % 1) * 1000))

    # 防止毫秒溢出
    if millis >= 1000:
        millis = 999

    return _SRT_TIME_FMT.format(hours, minutes, secs, millis)


def generate_srt_content(
    timed_lines: List[Tuple[float, float, str]],
) -> str:
    """
    從帶時間戳的行列表生成 SRT 格式字串。

    Args:
        timed_lines: [(start, end, text), ...]

    Returns:
        完整的 SRT 文件內容
    """
    parts: List[str] = []

    for idx, (start, end, text) in enumerate(timed_lines, 1):
        start_str = format_srt_time(start)
        end_str = format_srt_time(end)
        parts.append(f"{idx}\n{start_str} --> {end_str}\n{text}\n")

    return "\n".join(parts)


# ─── 輸出寫入器 ────────────────────────────────────────────

class OutputWriter:
    """
    轉錄結果輸出寫入器。

    從 token + timestamp 產生三種輸出：
    - SRT：帶時間戳的字幕文件
    - TXT：按標點分行的純文字
    - JSON：原始 token + timestamp 資料

    Args:
        tokens: 識別 token 列表
        timestamps: 對應的時間戳列表（秒）
    """

    def __init__(
        self, tokens: List[str], timestamps: List[float],
    ) -> None:
        self._tokens = list(tokens)
        self._timestamps = list(timestamps)

        # 延遲計算
        self._words: Optional[List[Word]] = None
        self._lines: Optional[List[str]] = None
        self._timed_lines: Optional[List[Tuple[float, float, str]]] = None

    @property
    def full_text(self) -> str:
        """完整的合併文字。"""
        return "".join(
            t.replace("@", "").replace("@@", "") for t in self._tokens
        )

    @property
    def words(self) -> List[Word]:
        """Word 列表（延遲建立）。"""
        if self._words is None:
            self._words = build_words(self._tokens, self._timestamps)
        return self._words

    @property
    def lines(self) -> List[str]:
        """分行後的文字（延遲計算）。"""
        if self._lines is None:
            self._lines = smart_split(self.full_text)
        return self._lines

    @property
    def timed_lines(self) -> List[Tuple[float, float, str]]:
        """帶時間戳的行列表（延遲計算）。"""
        if self._timed_lines is None:
            self._timed_lines = align_lines_to_words(self.lines, self.words)
        return self._timed_lines

    def save_srt(self, path: str | Path) -> None:
        """
        保存 SRT 字幕文件。

        Args:
            path: 輸出路徑
        """
        path = Path(path)
        content = generate_srt_content(self.timed_lines)
        path.write_text(content, encoding="utf-8")
        logger.info("SRT 已保存: %s (%d 條字幕)", path.name, len(self.timed_lines))

    def save_txt(self, path: str | Path) -> None:
        """
        保存 TXT 文本文件（按標點分行）。

        Args:
            path: 輸出路徑
        """
        path = Path(path)
        content = "\n".join(self.lines)
        path.write_text(content, encoding="utf-8")
        logger.info("TXT 已保存: %s (%d 行)", path.name, len(self.lines))

    def save_json(self, path: str | Path) -> None:
        """
        保存 JSON 原始資料（timestamps + tokens）。

        用於後續重新生成 SRT（使用者可以手動編輯 TXT 後重新對齊）。

        Args:
            path: 輸出路徑
        """
        path = Path(path)
        data = {
            "timestamps": self._timestamps,
            "tokens": self._tokens,
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("JSON 已保存: %s", path.name)
