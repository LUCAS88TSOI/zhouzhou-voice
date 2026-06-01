"""
州州語音 - LLM 潤色模組

提供 LLM 服務商管理、API 客戶端和處理邏輯。

子模組：
- provider: 服務商配置（ProviderInfo）和查詢函數
- client:   OpenAI 兼容 API 客戶端（LLMClient）
- processor: LLM 處理器（LLMProcessor、RoleConfig、LLMResult）
"""

from llm.client import LLMClient
from llm.processor import LLMProcessor, LLMResult, RoleConfig
from llm.provider import ProviderInfo, get_active_provider, list_available_providers

__all__ = [
    "LLMClient",
    "LLMProcessor",
    "LLMResult",
    "ProviderInfo",
    "RoleConfig",
    "get_active_provider",
    "list_available_providers",
]
