"""
州州語音 - LLM 服務商選擇器

提供一個 QComboBox 封裝，列出所有 LLM 服務商。
支持根據 key 設定 / 取得當前選中的服務商，並在切換時發出信號。

用法：
    combo = ProviderCombo(config.llm.providers)
    combo.provider_changed.connect(on_provider_changed)
    combo.set_provider_key("openai")
    key = combo.get_provider_key()  # "openai"
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox

from utils.logger import get_logger

logger = get_logger("provider_combo")

# 預設只顯示這些供應商（其餘隱藏但 config／key 保留，可逆）。
# 空集合 = 顯示全部。日後想加回供應商，改這裡即可。
VISIBLE_PROVIDERS: frozenset[str] = frozenset({"google", "siliconflow"})


def visible_provider_entries(
    providers: dict[str, dict[str, Any]],
    whitelist: frozenset[str] = VISIBLE_PROVIDERS,
    always_include: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """回傳 (key, display_name) 清單，只保留白名單內供應商（保持 config 原順序）。

    always_include 內的 key（如目前 active_provider）即使不在白名單也會保留，
    避免白名單把使用中的供應商隱藏掉。whitelist 為空時顯示全部（軟性閘門）。
    純函數，可獨立單元測試。
    """
    keep = {k for k in always_include if k}
    out: list[tuple[str, str]] = []
    for key, info in providers.items():
        if whitelist and key not in whitelist and key not in keep:
            continue
        out.append((key, info.get("name", key)))
    return out


class ProviderCombo(QComboBox):
    """
    LLM 服務商選擇器 — QComboBox 封裝。

    根據配置中的 providers 字典動態生成選項，
    顯示服務商的 ``name`` 欄位（如 ``"OpenAI"``、``"智譜"``），
    內部使用 key（如 ``"openai"``、``"zhipu"``）標識。
    """

    # 當用戶切換服務商時發出，攜帶所選的 provider key
    provider_changed = Signal(str)

    def __init__(
        self,
        providers: dict[str, dict[str, Any]],
        parent=None,  # noqa: ANN001
    ) -> None:
        super().__init__(parent)

        # 保留完整 providers，供 set_provider_key 對白名單外但真實存在的
        # 供應商（如使用中的 active_provider）按需加回，避免靜默改 active
        self._providers: dict[str, dict[str, Any]] = dict(providers)
        # 保存 (key, display_name) 的有序列表
        self._entries: list[tuple[str, str]] = []
        self._key_to_index: dict[str, int] = {}

        self._populate(providers)

        # 連接信號：索引變化 → 發出 provider_changed
        self.currentIndexChanged.connect(self._on_index_changed)

        logger.debug(
            "ProviderCombo 初始化完成，共 %d 個服務商", len(self._entries)
        )

    # ─── 公開 API ──────────────────────────────────────

    def get_provider_key(self) -> str:
        """
        取得目前選中的服務商 key。

        Returns:
            服務商 key，例如 ``"siliconflow"``
        """
        idx = self.currentIndex()
        if 0 <= idx < len(self._entries):
            return self._entries[idx][0]
        return ""

    def set_provider_key(self, key: str) -> None:
        """
        依據 key 設定選中項。

        若 key 不存在，選取第一項並記錄警告。

        Args:
            key: 服務商 key，例如 ``"openai"``
        """
        idx = self._key_to_index.get(key)
        if idx is None and key in self._providers:
            # 白名單外但真實存在（如使用中的 active_provider）→ 動態加回，
            # 避免回退到第一項令 get_provider_key() 在 Save 時靜默改寫 active
            display = self._providers[key].get("name", key)
            idx = len(self._entries)
            self._entries.append((key, display))
            self._key_to_index[key] = idx
            self.addItem(display)
        if idx is not None:
            self.setCurrentIndex(idx)
        else:
            logger.warning("未知的服務商 key '%s'，回退到第一項", key)
            if self._entries:
                self.setCurrentIndex(0)

    # ─── 內部方法 ──────────────────────────────────────

    def _populate(self, providers: Dict[str, Dict[str, Any]]) -> None:
        """從 providers 字典填充下拉選單。"""
        self.clear()
        self._entries.clear()
        self._key_to_index.clear()

        for idx, (key, display) in enumerate(visible_provider_entries(providers)):
            self._entries.append((key, display))
            self._key_to_index[key] = idx
            self.addItem(display)

    def _on_index_changed(self, index: int) -> None:
        """索引變化時發出 provider_changed 信號。"""
        if 0 <= index < len(self._entries):
            key = self._entries[index][0]
            logger.debug("服務商切換 → %s", key)
            self.provider_changed.emit(key)
