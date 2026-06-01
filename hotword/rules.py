"""
州州語音 - 規則替換引擎

從規則文件載入使用者定義的文字替換規則，
支援等值替換（簡單字串替換）和正則表達式替換。

規則文件格式（hot-rule.txt）：
    # 註解行
    愛皮愛 = API            ← 等值替換
    (艾特)\\s*(\\w+) = @\\2  ← 正則替換

設計原則：
- 不可變性：Rule 使用 frozen dataclass
- 安全性：正則編譯失敗不會中斷載入
- 順序性：等值規則先執行，正則規則後執行
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from utils.logger import get_logger

logger = get_logger("hotword.rules")


# ─── 資料結構 ──────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    """單條替換規則。"""
    pattern: str
    replacement: str
    is_regex: bool


@dataclass(frozen=True)
class CompiledRule:
    """已編譯的規則（內部使用）。"""
    rule: Rule
    compiled_pattern: re.Pattern[str] | None


# ─── 內部工具 ──────────────────────────────────────────────

_REGEX_SPECIAL = re.compile(r"[\\()\[\]{}.*+?^$|]")


def _looks_like_regex(pattern: str) -> bool:
    """
    啟發式判斷一個模式是否為正則表達式。

    如果模式包含正則特殊字元（反斜槓、括號、量詞等），
    則視為正則表達式。

    Args:
        pattern: 待判斷的模式字串

    Returns:
        True 表示可能是正則表達式
    """
    return bool(_REGEX_SPECIAL.search(pattern))


def _parse_rule_line(line: str) -> Rule | None:
    """
    解析單行規則文字。

    格式：pattern = replacement
    等號兩邊的空白會被去除。

    Args:
        line: 原始行文字

    Returns:
        Rule 物件，解析失敗返回 None
    """
    # 以第一個 " = " 分割（兩邊有空格的等號）
    # 退而求其次用 "=" 分割
    parts: list[str] | None = None

    if " = " in line:
        idx = line.index(" = ")
        parts = [line[:idx], line[idx + 3:]]
    elif "=" in line:
        idx = line.index("=")
        parts = [line[:idx], line[idx + 1:]]

    if parts is None or len(parts) != 2:
        return None

    pattern = parts[0].strip()
    replacement = parts[1].strip()

    if not pattern:
        return None

    is_regex = _looks_like_regex(pattern)

    return Rule(
        pattern=pattern,
        replacement=replacement,
        is_regex=is_regex,
    )


def _compile_rule(rule: Rule) -> CompiledRule:
    """
    編譯規則。正則規則會預編譯 pattern。

    Args:
        rule: 原始規則

    Returns:
        已編譯的規則
    """
    compiled: re.Pattern[str] | None = None

    if rule.is_regex:
        try:
            compiled = re.compile(rule.pattern)
        except re.error as err:
            logger.warning(
                "正則表達式編譯失敗，將跳過: '%s' — %s",
                rule.pattern, err,
            )

    return CompiledRule(rule=rule, compiled_pattern=compiled)


# ─── 規則引擎 ──────────────────────────────────────────────

class RuleEngine:
    """
    文字替換規則引擎。

    從規則文件載入替換規則，按順序應用到輸入文字：
    1. 先執行所有等值替換（str.replace）
    2. 再執行所有正則替換（re.sub）

    用法：
        engine = RuleEngine()
        engine.load(Path("hot-rule.txt"))
        result = engine.apply("愛皮愛很好用")
        # result == "API很好用"
    """

    def __init__(self) -> None:
        self._equals_rules: tuple[CompiledRule, ...] = ()
        self._regex_rules: tuple[CompiledRule, ...] = ()

    @property
    def rule_count(self) -> int:
        """已載入的規則總數。"""
        return len(self._equals_rules) + len(self._regex_rules)

    def load(self, file_path: Path) -> None:
        """
        從文件載入規則。

        忽略空行和 # 開頭的註解行。
        文件不存在時記錄警告，不拋出異常。

        Args:
            file_path: 規則文件路徑
        """
        if not file_path.exists():
            logger.warning("規則文件不存在，跳過載入: %s", file_path)
            self._equals_rules = ()
            self._regex_rules = ()
            return

        equals: list[CompiledRule] = []
        regex: list[CompiledRule] = []
        skipped = 0

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as err:
            logger.error("讀取規則文件失敗: %s — %s", file_path, err)
            return

        for line_num, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()

            # 跳過空行和註解
            if not line or line.startswith("#"):
                continue

            rule = _parse_rule_line(line)
            if rule is None:
                logger.warning(
                    "規則解析失敗（第 %d 行），已跳過: '%s'",
                    line_num, raw_line,
                )
                skipped += 1
                continue

            compiled = _compile_rule(rule)

            # 正則編譯失敗的跳過
            if rule.is_regex and compiled.compiled_pattern is None:
                skipped += 1
                continue

            if rule.is_regex:
                regex.append(compiled)
            else:
                equals.append(compiled)

        self._equals_rules = tuple(equals)
        self._regex_rules = tuple(regex)

        logger.info(
            "規則載入完成: %d 條等值 + %d 條正則（跳過 %d 條）",
            len(equals), len(regex), skipped,
        )

    def apply(self, text: str) -> str:
        """
        對輸入文字應用所有替換規則。

        執行順序：等值規則 → 正則規則。

        Args:
            text: 輸入文字

        Returns:
            替換後的文字
        """
        if not text:
            return text

        result = text

        # 第一輪：等值替換
        for compiled in self._equals_rules:
            if compiled.rule.pattern in result:
                result = result.replace(
                    compiled.rule.pattern,
                    compiled.rule.replacement,
                )

        # 第二輪：正則替換
        for compiled in self._regex_rules:
            if compiled.compiled_pattern is not None:
                try:
                    result = compiled.compiled_pattern.sub(
                        compiled.rule.replacement, result,
                    )
                except re.error as err:
                    logger.warning(
                        "正則替換執行失敗: '%s' — %s",
                        compiled.rule.pattern, err,
                    )

        return result
