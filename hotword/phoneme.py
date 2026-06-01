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


def _segment_text(text: str) -> list[str]:
    """
    將文字按中文/英文區塊分段。

    連續的中文字為一段，連續的英文/數字為一段，
    標點和空白被忽略。

    Args:
        text: 輸入文字

    Returns:
        分段列表
    """
    return _SEGMENT_PATTERN.findall(text)


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
    ngrams: list[str] = []
    for n in range(min_len, min(max_len, len(segment)) + 1):
        for start in range(len(segment) - n + 1):
            ngrams.append(segment[start : start + n])
    return ngrams


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
        英文段落原樣保留。

        Args:
            text: ASR 輸出文字
            threshold: 匹配閾值（0.0 ~ 1.0）

        Returns:
            替換後的文字
        """
        if not self._entries or not text:
            return text

        result = text

        # 對每個中文段落嘗試匹配
        segments = _segment_text(text)

        for segment in segments:
            if not _is_chinese(segment[0]):
                continue

            best = _find_best_match(
                segment, self._entries, self._min_len, self._max_len, threshold,
            )
            if best is not None:
                result = result.replace(best.original, best.matched, 1)

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

def _find_best_match(
    segment: str,
    entries: tuple[PhonemeEntry, ...],
    min_len: int,
    max_len: int,
    threshold: float,
) -> MatchResult | None:
    """
    在一個中文段落中找到最佳熱詞匹配。

    優先選擇：相似度最高 → 長度最長（更精確的匹配）。

    Args:
        segment: 中文文字段落
        entries: 熱詞條目
        min_len: 最短 n-gram
        max_len: 最長 n-gram
        threshold: 匹配閾值

    Returns:
        最佳匹配結果，無匹配則返回 None
    """
    best: MatchResult | None = None

    ngrams = _extract_ngrams(segment, min_len, max_len)

    for ngram in ngrams:
        ngram_pinyin = _text_to_pinyin(ngram)

        for entry in entries:
            # 跳過完全相同的（不需要替換）
            if entry.word == ngram:
                continue

            sim = _compute_similarity(ngram_pinyin, entry.pinyin)

            if sim < threshold:
                continue

            candidate = MatchResult(
                original=ngram,
                matched=entry.word,
                similarity=sim,
            )

            if best is None:
                best = candidate
            elif sim > best.similarity:
                best = candidate
            elif sim == best.similarity and len(ngram) > len(best.original):
                best = candidate

    return best
