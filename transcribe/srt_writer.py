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

# 單行字數硬上限：無標點來源（如 sensevoice-yue use_itn=False）靠標點切不開，
# 超過此長度一律強制切分，否則整份 SRT 只會產出一條蓋滿畫面的字幕。
_MAX_LINE_CHARS = 20

# 強制切分時，前段至少要有 limit 的這個比例，避免切出一兩個字的碎行
_MIN_CUT_RATIO = 0.5

# 單條字幕時長上限（秒）；超過則依 words 時間戳缺口二次切分。
# 7 秒是字幕慣例上限（觀眾一眼能讀完），也避免把只超標一點的句子切碎。
_MAX_SUBTITLE_DURATION = 7.0

# 短於此字數的字幕不再二次切分，否則會切出單字碎片
_MIN_SPLIT_CHARS = 6

# 二次切分候選點的邊界比例：只在中間 70% 找停頓，保證遞迴收斂且不切出碎片
_SPLIT_MARGIN_RATIO = 0.15

# 二次切分遞迴深度上限（防禦性；正常內容遠早於此就停）
_MAX_SPLIT_DEPTH = 8

# 判定「來源完全無標點」而發出 warning 的最短字數
_NO_PUNC_WARN_CHARS = 30

_EPS = 1e-6

# 所有分句標點（強 + 弱），供強制切分找切點用
_ALL_PUNC = _STRONG_PUNC | _WEAK_PUNC
_ALL_PUNC_STR = "".join(_ALL_PUNC)

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

def has_sentence_punctuation(text: str) -> bool:
    """文字中是否含任何分句標點（判斷 ASR 是否輸出標點）。"""
    return any(c in _ALL_PUNC for c in text)


def _find_cut_point(line: str, limit: int) -> int:
    """
    在過長的行中找強制切分位置，回傳 line[:cut] 的長度。

    優先順序：行內標點 > 空白（英文單字邊界）> 單字起點 > 硬切。
    切點不會早於 limit 的一半，避免切出碎行。
    """
    min_cut = max(1, int(limit * _MIN_CUT_RATIO))

    # 1. 行內標點：切在標點之後，語意最自然
    for i in range(limit - 1, min_cut - 2, -1):
        if line[i] in _ALL_PUNC:
            return i + 1

    # 2. 空白：英文以此為單字邊界（切點的空白由呼叫方 lstrip 掉）
    for i in range(limit, min_cut - 1, -1):
        if line[i].isspace():
            return i

    # 3. 剛好落在英文單字中間 → 退回該單字起點
    if line[limit - 1].isalnum() and line[limit].isalnum():
        for i in range(limit - 1, min_cut - 1, -1):
            if not line[i - 1].isalnum():
                return i

    # 4. 純中文無標點：硬切
    return limit


def _force_split_line(line: str, limit: int = _MAX_LINE_CHARS) -> List[str]:
    """
    無標點保底：把超過 limit 字的行強制切成多行。

    ASR 若 use_itn=False（如 sensevoice-yue-int8）輸出完全沒有標點，
    僅靠標點切分會讓整份 SRT 只剩一條字幕，故此處按字數兜底。

    Args:
        line: 已按標點切好、但可能仍過長的單行
        limit: 單行字數上限

    Returns:
        切分後的行列表（長度總和不遺失文字）
    """
    if len(line) <= limit:
        return [line] if line else []

    pieces: List[str] = []
    rest = line
    while len(rest) > limit:
        cut = _find_cut_point(rest, limit)
        head = rest[:cut].rstrip(_ALL_PUNC_STR).strip()
        if head:
            pieces.append(head)
        rest = rest[cut:].lstrip()

    tail = rest.rstrip(_ALL_PUNC_STR).strip()
    if tail:
        pieces.append(tail)

    return pieces


def smart_split(text: str) -> List[str]:
    """
    按標點符號將文字智慧分成多行。

    規則：
    - 強標點（。？！.?!）：總是換行
    - 弱標點（，,）：累積超過閾值才換行
    - 去除每行末尾標點
    - 保底：切完仍超過 `_MAX_LINE_CHARS` 字的行按字數強制切分

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

    # 保底：無標點來源靠標點切不開，按字數強制切分
    split_lines: List[str] = []
    for line in lines:
        split_lines.extend(_force_split_line(line))

    return split_lines


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

    return _split_long_cues(result, words)


def _word_span(words: List[Word], start: float, end: float) -> List[Word]:
    """取落在 [start, end] 區間內的 Word（含邊界容差）。"""
    return [
        w for w in words
        if w.start >= start - _EPS and w.end <= end + _EPS
    ]


def _split_long_cues(
    timed_lines: List[Tuple[float, float, str]],
    words: List[Word],
    depth: int = 0,
) -> List[Tuple[float, float, str]]:
    """
    對時長超過上限的字幕條做二次切分，切點落在 words 之間最大的停頓上。

    無標點的 ASR 輸出即使按字數切好行，對齊後仍可能出現橫跨十幾秒的字幕；
    依相鄰 Word 的 end→start 間隔找最大缺口，切點自然落在說話者的停頓處。

    Args:
        depth: 遞迴深度（防禦性上限，正常內容遠早於此就收斂）
    """
    if depth >= _MAX_SPLIT_DEPTH or not words:
        return timed_lines

    out: List[Tuple[float, float, str]] = []
    changed = False

    for start, end, text in timed_lines:
        if end - start <= _MAX_SUBTITLE_DURATION or len(text) < _MIN_SPLIT_CHARS:
            out.append((start, end, text))
            continue

        span = _word_span(words, start, end)
        cut = _find_pause_cut(span) if len(span) >= 2 else None

        if cut is None:
            # 找不到停頓（或無 word 資訊）→ 按字數中分，時間按比例切
            mid = len(text) // 2
            split_time = start + (end - start) * (mid / len(text))
        else:
            cut_index, split_time = cut
            # 文字按 word 比例切，避免時間切了文字沒切
            mid = max(1, min(len(text) - 1, round(len(text) * cut_index / len(span))))

        head, tail = text[:mid].strip(), text[mid:].strip()
        if not head or not tail:
            # 切點落在空白帶，會產出只有空白的字幕條 → 不如不切
            out.append((start, end, text))
            continue

        out.append((start, split_time, head))
        out.append((split_time, end, tail))
        changed = True

    return _split_long_cues(out, words, depth + 1) if changed else out


def _find_pause_cut(span: List[Word]) -> Optional[Tuple[int, float]]:
    """
    在 Word 序列中找最大停頓，回傳 (切點 word 索引, 切分時間)。

    只在中間區段找切點，保證兩側都有內容、遞迴能收斂。
    """
    margin = max(1, int(len(span) * _SPLIT_MARGIN_RATIO))
    lo, hi = margin, len(span) - margin
    if hi <= lo:
        return None

    best_index = lo
    best_gap = -1.0
    for i in range(lo, hi):
        gap = span[i].start - span[i - 1].end
        if gap > best_gap:
            best_gap = gap
            best_index = i

    return best_index, span[best_index].start


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
        full = self.full_text
        # 無標點模型（如 sensevoice-yue use_itn=False）靠標點切不出字幕，
        # 已按字數與停頓強制分行，但要讓用戶知道分行點不是語意邊界
        if len(full) >= _NO_PUNC_WARN_CHARS and not has_sentence_punctuation(full):
            logger.warning(
                "此模型未輸出標點，字幕已按字數與停頓強制分行: %s", path.name,
            )
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
