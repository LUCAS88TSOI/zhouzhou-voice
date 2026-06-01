"""
州州語音 - 糾錯歷史存儲

管理使用者的修正歷史記錄，供 LLM 上下文注入使用。
當 ASR 輸出的文字包含已知錯誤時，提供糾正建議給 LLM。

糾錯文件格式（hot-rectify.txt）：
    # 使用者修正記錄：錯誤 → 正確
    語音識別 → 語音辨識
    人工智慧 → 人工智能

設計原則：
- 不可變性：RectifyPair 使用 frozen dataclass
- 簡單匹配：使用子字串比對，不引入重量級 NLP
- 安全降級：文件不存在或格式錯誤不會中斷運行
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from utils.logger import get_logger

logger = get_logger("hotword.rectify")


# ─── 資料結構 ──────────────────────────────────────────────

@dataclass(frozen=True)
class RectifyPair:
    """一條修正記錄：錯誤 → 正確。"""
    wrong: str
    right: str


# ─── 內部工具 ──────────────────────────────────────────────

_ARROW_SEPARATORS = ("→", "->", "=>")


def _parse_rectify_line(line: str) -> RectifyPair | None:
    """
    解析單行修正記錄。

    支援多種箭頭格式：→、->、=>

    Args:
        line: 原始行文字

    Returns:
        RectifyPair 物件，解析失敗返回 None
    """
    for separator in _ARROW_SEPARATORS:
        if separator in line:
            parts = line.split(separator, maxsplit=1)
            if len(parts) == 2:
                wrong = parts[0].strip()
                right = parts[1].strip()
                if wrong and right:
                    return RectifyPair(wrong=wrong, right=right)
            break

    return None


def _has_overlap(text: str, wrong: str, threshold: float) -> bool:
    """
    判斷輸入文字是否與錯誤字串有足夠的重疊。

    策略：
    1. 完全包含：wrong 是 text 的子字串 → 直接命中
    2. 字元重疊：共同字元數 / wrong 的字元數 >= threshold

    Args:
        text: 輸入文字
        wrong: 錯誤字串
        threshold: 重疊閾值

    Returns:
        True 表示有足夠重疊
    """
    # 完全包含
    if wrong in text:
        return True

    # 字元級重疊
    text_chars = set(text)
    wrong_chars = set(wrong)
    common = text_chars & wrong_chars

    if not wrong_chars:
        return False

    overlap_ratio = len(common) / len(wrong_chars)
    return overlap_ratio >= threshold


# ─── 糾錯存儲 ──────────────────────────────────────────────

class RectifyStore:
    """
    糾錯歷史存儲。

    從文件載入使用者的修正記錄，根據輸入文字
    找出相關的糾正建議，供 LLM 上下文注入。

    用法：
        store = RectifyStore()
        store.load(Path("hot-rectify.txt"))
        context = store.get_context("語音識別很好用", threshold=0.6)
        # context == ['「語音識別」應修正為「語音辨識」']
    """

    def __init__(self) -> None:
        self._pairs: tuple[RectifyPair, ...] = ()

    @property
    def pair_count(self) -> int:
        """已載入的修正記錄數。"""
        return len(self._pairs)

    @property
    def pairs(self) -> tuple[RectifyPair, ...]:
        """所有修正記錄（唯讀）。"""
        return self._pairs

    def load(self, file_path: Path) -> None:
        """
        從文件載入修正記錄。

        忽略空行和 # 開頭的註解行。
        文件不存在時記錄警告，不拋出異常。

        Args:
            file_path: 糾錯文件路徑
        """
        if not file_path.exists():
            logger.warning("糾錯文件不存在，跳過載入: %s", file_path)
            self._pairs = ()
            return

        pairs: list[RectifyPair] = []
        skipped = 0

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as err:
            logger.error("讀取糾錯文件失敗: %s — %s", file_path, err)
            return

        for line_num, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()

            # 跳過空行和註解
            if not line or line.startswith("#"):
                continue

            pair = _parse_rectify_line(line)
            if pair is None:
                logger.warning(
                    "糾錯記錄解析失敗（第 %d 行），已跳過: '%s'",
                    line_num, raw_line,
                )
                skipped += 1
                continue

            pairs.append(pair)

        self._pairs = tuple(pairs)

        logger.info(
            "糾錯記錄載入完成: %d 條（跳過 %d 條）",
            len(pairs), skipped,
        )

    def get_context(
        self, text: str, threshold: float = 0.6,
    ) -> list[str]:
        """
        根據輸入文字找出相關的糾正建議。

        對每條修正記錄，檢查其「錯誤」部分是否與輸入文字
        有足夠的重疊，若有則生成修正提示字串。

        Args:
            text: 輸入文字（ASR 輸出）
            threshold: 字元重疊閾值（0.0 ~ 1.0）

        Returns:
            修正提示列表，如 ['「語音識別」應修正為「語音辨識」']
        """
        if not text or not self._pairs:
            return []

        suggestions: list[str] = []

        for pair in self._pairs:
            if _has_overlap(text, pair.wrong, threshold):
                suggestion = f"「{pair.wrong}」應修正為「{pair.right}」"
                suggestions.append(suggestion)

        if suggestions:
            logger.debug(
                "找到 %d 條相關糾錯建議（輸入: %s...）",
                len(suggestions),
                text[:20],
            )

        return suggestions

    def apply(self, text: str) -> str:
        """
        直接套用糾錯規則到文字（精確字串替換）。

        與 get_context() 不同，此方法直接將文字中的錯誤替換為正確寫法。

        Args:
            text: 要校正的文字

        Returns:
            校正後的文字
        """
        result = text
        for pair in self._pairs:
            if pair.wrong in result:
                result = result.replace(pair.wrong, pair.right)
        return result
