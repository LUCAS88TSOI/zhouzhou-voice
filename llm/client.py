"""
州州語音 - OpenAI 兼容 API 客戶端

使用 urllib3 連線池與任何 OpenAI 兼容端點通信。
支援串流（SSE）和非串流兩種模式。

設計原則：
- 連線池重用：同一 host 的請求復用 TCP+TLS 連線，省去重複握手
- 不可變輸入：ProviderInfo 是 frozen dataclass
- 容錯優先：所有錯誤被捕獲並記錄，永遠不會崩潰調用方
"""

from __future__ import annotations

import json
import socket
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import certifi
import urllib3

from llm.provider import ProviderInfo
from utils.logger import get_logger

logger = get_logger("llm.client")

# ─── 常數 ──────────────────────────────────────────────────

_DEFAULT_TIMEOUT: int = 30
_SSE_DONE_SENTINEL: str = "[DONE]"
_CHAT_COMPLETIONS_PATH: str = "/chat/completions"

# 全局連線池（所有 LLMClient 共享，重用 TCP+TLS 連線）
_POOL_MANAGER: urllib3.PoolManager = urllib3.PoolManager(
    num_pools=4,       # 最多 4 個不同 host 的連線池
    maxsize=2,         # 每個 host 最多 2 個持久連線
    retries=False,     # 不自動重試（LLM 請求不適合重試）
    timeout=urllib3.Timeout(connect=10, read=_DEFAULT_TIMEOUT),
    cert_reqs="CERT_REQUIRED",  # 強制 SSL 證書驗證
    ca_certs=certifi.where(),   # 使用 certifi 的根憑證
)


# ─── 請求/回應結構 ─────────────────────────────────────────

@dataclass(frozen=True)
class ChatRequest:
    """
    封裝一次 Chat Completion 請求的所有參數（不可變）。

    Attributes:
        messages:            對話訊息列表
        model:               模型名稱
        temperature:         採樣溫度
        max_tokens:          最大輸出 token 數
        stream:              是否串流回應
        top_p:               核採樣參數（None = 不發送）
        frequency_penalty:   頻率懲罰（None = 不發送）
        presence_penalty:    存在懲罰（None = 不發送）
        do_sample:           是否採樣（None = 不發送）
        provider_key:        服務商鍵名（容錯重試用）
    """

    messages: tuple[dict[str, str], ...]
    model: str
    temperature: float
    max_tokens: int
    stream: bool = True
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    do_sample: bool | None = None
    provider_key: str = ""

    _OPTIONAL_PARAMS: tuple[str, ...] = (
        "top_p", "frequency_penalty", "presence_penalty", "do_sample",
    )

    def to_payload(self, exclude: frozenset[str] | None = None) -> dict[str, Any]:
        """轉為 API 請求的 JSON body，可排除指定可選參數。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(self.messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
        }
        skip = exclude or frozenset()
        for name in self._OPTIONAL_PARAMS:
            if name in skip:
                continue
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class ChatResponse:
    """
    非串流模式的回應結構（不可變）。

    Attributes:
        content:       模型回覆文字
        total_tokens:  消耗的 token 總數
        finish_reason: 結束原因（如 "stop"、"length"）
    """

    content: str
    total_tokens: int
    finish_reason: str


# ─── 客戶端 ────────────────────────────────────────────────

class LLMClient:
    """
    OpenAI 兼容 API 客戶端（使用 urllib3 連線池）。

    支援任何遵循 OpenAI Chat Completions API 格式的端點，
    包括 SiliconFlow、DeepSeek、Groq 等。

    連線池在模組層級共享，同一 host 的請求復用 TCP+TLS 連線，
    省去每次 100-300ms 的握手開銷。

    用法::

        client = LLMClient(provider, temperature=0.3, max_tokens=1024)

        # 串流模式
        for chunk in client.chat(messages, stream=True):
            print(chunk, end="")

        # 非串流模式
        result = client.chat_sync(messages)
    """

    def __init__(
        self,
        provider: ProviderInfo,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        top_p: float = 1.0,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        do_sample: bool = True,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._frequency_penalty = frequency_penalty
        self._presence_penalty = presence_penalty
        self._do_sample = do_sample
        self._timeout = timeout
        self._endpoint = self._build_endpoint(provider.api_url)
        # iter 3 Bug C：`_param_warnings` 維持存在僅作為 deprecated 相容層
        # （舊呼叫者讀 client.param_warnings），但新的 chat_with_warnings /
        # chat_sync_with_warnings API 各自用獨立 list 收集，避免跨 thread 污染。
        self._param_warnings: list[str] = []

        # 預建構 headers（每次請求重用）
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
        }

        logger.info(
            "LLMClient 初始化: endpoint=%s, model=%s, temp=%.2f, max_tokens=%d",
            self._endpoint,
            provider.model,
            temperature,
            max_tokens,
        )

    @property
    def param_warnings(self) -> list[str]:
        """上次請求中被移除的參數警告。"""
        return list(self._param_warnings)

    # ─── 公開方法 ──────────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, str]],
        stream: bool = True,
    ) -> Generator[str, None, None]:
        """
        串流模式：逐 chunk 產生模型回覆文字。

        iter 3 Bug C：此方法為向後相容包裝；新程式碼應使用
        `chat_with_warnings()` 取得 per-call warnings，避免跨 thread 污染。

        Args:
            messages: 對話訊息列表
            stream:   是否串流（預設 True）

        Yields:
            模型回覆的文字片段

        Raises:
            RuntimeError: HTTP 請求失敗、超時或解析錯誤時拋出，包含具體原因
        """
        gen, warnings = self.chat_with_warnings(messages, stream=stream)
        # 向後相容：warnings 同步寫回 instance state（舊呼叫者讀 param_warnings）
        self._param_warnings = warnings
        yield from gen

    def chat_with_warnings(
        self,
        messages: list[dict[str, str]],
        stream: bool = True,
    ) -> tuple[Generator[str, None, None], list[str]]:
        """
        串流模式 per-call 版本：返回 (generator, warnings_list)。

        warnings_list 是此次呼叫專屬的 list，不存 instance state，
        多 thread 並發呼叫互不污染。

        容錯重試：若 HTTP 400 錯誤，自動移除不支援的可選參數後重試一次；
        重試時的警告會 append 到返回的 warnings_list。

        Returns:
            (generator, warnings_list)
            - generator: 逐 chunk yield 文字
            - warnings_list: 本次呼叫的警告（由此函數主動 append）

        Raises:
            RuntimeError: HTTP 請求失敗、超時或解析錯誤時拋出
        """
        warnings: list[str] = []
        gen = self._stream_chat_impl(messages, stream, warnings)
        return gen, warnings

    def _stream_chat_impl(
        self,
        messages: list[dict[str, str]],
        stream: bool,
        warnings: list[str],
    ) -> Generator[str, None, None]:
        """內部實作：串流 chat，把警告 append 到外部傳入的 list。"""
        request = self._build_chat_request(messages, stream)

        headers = {
            **self._headers,
            "Accept": "text/event-stream",
        }
        payload = json.dumps(request.to_payload()).encode("utf-8")

        try:
            response = _POOL_MANAGER.urlopen(
                "POST",
                self._endpoint,
                body=payload,
                headers=headers,
                preload_content=False,  # 串流：不預載全部內容
                timeout=urllib3.Timeout(
                    connect=10,
                    read=self._timeout,
                ),
            )
        except urllib3.exceptions.HTTPError as err:
            msg = f"網路連線失敗：{err}"
            logger.error(msg)
            raise RuntimeError(msg)
        except (socket.timeout, TimeoutError):
            msg = f"連線逾時（{self._timeout} 秒）"
            logger.error(msg)
            raise RuntimeError(msg)

        # HTTP 400 → 容錯重試（移除不支援的可選參數）
        if response.status == 400:
            error_body = self._read_response_body(response)
            response.release_conn()
            excluded = self._identify_unsupported_params(request, error_body)
            if excluded:
                for p in sorted(excluded):
                    warnings.append(f"已自動移除參數 {p}（服務商不支援）")
                logger.warning("容錯重試：移除參數 %s", excluded)
                retry_payload = json.dumps(request.to_payload(exclude=excluded)).encode("utf-8")
                try:
                    response = _POOL_MANAGER.urlopen(
                        "POST",
                        self._endpoint,
                        body=retry_payload,
                        headers=headers,
                        preload_content=False,
                        timeout=urllib3.Timeout(connect=10, read=self._timeout),
                    )
                except urllib3.exceptions.HTTPError as err:
                    msg = f"網路連線失敗：{err}"
                    logger.error(msg)
                    raise RuntimeError(msg)
                except (socket.timeout, TimeoutError):
                    msg = f"連線逾時（{self._timeout} 秒）"
                    logger.error(msg)
                    raise RuntimeError(msg)

        # 檢查 HTTP 狀態碼
        if response.status != 200:
            body = self._read_response_body(response)
            body = body.replace(self._provider.api_key, "[REDACTED]")
            response.release_conn()

            if response.status == 401:
                msg = "API Key 無效（HTTP 401）"
            elif response.status == 403:
                msg = "權限不足（HTTP 403）"
            elif response.status == 404:
                msg = f"API 端點不存在（HTTP 404）：{self._endpoint}"
            elif response.status == 429:
                msg = "請求過於頻繁或配額用盡（HTTP 429）"
            elif response.status >= 500:
                msg = f"伺服器錯誤（HTTP {response.status}）"
            else:
                msg = f"HTTP {response.status} 錯誤：{body[:200]}"

            logger.error(msg)
            raise RuntimeError(msg)

        try:
            yield from self._parse_sse_stream(response)
        except Exception as err:
            msg = f"串流解析錯誤：{err}"
            logger.error(msg)
            raise RuntimeError(msg)
        finally:
            response.release_conn()

    def chat_sync(self, messages: list[dict[str, str]]) -> str:
        """
        非串流模式：一次性取得完整回覆（向後相容包裝）。

        iter 3 Bug C：新程式碼應使用 `chat_sync_with_warnings()`。

        Returns:
            模型回覆的完整文字，出錯時返回空字串
        """
        content, warnings = self.chat_sync_with_warnings(messages)
        self._param_warnings = warnings  # 向後相容
        return content

    def chat_sync_with_warnings(
        self,
        messages: list[dict[str, str]],
    ) -> tuple[str, list[str]]:
        """
        非串流模式 per-call 版本：返回 (content, warnings_list)。

        warnings_list 是此次呼叫專屬，不存 instance state。
        """
        warnings: list[str] = []
        content = self._chat_sync_impl(messages, warnings)
        return content, warnings

    def _chat_sync_impl(
        self,
        messages: list[dict[str, str]],
        warnings: list[str],
    ) -> str:
        """內部實作：非串流 chat，警告 append 到外部 list。"""
        request = self._build_chat_request(messages, stream=False)

        headers = {
            **self._headers,
            "Accept": "application/json",
        }
        payload = json.dumps(request.to_payload()).encode("utf-8")

        try:
            response = _POOL_MANAGER.urlopen(
                "POST",
                self._endpoint,
                body=payload,
                headers=headers,
                preload_content=True,
                timeout=urllib3.Timeout(
                    connect=10,
                    read=self._timeout,
                ),
            )
        except urllib3.exceptions.HTTPError as err:
            logger.error("HTTP 請求失敗: %s", err)
            return ""
        except (socket.timeout, TimeoutError):
            logger.error("請求超時 (%d 秒): %s", self._timeout, self._endpoint)
            return ""

        # HTTP 400 → 容錯重試
        if response.status == 400:
            error_body = response.data.decode("utf-8", errors="replace")[:500]
            excluded = self._identify_unsupported_params(request, error_body)
            if excluded:
                for p in sorted(excluded):
                    warnings.append(f"已自動移除參數 {p}（服務商不支援）")
                logger.warning("容錯重試：移除參數 %s", excluded)
                retry_payload = json.dumps(request.to_payload(exclude=excluded)).encode("utf-8")
                try:
                    response = _POOL_MANAGER.urlopen(
                        "POST",
                        self._endpoint,
                        body=retry_payload,
                        headers=headers,
                        preload_content=True,
                        timeout=urllib3.Timeout(connect=10, read=self._timeout),
                    )
                except urllib3.exceptions.HTTPError as err:
                    logger.error("HTTP 請求失敗: %s", err)
                    return ""
                except (socket.timeout, TimeoutError):
                    logger.error("請求超時 (%d 秒): %s", self._timeout, self._endpoint)
                    return ""

        if response.status != 200:
            body = response.data.decode("utf-8", errors="replace")[:500]
            body = body.replace(self._provider.api_key, "[REDACTED]")
            logger.error("HTTP %d 錯誤: %s", response.status, body)
            return ""

        try:
            parsed = self._parse_sync_response(response)
            return parsed.content
        except Exception as err:
            logger.error("回應解析失敗: %s", err)
            return ""

    def test_connection(self, timeout: int = 10) -> tuple[bool, str]:
        """
        Send a minimal request to verify the API key and endpoint work.

        Returns:
            (success, message) — human-readable result for the UI.
        """
        request = ChatRequest(
            messages=({"role": "user", "content": "Hi"},),
            model=self._provider.model,
            temperature=0.1,
            max_tokens=8,
            stream=False,
        )

        headers = {
            **self._headers,
            "Accept": "application/json",
        }
        payload = json.dumps(request.to_payload()).encode("utf-8")

        try:
            response = _POOL_MANAGER.urlopen(
                "POST",
                self._endpoint,
                body=payload,
                headers=headers,
                preload_content=True,
                timeout=urllib3.Timeout(connect=10, read=timeout),
            )
        except urllib3.exceptions.HTTPError as err:
            return False, f"連線失敗：{err}"
        except (socket.timeout, TimeoutError):
            return False, f"連線超時（{timeout} 秒）：請檢查網路或 API URL"

        body = response.data.decode("utf-8", errors="replace")
        # 防止 API 回應中意外洩露 API Key
        body = body.replace(self._provider.api_key, "[REDACTED]")

        if response.status == 401:
            return False, f"API Key 無效（HTTP 401）：{body[:120]}"
        if response.status == 403:
            return False, f"權限不足（HTTP 403）：{body[:120]}"
        if response.status == 404:
            return False, "端點不存在（HTTP 404）：確認 API URL 是否正確"
        if response.status == 429:
            return False, "請求過於頻繁（HTTP 429）：請稍後重試"
        if response.status != 200:
            return False, f"HTTP {response.status} 錯誤：{body[:120]}"

        try:
            parsed = self._parse_sync_response(response)
            reply_preview = (
                parsed.content[:50] if parsed.content else "(空回覆)"
            )
            return True, f"連接成功！模型回應：{reply_preview}"
        except Exception as err:
            return False, f"回應解析失敗：{err}"

    # ─── 內部方法 ──────────────────────────────────────────

    def _build_chat_request(
        self,
        messages: list[dict[str, str]],
        stream: bool,
    ) -> ChatRequest:
        """構建包含所有可選參數的 ChatRequest。"""
        return ChatRequest(
            messages=tuple(messages),
            model=self._provider.model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            stream=stream,
            top_p=self._top_p,
            frequency_penalty=self._frequency_penalty,
            presence_penalty=self._presence_penalty,
            do_sample=self._do_sample,
            provider_key=self._provider.key,
        )

    @staticmethod
    def _identify_unsupported_params(
        request: ChatRequest,
        error_body: str,
    ) -> frozenset[str]:
        """
        識別請求中不被服務商支援的可選參數。

        優先嘗試從錯誤訊息中識別具體參數名；
        若無法識別，則根據 PROVIDER_PARAM_SUPPORT 矩陣判斷。
        """
        from utils.config import PROVIDER_PARAM_SUPPORT

        # 收集本次請求中實際發送的可選參數
        sent_optional: set[str] = set()
        for name in ChatRequest._OPTIONAL_PARAMS:
            if getattr(request, name) is not None:
                sent_optional.add(name)

        if not sent_optional:
            return frozenset()

        # 嘗試從錯誤訊息中識別具體參數
        body_lower = error_body.lower()
        mentioned: set[str] = set()
        for name in sent_optional:
            if name in body_lower:
                mentioned.add(name)

        if mentioned:
            return frozenset(mentioned)

        # 無法從錯誤訊息識別 → 用支援矩陣判斷
        supported = PROVIDER_PARAM_SUPPORT.get(request.provider_key)
        if supported is not None:
            unsupported = sent_optional - supported
            if unsupported:
                return frozenset(unsupported)

        # provider_key 未知或全部都在支援集 → 移除所有可選參數
        return frozenset(sent_optional)

    @staticmethod
    def _build_endpoint(api_url: str) -> str:
        """構建完整的 chat/completions URL。"""
        parsed = urlparse(api_url)
        if parsed.scheme != "https":
            raise ValueError(
                f"不支援的 URL 協議：{parsed.scheme!r}（僅允許 https）"
            )
        base = api_url.rstrip("/")
        return f"{base}{_CHAT_COMPLETIONS_PATH}"

    @staticmethod
    def _parse_sse_stream(response: Any) -> Generator[str, None, None]:
        """
        解析 SSE 串流回應。

        SSE 格式：
            data: {"choices": [{"delta": {"content": "hello"}}]}
            data: [DONE]

        每行以 "data: " 開頭，解析 JSON 並提取 delta.content。
        """
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()

            if not line:
                continue

            if not line.startswith("data:"):
                continue

            data_str = line[len("data:"):].strip()

            if data_str == _SSE_DONE_SENTINEL:
                logger.debug("SSE 串流結束: 收到 [DONE]")
                return

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                logger.debug("SSE 行 JSON 解析跳過: %s", data_str[:80])
                continue

            content = _extract_delta_content(data)
            if content:
                yield content

    @staticmethod
    def _parse_sync_response(response: Any) -> ChatResponse:
        """解析非串流回應的完整 JSON。"""
        if hasattr(response, "data"):
            body = response.data.decode("utf-8", errors="replace")
        else:
            body = response.read().decode("utf-8", errors="replace")

        data = json.loads(body)

        choices = data.get("choices", [])
        if not choices:
            logger.warning("API 回應無 choices: %s", body[:200])
            return ChatResponse(
                content="", total_tokens=0, finish_reason="error"
            )

        first_choice = choices[0]
        message = first_choice.get("message", {})
        content = message.get("content", "")
        finish_reason = first_choice.get("finish_reason", "unknown")

        usage = data.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)

        return ChatResponse(
            content=content,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _read_response_body(response: Any) -> str:
        """安全讀取 HTTP 回應 body。"""
        try:
            if hasattr(response, "data"):
                return response.data.decode("utf-8", errors="replace")
            raw = response.read()
            return raw.decode("utf-8", errors="replace")[:500]
        except Exception:
            return "(無法讀取回應內容)"


# ─── 工具函數 ──────────────────────────────────────────────

def _extract_delta_content(data: dict[str, Any]) -> str:
    """
    從 SSE JSON 中提取 delta content。

    支援的路徑: choices[0].delta.content
    """
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return ""

    delta = choices[0].get("delta", {})
    return delta.get("content", "") or ""
