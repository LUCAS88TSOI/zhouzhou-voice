"""
asr_tab -- ASR 模型資訊與選擇頁籤。

顯示已安裝模型、資源用量，以及可下載模型列表（含一鍵下載功能）。
嵌入於 SettingsPanel。

用法：
    tab = ASRModelTab(models_dir=Path("models"), current_key="sensevoice-small-int8")
    selected_key = tab.get_selected_model_key()
"""

from __future__ import annotations

import html
import threading
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.model_catalog import (
    ModelInfo,
    get_actual_size_mb,
    get_downloadable_models,
    get_installed_models,
)
from utils.logger import get_logger

logger = get_logger("asr_tab")


class ASRModelTab(QWidget):
    """
    語音識別模型設定頁籤。

    顯示：
    - 已安裝模型（可切換選擇）
    - 模型詳細資訊與資源用量
    - 可下載模型列表（含一鍵下載按鈕與進度）
    """

    # 跨線程安全 Signal（背景下載線程 → 主線程 UI 更新）
    _sig_progress = Signal(str, int)        # (model_key, percent 0-100)
    _sig_done = Signal(str, bool, str)      # (model_key, success, error_message)

    def __init__(
        self,
        models_dir: Path,
        current_key: str = "sensevoice-small-int8",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._models_dir = models_dir
        self._current_key = current_key

        # 掃描磁碟
        self._installed: List[ModelInfo] = get_installed_models(models_dir)
        self._downloadable: List[ModelInfo] = get_downloadable_models(models_dir)

        # 下載按鈕字典：model_key → QPushButton
        self._download_btns: Dict[str, QPushButton] = {}
        # 進行中的下載 key 集合（防並發重複觸發）
        self._active_downloads: set[str] = set()

        self._setup_ui()
        self._select_current_model()

        # 連接跨線程 Signal
        self._sig_progress.connect(self._on_download_progress)
        self._sig_done.connect(self._on_download_done)

    # ─── Public API ────────────────────────────────────

    def get_selected_model_key(self) -> str:
        """返回用戶當前選擇的模型 key。"""
        idx = self._model_combo.currentIndex()
        if idx < 0 or idx >= len(self._installed):
            return self._current_key
        return self._installed[idx].key

    def refresh_models(self) -> None:
        """重新掃描磁碟，刷新已安裝/可下載列表與 UI。"""
        self._installed = get_installed_models(self._models_dir)
        self._downloadable = get_downloadable_models(self._models_dir)

        # 刷新已安裝 combo
        prev_key = self.get_selected_model_key()
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for m in self._installed:
            self._model_combo.addItem(m.name, m.key)
        if not self._installed:
            self._model_combo.addItem("（未偵測到已安裝模型）")
            self._model_combo.setEnabled(False)
        else:
            self._model_combo.setEnabled(True)
        self._model_combo.blockSignals(False)

        # 嘗試還原選中
        restored = False
        for i, m in enumerate(self._installed):
            if m.key == prev_key:
                self._model_combo.setCurrentIndex(i)
                self._refresh_detail(m)
                restored = True
                break
        if not restored and self._installed:
            self._model_combo.setCurrentIndex(0)
            self._refresh_detail(self._installed[0])

        # 重建下載列表
        self._rebuild_download_list()

    # ─── UI 建構 ───────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── 當前使用模型 ──────────────────────────
        active_group = QGroupBox("當前使用的語音識別模型")
        active_layout = QVBoxLayout(active_group)

        # 模型選擇下拉
        combo_row = QHBoxLayout()
        combo_label = QLabel("選擇模型：")
        combo_label.setFont(QFont("Microsoft YaHei", 10))
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(260)
        for m in self._installed:
            self._model_combo.addItem(m.name, m.key)

        if not self._installed:
            self._model_combo.addItem("（未偵測到已安裝模型）")
            self._model_combo.setEnabled(False)

        combo_row.addWidget(combo_label)
        combo_row.addWidget(self._model_combo, stretch=1)
        active_layout.addLayout(combo_row)

        # 模型詳細資訊
        self._detail_label = QLabel()
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextFormat(Qt.TextFormat.RichText)
        self._detail_label.setFont(QFont("Microsoft YaHei", 9))
        active_layout.addWidget(self._detail_label)

        layout.addWidget(active_group)

        # ── 效能資訊 ─────────────────────────────
        perf_group = QGroupBox("效能與資源用量")
        perf_layout = QVBoxLayout(perf_group)

        self._perf_label = QLabel()
        self._perf_label.setWordWrap(True)
        self._perf_label.setTextFormat(Qt.TextFormat.RichText)
        self._perf_label.setFont(QFont("Microsoft YaHei", 9))
        perf_layout.addWidget(self._perf_label)

        layout.addWidget(perf_group)

        # ── 可下載模型 ────────────────────────────
        self._download_group = QGroupBox("更多可用模型（免費下載）")
        download_layout = QVBoxLayout(self._download_group)

        # 可滾動的下載列表容器
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(220)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self._download_list_widget = QWidget()
        self._download_list_layout = QVBoxLayout(self._download_list_widget)
        self._download_list_layout.setSpacing(6)
        self._download_list_layout.setContentsMargins(0, 0, 0, 0)
        self._download_list_layout.addStretch()

        scroll.setWidget(self._download_list_widget)
        download_layout.addWidget(scroll)

        layout.addWidget(self._download_group)

        layout.addStretch()

        # 連接信號
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)

        # 初始填充下載列表
        self._rebuild_download_list()

    def _rebuild_download_list(self) -> None:
        """清空並重建可下載模型列表（含下載按鈕）。"""
        # 移除現有 row widget（保留最後的 stretch）
        while self._download_list_layout.count() > 1:
            item = self._download_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._download_btns.clear()

        if not self._downloadable:
            placeholder = QLabel("<i>所有已知模型均已安裝。</i>")
            placeholder.setTextFormat(Qt.TextFormat.RichText)
            placeholder.setFont(QFont("Microsoft YaHei", 9))
            self._download_list_layout.insertWidget(0, placeholder)
            return

        for i, model in enumerate(self._downloadable):
            row = self._make_download_row(model)
            self._download_list_layout.insertWidget(i, row)

    def _make_download_row(self, model: ModelInfo) -> QWidget:
        """建立單個可下載模型的 row widget（資訊 + 下載按鈕）。"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 4, 4, 4)

        # 左側：模型資訊
        info_html = (
            f"<b>{model.name}</b> — {model.size_mb} MB &nbsp;"
            f"<span style='color:#888;font-size:11px;'>{model.license}</span><br>"
            f"<span style='color:#555;font-size:11px;'>{model.description}</span><br>"
            f"<span style='color:#888;font-size:10px;'>"
            f"語言：{model.languages} | 準確度：{model.accuracy} | 速度：{model.speed}"
            f"</span>"
        )
        info_label = QLabel(info_html)
        info_label.setTextFormat(Qt.TextFormat.RichText)
        info_label.setWordWrap(True)
        info_label.setFont(QFont("Microsoft YaHei", 9))

        # 右側：下載按鈕（若下載進行中則禁用）
        in_progress = model.key in self._active_downloads
        btn = QPushButton("下載中..." if in_progress else "下載")
        btn.setFixedWidth(70)
        btn.setFixedHeight(32)
        btn.setEnabled(not in_progress)
        btn.clicked.connect(lambda checked=False, m=model: self._start_download(m))
        self._download_btns[model.key] = btn

        h.addWidget(info_label, stretch=1)
        h.addWidget(btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        # 底部分隔線（非最後一項）
        row.setStyleSheet("QWidget { border-bottom: 1px solid #e0e0e0; }")
        return row

    # ─── 下載邏輯 ──────────────────────────────────────

    def _start_download(self, model: ModelInfo) -> None:
        """點擊下載按鈕：啟動背景下載線程（防並發重複觸發）。"""
        if model.key in self._active_downloads:
            return
        self._active_downloads.add(model.key)

        btn = self._download_btns.get(model.key)
        if btn:
            btn.setEnabled(False)
            btn.setText("下載中 0%")

        logger.info("開始下載模型: %s (%s)", model.name, model.key)

        progress_cb = lambda pct: self._sig_progress.emit(model.key, pct)
        done_cb = lambda ok, err: self._sig_done.emit(model.key, ok, err)

        if model.download_files:
            from core.model_downloader import download_multi_files
            threading.Thread(
                target=download_multi_files,
                args=(model.download_files, self._models_dir, model.model_dir,
                      progress_cb, done_cb),
                daemon=True,
                name="model-download",
            ).start()
        else:
            from core.model_downloader import download_and_extract
            threading.Thread(
                target=download_and_extract,
                args=(model.download_url, self._models_dir, model.model_dir,
                      progress_cb, done_cb),
                daemon=True,
                name="model-download",
            ).start()

    @Slot(str, int)
    def _on_download_progress(self, key: str, pct: int) -> None:
        """更新下載進度（主線程）。"""
        btn = self._download_btns.get(key)
        if btn:
            btn.setText(f"下載中 {pct}%")

    @Slot(str, bool, str)
    def _on_download_done(self, key: str, ok: bool, error: str) -> None:
        """下載完成處理（主線程）。"""
        self._active_downloads.discard(key)
        if ok:
            logger.info("模型下載成功: %s", key)
            self.refresh_models()
        else:
            logger.error("模型下載失敗 %s: %s", key, error)
            btn = self._download_btns.get(key)
            if btn:
                btn.setEnabled(True)
                btn.setText("下載")
            # 顯示錯誤在 detail label（臨時）
            self._detail_label.setText(
                f"<span style='color:red;'>⚠ 下載失敗：{html.escape(error)}</span>"
            )

    # ─── 內部邏輯 ──────────────────────────────────────

    def _select_current_model(self) -> None:
        """選中配置中的當前模型，並刷新詳情。"""
        for i, m in enumerate(self._installed):
            if m.key == self._current_key:
                self._model_combo.setCurrentIndex(i)
                self._refresh_detail(m)
                return

        # 若當前 key 不在已安裝列表，選第一個
        if self._installed:
            self._model_combo.setCurrentIndex(0)
            self._refresh_detail(self._installed[0])
        else:
            self._refresh_detail(None)

    def _on_model_changed(self, index: int) -> None:
        """用戶切換模型時更新詳情。"""
        if 0 <= index < len(self._installed):
            self._refresh_detail(self._installed[index])

    def _refresh_detail(self, model: Optional[ModelInfo]) -> None:
        """更新模型詳情和效能資訊。"""
        if model is None:
            self._detail_label.setText(
                "<i>未偵測到已安裝的語音識別模型。</i>"
            )
            self._perf_label.setText("")
            return

        # 模型詳情
        actual_size = get_actual_size_mb(self._models_dir, model)
        size_str = f"{actual_size:.1f}" if actual_size else str(model.size_mb)

        detail_html = (
            f"<b>{model.name}</b><br>"
            f"{model.description}<br><br>"
            f"<b>支援語言：</b>{model.languages}<br>"
            f"<b>引擎類型：</b>{model.engine_type}<br>"
            f"<b>授權協議：</b>{model.license}<br>"
            f"<b>狀態：</b><span style='color:#4CAF50;'>已安裝</span>"
        )
        self._detail_label.setText(detail_html)

        # 效能資訊
        perf_html = (
            f"<table cellspacing='6'>"
            f"<tr><td><b>模型大小（磁碟）：</b></td>"
            f"<td>{size_str} MB</td></tr>"
            f"<tr><td><b>預估記憶體用量：</b></td>"
            f"<td>~{model.memory_mb} MB</td></tr>"
            f"<tr><td><b>CPU 線程：</b></td>"
            f"<td>{model.cpu_threads} 個</td></tr>"
            f"<tr><td><b>準確度：</b></td>"
            f"<td>{model.accuracy}</td></tr>"
            f"<tr><td><b>推理速度：</b></td>"
            f"<td>{model.speed}</td></tr>"
            f"</table>"
        )
        self._perf_label.setText(perf_html)
