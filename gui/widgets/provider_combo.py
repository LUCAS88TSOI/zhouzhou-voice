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

from typing import Any, Dict, List, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox

from utils.logger import get_logger

logger = get_logger("provider_combo")


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
        providers: Dict[str, Dict[str, Any]],
        parent=None,  # noqa: ANN001
    ) -> None:
        super().__init__(parent)

        # 保存 (key, display_name) 的有序列表
        self._entries: List[Tuple[str, str]] = []
        self._key_to_index: Dict[str, int] = {}

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

        for idx, (key, info) in enumerate(providers.items()):
            display = info.get("name", key)
            self._entries.append((key, display))
            self._key_to_index[key] = idx
            self.addItem(display)

    def _on_index_changed(self, index: int) -> None:
        """索引變化時發出 provider_changed 信號。"""
        if 0 <= index < len(self._entries):
            key = self._entries[index][0]
            logger.debug("服務商切換 → %s", key)
            self.provider_changed.emit(key)
