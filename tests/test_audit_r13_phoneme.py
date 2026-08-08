"""
Bug R13：每個中文段落只替換一個熱詞

原本 `PhonemeIndex.match()` 對每個中文段落只取「單一最佳匹配」，
再用 `str.replace(original, matched, 1)` 套用；於是：

1. 一句無標點長句（廣東話模型常見）只會替換一個熱詞，其餘原封不動。
2. `str.replace` 是**全文**語意，跨段落時會改到前面段落的同名字串。

修復後：每段貪心挑選一組互不重疊的匹配，回傳原文絕對字元區間，
由 `match()` 從後往前做切片替換。
"""

from __future__ import annotations

import pytest

from hotword.phoneme import PhonemeIndex


def _index(hotwords: list[str]) -> PhonemeIndex:
    """建立並回傳已 build 的音素索引。"""
    index = PhonemeIndex()
    index.build(hotwords)
    return index


# ─── R13-1：同段多個熱詞全部替換 ───────────────────────────

class TestMultipleHotwordsInOneSegment:
    """一個中文段落內的多個熱詞必須全部被替換。"""

    def test_two_different_hotwords_both_replaced(self):
        """「粥粥語音」與「愛皮愛」在同一無標點長句中須同時替換。"""
        index = _index(["州州語音", "愛批愛"])

        result = index.match("我用粥粥語音來做愛皮愛開發")

        assert result == "我用州州語音來做愛批愛開發"

    def test_same_hotword_twice_both_replaced(self):
        """同一個熱詞在一句中出現兩次，兩處都要替換。"""
        index = _index(["州州語音"])

        result = index.match("粥粥語音同粥粥語音")

        assert result == "州州語音同州州語音"

    def test_three_hotwords_in_long_sentence(self):
        """長句中三個熱詞全部替換，且不影響其他文字。"""
        index = _index(["州州語音", "愛批愛"])

        result = index.match("先開粥粥語音再用愛皮愛然後關掉粥粥語音")

        assert result == "先開州州語音再用愛批愛然後關掉州州語音"


# ─── R13-2：重疊匹配只取最佳，且不得字元錯位 ───────────────

class TestOverlappingMatches:
    """匹配區間重疊時只套用最佳者，不得產生重疊替換／字元錯位。"""

    def test_higher_similarity_wins_over_overlapping_candidate(self):
        """低閾值下重疊候選並存時，只套用相似度最高那一個。"""
        # 「粥粥語音」對「州州語音」相似度 1.0，對「州州雨天」只有 0.75
        index = _index(["州州語音", "州州雨天"])

        result = index.match("粥粥語音", threshold=0.7)

        assert result == "州州語音"
        assert len(result) == 4  # 無重複拼接／字元錯位

    def test_longer_match_wins_on_similarity_tie(self):
        """相似度相同時取較長的匹配，較短的重疊候選被跳過。"""
        # 「粥粥語」對「州州雨」相似度 1.0，「粥粥語音」對「州州語音」也是 1.0
        index = _index(["州州語音", "州州雨"])

        result = index.match("我用粥粥語音", threshold=0.8)

        assert result == "我用州州語音"

    def test_no_character_duplication_in_dense_text(self):
        """密集重疊候選下輸出長度須維持穩定，不得吞字或多字。"""
        index = _index(["州州語音"])

        result = index.match("粥粥粥語音", threshold=0.7)

        # 只有一處會被選中（4 字換 4 字），總長度不變
        assert len(result) == len("粥粥粥語音")


# ─── R13-3：跨段落必須替換段內那一處 ───────────────────────

class TestCrossSegmentReplacement:
    """跨段落時替換的必須是段內那一處，而非全文第一處。"""

    def test_second_segment_match_does_not_hit_first_segment(self):
        """
        第一段的「愛皮愛」因為輸給更長的「粥粥語音」而未被舊實作替換，
        舊實作處理第二段時 str.replace 會誤改到第一段那個「愛皮愛」。
        """
        index = _index(["州州語音", "愛批愛"])

        result = index.match("愛皮愛粥粥語音。愛皮愛")

        assert result == "愛批愛州州語音。愛批愛"

    def test_punctuation_separated_segments_all_replaced(self):
        """標點分隔的多個段落，每段的熱詞都要各自替換。"""
        index = _index(["愛批愛"])

        result = index.match("愛皮愛，你好，愛皮愛")

        assert result == "愛批愛，你好，愛批愛"


# ─── R13-4：無匹配 / 既有行為不回歸 ────────────────────────

class TestNoRegression:
    """既有單一匹配與無匹配行為不得改變。"""

    def test_no_match_keeps_text_unchanged(self):
        """完全沒有匹配時原文一字不改。"""
        index = _index(["州州語音"])

        assert index.match("今天天氣很好") == "今天天氣很好"

    def test_single_match_still_works(self):
        """既有的單一匹配行為不回歸。"""
        index = _index(["州州語音"])

        assert index.match("我用粥粥語音") == "我用州州語音"

    def test_exact_hotword_is_untouched(self):
        """文字本身已是熱詞時不做任何替換。"""
        index = _index(["州州語音"])

        assert index.match("我用州州語音") == "我用州州語音"

    def test_empty_index_returns_text(self):
        """索引為空時原樣返回。"""
        assert PhonemeIndex().match("我用粥粥語音") == "我用粥粥語音"

    def test_empty_text_returns_text(self):
        """空字串原樣返回。"""
        assert _index(["州州語音"]).match("") == ""

    def test_english_segment_preserved(self):
        """英文段落原樣保留。"""
        index = _index(["州州語音"])

        result = index.match("open 粥粥語音 now")

        assert result == "open 州州語音 now"

    def test_threshold_semantics_unchanged(self):
        """閾值語意不變：相似度低於閾值不替換。"""
        index = _index(["州州語音"])

        # 「粥粥雨天」對「州州語音」只有 0.5，高閾值下不應替換
        assert index.match("粥粥雨天", threshold=0.85) == "粥粥雨天"

    def test_find_similar_still_reports_candidates(self):
        """find_similar() 公開 API 不受影響。"""
        index = _index(["州州語音"])

        results = index.find_similar("我用粥粥語音", threshold=0.85)

        assert any(r.matched == "州州語音" and r.original == "粥粥語音"
                   for r in results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
