"""
州州語音 - LLM 服務商配置

使用 frozen dataclass 封裝 LLM 服務商資訊，
提供從 LLMConfig 提取活躍/可用服務商的輔助函數。

設計原則：
- 不可變性：ProviderInfo 使用 frozen=True
- 純函數：所有輔助函數無副作用
- 驗證前置：api_url 和 api_key 必須非空才算「可用」
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from utils.logger import get_logger

logger = get_logger("llm.provider")


# ─── 服務商資料結構 ────────────────────────────────────────


@dataclass(frozen=True)
class ProviderInfo:
    """
    單一 LLM 服務商的完整配置（不可變）。

    Attributes:
        name:     顯示名稱（如 "OpenAI"、"DeepSeek"）
        api_url:  API 基礎 URL（不含 /chat/completions）
        api_key:  授權金鑰
        model:    模型名稱（如 "gpt-4o-mini"）
        enabled:  是否在 UI 中啟用
    """

    key: str
    name: str
    api_url: str
    api_key: str
    model: str
    enabled: bool = True

    @property
    def is_available(self) -> bool:
        """服務商是否已正確配置且可用。"""
        return (
            self.enabled
            and bool(self.api_url and self.api_url.strip())
            and bool(self.api_key and self.api_key.strip())
        )

    @property
    def masked_key(self) -> str:
        """遮蔽的 API Key，僅顯示前 4 碼和後 4 碼，用於日誌輸出。"""
        key = self.api_key.strip()
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}...{key[-4:]}"


# ─── 構建函數 ──────────────────────────────────────────────


def _build_provider_info(
    provider_key: str,
    provider_dict: Dict[str, Any],
) -> ProviderInfo | None:
    """
    從配置字典構建 ProviderInfo。

    Args:
        provider_key:  服務商識別 key（如 "openai"）
        provider_dict: 包含 name/api_url/api_key/model/enabled 的字典

    Returns:
        ProviderInfo 實例，欄位缺失時返回 None
    """
    try:
        api_key = str(provider_dict.get("api_key", ""))
        model = str(provider_dict.get("model", ""))

        return ProviderInfo(
            key=provider_key,
            name=str(provider_dict.get("name", provider_key)),
            api_url=str(provider_dict.get("api_url", "")),
            api_key=api_key,
            model=model,
            enabled=bool(provider_dict.get("enabled", True)),
        )
    except (TypeError, ValueError) as err:
        logger.warning("服務商 %s 配置解析失敗: %s", provider_key, err)
        return None


# ─── 公開 API ──────────────────────────────────────────────


def get_active_provider(config: Any) -> ProviderInfo | None:
    """
    取得配置中的活躍服務商。

    從 config.llm.active_provider 找到對應的服務商字典，
    構建 ProviderInfo 並驗證其可用性。

    Bug 4 修復：當指定的 provider 不可用時，嘗試其他可用 provider 作為 fallback。

    Args:
        config: 包含 llm 屬性的配置物件（LLMConfig 或 AppConfig.llm）

    Returns:
        可用的 ProviderInfo，或 None（未配置/無任何可用）
    """
    llm_config = getattr(config, "llm", config)
    active_key: str = getattr(llm_config, "active_provider", "")
    providers: Dict[str, Dict[str, Any]] = getattr(llm_config, "providers", {})

    if not active_key:
        logger.warning("未設定活躍服務商 (active_provider 為空)")
        return None

    provider_dict = providers.get(active_key)
    if provider_dict is None:
        logger.warning("活躍服務商 '%s' 不存在於配置中", active_key)
        return None

    info = _build_provider_info(active_key, provider_dict)
    if info is None:
        return None

    if info.is_available:
        logger.info(
            "活躍服務商: %s (%s), 模型: %s, Key: %s",
            active_key,
            info.name,
            info.model,
            info.masked_key,
        )
        return info

    # Bug 4 修復：當指定的 provider 不可用時，嘗試其他可用 provider
    logger.warning(
        "活躍服務商 '%s' (%s) 不可用，嘗試其他 provider...",
        active_key,
        info.name,
    )

    for key, provider_dict in providers.items():
        if key == active_key:
            continue  # 跳過已失敗的 provider
        fallback_info = _build_provider_info(key, provider_dict)
        if fallback_info is not None and fallback_info.is_available:
            logger.info(
                "使用 fallback provider: %s (%s), 模型: %s",
                key,
                fallback_info.name,
                fallback_info.model,
            )
            return fallback_info

    logger.error("無可用的 LLM provider，請配置 API Key")
    return None


def list_available_providers(config: Any) -> list[ProviderInfo]:
    """
    列出所有已配置且可用的服務商。

    篩選條件：enabled=True、api_url 非空、api_key 非空。

    Args:
        config: 包含 llm 屬性的配置物件

    Returns:
        可用 ProviderInfo 列表（可能為空）
    """
    llm_config = getattr(config, "llm", config)
    providers: Dict[str, Dict[str, Any]] = getattr(llm_config, "providers", {})

    available: list[ProviderInfo] = []
    for key, provider_dict in providers.items():
        info = _build_provider_info(key, provider_dict)
        if info is not None and info.is_available:
            available.append(info)

    logger.info(
        "可用服務商: %d / %d",
        len(available),
        len(providers),
    )
    return available
