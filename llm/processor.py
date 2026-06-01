"""
州州語音 - LLM 處理器

協調 LLM 呼叫流程：角色系統、串流輸出、中途停止控制。
管理對話歷史（多輪對話）和熱詞注入。

設計原則：
- 角色配置使用 frozen dataclass（不可變）
- LLMResult 為可變 dataclass（僅用於收集結果）
- 處理器本身是有狀態的（對話歷史），但所有輸入參數不可變
- 無服務商時優雅降級：返回原始文字
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from llm.client import LLMClient
from llm.provider import (
    ProviderInfo,
    get_active_provider,
    list_available_providers,
)
from utils.logger import get_logger

logger = get_logger("llm.processor")


# ─── 角色配置 ──────────────────────────────────────────────

@dataclass(frozen=True)
class RoleConfig:
    """
    LLM 角色配置（不可變）。

    每個角色定義了 LLM 的行為方式、系統提示詞和輸出模式。

    Attributes:
        name:            角色名稱（如 "潤色"、"翻譯"）
        system_prompt:   系統提示詞
        output_mode:     輸出模式 — "typing"（模擬打字）或 "toast"（通知氣泡）
        enable_history:  是否保留多輪對話歷史
        enable_hotwords: 是否在系統提示詞中注入熱詞上下文
    """

    name: str = "default"
    system_prompt: str = ""
    output_mode: str = "typing"
    enable_history: bool = False
    enable_hotwords: bool = False


# ─── 處理結果 ──────────────────────────────────────────────

@dataclass
class LLMResultStatus:
    """
    LLM 處理結果的結構化狀態。

    用於區分「不可用」、「錯誤」、「成功無變化」三種情況。

    Attributes:
        success:      是否成功處理（無錯誤）
        text:         結果文本（可能與原文相同）
        was_processed: 是否實際送交 LLM 處理
        error:        錯誤訊息（若有）
    """

    success: bool
    text: str
    was_processed: bool
    error: str = ""


@dataclass
class LLMResult:
    """
    一次 LLM 處理的結果。

    使用可變 dataclass，因為結果在串流過程中逐步累積。

    Attributes:
        text:         模型回覆的完整文字
        token_count:  估算的 token 數（基於 chunk 計數）
        elapsed_time: 處理耗時（秒）
        was_stopped:  是否被用戶中途停止
    """

    text: str = ""
    token_count: int = 0
    elapsed_time: float = 0.0
    was_stopped: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)


# ─── 常數 ──────────────────────────────────────────────────

_HOTWORD_INJECT_TEMPLATE: str = (
    "\n\n【專業術語參考】以下是本次對話可能涉及的專有名詞，"
    "請在輸出中使用正確的寫法：\n{hotwords}"
)

_MAX_HISTORY_TURNS: int = 10

# A1：auth/quota/網路類錯誤 → 值得切後備 provider 重試
_FAILOVER_ERROR_MARKERS: tuple[str, ...] = (
    "401", "403", "429", "API Key", "權限不足", "配額",
    "網路連線失敗", "連線逾時", "伺服器錯誤", "HTTP 5",
)
# key 永久失效 → 加入 session 黑名單長期跳過（429/網路屬暫時，不列入）
_PERMANENT_FAILURE_MARKERS: tuple[str, ...] = (
    "401", "403", "API Key", "權限不足",
)


# ─── 處理器 ────────────────────────────────────────────────

class LLMProcessor:
    """
    LLM 處理器 — 協調服務商、角色、串流和對話歷史。

    用法::

        processor = LLMProcessor(app_config.llm)
        result = processor.process(
            text="今天天氣很好",
            role=RoleConfig(name="潤色", system_prompt="修正語音識別的錯字"),
            on_token=lambda t: print(t, end=""),
            should_stop=lambda: keyboard.is_pressed("esc"),
        )
    """

    def __init__(self, config: Any) -> None:
        """
        初始化處理器。

        Args:
            config: LLMConfig 或包含 llm 屬性的 AppConfig
        """
        self._config = config
        self._llm_config = getattr(config, "llm", config)
        self._conversation_history: list[dict[str, str]] = []
        self._client: LLMClient | None = None
        self._provider: ProviderInfo | None = None
        # 保護 _conversation_history / _client / _provider 的並發讀寫。
        # voice / history-reprocess / file-transcribe workers 可同時呼叫 process()，
        # UI thread 同時可 clear_history() / update_config()。
        # RLock 允許同一線程重入（process() 內部呼叫 _append_history 等）。
        self._state_lock = threading.RLock()
        # history epoch：每次 clear_history() 遞增。process() snapshot 時記錄當前 epoch，
        # _append_history 比對 expected_epoch，不一致則 skip（避免 clear 後 in-flight
        # process 仍寫回舊歷史 → 上下文洩漏）。
        self._history_epoch: int = 0
        # A1：運行時 auth 失敗（401/403）的 provider key → 後續降級時跳過
        self._failed_providers: set[str] = set()

        self._init_client()

    # ─── 公開方法 ──────────────────────────────────────────

    def process(
        self,
        text: str,
        role: RoleConfig,
        hotword_context: list[str] | None = None,
        on_token: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> LLMResult:
        """
        處理一段文字：建立訊息、呼叫 LLM、串流回傳結果。

        流程：
        1. 檢查服務商是否可用（不可用則返回原文）
        2. 組裝 system prompt（含熱詞注入）
        3. 加入對話歷史（如啟用）
        4. 串流呼叫 LLM，逐 chunk 回調 on_token
        5. 每個 chunk 間檢查 should_stop
        6. 記錄結果到對話歷史

        Args:
            text:            要處理的原始文字（語音識別結果）
            role:            角色配置
            hotword_context: 熱詞列表（可選）
            on_token:        每收到一個 chunk 時的回調
            should_stop:     檢查是否應中途停止的回調

        Returns:
            LLMResult 包含完整回覆、token 計數、耗時和停止狀態
        """
        # 在 lock 下 snapshot 當前 client / history / epoch，之後不再讀 self.*。
        # 這保證 update_config() / clear_history() 替換或清空時，in-flight 的
        # process() 仍使用請求開始時的狀態完成，不會看到 partial state。
        # epoch snapshot 讓 _append_history 在寫回時能偵測 clear_history 是否
        # 在期間發生（若 epoch 不一致則 skip，防止舊對話洩漏到清空後的歷史）。
        with self._state_lock:
            if not self._is_ready():
                reason = "LLM 未就緒：無可用服務商（請在設定中配置 API Key）"
                logger.warning(reason)
                return LLMResult(text=text, elapsed_time=0.0, error=reason)
            client_snapshot = self._client
            provider_snapshot = self._provider
            history_snapshot = (
                list(self._conversation_history) if role.enable_history else []
            )
            epoch_snapshot = self._history_epoch

        start_time = time.monotonic()

        # 組裝訊息（使用 snapshot 的歷史，不再讀 self._conversation_history）
        messages = self._build_messages_with_history(
            text, role, hotword_context, history_snapshot,
        )
        logger.info(
            "LLM 處理開始: 角色=%s, 訊息數=%d, 文字長度=%d",
            role.name,
            len(messages),
            len(text),
        )

        # 串流呼叫（用 snapshot client，不讀 self._client）
        result = self._stream_chat(
            client=client_snapshot,
            messages=messages,
            on_token=on_token,
            should_stop=should_stop,
        )

        # A1：運行時自動降級 — auth/quota/網路類錯誤 → 試其他可用 provider。
        # 修復 provider.py 盲點：非空但失效的 key (is_available=True) 唔會喺
        # init 時被跳過，只有實際呼叫先知 401，所以降級必須喺呼叫失敗後做。
        if (
            result.error
            and not result.was_stopped
            and self._matches(result.error, _FAILOVER_ERROR_MARKERS)
        ):
            result = self._failover(
                messages=messages,
                failed_provider=provider_snapshot,
                first_error=result.error,
                on_token=on_token,
                should_stop=should_stop,
            )

        result.elapsed_time = time.monotonic() - start_time

        # LLM 呼叫完成但回應為空（API 錯誤等）→ 回傳原文並記錄錯誤
        if not result.text and not result.was_stopped and not result.error:
            result.error = "❌ LLM 回應為空（模型未返回任何內容，請檢查模型名稱是否正確）"
            result.text = text

        # 降級後仍失敗 → fallback 原文，讓上層貼出未潤色文字
        if result.error and not result.text:
            result.text = text

        # 記錄對話歷史（_append_history 內部加鎖 + epoch 檢查）
        if role.enable_history and result.text and not result.error:
            self._append_history(
                user_content=text,
                assistant_content=result.text,
                expected_epoch=epoch_snapshot,
            )

        logger.info(
            "LLM 處理完成: 角色=%s, 耗時=%.2fs, chunks=%d, 停止=%s, 結果長度=%d",
            role.name,
            result.elapsed_time,
            result.token_count,
            result.was_stopped,
            len(result.text),
        )

        return result

    def clear_history(self) -> None:
        """清空對話歷史並遞增 epoch，使 in-flight process() 寫回失效。"""
        with self._state_lock:
            count = len(self._conversation_history)
            self._conversation_history.clear()
            self._history_epoch += 1
        logger.info("對話歷史已清空 (%d 條訊息), epoch→%d", count, self._history_epoch)

    def reload_provider(self) -> None:
        """重新載入服務商配置（配置變更後調用）。"""
        with self._state_lock:
            self._init_client()

    def update_config(self, config: Any) -> None:
        """
        更新配置並重新初始化客戶端。

        Args:
            config: 新的 LLMConfig 或 AppConfig
        """
        with self._state_lock:
            self._config = config
            self._llm_config = getattr(config, "llm", config)
            self._init_client()
        logger.info("LLM 處理器配置已更新")

    # ─── 內部方法 ──────────────────────────────────────────

    def _init_client(self) -> None:
        """初始化或重新初始化 LLM 客戶端。"""
        self._provider = get_active_provider(self._config)

        if self._provider is None:
            self._client = None
            logger.info("無活躍服務商，LLM 客戶端未初始化")
            return

        self._client = self._build_client(self._provider)

    def _build_client(self, provider: ProviderInfo) -> LLMClient:
        """用當前 LLM 參數為指定 provider 建立 client（init 與降級共用）。"""
        return LLMClient(
            provider=provider,
            temperature=getattr(self._llm_config, "temperature", 0.3),
            max_tokens=getattr(self._llm_config, "max_tokens", 1024),
            top_p=getattr(self._llm_config, "top_p", 1.0),
            frequency_penalty=getattr(self._llm_config, "frequency_penalty", 0.0),
            presence_penalty=getattr(self._llm_config, "presence_penalty", 0.0),
            do_sample=getattr(self._llm_config, "do_sample", True),
        )

    @staticmethod
    def _matches(error: str, markers: tuple[str, ...]) -> bool:
        """error 訊息是否包含任一標記。"""
        return any(m in error for m in markers)

    def _failover(
        self,
        messages: list[dict[str, str]],
        failed_provider: ProviderInfo | None,
        first_error: str,
        on_token: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> LLMResult:
        """
        當前 provider auth/quota/網路失敗 → 依序試其他可用 provider。

        - 永久失效（401/403）的 provider 加入 session 黑名單，後續直接跳過。
        - 暫時性失敗（429/網路）不入黑名單，下次仍會嘗試。
        - 全部失敗時回傳最後一個 result（error 非空），由 process() fallback 原文。
        """
        if failed_provider is not None and self._matches(
            first_error, _PERMANENT_FAILURE_MARKERS
        ):
            self._failed_providers.add(failed_provider.key)

        failed_key = failed_provider.key if failed_provider else None
        last = LLMResult(text="", error=first_error)

        for prov in list_available_providers(self._config):
            if prov.key == failed_key or prov.key in self._failed_providers:
                continue
            logger.warning(
                "LLM 降級：改試後備 provider %s (%s)", prov.key, prov.name
            )
            result = self._stream_chat(
                client=self._build_client(prov),
                messages=messages,
                on_token=on_token,
                should_stop=should_stop,
            )
            if not result.error:
                logger.info("LLM 降級成功：%s (%s)", prov.key, prov.name)
                return result
            if self._matches(result.error, _PERMANENT_FAILURE_MARKERS):
                self._failed_providers.add(prov.key)
            last = result

        logger.error("LLM 降級失敗：冇可用 provider")
        return last

    def _is_ready(self) -> bool:
        """檢查 LLM 是否就緒。"""
        llm_enabled: bool = getattr(self._llm_config, "enabled", True)
        return llm_enabled and self._client is not None

    @staticmethod
    def _build_messages_with_history(
        text: str,
        role: RoleConfig,
        hotword_context: list[str] | None,
        history: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """用給定的歷史 snapshot 組裝訊息（不讀任何 self 狀態）。"""
        messages: list[dict[str, str]] = []

        # System prompt
        system_prompt = LLMProcessor._build_system_prompt(role, hotword_context)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # 對話歷史（呼叫者已依 role.enable_history 決定是否給）
        if history:
            messages.extend(history)

        # 用戶訊息
        messages.append({"role": "user", "content": text})

        return messages

    @staticmethod
    def _build_system_prompt(
        role: RoleConfig,
        hotword_context: list[str] | None,
    ) -> str:
        """
        構建系統提示詞，可選注入熱詞上下文。

        當 role.enable_hotwords=True 且 hotword_context 非空時，
        將熱詞列表附加到系統提示詞末尾。
        """
        prompt = role.system_prompt

        if not prompt:
            return ""

        # iter 3 Bug D：`hotword_context` 的真值判斷已涵蓋 None 和空 list，
        # 移除冗餘的 `len(hotword_context) > 0` 並修正 mypy 型別錯誤。
        if role.enable_hotwords and hotword_context:
            hotword_list = "、".join(hotword_context)
            prompt = prompt + _HOTWORD_INJECT_TEMPLATE.format(
                hotwords=hotword_list,
            )

        return prompt

    def _stream_chat(
        self,
        messages: list[dict[str, str]],
        on_token: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
        client: LLMClient | None = None,
    ) -> LLMResult:
        """
        執行串流 LLM 呼叫，收集結果。

        Args:
            client: 若給定則使用此 client，否則使用 self._client。
                    process() 會傳入 snapshot 的 client，避免並發 update_config()
                    替換 _client 而污染正在進行的請求。

        逐 chunk 處理：
        - 累積到完整文字
        - 呼叫 on_token 回調
        - 檢查 should_stop 中斷條件
        """
        result = LLMResult()
        chunks: list[str] = []

        active_client = client if client is not None else self._client
        if active_client is None:
            return result

        # iter 3 Bug C：用 per-call API 取 warnings，避免多 thread 共用 client
        # 時 instance 上的 _param_warnings 被其他 thread 覆蓋造成 UX 洩漏。
        try:
            stream, call_warnings = active_client.chat_with_warnings(
                messages, stream=True,
            )
            for chunk in stream:
                # 檢查停止條件
                if should_stop is not None and should_stop():
                    result.was_stopped = True
                    logger.info("LLM 串流被用戶中途停止")
                    break

                chunks.append(chunk)
                result.token_count += 1

                # 回調
                if on_token is not None:
                    try:
                        on_token(chunk)
                    except Exception as err:
                        logger.warning("on_token 回調異常: %s", err)

        except RuntimeError as err:
            # LLM client 拋出的具體錯誤
            result.error = str(err)
            logger.error("LLM 錯誤: %s", err)
            call_warnings = []
        except Exception as err:
            result.error = f"未預期的錯誤：{err}"
            logger.error("串流處理異常: %s", err)
            call_warnings = []

        result.text = "".join(chunks)
        # warnings 直接從 per-call list 取得（此 list 為本 call 專用，無共享）
        result.warnings = list(call_warnings)

        return result

    def _append_history(
        self,
        user_content: str,
        assistant_content: str,
        expected_epoch: int | None = None,
    ) -> None:
        """
        將一輪對話追加到歷史記錄。

        超過最大輪數時，從最早的對話開始移除（每輪 = 2 條訊息）。
        整段受 _state_lock 保護，避免 user/assistant 訊息被 clear_history() 拆散。

        Args:
            expected_epoch: 呼叫者 snapshot 時的 epoch；若當前 epoch 已遞增
                            （clear_history 期間發生），skip 寫入避免洩漏舊對話。
                            None 代表跳過檢查（直接寫入，舊行為相容）。
        """
        with self._state_lock:
            if expected_epoch is not None and expected_epoch != self._history_epoch:
                logger.info(
                    "history 已被 clear（expected_epoch=%d, current=%d），跳過 append",
                    expected_epoch, self._history_epoch,
                )
                return

            self._conversation_history.append(
                {"role": "user", "content": user_content},
            )
            self._conversation_history.append(
                {"role": "assistant", "content": assistant_content},
            )

            # 裁剪歷史：每輪 2 條訊息
            max_messages = _MAX_HISTORY_TURNS * 2
            if len(self._conversation_history) > max_messages:
                overflow = len(self._conversation_history) - max_messages
                self._conversation_history = self._conversation_history[overflow:]
                logger.debug(
                    "對話歷史已裁剪: 移除 %d 條, 保留 %d 條",
                    overflow,
                    len(self._conversation_history),
                )
