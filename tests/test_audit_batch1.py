"""
審查修復 Batch 1：R1 / R2 / R6 / R10

R1  糾錯規則盲替換單字（架 → 㗎 把「打架」改成「打㗎」）
R2  中文數字轉換破壞成語（一五一十 → 151十）
R6  gui/main_window.py 缺 from pathlib import Path
R10 Google API Key 放進 URL query 並洩漏進 log
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.text_processor import chinese_to_number
from hotword.rectify import RectifyPair, RectifyStore


# ═══════════════════════════════════════════════════════════
# R1：糾錯替換
# ═══════════════════════════════════════════════════════════

def _store_with(pairs: list[tuple[str, str]]) -> RectifyStore:
    store = RectifyStore()
    store._pairs = tuple(RectifyPair(wrong=w, right=r) for w, r in pairs)
    return store


class TestR1RectifyApply:
    def test_defaults_file_has_no_active_single_char_rule(self) -> None:
        """出廠糾錯檔不得包含未註解的單字盲替換規則。"""
        path = Path(__file__).parent.parent / "hotword" / "defaults" / "hot-rectify.txt"
        store = RectifyStore()
        store.load(path)

        single = [p for p in store.pairs if len(p.wrong) == 1]
        assert single == [], f"預設檔仍有單字規則: {single}"

    def test_apply_does_not_cascade(self) -> None:
        """前一條規則的產物不得被後一條規則再次替換。"""
        store = _store_with([("語音識別", "語音辨識"), ("辨識", "識別")])
        assert store.apply("這個語音識別很準") == "這個語音辨識很準"

    def test_apply_prefers_longest_match(self) -> None:
        """重疊規則以最長者優先，與檔案順序無關。"""
        store = _store_with([("州州", "洲洲"), ("州州語音", "州州語音輸入")])
        assert store.apply("我用州州語音") == "我用州州語音輸入"

    def test_apply_is_single_pass_over_original_text(self) -> None:
        """互換型規則不得互相吃掉對方的結果。"""
        store = _store_with([("甲", "乙"), ("乙", "甲")])
        assert store.apply("甲乙") == "乙甲"

    def test_apply_still_replaces_normal_pairs(self) -> None:
        store = _store_with([("全灣", "荃灣"), ("葵湧", "葵涌")])
        assert store.apply("我住全灣，返工去葵湧") == "我住荃灣，返工去葵涌"

    def test_apply_returns_text_unchanged_when_no_rule_hits(self) -> None:
        store = _store_with([("全灣", "荃灣")])
        assert store.apply("今日天氣唔錯") == "今日天氣唔錯"

    def test_apply_with_no_pairs(self) -> None:
        assert RectifyStore().apply("原文") == "原文"

    def test_apply_escapes_regex_metacharacters(self) -> None:
        """wrong 含正則特殊字元時仍當純文字處理。"""
        store = _store_with([("C++", "C 加加"), ("a.b", "AB")])
        assert store.apply("我學C++同axb") == "我學C 加加同axb"


# ═══════════════════════════════════════════════════════════
# R2：中文數字轉換
# ═══════════════════════════════════════════════════════════

class TestR2IdiomProtection:
    @pytest.mark.parametrize(
        "text",
        [
            "一五一十",
            "十之八九",
            "三三兩兩",
            "三三两两",
            "七上八下",
            "亂七八糟",
            "九牛一毛",
            "五花八門",
            "四面八方",
            "三心二意",
            "不三不四",
            "一乾二淨",
            "橫七豎八",
        ],
    )
    def test_idioms_are_not_converted(self, text: str) -> None:
        assert chinese_to_number(f"他{text}地說") == f"他{text}地說"

    def test_idiom_inside_longer_sentence(self) -> None:
        assert chinese_to_number("我一五一十講咗畀佢聽") == "我一五一十講咗畀佢聽"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("二三十個人", "二三十個人"),
            ("三四天", "三四天"),
            ("十五六個", "十五六個"),
            ("一兩個問題", "一兩個問題"),
        ],
    )
    def test_approximate_numbers_are_kept(self, text: str, expected: str) -> None:
        """概數（相鄰兩個連號數字）不轉換。"""
        assert chinese_to_number(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("幺九二", "192"),
            ("一二三四五", "12345"),
            ("百分之五十", "50%"),
            ("三百五十個", "350個"),
            ("二十三天", "23天"),
            ("一千二百塊", "1200塊"),
        ],
    )
    def test_real_numbers_still_convert(self, text: str, expected: str) -> None:
        assert chinese_to_number(text) == expected

    def test_sentinel_does_not_leak(self) -> None:
        """保護用的哨兵字元不得殘留在輸出。"""
        out = chinese_to_number("一五一十說了三百五十個字")
        assert "\x00" not in out
        assert "一五一十" in out
        assert "350個" in out


# ═══════════════════════════════════════════════════════════
# R6：main_window 缺 Path import
# ═══════════════════════════════════════════════════════════

class TestR6PathImport:
    def test_main_window_imports_path(self) -> None:
        """gui/main_window.py 使用了 Path，必須在模組命名空間內。"""
        import gui.main_window as mw

        assert hasattr(mw, "Path"), "gui.main_window 缺少 Path，拖放與文件轉錄會拋 NameError"

    def test_every_name_used_is_defined(self) -> None:
        """靜態掃描：確認 Path 出現在 import 節點中。"""
        src = (Path(__file__).parent.parent / "gui" / "main_window.py").read_text(
            encoding="utf-8",
        )
        tree = ast.parse(src)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update((a.asname or a.name).split(".")[0] for a in node.names)

        assert "Path" in imported


# ═══════════════════════════════════════════════════════════
# R10：Google API Key 洩漏
# ═══════════════════════════════════════════════════════════

class TestR10ApiKeyRedaction:
    def test_google_key_not_in_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """金鑰必須走 header，不得出現在 URL query。"""
        from llm import model_fetcher

        captured: dict[str, object] = {}

        def fake_get(url, headers, timeout, secret=None):
            captured["url"] = url
            captured["headers"] = headers
            return {"models": []}

        monkeypatch.setattr(model_fetcher, "_http_get_json", fake_get)

        fake_google_key = "FAKE-TEST-ONLY-NOT-REAL-0000000"  # 測試假值，非真實金鑰
        provider = model_fetcher.ProviderInfo(
            key="google",
            name="google",
            api_url="https://generativelanguage.googleapis.com/v1beta",
            api_key=fake_google_key,
            model="gemini",
            enabled=True,
        )
        model_fetcher._fetch_google(provider, 10)

        assert fake_google_key not in str(captured["url"])
        assert captured["headers"]["x-goog-api-key"] == fake_google_key

    def test_http_error_message_redacts_secret(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """底層例外訊息含金鑰時必須先 redact 再 raise。"""
        import urllib3

        from llm import model_fetcher

        fake_key_value = "FAKE-TEST-ONLY-NOT-REAL-0000000"  # 測試假值，非真實金鑰

        def boom(*args, **kwargs):
            raise urllib3.exceptions.HTTPError(
                f"Max retries exceeded with url: /v1beta/models?key={fake_key_value}",
            )

        monkeypatch.setattr(model_fetcher._POOL_MANAGER, "request", boom)

        with pytest.raises(RuntimeError) as exc:
            model_fetcher._http_get_json(
                "https://example.com/v1beta/models", {}, 10, secret=fake_key_value,
            )

        assert fake_key_value not in str(exc.value)
        assert "[REDACTED]" in str(exc.value)

    def test_http_body_error_redacts_secret(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """非 200 回應的 body 也可能回顯金鑰。"""
        from llm import model_fetcher

        fake_key_value = "FAKE-TEST-ONLY-NOT-REAL-0000000"  # 測試假值，非真實金鑰

        class FakeResp:
            status = 400
            data = f'{{"error":"bad key {fake_key_value}"}}'.encode()

        monkeypatch.setattr(
            model_fetcher._POOL_MANAGER, "request", lambda *a, **k: FakeResp(),
        )

        with pytest.raises(RuntimeError) as exc:
            model_fetcher._http_get_json(
                "https://example.com/models", {}, 10, secret=fake_key_value,
            )

        assert fake_key_value not in str(exc.value)

    def test_secret_none_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未傳 secret 時行為不變。"""
        from llm import model_fetcher

        class FakeResp:
            status = 200
            data = b'{"data": []}'

        monkeypatch.setattr(
            model_fetcher._POOL_MANAGER, "request", lambda *a, **k: FakeResp(),
        )
        assert model_fetcher._http_get_json("https://example.com/models", {}, 10) == {
            "data": [],
        }
