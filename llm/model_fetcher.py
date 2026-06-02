"""
州州語音 - 供應商模型清單抓取

呼叫各 LLM 供應商的「列出模型」端點（{api_url}/models），解析出可用模型 id，
並過濾掉非對話模型（embedding / TTS / 語音 / 影像等），供設定面板下拉選擇。

三種 adapter（依 provider.key 分派）：
- openai 相容（預設）：Authorization: Bearer，回應 data[].id
- google：?key= query 認證，回應 models[].name（去 models/ 前綴，僅留可生成內容者）
- anthropic：x-api-key + anthropic-version，回應 data[].id

設計原則：
- 複用 llm.client 的全域連線池（_POOL_MANAGER），同享 TLS 驗證
- 容錯：HTTP/解析錯誤一律 raise RuntimeError(可讀訊息)，由 UI 顯示
"""

from __future__ import annotations

import json
import socket
from typing import Any
from urllib.parse import urlparse

import urllib3

from llm.client import _POOL_MANAGER
from llm.provider import ProviderInfo
from utils.logger import get_logger

logger = get_logger("llm.model_fetcher")

_DEFAULT_TIMEOUT: int = 8

# 非對話模型的識別子串（全小寫比對 model id）。命中即過濾掉。
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed", "embedding", "text-embedding", "bge-", "m3e", "gte-",
    "bce-embedding", "jina-embed",
    "rerank", "reranker",
    "tts", "stt", "whisper", "audio", "speech", "voice",
    "ocr",
    "image", "vision-", "dall-e", "dalle",
    "stable-diffusion", "sdxl", "sd-", "flux",
    "cogview", "cogvideo", "video", "kolors",
    "moderation", "guard",
)


def _is_chat_model(model_id: str) -> bool:
    """以子串黑名單判斷是否對話模型（保守：只濾明確非對話者）。"""
    low = model_id.lower()
    return not any(marker in low for marker in _NON_CHAT_MARKERS)


def _http_get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    """GET 一個 JSON 端點，回傳解析後的 dict（沿用 client 的錯誤訊息風格）。"""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError(f"不支援的 URL 協議：{parsed.scheme!r}（僅允許 https）")

    try:
        resp = _POOL_MANAGER.request(
            "GET", url, headers=headers,
            timeout=urllib3.Timeout(connect=10, read=timeout),
        )
    except urllib3.exceptions.HTTPError as err:
        raise RuntimeError(f"網路連線失敗：{err}")
    except (socket.timeout, TimeoutError):
        raise RuntimeError(f"連線逾時（{timeout} 秒）")

    if resp.status != 200:
        body = resp.data.decode("utf-8", errors="replace")[:200]
        if resp.status == 401:
            raise RuntimeError("API Key 無效（HTTP 401）")
        if resp.status == 403:
            raise RuntimeError("權限不足（HTTP 403）")
        if resp.status == 404:
            raise RuntimeError("此供應商未提供模型清單端點（HTTP 404）")
        if resp.status == 429:
            raise RuntimeError("請求過於頻繁（HTTP 429）")
        if resp.status >= 500:
            raise RuntimeError(f"伺服器錯誤（HTTP {resp.status}）")
        raise RuntimeError(f"HTTP {resp.status} 錯誤：{body}")

    try:
        return json.loads(resp.data.decode("utf-8", errors="replace"))
    except ValueError as err:
        raise RuntimeError(f"模型清單解析失敗：{err}")


def _models_endpoint(api_url: str) -> str:
    """{api_url}/models（去尾斜線）。"""
    return f"{api_url.rstrip('/')}/models"


def _parse_openai(data: dict[str, Any]) -> list[str]:
    """OpenAI 相容：data[].id。"""
    items = data.get("data", [])
    return [str(m.get("id", "")) for m in items if m.get("id")]


def _parse_google(data: dict[str, Any]) -> list[str]:
    """Google：models[].name 去 models/ 前綴；僅留支援 generateContent 者。"""
    out: list[str] = []
    for m in data.get("models", []):
        name = str(m.get("name", ""))
        if not name:
            continue
        methods = m.get("supportedGenerationMethods")
        if methods is not None and "generateContent" not in methods:
            continue
        out.append(name.split("/", 1)[-1] if name.startswith("models/") else name)
    return out


def _fetch_openai_like(provider: ProviderInfo, timeout: int) -> list[str]:
    data = _http_get_json(
        _models_endpoint(provider.api_url),
        {"Authorization": f"Bearer {provider.api_key}"},
        timeout,
    )
    return _parse_openai(data)


def _fetch_google(provider: ProviderInfo, timeout: int) -> list[str]:
    url = f"{_models_endpoint(provider.api_url)}?key={provider.api_key}"
    data = _http_get_json(url, {}, timeout)
    return _parse_google(data)


def _fetch_anthropic(provider: ProviderInfo, timeout: int) -> list[str]:
    data = _http_get_json(
        _models_endpoint(provider.api_url),
        {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout,
    )
    return _parse_openai(data)


def fetch_models(provider: ProviderInfo, timeout: int = _DEFAULT_TIMEOUT) -> list[str]:
    """
    抓取供應商的可用對話模型清單。

    Args:
        provider: 需含 api_url 與 api_key。
        timeout:  read timeout（秒）。

    Returns:
        過濾非對話模型、去重、排序後的模型 id 清單。

    Raises:
        RuntimeError: 缺 key/url、HTTP 失敗、解析失敗。
    """
    if not provider.api_url.strip():
        raise RuntimeError("API URL 為空")
    if not provider.api_key.strip():
        raise RuntimeError("API Key 為空")

    if provider.key == "google":
        raw = _fetch_google(provider, timeout)
    elif provider.key == "anthropic":
        raw = _fetch_anthropic(provider, timeout)
    else:
        raw = _fetch_openai_like(provider, timeout)

    chat = sorted({m for m in raw if m and _is_chat_model(m)})
    logger.info(
        "抓取模型: %s → 原始 %d 個, 過濾後 %d 個對話模型",
        provider.key, len(raw), len(chat),
    )
    if not chat:
        raise RuntimeError("未取得任何對話模型（清單為空或全被過濾）")
    return chat
