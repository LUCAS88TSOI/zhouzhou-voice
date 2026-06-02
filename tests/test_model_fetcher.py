"""
Tests for:
  A - 供應商模型清單抓取（llm/model_fetcher.py）+ 快取 TTL（llm/model_cache.py）
  B - 潤色逾時：降級鏈在截止後不再嘗試後備 provider（llm/processor.py）
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm import model_fetcher as mf
from llm.provider import ProviderInfo


# ─── A1: 回應解析 ─────────────────────────────────────────

def test_parse_openai_extracts_ids():
    data = {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}, {"no_id": 1}]}
    assert mf._parse_openai(data) == ["gpt-4o-mini", "gpt-4o"]


def test_parse_google_strips_prefix_and_filters_methods():
    data = {"models": [
        {"name": "models/gemini-2.5-flash",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004",
         "supportedGenerationMethods": ["embedContent"]},
    ]}
    assert mf._parse_google(data) == ["gemini-2.5-flash"]


# ─── A2: 智能過濾（只留對話模型）──────────────────────────

def test_is_chat_model_keeps_chat():
    for m in ("gpt-4o-mini", "deepseek-chat", "glm-4-flash", "claude-3-haiku",
              "Qwen/Qwen3-235B-A22B-Instruct"):
        assert mf._is_chat_model(m), m


def test_is_chat_model_filters_non_chat():
    for m in ("text-embedding-3-large", "whisper-1", "tts-1",
              "BAAI/bge-m3", "dall-e-3", "stable-diffusion-xl",
              "cogview-3", "jina-reranker-v2"):
        assert not mf._is_chat_model(m), m


# ─── A3: fetch_models 端到端（mock HTTP）──────────────────

def _prov(key="bigmodel"):
    return ProviderInfo(key=key, name=key, api_url="https://x.test/v1",
                        api_key="sk-test", model="", enabled=True)


def test_fetch_models_filters_sorts_dedups(monkeypatch):
    monkeypatch.setattr(mf, "_http_get_json", lambda *a, **k: {"data": [
        {"id": "glm-4.6"}, {"id": "glm-4.5"}, {"id": "glm-4.5"},
        {"id": "embedding-3"},
    ]})
    assert mf.fetch_models(_prov()) == ["glm-4.5", "glm-4.6"]


def test_fetch_models_empty_after_filter_raises(monkeypatch):
    monkeypatch.setattr(mf, "_http_get_json",
                        lambda *a, **k: {"data": [{"id": "text-embedding-3"}]})
    try:
        mf.fetch_models(_prov())
        assert False, "應拋 RuntimeError"
    except RuntimeError:
        pass


def test_fetch_models_missing_key_raises():
    p = ProviderInfo(key="x", name="x", api_url="https://x.test/v1",
                     api_key="", model="", enabled=True)
    try:
        mf.fetch_models(p)
        assert False
    except RuntimeError as e:
        assert "Key" in str(e)


def test_fetch_models_google_uses_google_adapter(monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return {"models": [{"name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"]}]}

    monkeypatch.setattr(mf, "_http_get_json", fake_get)
    p = ProviderInfo(key="google", name="Google",
                     api_url="https://g.test/v1beta", api_key="AIzaKEY",
                     model="", enabled=True)
    assert mf.fetch_models(p) == ["gemini-2.5-flash"]
    assert "key=AIzaKEY" in seen["url"]  # google 用 query 認證


# ─── A4: 快取 TTL ─────────────────────────────────────────

def test_cache_set_get_roundtrip(tmp_path, monkeypatch):
    from llm import model_cache
    monkeypatch.setattr(model_cache, "_CACHE_FILE", tmp_path / "mc.json")
    model_cache.set("bigmodel", ["glm-4.5", "glm-4.6"])
    models, age = model_cache.get("bigmodel")
    assert models == ["glm-4.5", "glm-4.6"]
    assert age < 5
    assert model_cache.is_stale("bigmodel") is False


def test_cache_stale_when_old(tmp_path, monkeypatch):
    import json
    from llm import model_cache
    cache_file = tmp_path / "mc.json"
    monkeypatch.setattr(model_cache, "_CACHE_FILE", cache_file)
    old_ts = time.time() - (model_cache.CACHE_TTL + 100)
    cache_file.write_text(json.dumps(
        {"bigmodel": {"models": ["glm-4.5"], "ts": old_ts}}), encoding="utf-8")
    assert model_cache.is_stale("bigmodel") is True


def test_cache_missing_is_stale(tmp_path, monkeypatch):
    from llm import model_cache
    monkeypatch.setattr(model_cache, "_CACHE_FILE", tmp_path / "nope.json")
    models, age = model_cache.get("unknown")
    assert models is None
    assert model_cache.is_stale("unknown") is True


# ─── B: 潤色逾時 — 降級鏈在截止後立即中止 ─────────────────

def _make_processor():
    """建立一個 provider 為空的 processor（_init_client 安全跳過）。"""
    from llm.processor import LLMProcessor

    class _LLM:
        active_provider = ""
        providers: dict = {}
        enabled = True

    class _Cfg:
        llm = _LLM()

    return LLMProcessor(_Cfg())


def test_failover_aborts_when_deadline_reached(monkeypatch):
    from llm import processor as proc_mod
    from llm.processor import LLMResult

    p = _make_processor()
    fake_provs = [
        ProviderInfo(key="a", name="a", api_url="https://a.test/v1",
                     api_key="k", model="m", enabled=True),
        ProviderInfo(key="b", name="b", api_url="https://b.test/v1",
                     api_key="k", model="m", enabled=True),
    ]
    monkeypatch.setattr(proc_mod, "list_available_providers",
                        lambda cfg: fake_provs)
    calls = []
    monkeypatch.setattr(p, "_build_client", lambda prov, timeout=None: object())
    monkeypatch.setattr(p, "_stream_chat",
                        lambda **kw: calls.append(1) or LLMResult(error="連線逾時"))

    # should_stop 立即為 True（模擬已逾時）→ 一個後備都不該試
    result = p._failover(
        messages=[], failed_provider=None, first_error="連線逾時",
        on_token=None, should_stop=lambda: True, request_timeout=5,
    )
    assert calls == []
    assert result.error == "連線逾時"


def test_failover_tries_provider_when_time_left(monkeypatch):
    from llm import processor as proc_mod
    from llm.processor import LLMResult

    p = _make_processor()
    fake_provs = [
        ProviderInfo(key="b", name="b", api_url="https://b.test/v1",
                     api_key="k", model="m", enabled=True),
    ]
    monkeypatch.setattr(proc_mod, "list_available_providers",
                        lambda cfg: fake_provs)
    monkeypatch.setattr(p, "_build_client", lambda prov, timeout=None: object())
    monkeypatch.setattr(p, "_stream_chat",
                        lambda **kw: LLMResult(text="ok"))

    result = p._failover(
        messages=[], failed_provider=None, first_error="連線逾時",
        on_token=None, should_stop=lambda: False, request_timeout=5,
    )
    assert result.text == "ok"
    assert not result.error
