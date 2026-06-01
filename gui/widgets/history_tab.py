"""
錄音歷史頁籤

顯示錄音列表，支援：
- 播放錄音
- 重新處理 ASR
- 選擇角色重新潤色
- 刪除記錄
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.recording_db import RecordingDatabase, RecordingRecord
from gui.widgets.audio_player import AudioPlayerWidget
from utils.logger import get_logger

if TYPE_CHECKING:
    from utils.config import AppConfig

logger = get_logger("history_tab")


class HistoryTab(QWidget):
    """錄音歷史管理頁籤"""

    # 信號：請求重新處理錄音（record_id, role_id）
    reprocess_requested = Signal(int, str)

    def __init__(
        self,
        db: RecordingDatabase,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._records: List[RecordingRecord] = []

        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ── 頂部：設定區 ────────────────────────
        settings_row = QHBoxLayout()

        self._enabled_cb = QCheckBox("啟用錄音歷史")
        self._enabled_cb.setChecked(True)
        settings_row.addWidget(self._enabled_cb)

        settings_row.addWidget(QLabel("最短錄音："))
        self._min_duration_spin = QDoubleSpinBox()
        self._min_duration_spin.setRange(0.1, 10.0)
        self._min_duration_spin.setSingleStep(0.1)
        self._min_duration_spin.setSuffix(" 秒")
        self._min_duration_spin.setValue(0.5)
        settings_row.addWidget(self._min_duration_spin)

        settings_row.addStretch()

        self._refresh_btn = QPushButton("重新整理")
        self._refresh_btn.clicked.connect(self._refresh_list)
        settings_row.addWidget(self._refresh_btn)

        self._clear_btn = QPushButton("清空全部")
        self._clear_btn.clicked.connect(self._on_clear_all)
        settings_row.addWidget(self._clear_btn)

        layout.addLayout(settings_row)

        # ── 中間：錄音列表 ──────────────────────
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels([
            "時間", "長度", "識別結果", "角色", "操作"
        ])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        self._table.setColumnWidth(0, 120)
        self._table.setColumnWidth(1, 60)
        self._table.setColumnWidth(3, 80)
        self._table.setColumnWidth(4, 70)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table, stretch=1)

        # ── 底部：播放器 + 操作 ──────────────────
        bottom_row = QHBoxLayout()

        self._player = AudioPlayerWidget()
        bottom_row.addWidget(self._player, stretch=1)

        bottom_row.addWidget(QLabel("重處理角色："))
        self._role_combo = QComboBox()
        self._role_combo.setMinimumWidth(120)
        bottom_row.addWidget(self._role_combo)

        self._reprocess_btn = QPushButton("重新處理")
        self._reprocess_btn.clicked.connect(self._on_reprocess)
        bottom_row.addWidget(self._reprocess_btn)

        layout.addLayout(bottom_row)

        # ── 記錄數量標籤 ────────────────────────
        self._count_label = QLabel("共 0 筆記錄")
        layout.addWidget(self._count_label)

    def load_config(self, config: "AppConfig") -> None:
        """載入配置"""
        self._enabled_cb.setChecked(config.history.enabled)
        self._min_duration_spin.setValue(config.history.min_duration)

    def get_config_values(self) -> dict:
        """取得配置值"""
        return {
            "enabled": self._enabled_cb.isChecked(),
            "min_duration": round(self._min_duration_spin.value(), 1),
        }

    def refresh_roles(
        self,
        custom_roles: list,
        builtin_overrides: dict,
    ) -> None:
        """刷新角色下拉選單"""
        from llm.roles import get_all_roles

        self._role_combo.clear()
        self._role_combo.addItem("（僅 ASR，不使用 LLM）", "")

        all_roles = get_all_roles(custom_roles, builtin_overrides)
        for role_id, role_cfg, is_builtin in all_roles:
            display = role_cfg.name or role_id
            prefix = "（內建）" if is_builtin else "（自訂）"
            self._role_combo.addItem(f"{display} {prefix}", role_id)

        logger.debug("角色列表已刷新: %d 個角色", self._role_combo.count() - 1)

    def _refresh_list(self) -> None:
        """刷新錄音列表"""
        self._records = self._db.get_recent(limit=100)

        self._table.setRowCount(len(self._records))
        for row, rec in enumerate(self._records):
            # 時間
            time_str = rec.timestamp.strftime("%m-%d %H:%M:%S")
            self._table.setItem(row, 0, QTableWidgetItem(time_str))

            # 長度
            dur_str = f"{rec.duration:.1f}s"
            self._table.setItem(row, 1, QTableWidgetItem(dur_str))

            # 識別結果（截斷）
            text = rec.llm_text or rec.asr_text
            if len(text) > 50:
                text = text[:50] + "..."
            self._table.setItem(row, 2, QTableWidgetItem(text))

            # 角色
            role_display = rec.role_id if rec.role_id else "-"
            self._table.setItem(row, 3, QTableWidgetItem(role_display))

            # 操作按鈕
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(2, 2, 2, 2)
            btn_layout.setSpacing(2)

            play_btn = QPushButton("▶")
            play_btn.setFixedSize(28, 28)
            play_btn.setToolTip("播放")
            play_btn.clicked.connect(
                lambda checked, r=rec: self._on_play(r)
            )
            btn_layout.addWidget(play_btn)

            del_btn = QPushButton("🗑")
            del_btn.setFixedSize(28, 28)
            del_btn.setToolTip("刪除")
            del_btn.clicked.connect(
                lambda checked, rid=rec.id: self._on_delete(rid)
            )
            btn_layout.addWidget(del_btn)

            self._table.setCellWidget(row, 4, btn_widget)

        self._count_label.setText(f"共 {self._db.count()} 筆記錄")
        logger.info("錄音歷史已刷新: %d 筆", len(self._records))

    @Slot()
    def _on_play(self, record: RecordingRecord) -> None:
        """播放選中的錄音"""
        self._player.load_wav(record.audio_data)
        self._player._toggle_play()

    @Slot()
    def _on_delete(self, record_id: int) -> None:
        """刪除選中的記錄"""
        reply = QMessageBox.question(
            self,
            "確認刪除",
            "確定要刪除這條錄音記錄嗎？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._db.delete(record_id)
            self._refresh_list()

    @Slot()
    def _on_clear_all(self) -> None:
        """清空所有記錄"""
        count = self._db.count()
        if count == 0:
            return

        reply = QMessageBox.question(
            self,
            "確認清空",
            f"確定要刪除全部 {count} 筆錄音記錄嗎？\n此操作無法復原！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            for rec in self._db.get_recent(limit=10000):
                self._db.delete(rec.id)
            self._refresh_list()
            logger.info("已清空所有錄音記錄")

    @Slot()
    def _on_context_menu(self, pos) -> None:
        """右鍵選單：複製識別結果"""
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._records):
            return
        record = self._records[row]
        text = record.llm_text or record.asr_text
        if not text:
            return

        menu = QMenu(self)
        copy_action = menu.addAction("複製")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == copy_action:
            QApplication.clipboard().setText(text)

    @Slot()
    def _on_reprocess(self) -> None:
        """重新處理選中的錄音"""
        row = self._table.currentRow()
        if row < 0 or row >= len(self._records):
            QMessageBox.information(self, "提示", "請先選擇一條錄音記錄")
            return

        record = self._records[row]
        role_id = self._role_combo.currentData() or ""

        # 發射信號，由外部處理
        self.reprocess_requested.emit(record.id, role_id)
        logger.info("請求重新處理錄音: id=%d, role=%s", record.id, role_id or "(無)")
