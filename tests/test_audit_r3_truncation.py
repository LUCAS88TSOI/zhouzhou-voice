"""
審查修復 R3：LLM 潤色被 max_tokens 截斷後，半截文字照樣當成功貼出

串流路徑完全不解析 finish_reason，`result.text != text` 就判定成功，
半截文字貼進目標應用並存進歷史 —— 內容真的丟了，使用者只會覺得
「後半段講的話憑空消失」。
"""

from __future__ import annotations

import json

import pytest

from llm.client import LLMClient
from llm.processor import LLMResult


def _sse(*payloads: dict) -> list[bytes]:
    lines = [f"data: {json.dumps(p)}".encode() for p in payloads]
    lines.append(b"data: [DONE]")
    return lines


class TestParseSseStreamMeta:
    def test_finish_reason_length_is_recorded(self) -> None:
        meta: dict = {}
        chunks = list(
            LLMClient._parse_sse_stream(
                _sse(
                    {"choices": [{"delta": {"content": "前半"}}]},
                    {"choices": [{"delta": {"content": "後半"}, "finish_reason": "length"}]},
                ),
                meta,
            )
        )
        assert chunks == ["前半", "後半"]
        assert meta["finish_reason"] == "length"

    def test_finish_reason_stop_is_recorded(self) -> None:
        meta: dict = {}
        list(
            LLMClient._parse_sse_stream(
                _sse({"choices": [{"delta": {"content": "完整"}, "finish_reason": "stop"}]}),
                meta,
            )
        )
        assert meta["finish_reason"] == "stop"

    def test_last_non_null_finish_reason_wins(self) -> None:
        """中途 chunk 的 finish_reason 為 null，不得覆蓋最終值。"""
        meta: dict = {}
        list(
            LLMClient._parse_sse_stream(
                _sse(
                    {"choices": [{"delta": {"content": "a"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"content": "b"}, "finish_reason": "length"}]},
                    {"choices": [{"delta": {}, "finish_reason": None}]},
                ),
                meta,
            )
        )
        assert meta["finish_reason"] == "length"

    def test_meta_optional_keeps_backward_compat(self) -> None:
        """不傳 meta 時行為與原本一致。"""
        chunks = list(
            LLMClient._parse_sse_stream(
                _sse({"choices": [{"delta": {"content": "hi"}}]})
            )
        )
        assert chunks == ["hi"]


class TestLLMResultTruncated:
    def test_default_is_not_truncated(self) -> None:
        assert LLMResult().truncated is False

    def test_stream_chat_marks_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from llm.processor import LLMProcessor

        class FakeClient:
            def chat_with_warnings(self, messages, stream=True, meta=None, should_stop=None):
                if meta is not None:
                    meta["finish_reason"] = "length"
                return iter(["半截"]), []

        processor = LLMProcessor.__new__(LLMProcessor)
        processor._client = FakeClient()
        result = processor._stream_chat([], None, None, client=FakeClient())

        assert result.text == "半截"
        assert result.truncated is True

    def test_stream_chat_not_truncated_on_stop(self) -> None:
        from llm.processor import LLMProcessor

        class FakeClient:
            def chat_with_warnings(self, messages, stream=True, meta=None, should_stop=None):
                if meta is not None:
                    meta["finish_reason"] = "stop"
                return iter(["完整"]), []

        processor = LLMProcessor.__new__(LLMProcessor)
        processor._client = FakeClient()
        result = processor._stream_chat([], None, None, client=FakeClient())

        assert result.truncated is False


class TestMaxTokensSafetyNet:
    def test_max_tokens_scales_with_input_length(self) -> None:
        """長逐字稿要自動抬高 max_tokens，否則必被截斷。"""
        from llm.processor import compute_max_tokens

        assert compute_max_tokens(1024, "短句") == 1024
        assert compute_max_tokens(1024, "字" * 2000) > 1024

    def test_max_tokens_has_upper_bound(self) -> None:
        from llm.processor import compute_max_tokens

        assert compute_max_tokens(1024, "字" * 200000) <= 32768

    def test_never_lowers_user_setting(self) -> None:
        from llm.processor import compute_max_tokens

        assert compute_max_tokens(8192, "短句") == 8192
