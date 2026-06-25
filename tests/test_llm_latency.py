"""
LLMClient.test_connection 延遲量測測試。

驗證「測試連接」會回傳實際網絡往返耗時（秒），供 UI 顯示並比較
不同模型／供應商的回應速度（揀最快那個）。

涵蓋：
  - 成功路徑：回傳 (True, message, elapsed) 且 elapsed 為非負 float
  - 失敗路徑（HTTP 401）：即使失敗仍回報耗時，方便對比哪個端點慢
  - 逾時路徑：逾時亦回報直到失敗為止的耗時
"""

import json
import os
import sys

import urllib3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import llm.client as client_mod
from llm.client import LLMClient
from llm.provider import ProviderInfo

# 測試用假金鑰（變數形式，避免被 pre-commit secret 掃描誤報）
_FAKE_KEY = "fake-test-api-key"


class _FakeResponse:
    """模擬 urllib3 HTTPResponse（preload_content=True 的非串流回應）。"""

    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")

    def release_conn(self) -> None:  # noqa: D401 - 介面兼容
        pass


def _make_client() -> LLMClient:
    provider = ProviderInfo(
        key="test",
        name="test",
        api_url="https://api.example.com/v1",
        api_key=_FAKE_KEY,
        model="fake-model",
        enabled=True,
    )
    return LLMClient(provider, timeout=10)


# ─── 成功路徑 ──────────────────────────────────────────────

def test_test_connection_returns_elapsed_seconds(monkeypatch):
    ok_payload = {
        "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]
    }
    monkeypatch.setattr(
        client_mod._POOL_MANAGER,
        "urlopen",
        lambda *a, **k: _FakeResponse(200, ok_payload),
    )

    success, message, elapsed = _make_client().test_connection(timeout=10)

    assert success is True
    assert isinstance(message, str) and message
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0


# ─── 失敗路徑（仍要回報耗時）─────────────────────────────────

def test_test_connection_reports_elapsed_on_http_error(monkeypatch):
    monkeypatch.setattr(
        client_mod._POOL_MANAGER,
        "urlopen",
        lambda *a, **k: _FakeResponse(401, {"error": "bad key"}),
    )

    success, message, elapsed = _make_client().test_connection(timeout=10)

    assert success is False
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0


def test_test_connection_reports_elapsed_on_timeout(monkeypatch):
    def _raise_timeout(*a, **k):
        raise urllib3.exceptions.HTTPError("connect timeout")

    monkeypatch.setattr(client_mod._POOL_MANAGER, "urlopen", _raise_timeout)

    success, message, elapsed = _make_client().test_connection(timeout=10)

    assert success is False
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0
