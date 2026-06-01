# 州州語音 - 角色管理頁籤
# 提供 LLM 角色的瀏覽、新增、複製、刪除和提示詞編輯功能。
# 內建角色的提示詞也可編輯，修改存入 builtin_overrides。

from __future__ import annotations

import uuid
from typing import Any, Dict, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from utils.logger import get_logger

logger = get_logger("role_tab")


class RoleTab(QWidget):
    """
    角色管理頁籤 — 顯示所有角色（內建 + 自訂），支援編輯提示詞。

    內建角色：提示詞可編輯（修改存入 builtin_overrides），
    名稱和行為選項唯讀。提供「恢復預設」按鈕。
    自訂角色：所有欄位可自由編輯和刪除。

    Signals:
        role_changed(str) — 當前選中的角色 ID 變更時發射
    """

    role_changed = Signal(str)

    # ─── 初始化 ──────────────────────────────────────────

    def __init__(
        self,
        active_role_id: str,
        custom_roles: Sequence[Dict[str, Any]],
        builtin_overrides: Dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._active_role_id = active_role_id

        # 深拷貝用戶自訂角色（避免修改原始資料）
        self._custom_roles: list[Dict[str, Any]] = [
            dict(r) for r in custom_roles
        ]

        # 內建角色的提示詞覆蓋（role_id → 修改後的 prompt）
        self._builtin_overrides: Dict[str, str] = dict(
            builtin_overrides or {}
        )

        self._build_ui()
        self._refresh_role_list()

        # 選中當前使用的角色
        self._select_role_by_id(active_role_id)

        logger.info(
            "角色管理頁籤已建立，自訂角色數: %d, 內建覆蓋數: %d",
            len(self._custom_roles),
            len(self._builtin_overrides),
        )

    # ─── UI 建構 ─────────────────────────────────────────

    def _build_ui(self) -> None:
        """建構完整的角色管理介面。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── 頂部：角色選擇 + 操作按鈕 ──────────
        top_row = QHBoxLayout()

        top_row.addWidget(QLabel("角色："))
        self._role_combo = QComboBox()
        self._role_combo.setMinimumWidth(180)
        self._role_combo.currentIndexChanged.connect(self._on_role_selected)
        top_row.addWidget(self._role_combo, stretch=1)

        self._btn_new = QPushButton("新增")
        self._btn_new.setFixedWidth(60)
        self._btn_new.clicked.connect(self._on_new)
        top_row.addWidget(self._btn_new)

        self._btn_copy = QPushButton("複製")
        self._btn_copy.setFixedWidth(60)
        self._btn_copy.clicked.connect(self._on_copy)
        top_row.addWidget(self._btn_copy)

        self._btn_delete = QPushButton("刪除")
        self._btn_delete.setFixedWidth(60)
        self._btn_delete.clicked.connect(self._on_delete)
        top_row.addWidget(self._btn_delete)

        layout.addLayout(top_row)

        # ── 中間：角色詳情編輯 ─────────────────
        detail_group = QGroupBox("角色設定")
        detail_layout = QVBoxLayout(detail_group)

        # 名稱
        name_row = QFormLayout()
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("角色顯示名稱")
        name_row.addRow("名稱：", self._name_input)
        detail_layout.addLayout(name_row)

        # 系統提示詞
        prompt_label = QLabel("系統提示詞：")
        detail_layout.addWidget(prompt_label)

        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.setPlaceholderText("輸入 LLM 系統提示詞...")
        self._prompt_edit.setMinimumHeight(150)
        detail_layout.addWidget(self._prompt_edit)

        # 選項
        options_row = QHBoxLayout()
        self._history_check = QCheckBox("啟用多輪對話記憶")
        self._hotword_check = QCheckBox("啟用熱詞校正")
        options_row.addWidget(self._history_check)
        options_row.addWidget(self._hotword_check)
        options_row.addStretch()
        detail_layout.addLayout(options_row)

        # 內建角色提示（只有名稱和選項唯讀）
        self._builtin_hint_label = QLabel(
            "內建角色：提示詞可自由編輯，名稱和行為選項不可更改。"
        )
        self._builtin_hint_label.setStyleSheet(
            "color: #888; font-style: italic; padding: 4px;"
        )
        self._builtin_hint_label.setWordWrap(True)
        detail_layout.addWidget(self._builtin_hint_label)

        layout.addWidget(detail_group)

        # ── 重新潤色角色選擇 ────────────────────────
        repolish_row = QHBoxLayout()
        repolish_row.addWidget(QLabel("重新潤色角色："))
        self._repolish_role_combo = QComboBox()
        self._repolish_role_combo.setMinimumWidth(180)
        repolish_row.addWidget(self._repolish_role_combo, stretch=1)
        repolish_row.addStretch()
        layout.addLayout(repolish_row)

        # ── 底部按鈕列 ────────────────────────
        bottom_row = QHBoxLayout()

        self._btn_restore = QPushButton("恢復預設提示詞")
        self._btn_restore.setFixedWidth(130)
        self._btn_restore.clicked.connect(self._on_restore_default)
        bottom_row.addWidget(self._btn_restore)

        bottom_row.addStretch()

        self._btn_save = QPushButton("儲存修改")
        self._btn_save.setFixedWidth(100)
        self._btn_save.clicked.connect(self._on_save_edits)
        bottom_row.addWidget(self._btn_save)

        layout.addLayout(bottom_row)

    # ─── 角色列表管理 ────────────────────────────────────

    def _refresh_role_list(self) -> None:
        """重新填充下拉選單（內建 + 自訂角色）。"""
        from llm.roles import get_all_roles

        self._role_combo.blockSignals(True)
        self._role_combo.clear()

        all_roles = get_all_roles(
            self._custom_roles,
            self._builtin_overrides,
        )
        for role_id, role_cfg, is_builtin in all_roles:
            display = role_cfg.name or role_id
            if is_builtin:
                # 標記已修改的內建角色
                if role_id in self._builtin_overrides:
                    display = f"{display}（已修改）"
                else:
                    display = f"{display}（內建）"
            self._role_combo.addItem(display, userData=role_id)

        self._role_combo.blockSignals(False)

    def _select_role_by_id(self, role_id: str) -> None:
        """選中指定 ID 的角色。"""
        for i in range(self._role_combo.count()):
            if self._role_combo.itemData(i) == role_id:
                self._role_combo.setCurrentIndex(i)
                self._on_role_selected(i)
                return
        # fallback: 選第一個
        if self._role_combo.count() > 0:
            self._role_combo.setCurrentIndex(0)
            self._on_role_selected(0)

    def _on_role_selected(self, index: int) -> None:
        """當用戶切換角色時，載入該角色的資訊到編輯區。"""
        if index < 0:
            return

        role_id = self._role_combo.itemData(index)
        if role_id is None:
            return

        from llm.roles import BUILTIN_ROLES, get_all_roles

        is_builtin = role_id in BUILTIN_ROLES

        # 找出角色配置（含 overrides）
        all_roles = get_all_roles(
            self._custom_roles,
            self._builtin_overrides,
        )
        role_cfg = None
        for rid, cfg, _ in all_roles:
            if rid == role_id:
                role_cfg = cfg
                break

        if role_cfg is None:
            return

        # 填入資訊
        self._name_input.setText(role_cfg.name)
        self._prompt_edit.setPlainText(role_cfg.system_prompt)
        self._history_check.setChecked(role_cfg.enable_history)
        self._hotword_check.setChecked(role_cfg.enable_hotwords)

        # 控制可編輯性
        if is_builtin:
            # 內建角色：提示詞可編輯，名稱和選項唯讀
            self._name_input.setReadOnly(True)
            self._prompt_edit.setReadOnly(False)  # 提示詞可以改！
            self._history_check.setEnabled(False)
            self._hotword_check.setEnabled(False)
            self._btn_delete.setEnabled(False)
            self._btn_save.setEnabled(True)
            self._btn_restore.setVisible(True)
            self._builtin_hint_label.setVisible(True)
        else:
            # 自訂角色：全部可編輯
            self._name_input.setReadOnly(False)
            self._prompt_edit.setReadOnly(False)
            self._history_check.setEnabled(True)
            self._hotword_check.setEnabled(True)
            self._btn_delete.setEnabled(True)
            self._btn_save.setEnabled(True)
            self._btn_restore.setVisible(False)
            self._builtin_hint_label.setVisible(False)

        self._active_role_id = role_id
        self.role_changed.emit(role_id)

    # ─── 操作按鈕 ────────────────────────────────────────

    def _on_new(self) -> None:
        """新增一個空白自訂角色。"""
        new_id = f"custom_{uuid.uuid4().hex[:8]}"
        new_role: Dict[str, Any] = {
            "id": new_id,
            "name": "新角色",
            "system_prompt": "",
            "enable_history": False,
            "enable_hotwords": True,
        }
        self._custom_roles.append(new_role)
        self._refresh_role_list()
        self._select_role_by_id(new_id)
        logger.info("新增自訂角色: %s", new_id)

    def _on_copy(self) -> None:
        """複製當前選中的角色為新的自訂角色。"""
        from llm.roles import get_all_roles

        current_id = self._role_combo.currentData()
        if current_id is None:
            return

        # 找到當前角色配置（含 overrides）
        all_roles = get_all_roles(
            self._custom_roles,
            self._builtin_overrides,
        )
        source_cfg = None
        for rid, cfg, _ in all_roles:
            if rid == current_id:
                source_cfg = cfg
                break

        if source_cfg is None:
            return

        new_id = f"custom_{uuid.uuid4().hex[:8]}"
        new_role: Dict[str, Any] = {
            "id": new_id,
            "name": f"{source_cfg.name}（副本）",
            "system_prompt": source_cfg.system_prompt,
            "enable_history": source_cfg.enable_history,
            "enable_hotwords": source_cfg.enable_hotwords,
        }
        self._custom_roles.append(new_role)
        self._refresh_role_list()
        self._select_role_by_id(new_id)
        logger.info("複製角色 %s → %s", current_id, new_id)

    def _on_delete(self) -> None:
        """刪除當前選中的自訂角色。"""
        from llm.roles import BUILTIN_ROLES

        current_id = self._role_combo.currentData()
        if current_id is None or current_id in BUILTIN_ROLES:
            return

        reply = QMessageBox.question(
            self,
            "確認刪除",
            f"確定要刪除角色「{self._name_input.text()}」嗎？此操作無法復原。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        self._custom_roles = [
            r for r in self._custom_roles if r.get("id") != current_id
        ]
        self._refresh_role_list()

        # 如果刪除的是正在使用的角色，切回 default
        if self._active_role_id == current_id:
            self._select_role_by_id("default")

        logger.info("刪除自訂角色: %s", current_id)

    def _on_save_edits(self) -> None:
        """將編輯區的修改存回（內建 → overrides / 自訂 → custom_roles）。"""
        from llm.roles import BUILTIN_ROLES

        current_id = self._role_combo.currentData()
        if current_id is None:
            return

        if current_id in BUILTIN_ROLES:
            # 內建角色：只存提示詞到 overrides
            new_prompt = self._prompt_edit.toPlainText()
            self._builtin_overrides[current_id] = new_prompt
            logger.info("內建角色提示詞已修改: %s", current_id)
        else:
            # 自訂角色：存全部欄位
            for role in self._custom_roles:
                if role.get("id") == current_id:
                    role["name"] = (
                        self._name_input.text().strip() or current_id
                    )
                    role["system_prompt"] = self._prompt_edit.toPlainText()
                    role["enable_history"] = self._history_check.isChecked()
                    role["enable_hotwords"] = self._hotword_check.isChecked()
                    break
            logger.info("自訂角色已儲存: %s", current_id)

        # 重新整理下拉選單（名稱/狀態可能變了）
        self._refresh_role_list()
        self._select_role_by_id(current_id)

    def _on_restore_default(self) -> None:
        """恢復內建角色的預設提示詞。"""
        from llm.roles import BUILTIN_ROLES, get_builtin_prompt

        current_id = self._role_combo.currentData()
        if current_id is None or current_id not in BUILTIN_ROLES:
            return

        reply = QMessageBox.question(
            self,
            "恢復預設",
            f"確定要將角色提示詞恢復為預設值嗎？\n你的修改將會被覆蓋。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        # 移除 override
        self._builtin_overrides.pop(current_id, None)

        # 重新載入原始提示詞
        original_prompt = get_builtin_prompt(current_id)
        self._prompt_edit.setPlainText(original_prompt)

        self._refresh_role_list()
        self._select_role_by_id(current_id)
        logger.info("內建角色已恢復預設: %s", current_id)

    # ─── 公開方法（供 SettingsDialog 讀取） ──────────────

    def get_active_role_id(self) -> str:
        """取得當前選中的角色 ID。"""
        return self._active_role_id

    def get_custom_roles(self) -> list[Dict[str, Any]]:
        """取得所有自訂角色的資料（用於存入 config）。"""
        from llm.roles import BUILTIN_ROLES

        # 先把當前編輯中的自訂角色內容存回
        current_id = self._role_combo.currentData()
        if current_id and current_id not in BUILTIN_ROLES:
            for role in self._custom_roles:
                if role.get("id") == current_id:
                    role["name"] = (
                        self._name_input.text().strip() or current_id
                    )
                    role["system_prompt"] = self._prompt_edit.toPlainText()
                    role["enable_history"] = self._history_check.isChecked()
                    role["enable_hotwords"] = self._hotword_check.isChecked()
                    break

        return [dict(r) for r in self._custom_roles]

    def get_builtin_overrides(self) -> Dict[str, str]:
        """取得所有內建角色的提示詞修改（用於存入 config）。"""
        from llm.roles import BUILTIN_ROLES

        # 如果當前正在編輯內建角色，先存回
        current_id = self._role_combo.currentData()
        if current_id and current_id in BUILTIN_ROLES:
            self._builtin_overrides[current_id] = (
                self._prompt_edit.toPlainText()
            )

        return dict(self._builtin_overrides)

    # ─── 重新潤色角色 ────────────────────────────────────

    def refresh_repolish_role_combo(self, selected_role: str = "") -> None:
        """刷新重新潤色角色下拉選單。"""
        from llm.roles import get_all_roles

        self._repolish_role_combo.blockSignals(True)
        self._repolish_role_combo.clear()
        self._repolish_role_combo.addItem("與主角色相同", "")

        all_roles = get_all_roles(
            self._custom_roles,
            self._builtin_overrides,
        )
        for role_id, role_cfg, is_builtin in all_roles:
            display = role_cfg.name or role_id
            prefix = "（內建）" if is_builtin else "（自訂）"
            self._repolish_role_combo.addItem(f"{display}{prefix}", role_id)

        # 設置選中項
        if selected_role:
            idx = self._repolish_role_combo.findData(selected_role)
            if idx >= 0:
                self._repolish_role_combo.setCurrentIndex(idx)
        else:
            self._repolish_role_combo.setCurrentIndex(0)

        self._repolish_role_combo.blockSignals(False)

    def get_repolish_role(self) -> str:
        """取得選中的重新潤色角色 ID。"""
        return self._repolish_role_combo.currentData() or ""
