"""
稽核 R11 / R12 — 檔案轉錄邊界去重 與 SRT 無標點保底切分

R11: transcribe/file_transcriber.py — merge_segment_tokens()
     段落邊界只做「最長精確前後綴」比對；ASR 對重疊區常差一兩個同音字
     （「再」vs「在」），此時最長重疊變 0，整段重疊文字被原封追加，
     稿子每分鐘結巴重講一次。改用 SequenceMatcher 模糊比對。

R12: transcribe/srt_writer.py — smart_split() / timed_lines
     只靠標點切分，無標點保底。sensevoice-yue-int8（use_itn=False）輸出
     完全沒有標點 → 740 字整份 SRT 只產出一條字幕，時間軸橫跨 3 分半。
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════
# R11 — 段落邊界模糊去重
# ═══════════════════════════════════════════════════════════


def _seg(offset, tokens, timestamps):
    """建立一個 SegmentResult（timestamps 為段內 local 時間）。"""
    from transcribe.file_transcriber import SegmentResult

    return SegmentResult(
        offset=offset,
        text="".join(tokens),
        tokens=list(tokens),
        timestamps=list(timestamps),
        duration=(timestamps[-1] if timestamps else 0.0),
    )


class TestR11FuzzyOverlapDedupe:
    """重疊區去重不得因一兩個同音字之差而完全失效。"""

    def test_homophone_diff_does_not_duplicate_overlap(self):
        """
        prev 尾部「今日再講一次」vs curr 頭部「今日在講一次」只差一個同音字。
        精確前後綴比對會得出 max_overlap == 0，整段重疊被重複追加。
        修復後：重疊區應整段丟棄，只保留 curr 的新內容。
        """
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(
            0.0,
            ["我", "哋", "今", "日", "再", "講", "一", "次"],
            [8.0, 8.5, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5],
        )
        curr = _seg(
            10.0,
            ["今", "日", "在", "講", "一", "次", "多", "謝"],
            [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 4.5, 5.0],
        )

        merged_text, tokens, ts = merge_segment_tokens([prev, curr], overlap=4.0)

        assert merged_text == "我哋今日再講一次多謝", (
            f"R11: 重疊區未去重，結果結巴重講: '{merged_text}'"
        )
        assert merged_text.count("講一次") == 1, "R11: 重疊文字重複出現"
        assert len(tokens) == len(ts), "token 與 timestamp 數量必須一致"
        assert ts == sorted(ts), f"時間戳必須單調不遞減: {ts}"

    def test_multichar_token_homophone_diff(self):
        """多字 token 的重疊區同樣要模糊去重。"""
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(
            0.0,
            ["然後", "我哋", "開始", "錄音"],
            [8.0, 10.0, 11.0, 12.0],
        )
        curr = _seg(
            10.0,
            ["我地", "開始", "錄音", "測試"],
            [0.0, 1.0, 2.0, 5.0],
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=4.0)

        assert merged_text.count("開始") == 1, (
            f"R11: '開始' 重複出現: '{merged_text}'"
        )
        assert merged_text.count("錄音") == 1, (
            f"R11: '錄音' 重複出現: '{merged_text}'"
        )
        assert merged_text.endswith("測試"), f"新內容遺失: '{merged_text}'"

    def test_exact_overlap_behaviour_unchanged(self):
        """精確匹配時的行為必須與原本完全一致。"""
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(0.0, ["AB", "CD"], [0.0, 0.5])
        curr = _seg(0.5, ["CD", "EF"], [0.0, 0.5])
        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.5)
        assert merged_text == "ABCDEF", f"精確匹配行為改變: '{merged_text}'"

    def test_exact_overlap_partial_token_suffix_kept(self):
        """精確匹配落在 token 內部時，非重疊後綴仍須保留。"""
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(0.0, ["ABC"], [0.0])
        curr = _seg(0.0, ["BCD"], [0.0])
        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.5)
        assert merged_text == "ABCD", f"部分重疊後綴遺失: '{merged_text}'"

    def test_exact_single_char_tokens_unchanged(self):
        """單字 token 精確重疊行為不變。"""
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(0.0, list("ABCDE"), [0.0, 0.1, 0.2, 0.3, 0.4])
        curr = _seg(0.2, list("CDEFG"), [0.0, 0.1, 0.2, 0.3, 0.4])
        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.3)
        assert merged_text == "ABCDEFG", f"單字 token 行為改變: '{merged_text}'"

    def test_unrelated_segments_not_truncated(self):
        """兩段完全不相關時不得誤刪 curr 的內容。"""
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(
            0.0,
            ["天", "氣", "真", "係", "好", "好"],
            [10.0, 10.5, 11.0, 11.5, 12.0, 12.5],
        )
        curr = _seg(
            10.0,
            ["股", "票", "市", "場", "波", "動", "好", "大"],
            [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 4.5, 5.0],
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=4.0)

        assert merged_text == "天氣真係好好股票市場波動好大", (
            f"R11: 不相關的段落被誤刪: '{merged_text}'"
        )

    def test_short_overlap_zone_not_dropped_blindly(self):
        """重疊區極短（1-2 字）且無精確匹配時，不得盲目丟棄。"""
        from transcribe.file_transcriber import merge_segment_tokens

        prev = _seg(0.0, ["你", "好"], [9.0, 10.0])
        curr = _seg(10.0, ["係", "唔", "係", "呀"], [0.0, 4.5, 5.0, 5.5])

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=4.0)

        assert "係唔係呀" in merged_text, f"短重疊區內容被誤刪: '{merged_text}'"

    def test_docstring_matches_implementation(self):
        """docstring 聲稱使用 SequenceMatcher，實作必須真的用到。"""
        import inspect

        from transcribe import file_transcriber

        source = inspect.getsource(file_transcriber)
        # SequenceMatcher 必須實際被呼叫，而非只 import 不用
        assert "SequenceMatcher(" in source, (
            "R11: SequenceMatcher 只 import 沒使用，註解與實作不符"
        )


# ═══════════════════════════════════════════════════════════
# R12 — SRT 無標點保底切分
# ═══════════════════════════════════════════════════════════

_POOL = "今天我哋開會討論產品設計同埋市場推廣計劃嘅細節部分需要再確認一次時間安排"


def _no_punc_text(length: int) -> str:
    """產生指定長度、完全沒有標點的中文文字。"""
    return "".join(_POOL[i % len(_POOL)] for i in range(length))


class TestR12SmartSplitFallback:
    """smart_split() 必須有無標點保底。"""

    def test_740_chars_without_punctuation_splits(self):
        """740 字無標點輸入不得只回傳一行。"""
        from transcribe.srt_writer import _MAX_LINE_CHARS, smart_split

        text = _no_punc_text(740)
        lines = smart_split(text)

        assert len(lines) > 1, (
            f"R12: 740 字無標點只切出 {len(lines)} 行（整份 SRT 只有一條字幕）"
        )
        longest = max(len(line) for line in lines)
        assert longest <= _MAX_LINE_CHARS, (
            f"R12: 最長行 {longest} 字，超過上限 {_MAX_LINE_CHARS}"
        )

    def test_force_split_loses_no_characters(self):
        """強制切分不得吞掉任何文字。"""
        from transcribe.srt_writer import smart_split

        text = _no_punc_text(740)
        lines = smart_split(text)
        assert "".join(lines) == text, "R12: 強制切分過程中遺失文字"

    def test_upper_bound_is_reasonable(self):
        """單行上限應落在 20-25 字這個可讀區間。"""
        from transcribe.srt_writer import _MAX_LINE_CHARS

        assert 15 <= _MAX_LINE_CHARS <= 25, (
            f"單行上限 {_MAX_LINE_CHARS} 不在合理區間"
        )

    def test_punctuated_text_split_unchanged(self):
        """有標點的正常輸入切分行為不變。"""
        from transcribe.srt_writer import smart_split

        text = "今天天氣很好，我們一起去公園散步吧。晚上再回來吃飯。"
        assert smart_split(text) == [
            "今天天氣很好，我們一起去公園散步吧",
            "晚上再回來吃飯",
        ]

    def test_short_text_unchanged(self):
        """短句不受保底影響。"""
        from transcribe.srt_writer import smart_split

        assert smart_split("你好嗎？我很好。") == ["你好嗎", "我很好"]

    def test_empty_text(self):
        from transcribe.srt_writer import smart_split

        assert smart_split("") == []

    def test_english_not_split_mid_word(self):
        """英文強制切分時盡量不要切在單字中間。"""
        from transcribe.srt_writer import smart_split

        words = (
            "the quick brown fox jumps over the lazy dog while "
            "everybody watches quietly from a distance today"
        ).split()
        text = " ".join(words * 3)
        lines = smart_split(text)

        assert len(lines) > 1, "長英文句應被切成多行"
        rejoined = " ".join(lines).split()
        assert rejoined == text.split(), (
            f"R12: 英文被切在單字中間或遺失內容\n原: {text.split()[:12]}\n"
            f"後: {rejoined[:12]}"
        )


class TestR12SubtitleDuration:
    """單條字幕時長必須受控。"""

    @staticmethod
    def _writer(chars: str, timestamps: list[float]):
        from transcribe.srt_writer import OutputWriter

        return OutputWriter(list(chars), timestamps)

    def test_no_punctuation_produces_many_subtitles(self):
        """740 字無標點 → SRT 不得只有一條字幕。"""
        text = _no_punc_text(740)
        timestamps = [i * 0.3 for i in range(740)]
        writer = self._writer(text, timestamps)

        assert len(writer.timed_lines) > 1, (
            f"R12: 整份 SRT 只有 {len(writer.timed_lines)} 條字幕"
        )

    def test_single_subtitle_duration_capped(self):
        """任何一條字幕的時長都不得超過 10 秒。"""
        text = _no_punc_text(300)
        # 0.6s 一字（慢速講話）→ 全長 180 秒
        timestamps = [i * 0.6 for i in range(300)]
        writer = self._writer(text, timestamps)

        too_long = [
            (s, e, t) for (s, e, t) in writer.timed_lines if e - s > 10.0
        ]
        assert not too_long, (
            f"R12: {len(too_long)} 條字幕超過 10 秒，最長 "
            f"{max(e - s for s, e, _ in too_long):.1f}s"
        )

    def test_split_point_falls_on_pause(self):
        """有停頓時，二次切分點應落在 words 時間戳缺口最大處。"""
        text = _no_punc_text(20)
        # 前 8 字 0.5s 一個（0.0~3.5），停頓 3 秒，後 12 字由 6.5 起
        timestamps = [i * 0.5 for i in range(8)]
        timestamps += [6.5 + i * 0.5 for i in range(12)]
        writer = self._writer(text, timestamps)

        lines = writer.timed_lines
        assert len(lines) >= 2, f"12 秒的字幕應被切開: {lines}"
        assert lines[0][2] == text[:8], (
            f"R12: 切點未落在停頓上，第一條為 '{lines[0][2]}'（應為 '{text[:8]}'）"
        )
        assert lines[1][0] >= 6.4, (
            f"R12: 第二條起始時間應在停頓之後，實際 {lines[1][0]}"
        )

    def test_punctuated_input_duration_also_capped(self):
        """有標點但語速慢的輸入同樣受時長上限保護。"""
        sentence = "今天我們開會討論產品設計的細節部分需要再確認一次時間安排"
        chars = sentence
        timestamps = [i * 0.5 for i in range(len(chars))]
        writer = self._writer(chars, timestamps)

        for start, end, txt in writer.timed_lines:
            assert end - start <= 10.0, (
                f"字幕 '{txt}' 長 {end - start:.1f}s，超過 10 秒"
            )

    def test_timed_lines_preserve_text(self):
        """二次切分不得遺失文字。"""
        text = _no_punc_text(300)
        timestamps = [i * 0.6 for i in range(300)]
        writer = self._writer(text, timestamps)

        joined = "".join(t for _, _, t in writer.timed_lines)
        assert joined == text, "R12: 二次切分過程中遺失文字"

    def test_timed_lines_are_ordered(self):
        """切分後時間軸必須遞增且 start <= end。"""
        text = _no_punc_text(300)
        timestamps = [i * 0.6 for i in range(300)]
        writer = self._writer(text, timestamps)

        prev_end = -1.0
        for start, end, _ in writer.timed_lines:
            assert start <= end, f"start {start} > end {end}"
            assert start >= prev_end - 1e-6, "字幕時間軸倒退"
            prev_end = end


class TestR12NoPunctuationWarning:
    """來源無標點時應留下 warning log。"""

    def test_save_srt_warns_when_source_has_no_punctuation(self, tmp_path):
        from transcribe import srt_writer

        text = _no_punc_text(200)
        writer = srt_writer.OutputWriter(
            list(text), [i * 0.3 for i in range(200)],
        )

        with mock.patch.object(srt_writer.logger, "warning") as warn:
            writer.save_srt(tmp_path / "out.srt")

        assert warn.called, "R12: 來源無標點時未記錄 warning"

    def test_save_srt_no_warning_when_punctuated(self, tmp_path):
        from transcribe import srt_writer

        chars = list("今天天氣很好，我們一起去公園散步吧。晚上再回來吃飯。" * 4)
        writer = srt_writer.OutputWriter(
            chars, [i * 0.3 for i in range(len(chars))],
        )

        with mock.patch.object(srt_writer.logger, "warning") as warn:
            writer.save_srt(tmp_path / "out.srt")

        assert not warn.called, "R12: 有標點的來源不應發 warning"

    def test_srt_file_has_multiple_cues(self, tmp_path):
        """端到端：無標點來源存出的 .srt 應有多條字幕。"""
        from transcribe.srt_writer import OutputWriter

        text = _no_punc_text(740)
        writer = OutputWriter(list(text), [i * 0.3 for i in range(740)])
        out = tmp_path / "meeting.srt"
        writer.save_srt(out)

        content = out.read_text(encoding="utf-8")
        assert content.count("-->") > 1, (
            f"R12: .srt 只有 {content.count('-->')} 條字幕"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestR12NoEmptyCues:
    """二次切分不得產出只有空白的字幕條（隨機測試找到的邊界）。"""

    def test_whitespace_split_point_does_not_create_empty_cue(self):
        from transcribe.srt_writer import OutputWriter

        # 中英混排 + 慢語速 → 切點容易落在空白帶
        chars = list("今天 我們 開會 討論 產品 設計 的 細節 部分")
        writer = OutputWriter(chars, [i * 0.9 for i in range(len(chars))])

        for start, end, text in writer.timed_lines:
            assert text.strip(), f"產生了空白字幕條: {(start, end, text)!r}"

    def test_randomised_inputs_stay_well_formed(self):
        import random

        from transcribe.srt_writer import OutputWriter

        random.seed(20260808)
        for _ in range(200):
            n = random.randint(1, 300)
            chars = [
                random.choice("今天我們開會討論產品設計，。的細節需要確認abc ")
                for _ in range(n)
            ]
            stamps, t = [], 0.0
            for _ in range(n):
                stamps.append(t)
                t += random.choice([0.05, 0.2, 0.6, 2.0])

            prev_end = -1.0
            for start, end, text in OutputWriter(chars, stamps).timed_lines:
                assert text.strip(), "空白字幕條"
                assert start <= end + 1e-6, "start 大於 end"
                assert start >= prev_end - 1e-6, "時間軸倒退"
                prev_end = end
