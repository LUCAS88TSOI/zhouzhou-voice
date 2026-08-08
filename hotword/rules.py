"""
州州語音 - 規則替換引擎

從規則文件載入使用者定義的文字替換規則。

規則文件格式（hot-rule.txt）：
    # 註解行
    愛皮愛 = API                ← 純文字替換（預設）
    C++ = C加加                 ← 特殊字元一律當字面處理
    re:(艾特)\\s*(\\w+) = @\\2  ← 正則替換（必須加 re: 前綴）

設計原則：
- 明確優於猜測：預設純文字比對，要用正則必須加 `re:` 前綴
  （舊版靠字元啟發式猜測，會令 `(笑)`、`C++`、`Node.js` 這類規則誤判成正則）
- 不可變性：Rule 使用 frozen dataclass
- 錯誤可見：正則編譯失敗不中斷載入，但會記入 `invalid_rules` 供 UI 顯示
- 順序性：純文字規則先執行，正則規則後執行

註：想比對字面上的 "re:" 開頭文字，暫時只能用正則寫法（例如 `re:re:`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from utils.logger import get_logger

logger = get_logger("hotword.rules")


# 正則規則前綴：pattern 以此開頭才視為正則，前綴後的內容才是 pattern
REGEX_PREFIX = "re:"


# ─── 資料結構 ──────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    """單條替換規則（pattern 已去除 re: 前綴）。"""
    pattern: str
    replacement: str
    is_regex: bool


@dataclass(frozen=True)
class CompiledRule:
    """已編譯的正則規則（內部使用）。"""
    rule: Rule
    compiled_pattern: re.Pattern[str]


# ─── 內部工具 ──────────────────────────────────────────────

def _split_regex_prefix(pattern: str) -> tuple[str, bool]:
    """
    拆出 pattern 本體與「是否正則」。

    Args:
        pattern: 使用者輸入的模式字串

    Returns:
        (pattern 本體, 是否為正則)
    """
    if pattern.startswith(REGEX_PREFIX):
        return pattern[len(REGEX_PREFIX):].strip(), True
    return pattern, False


def validate_pattern(pattern: str) -> str | None:
    """
    驗證使用者輸入的 pattern（供 UI 新增規則前呼叫）。

    純文字 pattern 一律合法；`re:` 開頭的會試編譯。

    Args:
        pattern: 使用者輸入的模式字串（可含 re: 前綴）

    Returns:
        合法返回 None，不合法返回錯誤訊息
    """
    body, is_regex = _split_regex_prefix(pattern.strip())

    if not body:
        return "規則內容不可為空"

    if not is_regex:
        return None

    try:
        re.compile(body)
    except re.error as err:
        return f"正則表達式無效：{err}"

    return None


def _parse_rule_line(line: str) -> Rule | None:
    """
    解析單行規則文字。

    格式：pattern = replacement（等號兩邊空白會被去除）。
    pattern 以 `re:` 開頭時視為正則，其餘一律純文字。

    Args:
        line: 原始行文字

    Returns:
        Rule 物件，解析失敗返回 None
    """
    # 以第一個 " = " 分割（兩邊有空格的等號），退而求其次用 "=" 分割
    if " = " in line:
        idx = line.index(" = ")
        raw_pattern, replacement = line[:idx], line[idx + 3:]
    elif "=" in line:
        idx = line.index("=")
        raw_pattern, replacement = line[:idx], line[idx + 1:]
    else:
        return None

    pattern, is_regex = _split_regex_prefix(raw_pattern.strip())

    if not pattern:
        return None

    return Rule(
        pattern=pattern,
        replacement=replacement.strip(),
        is_regex=is_regex,
    )


# ─── 規則引擎 ──────────────────────────────────────────────

class RuleEngine:
    """
    文字替換規則引擎。

    從規則文件載入替換規則，按順序應用到輸入文字：
    1. 先執行所有純文字替換（str.replace，字面比對）
    2. 再執行所有正則替換（re.sub，僅限 `re:` 前綴的規則）

    無效規則（格式錯誤或正則編譯失敗）不會中斷載入，
    但會記入 `invalid_rules`，讓 UI 提示使用者。

    用法：
        engine = RuleEngine()
        engine.load(Path("hot-rule.txt"))
        result = engine.apply("愛皮愛很好用")
        # result == "API很好用"
    """

    def __init__(self) -> None:
        self._plain_rules: tuple[Rule, ...] = ()
        self._regex_rules: tuple[CompiledRule, ...] = ()
        self._invalid_rules: tuple[tuple[str, str], ...] = ()

    @property
    def rule_count(self) -> int:
        """已生效的規則總數（不含無效規則）。"""
        return len(self._plain_rules) + len(self._regex_rules)

    @property
    def invalid_rules(self) -> list[tuple[str, str]]:
        """無效規則清單：[(原始規則行, 錯誤訊息), ...]。"""
        return list(self._invalid_rules)

    def _reset(self) -> None:
        """清空所有已載入狀態。"""
        self._plain_rules = ()
        self._regex_rules = ()
        self._invalid_rules = ()

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
            self._reset()
            return

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as err:
            logger.error("讀取規則文件失敗: %s — %s", file_path, err)
            self._reset()
            return

        plain: list[Rule] = []
        regex: list[CompiledRule] = []
        invalid: list[tuple[str, str]] = []

        for line_num, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()

            # 跳過空行和註解
            if not line or line.startswith("#"):
                continue

            rule = _parse_rule_line(line)
            if rule is None:
                logger.warning("規則格式錯誤（第 %d 行）: '%s'", line_num, line)
                invalid.append((line, "格式錯誤：應為「匹配詞 = 替換詞」"))
                continue

            if not rule.is_regex:
                plain.append(rule)
                continue

            try:
                compiled = re.compile(rule.pattern)
            except re.error as err:
                logger.warning(
                    "正則編譯失敗（第 %d 行）: '%s' — %s", line_num, line, err,
                )
                invalid.append((line, f"正則表達式無效：{err}"))
                continue

            regex.append(CompiledRule(rule=rule, compiled_pattern=compiled))

        self._plain_rules = tuple(plain)
        self._regex_rules = tuple(regex)
        self._invalid_rules = tuple(invalid)

        logger.info(
            "規則載入完成: %d 條純文字 + %d 條正則（無效 %d 條）",
            len(plain), len(regex), len(invalid),
        )

    def apply(self, text: str) -> str:
        """
        對輸入文字應用所有替換規則。

        執行順序：純文字規則 → 正則規則。

        Args:
            text: 輸入文字

        Returns:
            替換後的文字
        """
        if not text:
            return text

        result = text

        # 第一輪：純文字替換（str.replace 本身就是字面比對，等同 re.escape）
        for rule in self._plain_rules:
            result = result.replace(rule.pattern, rule.replacement)

        # 第二輪：正則替換
        for compiled in self._regex_rules:
            try:
                result = compiled.compiled_pattern.sub(
                    compiled.rule.replacement, result,
                )
            except re.error as err:
                logger.warning(
                    "正則替換執行失敗: '%s' — %s", compiled.rule.pattern, err,
                )

        return result
