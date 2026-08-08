"""
審查修復 R8：替換規則的「正則猜測」啟發式

問題：hotword/rules.py 用 `_looks_like_regex()` 靠字元猜測使用者輸入是不是正則，
      UI 上完全沒有正則選項或提示，導致：
      - `(笑) = 😄`    → 「他微笑著說(笑)」變成「他微😄著說(😄)」
      - `C++ = C加加`  → 「我學C++很久了」變成「我學C加加++很久了」
      - `Node.js = ..` → 「NodeXjsX測試」被誤改
      另外正則編譯失敗只寫 log 就靜默跳過，使用者不知道規則沒生效。

修法：預設一律純文字比對；要用正則必須加 `re:` 前綴；
      編譯失敗記入 `RuleEngine.invalid_rules` 讓 UI 顯示。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hotword.rules import REGEX_PREFIX, RuleEngine, validate_pattern


# ═══════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════

def _engine_with(tmp_path: Path, lines: list[str]) -> RuleEngine:
    """把規則行寫進暫存檔並載入成 RuleEngine。"""
    path = tmp_path / "hot-rule.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    engine = RuleEngine()
    engine.load(path)
    return engine


# ═══════════════════════════════════════════════════════════
# R8-1：預設純文字，不再猜測正則
# ═══════════════════════════════════════════════════════════

class TestPlainTextByDefault:
    def test_parentheses_pattern_matches_literally(self, tmp_path: Path) -> None:
        """`(笑) = 😄` 只替換字面 (笑)，不得把「笑」當捕獲組。"""
        engine = _engine_with(tmp_path, ["(笑) = 😄"])
        assert engine.apply("他微笑著說(笑)") == "他微笑著說😄"

    def test_plus_quantifier_pattern_matches_literally(self, tmp_path: Path) -> None:
        """`C++ = C加加`：`++` 在 Python 3.11+ 是 possessive quantifier，能編譯但語意錯。"""
        engine = _engine_with(tmp_path, ["C++ = C加加"])
        assert engine.apply("我學C++很久了") == "我學C加加很久了"

    def test_dot_pattern_matches_literally(self, tmp_path: Path) -> None:
        """`Node.js = NodeJS`：點號不得當萬用字元。"""
        engine = _engine_with(tmp_path, ["Node.js = NodeJS"])
        assert engine.apply("NodeXjsX測試") == "NodeXjsX測試"
        assert engine.apply("Node.js很好") == "NodeJS很好"

    @pytest.mark.parametrize(
        "pattern,replacement,text,expected",
        [
            ("a*b", "AB", "aaab 同 a*b", "aaab 同 AB"),
            ("[粵語]", "廣東話", "這是[粵語]測試", "這是廣東話測試"),
            ("$100", "一百蚊", "俾$100佢", "俾一百蚊佢"),
            ("2^3", "八", "2^3等於八", "八等於八"),
            (r"C:\temp", "暫存夾", r"打開C:\temp", "打開暫存夾"),
            ("a|b", "AB", "a|b 而唔係 a", "AB 而唔係 a"),
        ],
    )
    def test_regex_metacharacters_are_literal(
        self, tmp_path: Path, pattern: str, replacement: str,
        text: str, expected: str,
    ) -> None:
        engine = _engine_with(tmp_path, [f"{pattern} = {replacement}"])
        assert engine.apply(text) == expected

    def test_heuristic_helper_is_gone(self) -> None:
        """啟發式猜測函式必須被移除，避免日後又被接回去。"""
        import hotword.rules as rules_mod

        assert not hasattr(rules_mod, "_looks_like_regex")

    def test_existing_plain_rules_unchanged(self, tmp_path: Path) -> None:
        """既有的純文字規則（出廠粵語規則）行為不變。"""
        engine = _engine_with(
            tmp_path,
            [
                "# 註解行",
                "",
                "愛皮愛 = API",
                "架喎 = 㗎喎",
                "taxi = 的士",
            ],
        )
        assert engine.rule_count == 3
        assert engine.apply("今日用愛皮愛叫taxi架喎") == "今日用API叫的士㗎喎"

    def test_empty_text_returns_unchanged(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, ["愛皮愛 = API"])
        assert engine.apply("") == ""


# ═══════════════════════════════════════════════════════════
# R8-2：re: 前綴才是正則
# ═══════════════════════════════════════════════════════════

class TestExplicitRegexPrefix:
    def test_prefix_constant_is_exported(self) -> None:
        assert REGEX_PREFIX == "re:"

    def test_prefixed_rule_works_as_regex(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, [r"re:\d+ = N"])
        assert engine.apply("有123個同45隻") == "有N個同N隻"

    def test_prefix_is_stripped_from_pattern(self, tmp_path: Path) -> None:
        """`re:` 前綴後的內容才是 pattern，前綴本身不參與比對。"""
        engine = _engine_with(tmp_path, [r"re:\d+ = N"])
        assert engine.apply("re:123") == "re:N"

    def test_regex_backreference_replacement(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, [r"re:(艾特)\s*(\w+) = @\2"])
        assert engine.apply("艾特 lucas 你好") == "@lucas 你好"

    def test_prefixed_pattern_with_space_after_prefix(self, tmp_path: Path) -> None:
        """`re: \\d+` 也應該被接受（前綴後的空白會被去掉）。"""
        engine = _engine_with(tmp_path, [r"re: \d+ = N"])
        assert engine.apply("有123個") == "有N個"

    def test_plain_and_regex_rules_coexist(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, ["C++ = C加加", r"re:\d+ = N"])
        assert engine.apply("我學C++學了3年") == "我學C加加學了N年"
        assert engine.rule_count == 2


# ═══════════════════════════════════════════════════════════
# R8-3：壞正則不再靜默跳過
# ═══════════════════════════════════════════════════════════

class TestInvalidRulesAreReported:
    def test_broken_regex_recorded(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, ["re:[ = X"])
        assert len(engine.invalid_rules) == 1
        line, message = engine.invalid_rules[0]
        assert "re:[" in line
        assert message  # 必須有可讀的錯誤訊息

    def test_broken_regex_does_not_break_other_rules(self, tmp_path: Path) -> None:
        engine = _engine_with(
            tmp_path,
            ["re:[ = X", "愛皮愛 = API", r"re:\d+ = N"],
        )
        assert engine.apply("愛皮愛用了3次") == "API用了N次"
        assert len(engine.invalid_rules) == 1

    def test_invalid_rule_not_counted(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, ["re:[ = X", "愛皮愛 = API"])
        assert engine.rule_count == 1

    def test_invalid_rules_empty_when_all_good(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, ["愛皮愛 = API", r"re:\d+ = N"])
        assert engine.invalid_rules == []

    def test_invalid_rules_reset_on_reload(self, tmp_path: Path) -> None:
        """重新載入乾淨的檔案後，舊的失敗記錄必須清空。"""
        path = tmp_path / "hot-rule.txt"
        path.write_text("re:[ = X", encoding="utf-8")
        engine = RuleEngine()
        engine.load(path)
        assert len(engine.invalid_rules) == 1

        path.write_text("愛皮愛 = API", encoding="utf-8")
        engine.load(path)
        assert engine.invalid_rules == []

    def test_missing_file_clears_invalid_rules(self, tmp_path: Path) -> None:
        engine = _engine_with(tmp_path, ["re:[ = X"])
        engine.load(tmp_path / "not-exist.txt")
        assert engine.invalid_rules == []
        assert engine.rule_count == 0

    def test_malformed_line_recorded(self, tmp_path: Path) -> None:
        """缺少等號的行也要讓使用者知道。"""
        engine = _engine_with(tmp_path, ["這行沒有等號", "愛皮愛 = API"])
        assert len(engine.invalid_rules) == 1
        assert engine.apply("愛皮愛") == "API"


# ═══════════════════════════════════════════════════════════
# R8-4：validate_pattern（供 UI 呼叫）
# ═══════════════════════════════════════════════════════════

class TestValidatePattern:
    @pytest.mark.parametrize("pattern", ["愛皮愛", "C++", "(笑)", "Node.js", r"re:\d+"])
    def test_valid_patterns_return_none(self, pattern: str) -> None:
        assert validate_pattern(pattern) is None

    @pytest.mark.parametrize("pattern", ["re:[", "re:(未關閉", "re:*", "re:"])
    def test_invalid_patterns_return_message(self, pattern: str) -> None:
        msg = validate_pattern(pattern)
        assert isinstance(msg, str) and msg

    def test_empty_pattern_returns_message(self) -> None:
        assert validate_pattern("   ") is not None


# ═══════════════════════════════════════════════════════════
# R8-5：GUI（hotword_tab）
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def qapp():
    """取得（或建立）QApplication；無 PySide6 時跳過。"""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("未安裝 PySide6")
    return QApplication.instance() or QApplication(sys.argv)


class FakeManager:
    """最小可用的 HotwordManager 替身。"""

    def __init__(self, rules: list[str], invalid: list[tuple[str, str]]) -> None:
        self._rules = list(rules)
        self.added: list[tuple[str, str]] = []
        self.removed: list[str] = []

        class _Engine:
            invalid_rules = list(invalid)

        self._rule_engine = _Engine()

    hotword_count = 0
    rectify_count = 0

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def get_hotwords(self) -> list[str]:
        return []

    def get_rules(self) -> list[str]:
        return list(self._rules)

    def get_rectify_pairs(self) -> list[str]:
        return []

    def add_rule(self, pattern: str, replacement: str) -> None:
        self.added.append((pattern, replacement))
        self._rules.append(f"{pattern} = {replacement}")

    def remove_rule(self, line: str) -> bool:
        self.removed.append(line)
        return True


@pytest.fixture
def tab(qapp):
    from gui.widgets.hotword_tab import HotwordTab
    from utils.config import HotwordConfig

    widget = HotwordTab(HotwordConfig())
    yield widget
    widget.deleteLater()


class TestHotwordTabRegexUX:
    def test_hint_label_explains_prefix(self, tab) -> None:
        """UI 要告訴使用者預設純文字、要正則加 re: 前綴。"""
        from PySide6.QtWidgets import QLabel

        texts = [w.text() for w in tab.findChildren(QLabel)]
        joined = "".join(texts)
        assert "純文字" in joined
        assert "re:" in joined

    def test_bad_regex_rejected_with_message_box(
        self, tab, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gui.widgets import hotword_tab as mod

        manager = FakeManager(rules=[], invalid=[])
        tab.set_manager(manager)

        shown: list[str] = []
        monkeypatch.setattr(
            mod.QMessageBox, "warning",
            lambda *args, **kwargs: shown.append(str(args[2])),
        )

        tab._rule_pattern_input.setText("re:[")
        tab._rule_replace_input.setText("X")
        tab._on_add_rule()

        assert manager.added == []
        assert len(shown) == 1
        # 輸入不得被清空，讓使用者可以修正
        assert tab._rule_pattern_input.text() == "re:["

    def test_good_regex_accepted(self, tab, monkeypatch: pytest.MonkeyPatch) -> None:
        from gui.widgets import hotword_tab as mod

        manager = FakeManager(rules=[], invalid=[])
        tab.set_manager(manager)
        monkeypatch.setattr(
            mod.QMessageBox, "warning", lambda *a, **k: pytest.fail("不應彈窗"),
        )

        tab._rule_pattern_input.setText(r"re:\d+")
        tab._rule_replace_input.setText("N")
        tab._on_add_rule()

        assert manager.added == [(r"re:\d+", "N")]
        assert tab._rule_pattern_input.text() == ""

    def test_plain_pattern_with_metachars_accepted(
        self, tab, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """沒有 re: 前綴的 `(笑)` 是純文字，不得被當壞正則擋下。"""
        from gui.widgets import hotword_tab as mod

        manager = FakeManager(rules=[], invalid=[])
        tab.set_manager(manager)
        monkeypatch.setattr(
            mod.QMessageBox, "warning", lambda *a, **k: pytest.fail("不應彈窗"),
        )

        tab._rule_pattern_input.setText("(笑)")
        tab._rule_replace_input.setText("😄")
        tab._on_add_rule()

        assert manager.added == [("(笑)", "😄")]

    def test_invalid_rule_marked_in_list(self, tab) -> None:
        manager = FakeManager(
            rules=["愛皮愛 = API", "re:[ = X"],
            invalid=[("re:[ = X", "unterminated character set")],
        )
        tab.set_manager(manager)

        items = [tab._rule_list.item(i) for i in range(tab._rule_list.count())]
        texts = [it.text() for it in items]

        assert any("規則無效" in t for t in texts)
        assert not any("規則無效" in t for t in texts if t.startswith("愛皮愛"))

    def test_remove_uses_original_line_not_marked_text(self, tab) -> None:
        """標記過的項目被刪除時，送給 manager 的必須是原始規則行。"""
        manager = FakeManager(
            rules=["re:[ = X"],
            invalid=[("re:[ = X", "unterminated character set")],
        )
        tab.set_manager(manager)

        tab._rule_list.setCurrentRow(0)
        tab._on_remove_rule()

        assert manager.removed == ["re:[ = X"]
