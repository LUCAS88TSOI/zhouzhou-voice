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

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.recording_db import RecordingDatabase, RecordingMeta
from gui.widgets.audio_player import AudioPlayerWidget
from utils.logger import get_logger

if TYPE_CHECKING:
    from utils.config import AppConfig

logger = get_logger("history_tab")


class HistoryTab(QWidget):
    """錄音歷史管理頁籤"""

    # 信號：請求重新處理錄音（record_id, role_id）
    reprocess_requested = Signal(int, str)

    PAGE_SIZE = 100
    SEARCH_DEBOUNCE_MS = 300
    TEXT_PREVIEW_CHARS = 50

    def __init__(
        self,
        db: RecordingDatabase,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._records: List[RecordingMeta] = []
        self._page_size = self.PAGE_SIZE
        self._shown = self.PAGE_SIZE
        self._keyword = ""
        self._dirty = False

        self._build_ui()
        self._refresh_list()

    # ── 刷新排程 ──────────────────────────────
    #
    # refresh_history 是每次語音輸入完成都會走的路徑。重建 100 列表格
    # （300 個 item + 200 顆按鈕）在主線程做，講完話貼上就會頓一下 ——
    # 而設定頁多數時間根本沒打開。所以隱藏時只記帳，可見時才付錢。

    def mark_dirty(self) -> None:
        """標記歷史已變動；分頁可見時立即刷新，否則等 showEvent。"""
        self._dirty = True
        if self.isVisible():
            self._refresh_list()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
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

        # ── 搜尋列 ──────────────────────────────
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜尋："))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("輸入關鍵字搜尋識別結果或潤色結果…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(self.SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_edit.textChanged.connect(lambda _: self._search_timer.start())
        self._search_edit.returnPressed.connect(self._apply_search)
        search_row.addWidget(self._search_edit, stretch=1)
        layout.addLayout(search_row)

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
        self._table.itemDoubleClicked.connect(self._on_item_double_clicked)
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

        # ── 記錄數量 + 載入更多 ──────────────────
        count_row = QHBoxLayout()
        self._count_label = QLabel("共 0 筆記錄")
        count_row.addWidget(self._count_label)
        count_row.addStretch()
        self._more_btn = QPushButton("載入更多")
        self._more_btn.clicked.connect(self._on_load_more)
        self._more_btn.setVisible(False)
        count_row.addWidget(self._more_btn)
        layout.addLayout(count_row)

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
        """刷新錄音列表（只取中繼資料，音訊按需再撈）"""
        self._dirty = False
        self._records = self._db.get_recent_meta(
            limit=self._shown, keyword=self._keyword,
        )

        self._table.setRowCount(len(self._records))
        for row, rec in enumerate(self._records):
            # 時間
            time_str = rec.timestamp.strftime("%m-%d %H:%M:%S")
            self._table.setItem(row, 0, QTableWidgetItem(time_str))

            # 長度
            dur_str = f"{rec.duration:.1f}s"
            self._table.setItem(row, 1, QTableWidgetItem(dur_str))

            # 識別結果（截斷顯示，完整內容放 tooltip）
            full_text = rec.display_text
            shown = (
                full_text[:self.TEXT_PREVIEW_CHARS] + "..."
                if len(full_text) > self.TEXT_PREVIEW_CHARS
                else full_text
            )
            text_item = QTableWidgetItem(shown)
            text_item.setToolTip(full_text)
            self._table.setItem(row, 2, text_item)

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

        total = self._db.count(keyword=self._keyword)
        loaded = len(self._records)
        scope = f"符合「{self._keyword}」共 {total} 筆" if self._keyword else f"共 {total} 筆記錄"
        self._count_label.setText(
            f"顯示 {loaded} ／ {scope}" if loaded < total else scope
        )
        self._more_btn.setVisible(loaded < total)
        logger.info("錄音歷史已刷新: %d/%d 筆", loaded, total)

    @Slot()
    def _apply_search(self) -> None:
        """套用搜尋關鍵字（下推到 SQL），並把分頁視窗重置回第一頁。"""
        self._search_timer.stop()
        self._keyword = self._search_edit.text().strip()
        self._shown = self._page_size
        self._refresh_list()

    @Slot()
    def _on_load_more(self) -> None:
        """再多載一頁"""
        self._shown += self._page_size
        self._refresh_list()

    @Slot()
    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        """雙擊任一列複製該筆完整文字"""
        row = item.row()
        if 0 <= row < len(self._records):
            text = self._records[row].display_text
            if text:
                QApplication.clipboard().setText(text)
                logger.debug("已複製歷史記錄全文: %d 個字元", len(text))

    @Slot()
    def _on_play(self, record: RecordingMeta) -> None:
        """播放選中的錄音（此時才按 id 撈 WAV blob）"""
        full = self._db.get_by_id(record.id)
        if full is None:
            logger.warning("錄音記錄已不存在: id=%d", record.id)
            return
        self._player.load_wav(full.audio_data)
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
        text = record.display_text
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
