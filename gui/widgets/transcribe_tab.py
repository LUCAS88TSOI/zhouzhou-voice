# 州州語音 - 文件轉錄頁籤
# 提供拖放匯入、文件列表管理、轉錄配置和一鍵轉錄功能。

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from utils.config import FileConfig
from utils.logger import get_logger

logger = get_logger("transcribe_tab")


def _human_size(size_bytes: int) -> str:
    """將位元組數轉為人類可讀格式。"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TB"


class _DropZone(QFrame):
    """拖放區域 — 虛線邊框，點擊或拖放匯入文件。"""

    files_added = Signal(list)  # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QFrame { border: 2px dashed #888; border-radius: 8px; "
            "background: transparent; }"
            "QFrame:hover { border-color: #4a9eff; background: rgba(74,158,255,0.05); }"
        )

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("拖放或點擊選擇音視頻文件")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("border: none; color: #888; font-size: 14px;")
        layout.addWidget(label)

    def mousePressEvent(self, event) -> None:
        from transcribe.file_transcriber import MEDIA_EXTENSIONS

        ext_list = " ".join(f"*{ext}" for ext in sorted(MEDIA_EXTENSIONS))
        filter_str = f"Media Files ({ext_list});;All Files (*)"
        files, _ = QFileDialog.getOpenFileNames(
            self, "選擇音視頻文件", "", filter_str,
        )
        if files:
            self.files_added.emit([Path(f) for f in files])

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        from transcribe.file_transcriber import MEDIA_EXTENSIONS

        paths: list[Path] = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS:
                paths.append(p)
        if paths:
            self.files_added.emit(paths)


class TranscribeTab(QWidget):
    """文件轉錄頁籤 — 拖放匯入、文件列表、配置和轉錄控制。"""

    transcribe_requested = Signal(list, object)  # list[Path], FileConfig

    _COL_NAME = 0
    _COL_SIZE = 1
    _COL_STATUS = 2
    _COL_REMOVE = 3

    def __init__(
        self,
        config: FileConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._build_ui()
        self.load_config(config)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 拖放區 ──
        self._drop_zone = _DropZone(self)
        self._drop_zone.files_added.connect(self._add_files)
        layout.addWidget(self._drop_zone)

        # ── 文件列表 ──
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["文件名", "大小", "狀態", ""])
        self._table.horizontalHeader().setSectionResizeMode(
            self._COL_NAME, QHeaderView.ResizeMode.Stretch,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            self._COL_SIZE, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            self._COL_STATUS, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            self._COL_REMOVE, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        # ── 配置區 ──
        config_group = QGroupBox("轉錄設定")
        config_layout = QVBoxLayout(config_group)

        format_row = QHBoxLayout()
        self._srt_check = QCheckBox("SRT 字幕")
        self._txt_check = QCheckBox("TXT 文字")
        self._json_check = QCheckBox("JSON")
        format_row.addWidget(self._srt_check)
        format_row.addWidget(self._txt_check)
        format_row.addWidget(self._json_check)
        format_row.addStretch()
        config_layout.addLayout(format_row)

        self._llm_polish_check = QCheckBox("轉錄完成後交由 LLM 優化文字")
        config_layout.addWidget(self._llm_polish_check)

        layout.addWidget(config_group)

        # ── 操作按鈕 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._clear_btn = QPushButton("清空列表")
        self._clear_btn.clicked.connect(self._clear_files)
        btn_row.addWidget(self._clear_btn)

        self._start_btn = QPushButton("開始轉錄")
        self._start_btn.setStyleSheet(
            "QPushButton { background: #4a9eff; color: white; "
            "padding: 6px 20px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background: #3a8eef; }"
            "QPushButton:disabled { background: #ccc; color: #888; }"
        )
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)

        layout.addLayout(btn_row)

        self._update_buttons()

    # ─── 文件管理 ──────────────────────────────────────────

    def _add_files(self, paths: list[Path]) -> None:
        """添加文件到列表（去重）。"""
        existing = self._get_all_paths()
        for p in paths:
            if str(p) in existing:
                continue
            row = self._table.rowCount()
            self._table.insertRow(row)

            name_item = QTableWidgetItem(p.name)
            name_item.setData(Qt.ItemDataRole.UserRole, str(p))
            name_item.setToolTip(str(p))
            self._table.setItem(row, self._COL_NAME, name_item)

            size = p.stat().st_size if p.exists() else 0
            size_item = QTableWidgetItem(_human_size(size))
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, self._COL_SIZE, size_item)

            status_item = QTableWidgetItem("待轉錄")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, self._COL_STATUS, status_item)

            remove_btn = QPushButton("✕")
            remove_btn.setFixedWidth(30)
            remove_btn.setStyleSheet("border: none; color: #888;")
            remove_btn.clicked.connect(lambda _, r=row: self._remove_row(r))
            self._table.setCellWidget(row, self._COL_REMOVE, remove_btn)

        self._update_buttons()

    def _remove_row(self, row: int) -> None:
        if 0 <= row < self._table.rowCount():
            self._table.removeRow(row)
            self._reconnect_remove_buttons()
            self._update_buttons()

    def _reconnect_remove_buttons(self) -> None:
        """移除行後重新綁定所有刪除按鈕的 row 索引。"""
        for r in range(self._table.rowCount()):
            btn = self._table.cellWidget(r, self._COL_REMOVE)
            if btn:
                btn.clicked.disconnect()
                btn.clicked.connect(lambda _, row=r: self._remove_row(row))

    def _clear_files(self) -> None:
        self._table.setRowCount(0)
        self._update_buttons()

    def _get_all_paths(self) -> set[str]:
        paths: set[str] = set()
        for r in range(self._table.rowCount()):
            item = self._table.item(r, self._COL_NAME)
            if item:
                paths.add(item.data(Qt.ItemDataRole.UserRole))
        return paths

    def _update_buttons(self) -> None:
        has_files = self._table.rowCount() > 0
        self._start_btn.setEnabled(has_files)
        self._clear_btn.setEnabled(has_files)

    def _on_start(self) -> None:
        paths = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, self._COL_NAME)
            if item:
                paths.append(Path(item.data(Qt.ItemDataRole.UserRole)))
                status = self._table.item(r, self._COL_STATUS)
                if status:
                    status.setText("排隊中...")
        if paths:
            # 防連點：批次開始後 disable 按鈕，直到 MainWindow 收尾時呼叫 set_busy(False)
            self.set_busy(True)
            self.transcribe_requested.emit(paths, self.get_config())

    def set_busy(self, busy: bool) -> None:
        """批次轉錄進行中 → disable 按鈕，避免重複觸發 ASR worker 搶通道。"""
        self._start_btn.setEnabled(not busy and self._table.rowCount() > 0)
        self._clear_btn.setEnabled(not busy and self._table.rowCount() > 0)

    # ─── 外部更新 ──────────────────────────────────────────

    def update_file_status(self, file_path: str, status: str) -> None:
        """從外部更新指定文件的狀態列。"""
        for r in range(self._table.rowCount()):
            item = self._table.item(r, self._COL_NAME)
            if item and item.data(Qt.ItemDataRole.UserRole) == file_path:
                status_item = self._table.item(r, self._COL_STATUS)
                if status_item:
                    status_item.setText(status)
                break

    # ─── 配置 ──────────────────────────────────────────────

    def load_config(self, config: FileConfig) -> None:
        self._srt_check.setChecked(config.save_srt)
        self._txt_check.setChecked(config.save_txt)
        self._json_check.setChecked(config.save_json)
        self._llm_polish_check.setChecked(config.llm_polish)

    def get_config(self) -> FileConfig:
        return FileConfig(
            save_srt=self._srt_check.isChecked(),
            save_txt=self._txt_check.isChecked(),
            save_json=self._json_check.isChecked(),
            llm_polish=self._llm_polish_check.isChecked(),
        )
