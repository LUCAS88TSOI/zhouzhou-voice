"""
州州語音 - LLM 批量測速對話框

讓用戶勾選同一供應商下的多個模型，一鍵批量測試「測試連接」，
即時按回應最快排序，方便揀出最快的模型並一鍵套用。

- 勾選清單：用戶自選要測哪些模型（選擇會由 SettingsPanel 持久化保存）
- 背景測試：threading.Thread 逐個 LLMClient.test_connection，
  經 Qt Signal 回主線程更新（不卡 UI）
- 排行榜：成功者依耗時升序在前，失敗者排最後（見 llm.benchmark）
- 套用最快：一鍵把最快成功模型回填到設定頁
"""

from __future__ import annotations

import html
import threading
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from llm.benchmark import (
    BenchmarkRow, fastest_model, format_elapsed, is_rate_limited,
    sort_benchmark_rows,
)
from llm.client import LLMClient
from llm.provider import ProviderInfo
from utils.logger import get_logger

logger = get_logger("llm.benchmark.dialog")

# 每個模型測試逾時（秒）— 略高於單次測試，容忍冷啟動慢的端點
_PER_MODEL_TIMEOUT = 15
# 節流：每個模型測試之間的間隔（秒），避免請求過密觸發供應商限流（429）
_REQUEST_GAP = 1.0
# 撞 429（請求過密）時的額外退避秒數，退避後重試該模型一次
_RATE_LIMIT_BACKOFF = 5.0


class LlmBenchmarkDialog(QDialog):
    """同一供應商多模型批量測速。"""

    # 背景線程 → 主線程：(model, success, message, elapsed)
    _result_ready = Signal(str, bool, str, float)
    # 背景線程 → 主線程：全部測試完成
    _all_done = Signal()

    def __init__(
        self,
        provider_key: str,
        api_url: str,
        api_key: str,
        candidate_models: list[str],
        preselected: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Final：建構後不變，故背景測試線程讀取它們是線程安全的
        self._provider_key: Final[str] = provider_key
        self._api_url: Final[str] = api_url
        self._api_key: Final[str] = api_key
        # 去重保序的候選模型
        self._candidates = list(dict.fromkeys(m for m in candidate_models if m))

        # 首次（無保存）預設全選，方便一鍵測試；其後沿用保存的選擇
        preselected = preselected or []
        self._default_checked = (
            set(preselected) if preselected else set(self._candidates)
        )

        # 結果累積（model -> BenchmarkRow），用於即時排序顯示
        self._rows: dict[str, BenchmarkRow] = {}
        self._cancel = threading.Event()
        self._expected = 0
        self._received = 0
        # 套用最快時回填的模型（None 表示用戶未套用）
        self.chosen_model: str | None = None

        self.setWindowTitle("批量測速 — 揀最快模型")
        self.setMinimumWidth(460)
        self._build_ui()

        self._result_ready.connect(self._on_result)
        self._all_done.connect(self._on_all_done)

    # ─── UI ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("勾選要測試的模型（只測你選取的）："))

        # 搜尋過濾：模型清單太雜時可快速篩選（只影響顯示，不改已勾選狀態）
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("搜尋模型名稱…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setMaximumHeight(160)
        for model in self._candidates:
            item = QListWidgetItem(model)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if model in self._default_checked
                else Qt.CheckState.Unchecked
            )
            self._list.addItem(item)
        layout.addWidget(self._list)

        # 選取操作列
        sel_row = QHBoxLayout()
        btn_all = QPushButton("全選")
        btn_none = QPushButton("全不選")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addStretch(1)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888;")
        sel_row.addWidget(self._status_label)
        layout.addLayout(sel_row)

        # 結果排行榜
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["模型", "耗時", "狀態"])
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

        # 動作列
        act_row = QHBoxLayout()
        self._start_btn = QPushButton("開始測試")
        self._start_btn.clicked.connect(self._on_start)
        self._apply_btn = QPushButton("套用最快")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply_fastest)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(self.reject)
        act_row.addWidget(self._start_btn)
        act_row.addWidget(self._apply_btn)
        act_row.addStretch(1)
        act_row.addWidget(close_btn)
        layout.addLayout(act_row)

    def _set_all(self, checked: bool) -> None:
        """全選／全不選 —— 只作用於目前可見（未被搜尋過濾隱藏）的項。"""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._list.count()):
            item = self._list.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    def _apply_filter(self, text: str) -> None:
        """依搜尋字串顯示／隱藏模型項（不改動勾選狀態）。"""
        needle = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(needle != "" and needle not in item.text().lower())

    @property
    def selected_models(self) -> list[str]:
        """目前勾選的模型（供 SettingsPanel 持久化保存）。"""
        out: list[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.text())
        return out

    # ─── 測試流程 ──────────────────────────────────────────

    def _on_start(self) -> None:
        models = self.selected_models
        if not models:
            self._status_label.setText("請至少勾選一個模型")
            return

        self._rows.clear()
        self._table.setRowCount(0)
        self._cancel.clear()
        self._expected = len(models)
        self._received = 0
        self.chosen_model = None
        self._apply_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._set_list_enabled(False)
        self._status_label.setText(f"測試中 0/{self._expected}…")

        threading.Thread(
            target=self._worker, args=(models,), daemon=True
        ).start()

    def _worker(self, models: list[str]) -> None:
        """背景：逐個模型測試（節流 + 429 退避重試），結果經信號回主線程。"""
        for idx, model in enumerate(models):
            if self._cancel.is_set():
                break
            # 節流：與上一個請求保持間隔，避免請求過密觸發限流（可被取消即時打斷）
            if idx > 0 and self._cancel.wait(timeout=_REQUEST_GAP):
                break

            success, message, elapsed = self._test_one(model)
            # 撞 429（請求過密）→ 退避後重試一次，仍失敗才回報
            if not success and is_rate_limited(message):
                if self._cancel.wait(timeout=_RATE_LIMIT_BACKOFF):
                    break
                success, message, elapsed = self._test_one(model)
            self._result_ready.emit(model, success, message, elapsed)

        self._all_done.emit()

    def _test_one(self, model: str) -> tuple[bool, str, float]:
        """測試單一模型，回傳 (success, message, elapsed)；任何例外都轉成失敗結果。

        在背景線程呼叫；只讀取 __init__ 固定的 _provider_key/_api_url/_api_key
        （Final，建構後不變），故線程安全。
        """
        try:
            provider = ProviderInfo(
                key=self._provider_key,
                name="benchmark",
                api_url=self._api_url,
                api_key=self._api_key,
                model=model,
                enabled=True,
            )
            client = LLMClient(provider, timeout=_PER_MODEL_TIMEOUT)
            return client.test_connection(timeout=_PER_MODEL_TIMEOUT)
        except Exception as err:  # noqa: BLE001 — 任何失敗都回報 UI
            return False, f"測試異常：{err}", 0.0

    def _on_result(
        self, model: str, success: bool, message: str, elapsed: float,
    ) -> None:
        """單一模型測試完成（主線程 slot）。"""
        # 忽略 dialog 關閉後才抵達的過期 queued signal（避免操作已隱藏的控件）
        if self._cancel.is_set():
            return
        self._rows[model] = BenchmarkRow(
            model=model, success=success, elapsed=elapsed, message=message,
        )
        self._received += 1
        self._status_label.setText(f"測試中 {self._received}/{self._expected}…")
        self._refresh_table()

    def _on_all_done(self) -> None:
        """全部測試完成（主線程 slot）。"""
        # 忽略 dialog 關閉後才抵達的過期 queued signal
        if self._cancel.is_set():
            return
        self._start_btn.setEnabled(True)
        self._set_list_enabled(True)
        fastest = fastest_model(list(self._rows.values()))
        if fastest:
            self._apply_btn.setEnabled(True)
            self._status_label.setText(
                f"完成 ✓ 最快：{fastest}（{format_elapsed(self._rows[fastest].elapsed)}）"
            )
        else:
            self._status_label.setText("完成，但全部測試失敗")

    def _refresh_table(self) -> None:
        """依最快排序重繪結果表，最快列標綠。"""
        ranked = sort_benchmark_rows(list(self._rows.values()))
        self._table.setRowCount(len(ranked))
        for r, row in enumerate(ranked):
            is_fastest = row.success and r == 0
            cells = [
                row.model,
                format_elapsed(row.elapsed) if row.success else "—",
                ("✓ 成功" if row.success else "✗ 失敗") + (
                    "（最快）" if is_fastest else ""
                ),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                # escape 第三方回應片段，避免 Qt 把含 HTML 標籤的內容當 rich-text
                # 渲染（可能觸發資源載入），統一當純文字顯示
                item.setToolTip(html.escape(row.message))
                if is_fastest:
                    item.setForeground(Qt.GlobalColor.darkGreen)
                elif not row.success:
                    item.setForeground(Qt.GlobalColor.red)
                self._table.setItem(r, c, item)

    def _on_apply_fastest(self) -> None:
        fastest = fastest_model(list(self._rows.values()))
        if fastest:
            self.chosen_model = fastest
            self.accept()

    def _set_list_enabled(self, enabled: bool) -> None:
        self._list.setEnabled(enabled)

    # ─── 生命週期 ──────────────────────────────────────────

    def reject(self) -> None:  # noqa: D401 — Qt override
        self._cancel.set()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._cancel.set()
        super().closeEvent(event)
