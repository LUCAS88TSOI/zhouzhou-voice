"""
州州語音 - 音素匹配引擎

將中文文字轉換為拼音音素，使用餘弦相似度模糊匹配熱詞。
用於修正 ASR 輸出中常見的同音字/近音字錯誤。

設計原則：
- 不可變性：所有資料結構使用 frozen dataclass
- 惰性載入：pypinyin 僅在需要時匯入
- 純函數：匹配邏輯無副作用
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from utils.logger import get_logger

logger = get_logger("hotword.phoneme")


# ─── 資料結構 ──────────────────────────────────────────────

@dataclass(frozen=True)
class PhonemeEntry:
    """單個熱詞的音素記錄。"""
    word: str
    pinyin: tuple[str, ...]

    @property
    def length(self) -> int:
        """拼音音節數。"""
        return len(self.pinyin)


@dataclass(frozen=True)
class MatchResult:
    """匹配結果。"""
    original: str
    matched: str
    similarity: float


@dataclass(frozen=True)
class MatchSpan:
    """
    一處匹配及其在**原文**中的絕對字元區間 [start, end)。

    帶位置資訊才能做切片替換，避免 str.replace 的全文語意誤改別處。
    """
    start: int
    end: int
    original: str
    matched: str
    similarity: float

    def overlaps(self, other: "MatchSpan") -> bool:
        """判斷兩個區間是否有交集（半開區間，相鄰不算重疊）。"""
        return self.start < other.end and other.start < self.end


# ─── 拼音工具函數 ──────────────────────────────────────────

_CHINESE_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def _is_chinese(char: str) -> bool:
    """判斷單個字元是否為中文。"""
    return bool(_CHINESE_CHAR_PATTERN.match(char))


def _text_to_pinyin(text: str) -> tuple[str, ...]:
    """
    將中文文字轉換為拼音序列。

    非中文字元原樣保留（轉小寫）。
    例如: "語音API" → ("yu", "yin", "api")

    Args:
        text: 輸入文字

    Returns:
        拼音元組（不可變）
    """
    from pypinyin import lazy_pinyin

    result: list[str] = []
    buffer = ""

    for char in text:
        if _is_chinese(char):
            # 先沖刷非中文暫存區
            if buffer:
                result.append(buffer.lower().strip())
                buffer = ""
            # 中文字單獨轉拼音
            pinyin_list = lazy_pinyin(char)
            if pinyin_list:
                result.append(pinyin_list[0])
        elif char.isspace():
            # 空白作為分隔，沖刷暫存區
            if buffer:
                result.append(buffer.lower().strip())
                buffer = ""
        else:
            # 英文/數字等累積
            buffer += char

    # 沖刷剩餘暫存區
    if buffer:
        result.append(buffer.lower().strip())

    return tuple(syllable for syllable in result if syllable)


def _compute_similarity(
    source: tuple[str, ...],
    target: tuple[str, ...],
) -> float:
    """
    計算兩個拼音序列的相似度。

    算法：滑動窗口匹配 — 以較短序列在較長序列上滑動，
    找到匹配音節數最多的位置，相似度 = 匹配數 / 較長序列長度。

    Args:
        source: 來源拼音序列
        target: 目標拼音序列

    Returns:
        0.0 ~ 1.0 之間的相似度
    """
    if not source or not target:
        return 0.0

    # 長度相同：直接逐位比較
    if len(source) == len(target):
        matches = sum(1 for s, t in zip(source, target) if s == t)
        return matches / len(source)

    # 以較短序列在較長序列上滑動
    short, long = (
        (source, target) if len(source) <= len(target)
        else (target, source)
    )
    best_matches = 0
    window_size = len(short)

    for offset in range(len(long) - window_size + 1):
        matches = sum(
            1 for i, syllable in enumerate(short)
            if syllable == long[offset + i]
        )
        best_matches = max(best_matches, matches)

    return best_matches / len(long)


# ─── 分詞工具 ──────────────────────────────────────────────

_SEGMENT_PATTERN = re.compile(
    r"([\u4e00-\u9fff]+|[a-zA-Z0-9]+)"
)


def _segment_spans(text: str) -> list[tuple[int, str]]:
    """
    將文字按中文/英文區塊分段，並保留每段在原文的起始位置。

    連續的中文字為一段，連續的英文/數字為一段，
    標點和空白被忽略（因此各段在原文中互不重疊）。

    Args:
        text: 輸入文字

    Returns:
        (原文起始索引, 段落文字) 列表
    """
    return [(m.start(), m.group()) for m in _SEGMENT_PATTERN.finditer(text)]


def _segment_text(text: str) -> list[str]:
    """
    將文字按中文/英文區塊分段。

    Args:
        text: 輸入文字

    Returns:
        分段列表
    """
    return [segment for _, segment in _segment_spans(text)]


def _extract_ngram_spans(
    segment: str, min_len: int, max_len: int,
) -> list[tuple[int, int]]:
    """
    列出段落中所有 n-gram 的字元區間 [start, end)。

    Args:
        segment: 中文文字段落
        min_len: 最短 n-gram 字數
        max_len: 最長 n-gram 字數

    Returns:
        區間列表（按長度遞增、起點遞增排列）
    """
    return [
        (start, start + n)
        for n in range(min_len, min(max_len, len(segment)) + 1)
        for start in range(len(segment) - n + 1)
    ]


def _extract_ngrams(
    segment: str, min_len: int, max_len: int,
) -> list[str]:
    """
    從中文段落中提取所有 n-gram 子串。

    Args:
        segment: 中文文字段落
        min_len: 最短 n-gram 字數
        max_len: 最長 n-gram 字數

    Returns:
        所有可能的 n-gram 子串
    """
    return [
        segment[start:end]
        for start, end in _extract_ngram_spans(segment, min_len, max_len)
    ]


# ─── 音素索引 ──────────────────────────────────────────────

class PhonemeIndex:
    """
    熱詞音素索引。

    維護一份熱詞→拼音的對照表，提供模糊匹配能力。
    索引建立後可重複使用，透過 build() 重建。
    """

    def __init__(self) -> None:
        self._entries: tuple[PhonemeEntry, ...] = ()
        self._min_len: int = 1
        self._max_len: int = 1

    @property
    def size(self) -> int:
        """索引中的熱詞數量。"""
        return len(self._entries)

    @property
    def entries(self) -> tuple[PhonemeEntry, ...]:
        """所有熱詞條目（唯讀）。"""
        return self._entries

    def build(self, hotwords: list[str]) -> None:
        """
        建立音素索引。

        跳過空字串和重複項。每個熱詞轉為拼音並存入索引。

        Args:
            hotwords: 熱詞列表
        """
        seen: set[str] = set()
        entries: list[PhonemeEntry] = []

        for word in hotwords:
            cleaned = word.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)

            pinyin = _text_to_pinyin(cleaned)
            if pinyin:
                entries.append(PhonemeEntry(word=cleaned, pinyin=pinyin))

        self._entries = tuple(entries)

        # 計算 n-gram 範圍
        if entries:
            lengths = [e.length for e in entries]
            self._min_len = max(1, min(lengths))
            self._max_len = max(lengths)

        logger.info("音素索引已建立，共 %d 個熱詞", len(entries))

    def match(self, text: str, threshold: float = 0.85) -> str:
        """
        對輸入文字進行熱詞音素匹配替換。

        將文字分段後，對每個中文段落提取 n-gram，
        與索引中的熱詞比較拼音相似度，超過閾值則替換。
        每段可命中多個互不重疊的熱詞，英文段落原樣保留。

        Args:
            text: ASR 輸出文字
            threshold: 匹配閾值（0.0 ~ 1.0）

        Returns:
            替換後的文字
        """
        if not self._entries or not text:
            return text

        # 收集所有段落的匹配（位置為原文絕對索引，彼此互不重疊）
        spans: list[MatchSpan] = []
        for offset, segment in _segment_spans(text):
            if not _is_chinese(segment[0]):
                continue

            spans.extend(_find_matches(
                segment, offset, self._entries,
                self._min_len, self._max_len, threshold,
            ))

        if not spans:
            return text

        # 從後往前做切片替換：前面的區間索引不受影響
        result = text
        for span in sorted(spans, key=lambda s: s.start, reverse=True):
            result = result[:span.start] + span.matched + result[span.end:]

        return result

    def find_similar(
        self, text: str, threshold: float = 0.6,
    ) -> list[MatchResult]:
        """
        找出輸入文字中與熱詞相似的片段（不替換，僅回報）。

        用於提供 LLM 上下文提示。

        Args:
            text: 輸入文字
            threshold: 相似度閾值

        Returns:
            相似匹配結果列表
        """
        if not self._entries or not text:
            return []

        results: list[MatchResult] = []
        segments = _segment_text(text)

        for segment in segments:
            if not _is_chinese(segment[0]):
                continue

            ngrams = _extract_ngrams(
                segment, self._min_len, self._max_len,
            )
            for ngram in ngrams:
                ngram_pinyin = _text_to_pinyin(ngram)
                for entry in self._entries:
                    if entry.word == ngram:
                        continue
                    sim = _compute_similarity(ngram_pinyin, entry.pinyin)
                    if sim >= threshold:
                        results.append(MatchResult(
                            original=ngram,
                            matched=entry.word,
                            similarity=sim,
                        ))

        # 去重：同一個 original 只保留最高相似度的結果
        best_map: dict[str, MatchResult] = {}
        for r in results:
            key = r.original
            if key not in best_map or r.similarity > best_map[key].similarity:
                best_map[key] = r

        return list(best_map.values())


# ─── 內部匹配函數 ──────────────────────────────────────────

def _find_matches(
    segment: str,
    offset: int,
    entries: tuple[PhonemeEntry, ...],
    min_len: int,
    max_len: int,
    threshold: float,
) -> list[MatchSpan]:
    """
    在一個中文段落中找出一組**互不重疊**的熱詞匹配。

    流程：
    1. 對每個 n-gram 區間取相似度最高的熱詞（同分取索引中較前者），
       達到閾值才成為候選。
    2. 候選按「相似度降序 → 長度降序 → 起點升序」排序後貪心挑選，
       與已選區間重疊者跳過。

    段落中已與熱詞完全相同的 n-gram 不需替換，但仍會佔位，
    避免被相似度較低的模糊匹配覆蓋。

    Args:
        segment: 中文文字段落
        offset: 該段落在原文中的起始索引
        entries: 熱詞條目
        min_len: 最短 n-gram
        max_len: 最長 n-gram
        threshold: 匹配閾值

    Returns:
        需要替換的匹配列表（位置為原文絕對索引，互不重疊）
    """
    pinyin_cache: dict[str, tuple[str, ...]] = {}
    candidates: list[MatchSpan] = []

    for start, end in _extract_ngram_spans(segment, min_len, max_len):
        ngram = segment[start:end]

        if ngram not in pinyin_cache:
            pinyin_cache[ngram] = _text_to_pinyin(ngram)
        ngram_pinyin = pinyin_cache[ngram]

        best_word: str | None = None
        best_sim = 0.0

        for entry in entries:
            # 完全相同：無需替換，但佔位保護（相似度視為滿分）
            if entry.word == ngram:
                best_word, best_sim = ngram, 1.0
                break

            sim = _compute_similarity(ngram_pinyin, entry.pinyin)
            if sim >= threshold and sim > best_sim:
                best_word, best_sim = entry.word, sim

        if best_word is not None:
            candidates.append(MatchSpan(
                start=offset + start,
                end=offset + end,
                original=ngram,
                matched=best_word,
                similarity=best_sim,
            ))

    # 相似度高、覆蓋長的優先；同分時取靠前者，確保結果穩定
    candidates.sort(
        key=lambda c: (-c.similarity, -(c.end - c.start), c.start),
    )

    selected: list[MatchSpan] = []
    for candidate in candidates:
        if any(candidate.overlaps(chosen) for chosen in selected):
            continue
        selected.append(candidate)

    # 過濾佔位用的同字匹配
    return [span for span in selected if span.matched != span.original]
