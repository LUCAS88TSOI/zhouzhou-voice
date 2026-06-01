# 州州語音 - 熱詞管理頁籤
# 提供熱詞列表、替換規則、糾錯記錄的 CRUD 介面和參數調整。

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from utils.config import HotwordConfig
from utils.logger import get_logger

if TYPE_CHECKING:
    from hotword.manager import HotwordManager

logger = get_logger("hotword_tab")


class HotwordTab(QWidget):
    """
    熱詞管理頁籤 — 提供熱詞、規則、糾錯的 CRUD 介面。

    使用子分頁切換三個列表，頂部顯示設定和統計。
    透過 HotwordManager 實例直接操作文件。
    """

    def __init__(
        self,
        config: HotwordConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager: HotwordManager | None = None
        self._build_ui()
        self.load_config(config)

    # ─── UI 建構 ─────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── 設定區 ────────────────────────────────
        settings_group = QGroupBox("熱詞設定")
        settings_layout = QVBoxLayout(settings_group)

        self._enabled_check = QCheckBox("啟用熱詞系統")
        settings_layout.addWidget(self._enabled_check)

        form = QFormLayout()

        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0.5, 1.0)
        self._threshold_spin.setSingleStep(0.05)
        self._threshold_spin.setDecimals(2)
        self._threshold_spin.setToolTip(
            "音素匹配門檻：越高越嚴格，越低越寬鬆。"
            "\n建議值 0.80–0.90"
        )
        form.addRow("匹配門檻：", self._threshold_spin)

        self._similar_spin = QDoubleSpinBox()
        self._similar_spin.setRange(0.3, 1.0)
        self._similar_spin.setSingleStep(0.05)
        self._similar_spin.setDecimals(2)
        self._similar_spin.setToolTip(
            "相似度門檻：用於提供 LLM 上下文提示。"
            "\n越低會提供更多提示詞"
        )
        form.addRow("相似門檻：", self._similar_spin)

        settings_layout.addLayout(form)

        self._stats_label = QLabel("統計：載入中...")
        self._stats_label.setStyleSheet("color: #888; padding: 2px;")
        settings_layout.addWidget(self._stats_label)

        layout.addWidget(settings_group)

        # ── 子分頁 ────────────────────────────────
        self._sub_tabs = QTabWidget()

        self._sub_tabs.addTab(self._build_hotword_list_tab(), "熱詞列表")
        self._sub_tabs.addTab(self._build_rule_tab(), "替換規則")
        self._sub_tabs.addTab(self._build_rectify_tab(), "糾錯記錄")

        layout.addWidget(self._sub_tabs, stretch=1)

    def _build_hotword_list_tab(self) -> QWidget:
        """建構熱詞列表子分頁。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        self._hotword_list = QListWidget()
        layout.addWidget(self._hotword_list, stretch=1)

        row = QHBoxLayout()
        self._hotword_input = QLineEdit()
        self._hotword_input.setPlaceholderText("輸入熱詞，例如：API")
        self._hotword_input.returnPressed.connect(self._on_add_hotword)
        row.addWidget(self._hotword_input, stretch=1)

        btn_add = QPushButton("新增")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._on_add_hotword)
        row.addWidget(btn_add)

        btn_remove = QPushButton("刪除選中")
        btn_remove.setFixedWidth(80)
        btn_remove.clicked.connect(self._on_remove_hotword)
        row.addWidget(btn_remove)

        layout.addLayout(row)
        return page

    def _build_rule_tab(self) -> QWidget:
        """建構替換規則子分頁。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        self._rule_list = QListWidget()
        layout.addWidget(self._rule_list, stretch=1)

        row = QHBoxLayout()
        self._rule_pattern_input = QLineEdit()
        self._rule_pattern_input.setPlaceholderText("匹配詞，例如：愛皮愛")
        row.addWidget(self._rule_pattern_input, stretch=1)

        eq_label = QLabel("=")
        eq_label.setFixedWidth(16)
        eq_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(eq_label)

        self._rule_replace_input = QLineEdit()
        self._rule_replace_input.setPlaceholderText("替換為，例如：API")
        self._rule_replace_input.returnPressed.connect(self._on_add_rule)
        row.addWidget(self._rule_replace_input, stretch=1)

        btn_add = QPushButton("新增")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._on_add_rule)
        row.addWidget(btn_add)

        btn_remove = QPushButton("刪除選中")
        btn_remove.setFixedWidth(80)
        btn_remove.clicked.connect(self._on_remove_rule)
        row.addWidget(btn_remove)

        layout.addLayout(row)
        return page

    def _build_rectify_tab(self) -> QWidget:
        """建構糾錯記錄子分頁。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        self._rectify_list = QListWidget()
        layout.addWidget(self._rectify_list, stretch=1)

        row = QHBoxLayout()
        self._rectify_wrong_input = QLineEdit()
        self._rectify_wrong_input.setPlaceholderText("錯誤詞，例如：語音識別")
        row.addWidget(self._rectify_wrong_input, stretch=1)

        arrow_label = QLabel("→")
        arrow_label.setFixedWidth(16)
        arrow_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(arrow_label)

        self._rectify_right_input = QLineEdit()
        self._rectify_right_input.setPlaceholderText("正確詞，例如：語音辨識")
        self._rectify_right_input.returnPressed.connect(self._on_add_rectify)
        row.addWidget(self._rectify_right_input, stretch=1)

        btn_add = QPushButton("新增")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._on_add_rectify)
        row.addWidget(btn_add)

        btn_remove = QPushButton("刪除選中")
        btn_remove.setFixedWidth(80)
        btn_remove.clicked.connect(self._on_remove_rectify)
        row.addWidget(btn_remove)

        layout.addLayout(row)
        return page

    # ─── CRUD 操作 ────────────────────────────────────────

    def _on_add_hotword(self) -> None:
        word = self._hotword_input.text().strip()
        if not word or self._manager is None:
            return
        self._manager.add_hotword(word)
        self._hotword_input.clear()
        self._refresh_lists()

    def _on_remove_hotword(self) -> None:
        item = self._hotword_list.currentItem()
        if item is None or self._manager is None:
            return
        self._manager.remove_hotword(item.text())
        self._refresh_lists()

    def _on_add_rule(self) -> None:
        pattern = self._rule_pattern_input.text().strip()
        replacement = self._rule_replace_input.text().strip()
        if not pattern or not replacement or self._manager is None:
            return
        self._manager.add_rule(pattern, replacement)
        self._rule_pattern_input.clear()
        self._rule_replace_input.clear()
        self._refresh_lists()

    def _on_remove_rule(self) -> None:
        item = self._rule_list.currentItem()
        if item is None or self._manager is None:
            return
        self._manager.remove_rule(item.text())
        self._refresh_lists()

    def _on_add_rectify(self) -> None:
        wrong = self._rectify_wrong_input.text().strip()
        right = self._rectify_right_input.text().strip()
        if not wrong or not right or self._manager is None:
            return
        self._manager.add_rectify(wrong, right)
        self._rectify_wrong_input.clear()
        self._rectify_right_input.clear()
        self._refresh_lists()

    def _on_remove_rectify(self) -> None:
        item = self._rectify_list.currentItem()
        if item is None or self._manager is None:
            return
        self._manager.remove_rectify(item.text())
        self._refresh_lists()

    # ─── 列表刷新 ─────────────────────────────────────────

    def _refresh_lists(self) -> None:
        """從 HotwordManager 讀取文件內容刷新所有列表。"""
        if self._manager is None:
            self._hotword_list.clear()
            self._rule_list.clear()
            self._rectify_list.clear()
            self._stats_label.setText("統計：未連接熱詞管理器")
            return

        self._hotword_list.clear()
        for word in self._manager.get_hotwords():
            self._hotword_list.addItem(word)

        self._rule_list.clear()
        for rule in self._manager.get_rules():
            self._rule_list.addItem(rule)

        self._rectify_list.clear()
        for pair in self._manager.get_rectify_pairs():
            self._rectify_list.addItem(pair)

        self._update_stats()

    def _update_stats(self) -> None:
        """更新統計標籤。"""
        if self._manager is None:
            return
        self._stats_label.setText(
            f"統計：{self._manager.hotword_count} 個熱詞, "
            f"{self._manager.rule_count} 條規則, "
            f"{self._manager.rectify_count} 條糾錯"
        )

    # ─── 公開方法 ─────────────────────────────────────────

    def set_manager(self, manager: HotwordManager | None) -> None:
        """設定 HotwordManager 引用並刷新列表。"""
        self._manager = manager
        self._refresh_lists()

    def load_config(self, config: HotwordConfig) -> None:
        """從 HotwordConfig 載入 UI 值。"""
        self._enabled_check.setChecked(config.enabled)
        self._threshold_spin.setValue(config.threshold)
        self._similar_spin.setValue(config.similar_threshold)

    def get_hotword_config(self) -> HotwordConfig:
        """從 UI 讀取 HotwordConfig。"""
        return HotwordConfig(
            enabled=self._enabled_check.isChecked(),
            threshold=self._threshold_spin.value(),
            similar_threshold=self._similar_spin.value(),
        )
