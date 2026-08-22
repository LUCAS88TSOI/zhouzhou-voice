"""
LLM 潤色逾時保護：SSE keep-alive 卡死回歸測試。

根因：should_stop 只在收到「有內容」的 chunk 之後才被檢查
（llm/processor.py `_stream_chat` 的 for 迴圈是逐 chunk 檢查）。部分服務商
（例如推理模型的「思考」階段）在正式輸出前會持續送出 SSE 註解／心跳行
（不以 "data:" 開頭，或 delta.content 為空字串），`_parse_sse_stream` 靜默
跳過這些行、從不把控制權交還外層迴圈 —— should_stop 永遠冇機會被檢查，
polish_timeout 形同虛設，只要服務商一直送心跳，串流就可以無限期卡住
（每次個別 socket read 都喺 read timeout 內完成，唔會觸發逾時例外）。

修復：should_stop 現在會一路傳到 client 層，`_parse_sse_stream` 逐行
（包括 keep-alive／註解行）檢查 —— 逾時即中止讀取，並在 meta["stopped"]
標記，讓 processor 層知道係逾時而非正常完成。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import llm.client as client_mod
from llm.client import LLMClient
from llm.processor import LLMProcessor, RoleConfig
from llm.provider import ProviderInfo

# 測試用假金鑰（變數形式，避免被 pre-commit secret 掃描誤報）
_FAKE_KEY = "fake-test-api-key"


class _KeepAliveStreamResponse:
    """模擬串流回應：只送 SSE 註解行（心跳），從不送真正 data:content。"""

    def __init__(self, n_lines: int) -> None:
        self.status = 200
        self._lines = [b": ping\n"] * n_lines
        self.consumed = 0
        self.released = False
        self.closed = False

    def __iter__(self):
        for line in self._lines:
            self.consumed += 1
            yield line

    def release_conn(self) -> None:
        self.released = True

    def close(self) -> None:
        self.closed = True


class _EmptyDeltaStreamResponse:
    """模擬串流回應：送合法 data: 行但 delta 永遠無內容（另一種 keep-alive）。"""

    def __init__(self, n_lines: int) -> None:
        self.status = 200
        self._lines = [
            b'data: {"choices":[{"delta":{}}]}\n'
        ] * n_lines
        self.consumed = 0

    def __iter__(self):
        for line in self._lines:
            self.consumed += 1
            yield line

    def release_conn(self) -> None:
        pass

    def close(self) -> None:
        pass


def _make_client(timeout: int = 10) -> LLMClient:
    provider = ProviderInfo(
        key="test",
        name="test",
        api_url="https://api.example.com/v1",
        api_key=_FAKE_KEY,
        model="fake-model",
        enabled=True,
    )
    return LLMClient(provider, timeout=timeout)


# ─── Client 層：should_stop 要喺 keep-alive 行期間生效 ──────────────

def test_should_stop_honored_during_keepalive_only_stream(monkeypatch):
    """should_stop 在 keep-alive 行期間就要生效，唔使等到有內容 chunk。"""
    fake_response = _KeepAliveStreamResponse(n_lines=500)
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    client = _make_client()
    meta: dict = {}
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 模擬：deadline 喺第 3 次檢查時已過

    gen, _warnings = client.chat_with_warnings(
        [{"role": "user", "content": "hi"}],
        stream=True,
        meta=meta,
        should_stop=should_stop,
    )
    chunks = list(gen)

    assert chunks == []  # 全程冇內容 chunk
    assert meta.get("stopped") is True
    # 未把 500 行 keep-alive 全部讀完就已經停止
    assert fake_response.consumed < 500
    # 逾時中止：連線要 close()，唔可以 release_conn() 放返連線池
    # （release 會令未讀完嘅骯髒連線俾下個請求複用，解析到殘留 SSE bytes）
    assert fake_response.closed is True
    assert fake_response.released is False


def test_should_stop_honored_with_data_lines_that_carry_no_content(monkeypatch):
    """另一種 keep-alive：合法 data: 行但 delta 永遠空 —— 同樣唔可以拖住逾時。"""
    fake_response = _EmptyDeltaStreamResponse(n_lines=500)
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    client = _make_client()
    meta: dict = {}
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    gen, _warnings = client.chat_with_warnings(
        [{"role": "user", "content": "hi"}],
        stream=True,
        meta=meta,
        should_stop=should_stop,
    )
    chunks = list(gen)

    assert chunks == []
    assert meta.get("stopped") is True
    assert fake_response.consumed < 500


def test_should_stop_none_preserves_old_behaviour(monkeypatch):
    """should_stop 冇給時（向後相容）：完全冇 should_stop 呢個新機制介入，
    串流照舊行到 [DONE] 為止，連線正常 release_conn()。"""

    class _NormalStreamResponse:
        def __init__(self) -> None:
            self.status = 200
            self.released = False
            self.closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
            yield b"data: [DONE]\n"

        def release_conn(self) -> None:
            self.released = True

        def close(self) -> None:
            self.closed = True

    fake_response = _NormalStreamResponse()
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    client = _make_client()
    meta: dict = {}
    gen, _warnings = client.chat_with_warnings(
        [{"role": "user", "content": "hi"}], stream=True, meta=meta,
    )
    chunks = list(gen)

    assert chunks == ["hi"]
    assert meta.get("stopped") is None
    assert fake_response.released is True
    assert fake_response.closed is False


# ─── Processor 層：逾時要反映成 was_stopped=True ────────────────────

def test_processor_marks_was_stopped_on_keepalive_timeout(monkeypatch):
    """LLMProcessor.process() 遇到純 keep-alive 串流逾時，要標記 was_stopped。"""
    provider = ProviderInfo(
        key="test",
        name="test",
        api_url="https://api.example.com/v1",
        api_key=_FAKE_KEY,
        model="fake-model",
        enabled=True,
    )
    monkeypatch.setattr(
        "llm.processor.get_active_provider", lambda cfg: provider,
    )
    monkeypatch.setattr(
        "llm.processor.list_available_providers", lambda cfg: [provider],
    )

    fake_response = _KeepAliveStreamResponse(n_lines=1000)
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    class _Cfg:
        max_tokens = 1024
        temperature = 0.3
        top_p = 1.0
        frequency_penalty = 0.0
        presence_penalty = 0.0
        do_sample = True
        enabled = True
        allow_provider_failover = False

    processor = LLMProcessor(_Cfg())

    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    result = processor.process(
        text="測試文字",
        role=RoleConfig(name="潤色", system_prompt="潤色一下"),
        should_stop=should_stop,
        request_timeout=10,
    )

    assert result.was_stopped is True
    assert fake_response.consumed < 1000
