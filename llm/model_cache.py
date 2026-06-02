"""
州州語音 - LLM 模型清單快取

把各供應商 GET /models 的結果快取到 %APPDATA%\\zhouzhou-voice\\model_cache.json，
附時間戳，供設定面板判斷是否過期（stale）需要重抓。

設計原則：
- 純函數式讀寫，無全域狀態（每次讀檔/寫檔）
- 容錯優先：任何 IO/JSON 錯誤都不拋給呼叫方，回傳安全預設值
- 不 import config.py，路徑來自 utils.paths，避免循環依賴
"""

from __future__ import annotations

import json
import time
from typing import Any

from utils.logger import get_logger
from utils.paths import APP_DATA_DIR

logger = get_logger("llm.model_cache")

# 快取檔（與 config.json / logs 同層）
_CACHE_FILE = APP_DATA_DIR / "model_cache.json"

# 超過此秒數視為過期（24 小時）
CACHE_TTL: int = 86400


def _load() -> dict[str, Any]:
    """讀整個快取檔，失敗回空 dict。"""
    try:
        if not _CACHE_FILE.exists():
            return {}
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        logger.warning("模型快取讀取失敗，視為空: %s", err)
        return {}


def get(provider_key: str) -> tuple[list[str] | None, float]:
    """
    取得指定供應商的快取模型清單與其年齡（秒）。

    Returns:
        (models, age_seconds)
        - models 為 None 代表無快取；age 為 +inf
    """
    data = _load()
    entry = data.get(provider_key)
    if not isinstance(entry, dict):
        return None, float("inf")

    models = entry.get("models")
    ts = entry.get("ts", 0)
    if not isinstance(models, list) or not models:
        return None, float("inf")

    age = max(0.0, time.time() - float(ts))
    return [str(m) for m in models], age


def is_stale(provider_key: str) -> bool:
    """快取缺失或超過 TTL → True。"""
    models, age = get(provider_key)
    return models is None or age > CACHE_TTL


def set(provider_key: str, models: list[str]) -> None:
    """寫入/更新指定供應商的模型清單（附當前時間戳）。"""
    if not models:
        return
    data = _load()
    data[provider_key] = {"models": list(models), "ts": time.time()}
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("模型快取已更新: %s (%d 個)", provider_key, len(models))
    except OSError as err:
        logger.warning("模型快取寫入失敗: %s", err)
