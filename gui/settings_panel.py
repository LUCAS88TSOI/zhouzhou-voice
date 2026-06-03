"""
CC語音 - 設定面板（可嵌入 Widget）

SettingsPanel 是 SettingsDialog 的無框版本，可直接嵌入到任何父 Widget。
不依賴 QDialog，不含 OK/Cancel 按鈕列。
由外層容器（如 MainWindow 的設定頁）負責「儲存」和「返回」邏輯。

用法：
    panel = SettingsPanel(config=current_config, parent=parent_widget)
    panel.load_config(updated_config)   # 每次顯示前刷新
    new_config = panel.get_config()     # 讀取 UI 值
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.recording_db import RecordingDatabase
from gui.widgets.asr_tab import ASRModelTab
from gui.widgets.history_tab import HistoryTab
from gui.widgets.hotword_tab import HotwordTab
from gui.widgets.provider_combo import ProviderCombo
from gui.widgets.role_tab import RoleTab
from gui.widgets.shortcut_input import ShortcutInput
from utils.config import (
    ASRConfig,
    AppConfig,
    HistoryConfig,
    HotwordConfig,
    LLMConfig,
    OutputConfig,
    ShortcutConfig,
)
from utils.logger import get_logger

logger = get_logger("settings_panel")

# 專案與贊助連結（單一來源，README 與測試共用此處定義的值）
PROJECT_URL = "https://github.com/LUCAS88TSOI/zhouzhou-voice"
DONATE_URL = "https://github.com/LUCAS88TSOI/zhouzhou-voice#贊助支持"
PAYME_URL = "https://payme.hsbc/289b982f31514bdfafa7d3e597aa1ab2"


class SettingsPanel(QWidget):
    """
    可嵌入的設定面板（QWidget）。

    包含六個頁籤：快捷鍵、語音識別、LLM、角色、輸出、關於。
    所有配置修改遵循不可變原則：透過 dataclasses.replace() 建立新物件。
    """

    mic_test_requested = Signal()  # 請求開啟麥克風測試
    # 背景抓取模型清單完成：(provider_key, models, error_msg)
    _models_fetched = Signal(str, list, str)

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        recording_db: RecordingDatabase | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config

        # 服務商欄位暫存快取 & 當前編輯中的 provider key
        self._provider_cache_store: Dict[str, Dict[str, str]] = {}
        self._current_provider_key: str = ""
        # 正在背景抓取模型清單的 provider（防重複觸發）
        self._fetching_models: set[str] = set()
        self._models_fetched.connect(self._on_models_fetched)

        # ── 主佈局 ────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # 模型目錄（統一路徑解析）
        from utils.paths import MODELS_DIR
        self._models_dir = MODELS_DIR

        # 錄音歷史資料庫（優先使用外部傳入的實例）
        self._recording_db = recording_db or RecordingDatabase()

        # 建立六個頁籤
        self._tab_shortcut = self._build_shortcut_tab()
        self._tab_asr = ASRModelTab(
            models_dir=self._models_dir,
            current_key=config.asr.model,
            parent=self,
        )
        self._tab_llm = self._build_llm_tab()
        self._tab_role = RoleTab(
            active_role_id=config.llm.active_role,
            custom_roles=config.llm.custom_roles,
            builtin_overrides=config.llm.builtin_overrides,
            parent=self,
        )
        self._tab_hotword = HotwordTab(
            config=config.hotword,
            parent=self,
        )
        self._tab_output = self._build_output_tab()
        self._tab_history = HistoryTab(self._recording_db, parent=self)
        self._tab_about = self._build_about_tab()

        self._tabs.addTab(self._tab_shortcut, "快捷鍵")
        self._tabs.addTab(self._tab_asr, "語音識別")
        self._tabs.addTab(self._tab_llm, "LLM")
        self._tabs.addTab(self._tab_role, "角色")
        self._tabs.addTab(self._tab_hotword, "熱詞")
        self._tabs.addTab(self._tab_output, "輸出")
        self._tabs.addTab(self._tab_history, "錄音歷史")
        self._tabs.addTab(self._tab_about, "關於")

        # ── 填入當前配置 ──────────────────────────
        self._load_from_config(config)

        logger.debug("SettingsPanel 已建立")

    # ─────────────────────────────────────────────
    #  公開 API
    # ─────────────────────────────────────────────

    def refresh_history(self) -> None:
        """刷新錄音歷史列表。"""
        self._tab_history._refresh_list()

    def load_config(self, config: AppConfig) -> None:
        """
        用新的 AppConfig 刷新所有 UI 控件。

        每次顯示設定頁面前應呼叫，確保顯示最新的配置值。
        同時清空服務商快取，避免顯示上次未儲存的殘留值。
        注意：角色頁籤（RoleTab）和 ASR 頁籤維護自身狀態，不在此重置。

        Args:
            config: 最新的 AppConfig 快照
        """
        self._config = config
        # 清空暫存快取，確保顯示最新 config 而非未儲存的舊值
        self._provider_cache_store.clear()
        self._current_provider_key = ""

        self._load_from_config(config)
        logger.debug("SettingsPanel 已刷新配置")

    def get_config(self) -> AppConfig:
        """
        從當前 UI 值建立新的 AppConfig（不可變）。

        Returns:
            全新的 AppConfig 實例
        """
        providers = self._build_updated_providers()

        new_shortcut = ShortcutConfig(
            key=self._key_input.get_key(),
            threshold=round(self._threshold_spin.value(), 1),
            suppress=self._suppress_check.isChecked(),
            repolish_key=self._repolish_key_input.get_key(),
            repolish_instant=self._repolish_instant_combo.currentIndex() == 0,
        )

        new_llm = LLMConfig(
            enabled=self._llm_enabled_check.isChecked(),
            active_provider=self._provider_combo.get_provider_key(),
            active_role=self._tab_role.get_active_role_id(),
            stop_key=self._stop_key_combo.get_key(),
            temperature=round(self._temperature_spin.value(), 1),
            max_tokens=self._max_tokens_spin.value(),
            top_p=round(self._top_p_spin.value(), 2),
            frequency_penalty=round(self._freq_penalty_spin.value(), 1),
            presence_penalty=round(self._pres_penalty_spin.value(), 1),
            do_sample=self._do_sample_check.isChecked(),
            providers=providers,
            custom_roles=self._tab_role.get_custom_roles(),
            builtin_overrides=self._tab_role.get_builtin_overrides(),
            repolish_provider=self._repolish_provider_combo.currentData() or "",
            repolish_model=self._repolish_model_input.currentText().strip() if self._repolish_provider_combo.currentData() else "",
            repolish_role=self._tab_role.get_repolish_role(),
        )

        new_output = OutputConfig(
            paste_mode=self._paste_mode_check.isChecked(),
            restore_clip=self._restore_clip_check.isChecked(),
            traditional_convert=self._trad_convert_check.isChecked(),
            traditional_locale=self._locale_combo.currentText(),
            format_num=self._format_num_check.isChecked(),
            format_spell=self._format_spell_check.isChecked(),
            trash_punc=self._trash_punc_input.text(),
        )

        new_asr = ASRConfig(
            model=self._tab_asr.get_selected_model_key(),
            language=self._config.asr.language,
        )

        history_values = self._tab_history.get_config_values()
        new_history = HistoryConfig(
            enabled=history_values["enabled"],
            min_duration=history_values["min_duration"],
            max_records=self._config.history.max_records,
            auto_cleanup_days=self._config.history.auto_cleanup_days,
        )

        new_hotword = self._tab_hotword.get_hotword_config()

        return replace(
            self._config,
            shortcut=new_shortcut,
            asr=new_asr,
            llm=new_llm,
            output=new_output,
            history=new_history,
            hotword=new_hotword,
            file=self._config.file,
        )

    # ─────────────────────────────────────────────
    #  頁籤 1：快捷鍵
    # ─────────────────────────────────────────────

    def _build_shortcut_tab(self) -> QWidget:
        """建構快捷鍵設定頁籤（包裹在 QScrollArea 確保所有欄位可見）。"""
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(8, 8, 8, 8)

        self._key_input = ShortcutInput()
        form.addRow("觸發按鍵：", self._key_input)

        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0.1, 1.0)
        self._threshold_spin.setSingleStep(0.1)
        self._threshold_spin.setDecimals(1)
        self._threshold_spin.setSuffix(" 秒")
        form.addRow("長按門檻：", self._threshold_spin)

        self._suppress_check = QCheckBox("攔截原按鍵（停用後 CapsLock 可即時切換大小寫）")
        form.addRow(self._suppress_check)

        self._repolish_key_input = ShortcutInput()
        form.addRow("重新潤色鍵：", self._repolish_key_input)

        self._repolish_instant_combo = QComboBox()
        self._repolish_instant_combo.addItems(["速發（鬆開觸發）", "長按（按住 0.3 秒）"])
        form.addRow("重新潤色模式：", self._repolish_instant_combo)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    # ─────────────────────────────────────────────
    #  頁籤 2：LLM
    # ─────────────────────────────────────────────

    def _build_llm_tab(self) -> QWidget:
        """建構 LLM 設定頁籤。"""
        page = QWidget()
        layout = QVBoxLayout(page)

        self._llm_enabled_check = QCheckBox("啟用 LLM 潤色")
        layout.addWidget(self._llm_enabled_check)

        # ── 服務商群組 ────────────────────────────
        provider_group = QGroupBox("服務商")
        provider_form = QFormLayout(provider_group)

        self._provider_combo = ProviderCombo(self._config.llm.providers)
        provider_form.addRow("服務商：", self._provider_combo)

        self._api_url_input = QLineEdit()
        self._api_url_input.setPlaceholderText("https://api.example.com/v1")
        provider_form.addRow("API URL：", self._api_url_input)

        key_row = QHBoxLayout()
        self._api_key_input = QLineEdit()
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_input.setPlaceholderText("輸入 API Key")
        self._api_key_input.textChanged.connect(self._on_api_key_changed)
        key_row.addWidget(self._api_key_input)

        self._toggle_key_btn = QPushButton("顯示")
        self._toggle_key_btn.setFixedWidth(50)
        self._toggle_key_btn.setCheckable(True)
        self._toggle_key_btn.toggled.connect(self._toggle_api_key_visibility)
        key_row.addWidget(self._toggle_key_btn)

        provider_form.addRow("API Key：", key_row)

        model_row = QHBoxLayout()
        self._model_input = QComboBox()
        self._model_input.setEditable(True)
        self._model_input.lineEdit().setPlaceholderText("模型名稱")
        model_row.addWidget(self._model_input, stretch=1)
        self._refresh_models_btn = QPushButton("🔄")
        self._refresh_models_btn.setFixedWidth(28)
        self._refresh_models_btn.setToolTip("從供應商取得最新模型清單")
        self._refresh_models_btn.clicked.connect(self._on_refresh_models_clicked)
        model_row.addWidget(self._refresh_models_btn)
        self._del_model_btn = QPushButton("×")
        self._del_model_btn.setFixedWidth(28)
        self._del_model_btn.setToolTip("刪除此模型歷史記錄")
        self._del_model_btn.clicked.connect(self._on_delete_model_history)
        model_row.addWidget(self._del_model_btn)
        provider_form.addRow("模型：", model_row)

        self._test_btn = QPushButton("測試連接")
        self._test_btn.setFixedWidth(100)
        self._test_btn.clicked.connect(self._on_test_connection)
        self._test_result_label = QLabel("")
        self._test_result_label.setWordWrap(True)
        self._test_result_label.setStyleSheet("font-size: 11px;")

        self._reset_provider_btn = QPushButton("重置")
        self._reset_provider_btn.setFixedWidth(60)
        self._reset_provider_btn.setToolTip("重置此供應商為預設值")
        self._reset_provider_btn.clicked.connect(self._on_reset_provider)

        test_row = QHBoxLayout()
        test_row.addWidget(self._test_btn)
        test_row.addWidget(self._reset_provider_btn)
        test_row.addWidget(self._test_result_label, stretch=1)
        provider_form.addRow(test_row)

        layout.addWidget(provider_group)

        # ── 參數群組 ──────────────────────────────
        param_group = QGroupBox("生成參數")
        param_form = QFormLayout(param_group)

        self._temperature_spin = QDoubleSpinBox()
        self._temperature_spin.setRange(0.0, 2.0)
        self._temperature_spin.setSingleStep(0.1)
        self._temperature_spin.setDecimals(1)
        param_form.addRow("Temperature：", self._temperature_spin)

        self._max_tokens_spin = QSpinBox()
        self._max_tokens_spin.setRange(128, 4096)
        self._max_tokens_spin.setSingleStep(128)
        param_form.addRow("Max Tokens：", self._max_tokens_spin)

        self._top_p_spin = QDoubleSpinBox()
        self._top_p_spin.setRange(0.0, 1.0)
        self._top_p_spin.setSingleStep(0.05)
        self._top_p_spin.setDecimals(2)
        param_form.addRow("Top P：", self._top_p_spin)

        self._freq_penalty_spin = QDoubleSpinBox()
        self._freq_penalty_spin.setRange(-2.0, 2.0)
        self._freq_penalty_spin.setSingleStep(0.1)
        self._freq_penalty_spin.setDecimals(1)
        param_form.addRow("Frequency Penalty：", self._freq_penalty_spin)

        self._pres_penalty_spin = QDoubleSpinBox()
        self._pres_penalty_spin.setRange(-2.0, 2.0)
        self._pres_penalty_spin.setSingleStep(0.1)
        self._pres_penalty_spin.setDecimals(1)
        param_form.addRow("Presence Penalty：", self._pres_penalty_spin)

        self._do_sample_check = QCheckBox("啟用隨機採樣 (do_sample)")
        param_form.addRow("", self._do_sample_check)

        self._stop_key_combo = ShortcutInput()
        param_form.addRow("停止鍵：", self._stop_key_combo)

        layout.addWidget(param_group)

        # ── 重新潤色群組 ──────────────────────────
        repolish_group = QGroupBox("重新潤色")
        repolish_layout = QVBoxLayout(repolish_group)

        repolish_form = QFormLayout()
        repolish_layout.addLayout(repolish_form)

        # 使用普通 QComboBox 來正確處理「與主服務商相同」選項
        self._repolish_provider_combo = QComboBox()
        self._repolish_provider_combo.addItem("與主服務商相同", "")
        for pkey, pinfo in self._config.llm.providers.items():
            self._repolish_provider_combo.addItem(pinfo.get("name", pkey), pkey)
        self._repolish_provider_combo.currentIndexChanged.connect(self._on_repolish_provider_index_changed)
        repolish_form.addRow("服務商：", self._repolish_provider_combo)

        # 重新潤色專用字段（與主服務商相同時隱藏）
        self._repolish_fields_widget = QWidget()
        repolish_fields_layout = QFormLayout(self._repolish_fields_widget)
        repolish_fields_layout.setContentsMargins(0, 0, 0, 0)

        self._repolish_api_url_input = QLineEdit()
        self._repolish_api_url_input.setPlaceholderText("https://api.example.com/v1")
        repolish_fields_layout.addRow("API URL：", self._repolish_api_url_input)

        repolish_key_row = QHBoxLayout()
        self._repolish_api_key_input = QLineEdit()
        self._repolish_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._repolish_api_key_input.setPlaceholderText("輸入 API Key")
        repolish_key_row.addWidget(self._repolish_api_key_input)

        self._toggle_repolish_key_btn = QPushButton("顯示")
        self._toggle_repolish_key_btn.setFixedWidth(50)
        self._toggle_repolish_key_btn.setCheckable(True)
        self._toggle_repolish_key_btn.toggled.connect(self._toggle_repolish_api_key_visibility)
        repolish_key_row.addWidget(self._toggle_repolish_key_btn)

        repolish_fields_layout.addRow("API Key：", repolish_key_row)

        self._repolish_model_input = QComboBox()
        self._repolish_model_input.setEditable(True)
        self._repolish_model_input.lineEdit().setPlaceholderText("模型名稱")
        repolish_fields_layout.addRow("模型：", self._repolish_model_input)

        repolish_layout.addWidget(self._repolish_fields_widget)

        layout.addWidget(repolish_group)
        layout.addStretch()

        self._provider_combo.provider_changed.connect(self._on_provider_changed)

        return page

    # ─────────────────────────────────────────────
    #  頁籤 3：輸出
    # ─────────────────────────────────────────────

    def _build_output_tab(self) -> QWidget:
        """建構輸出設定頁籤。"""
        page = QWidget()
        form = QFormLayout(page)

        self._paste_mode_check = QCheckBox("使用粘貼模式輸出（Ctrl+V）")
        form.addRow(self._paste_mode_check)

        self._restore_clip_check = QCheckBox("輸出後恢復剪貼板原內容")
        form.addRow(self._restore_clip_check)

        self._trad_convert_check = QCheckBox("簡體轉繁體")
        form.addRow(self._trad_convert_check)

        self._locale_combo = QComboBox()
        self._locale_combo.addItems(["zh-hk", "zh-tw", "zh-hans"])
        form.addRow("繁體地區：", self._locale_combo)

        self._format_num_check = QCheckBox("數字格式化（一百→100）")
        form.addRow(self._format_num_check)

        self._format_spell_check = QCheckBox("英文拼寫格式化")
        form.addRow(self._format_spell_check)

        self._trash_punc_input = QLineEdit()
        self._trash_punc_input.setPlaceholderText("要移除的標點符號")
        form.addRow("移除標點：", self._trash_punc_input)

        return page

    # ─────────────────────────────────────────────
    #  頁籤 4：關於
    # ─────────────────────────────────────────────

    def _build_about_tab(self) -> QWidget:
        """建構關於頁籤。"""
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel(f"CC語音 v{self._config.version}")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel(
            "Windows 離線語音輸入工具\n"
            "按住快捷鍵說話，鬆開即輸入文字到任意應用程式。"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        license_group = QGroupBox("開源組件授權")
        license_layout = QVBoxLayout(license_group)

        licenses_text = QTextBrowser()
        licenses_text.setOpenExternalLinks(True)
        licenses_text.setHtml(
            "<ul>"
            "<li><b>sherpa-onnx</b> — Apache 2.0</li>"
            "<li><b>PySide6</b> — LGPLv3</li>"
            "<li><b>pynput</b> — LGPLv3</li>"
            "<li><b>sounddevice</b> — MIT</li>"
            "<li><b>OpenCC</b> — Apache 2.0</li>"
            "<li><b>NumPy</b> — BSD-3-Clause</li>"
            "</ul>"
        )
        licenses_text.setMaximumHeight(160)
        license_layout.addWidget(licenses_text)
        layout.addWidget(license_group)

        link = QLabel(f'<a href="{PROJECT_URL}">GitHub 專案頁面</a>')
        link.setOpenExternalLinks(True)
        link.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(link)

        # ── 贊助支持 ──────────────────────────────
        donate_group = QGroupBox("贊助支持")
        donate_layout = QVBoxLayout(donate_group)

        donate_hint = QLabel(
            "州州語音免費開源，由業餘時間維護。\n"
            "如果幫到你，歡迎請我飲杯咖啡 ☕，畀啲動力我繼續更新 🙏"
        )
        donate_hint.setWordWrap(True)
        donate_layout.addWidget(donate_hint)

        donate_btn = QPushButton("💛  贊助支持作者")
        donate_btn.setFixedWidth(160)
        donate_btn.setToolTip("開啟贊助頁面（PayMe 一掃即過數）")
        donate_btn.clicked.connect(self._open_donate_page)
        donate_layout.addWidget(donate_btn)

        layout.addWidget(donate_group)

        mic_test_btn = QPushButton("測試麥克風")
        mic_test_btn.setFixedWidth(120)
        mic_test_btn.clicked.connect(self.mic_test_requested.emit)
        layout.addWidget(mic_test_btn)

        layout.addStretch()
        return page

    def _open_donate_page(self) -> None:
        """以系統瀏覽器開啟官網贊助區（含 PayMe 一鍵過數連結）。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(DONATE_URL))
        logger.info("開啟贊助頁面：%s", DONATE_URL)

    # ─────────────────────────────────────────────
    #  載入 / 讀取配置
    # ─────────────────────────────────────────────

    def _load_from_config(self, config: AppConfig) -> None:
        """將 AppConfig 的值填入所有 UI 控件。"""
        sc = config.shortcut
        self._key_input.set_key(sc.key)
        self._threshold_spin.setValue(sc.threshold)
        self._suppress_check.setChecked(sc.suppress)
        self._repolish_key_input.set_key(sc.repolish_key)
        self._repolish_instant_combo.setCurrentIndex(0 if sc.repolish_instant else 1)

        llm = config.llm
        self._llm_enabled_check.setChecked(llm.enabled)

        # 阻塞信號，防止觸發 _on_provider_changed 把空值寫入快取
        self._provider_combo.blockSignals(True)
        self._provider_combo.set_provider_key(llm.active_provider)
        self._provider_combo.blockSignals(False)

        self._current_provider_key = llm.active_provider
        self._temperature_spin.setValue(llm.temperature)
        self._max_tokens_spin.setValue(llm.max_tokens)
        self._top_p_spin.setValue(llm.top_p)
        self._freq_penalty_spin.setValue(llm.frequency_penalty)
        self._pres_penalty_spin.setValue(llm.presence_penalty)
        self._do_sample_check.setChecked(llm.do_sample)
        self._stop_key_combo.set_key(llm.stop_key)

        self._sync_provider_fields(llm.active_provider)

        # 重新潤色服務商
        self._repolish_provider_combo.blockSignals(True)
        if llm.repolish_provider:
            # 找到對應的服務商索引
            for i in range(self._repolish_provider_combo.count()):
                if self._repolish_provider_combo.itemData(i) == llm.repolish_provider:
                    self._repolish_provider_combo.setCurrentIndex(i)
                    break
            self._repolish_fields_widget.setVisible(True)
            # 填入該服務商的值
            provider = llm.providers.get(llm.repolish_provider, {})
            self._repolish_api_url_input.setText(provider.get("api_url", ""))
            self._repolish_api_key_input.setText(provider.get("api_key", ""))

            # 重建模型下拉（API 清單 + 歷史）
            self._populate_model_combo(
                self._repolish_model_input, llm.repolish_provider,
                list(provider.get("model_history", [])),
                llm.repolish_model or provider.get("model", ""),
            )
        else:
            self._repolish_provider_combo.setCurrentIndex(0)  # "與主服務商相同"
            self._repolish_fields_widget.setVisible(False)
        self._repolish_provider_combo.blockSignals(False)

        # 重新潤色角色（在 RoleTab 中）
        self._tab_role.refresh_repolish_role_combo(llm.repolish_role)

        out = config.output
        self._paste_mode_check.setChecked(out.paste_mode)
        self._restore_clip_check.setChecked(out.restore_clip)
        self._trad_convert_check.setChecked(out.traditional_convert)

        locale_idx = self._locale_combo.findText(out.traditional_locale)
        if locale_idx >= 0:
            self._locale_combo.setCurrentIndex(locale_idx)

        self._format_num_check.setChecked(out.format_num)
        self._format_spell_check.setChecked(out.format_spell)
        self._trash_punc_input.setText(out.trash_punc)

        # 熱詞頁籤
        self._tab_hotword.load_config(config.hotword)

        # 錄音歷史頁籤
        self._tab_history.load_config(config)
        self._tab_history.refresh_roles(
            config.llm.custom_roles,
            config.llm.builtin_overrides,
        )

    # ─────────────────────────────────────────────
    #  服務商切換邏輯
    # ─────────────────────────────────────────────

    def _on_provider_changed(self, new_key: str) -> None:
        """切換服務商時，先保存舊欄位再載入新服務商的值。"""
        old_key = self._current_provider_key
        if old_key:
            self._provider_cache_store[old_key] = {
                "api_key": self._api_key_input.text(),
                "model": self._model_input.currentText(),
                "api_url": self._api_url_input.text(),
            }
        self._current_provider_key = new_key
        self._sync_provider_fields(new_key)
        self._maybe_fetch_models()

    def _sync_provider_fields(self, provider_key: str) -> None:
        """將指定服務商的 API Key 和模型填入，下拉載入 API 模型清單（快取）+ 歷史。

        若快取過期且已有 API Key，背景抓取最新清單（不卡 UI）。
        """
        self._current_provider_key = provider_key
        provider = self._config.llm.providers.get(provider_key, {})
        cached = self._provider_cache_store.get(provider_key)

        if cached is not None:
            api_key = cached.get("api_key", "")
            model = cached.get("model", "")
            api_url = cached.get("api_url", "")
        else:
            api_key = provider.get("api_key", "")
            model = provider.get("model", "")
            api_url = provider.get("api_url", "")

        self._api_key_input.blockSignals(True)
        self._api_key_input.setText(api_key)
        self._api_url_input.setText(api_url)
        self._api_key_input.blockSignals(False)

        # 模型下拉：API 清單（快取）為主，歷史補上不重複者
        # 注意：此處只「讀快取」，不主動抓網路。背景抓取由 _maybe_fetch_models()
        # 在「打開設定頁 / 切換供應商」時觸發（避免 app 啟動載入設定就連網）。
        history = list(provider.get("model_history", []))
        self._populate_model_combo(self._model_input, provider_key, history, model)

    def _populate_model_combo(
        self, combo: QComboBox, provider_key: str,
        history: list[str], current_text: str,
    ) -> None:
        """用快取的 API 模型清單 + 歷史填充下拉，保留當前選字。"""
        from llm import model_cache
        fetched, _age = model_cache.get(provider_key)
        merged: list[str] = []
        seen: set[str] = set()
        for m in [*(fetched or []), *history]:
            if m and m not in seen:
                seen.add(m)
                merged.append(m)
        combo.blockSignals(True)
        combo.clear()
        if merged:
            combo.addItems(merged)
        combo.setCurrentText(current_text)
        combo.blockSignals(False)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt override
        """設定面板顯示時，自動抓當前供應商的模型清單（若快取過期）。"""
        super().showEvent(event)
        self._maybe_fetch_models()

    def _maybe_fetch_models(self) -> None:
        """當前 provider 快取過期且有 key/url → 背景抓取（不卡 UI）。"""
        from llm import model_cache
        provider_key = self._current_provider_key
        if not provider_key or not model_cache.is_stale(provider_key):
            return
        api_url = self._api_url_input.text().strip()
        api_key = self._api_key_input.text().strip()
        if api_key and api_url:
            self._start_model_fetch(provider_key, api_url, api_key)

    def _start_model_fetch(
        self, provider_key: str, api_url: str, api_key: str,
    ) -> None:
        """背景執行緒抓取模型清單；完成後經 _models_fetched 信號回主線程。"""
        if provider_key in self._fetching_models:
            return
        self._fetching_models.add(provider_key)

        def _worker() -> None:
            from llm.model_fetcher import fetch_models
            from llm.provider import ProviderInfo
            try:
                models = fetch_models(ProviderInfo(
                    key=provider_key, name=provider_key,
                    api_url=api_url, api_key=api_key, model="", enabled=True,
                ))
                self._models_fetched.emit(provider_key, models, "")
            except Exception as err:  # noqa: BLE001 — 任何失敗都回報給 UI
                self._models_fetched.emit(provider_key, [], str(err))

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _on_models_fetched(
        self, provider_key: str, models: list, error: str,
    ) -> None:
        """背景抓取完成（主線程 slot）。"""
        self._fetching_models.discard(provider_key)
        self._refresh_models_btn.setEnabled(True)

        if error:
            logger.info("模型清單抓取失敗 (%s): %s", provider_key, error)
            if provider_key == self._current_provider_key:
                self._test_result_label.setStyleSheet(
                    "font-size: 11px; color: #d32f2f;"
                )
                self._test_result_label.setText(f"模型更新失敗：{error}")
            return

        from llm import model_cache
        model_cache.set(provider_key, models)
        logger.info("模型清單已更新 (%s): %d 個", provider_key, len(models))

        # 若使用者仍停在該 provider，刷新下拉（保留當前選字）
        if provider_key == self._current_provider_key:
            history = list(
                self._config.llm.providers.get(provider_key, {}).get(
                    "model_history", []
                )
            )
            self._populate_model_combo(
                self._model_input, provider_key, history,
                self._model_input.currentText(),
            )
            self._test_result_label.setStyleSheet(
                "font-size: 11px; color: #4CAF50;"
            )
            self._test_result_label.setText(f"已更新 {len(models)} 個模型")

    def _on_refresh_models_clicked(self) -> None:
        """手動更新模型清單（忽略 TTL）。"""
        provider_key = self._provider_combo.get_provider_key()
        api_url = self._api_url_input.text().strip()
        api_key = self._api_key_input.text().strip()
        if not api_key or not api_url:
            self._test_result_label.setStyleSheet(
                "font-size: 11px; color: #d32f2f;"
            )
            self._test_result_label.setText("請先填 API URL 與 API Key")
            return
        self._refresh_models_btn.setEnabled(False)
        self._test_result_label.setStyleSheet("font-size: 11px; color: #888;")
        self._test_result_label.setText("更新模型中…")
        self._start_model_fetch(provider_key, api_url, api_key)

    def _save_current_provider_fields(self) -> None:
        """將目前 API Key / Model 欄位的值暫存到快取。"""
        key = self._current_provider_key
        if key:
            self._provider_cache_store[key] = {
                "api_key": self._api_key_input.text().strip(),
                "model": self._model_input.currentText(),
                "api_url": self._api_url_input.text(),
            }

    def _build_updated_providers(self) -> Dict[str, Dict[str, Any]]:
        """構建更新後的 providers 字典，將快取合併回原始 providers，並更新模型歷史。"""
        self._save_current_provider_fields()

        providers: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in self._config.llm.providers.items()
        }
        for pkey, cached in self._provider_cache_store.items():
            if pkey in providers:
                providers[pkey] = {**providers[pkey], **cached}
                new_model = cached.get("model", "").strip()

                if pkey == self._current_provider_key:
                    # 當前 provider：從 QComboBox 下拉項讀取 model_history
                    # （使用者的刪除/重置操作已反映在 UI 控件中）
                    ui_items = [
                        self._model_input.itemText(i)
                        for i in range(self._model_input.count())
                    ]
                    if new_model and new_model not in ui_items:
                        ui_items.insert(0, new_model)
                    providers[pkey]["model_history"] = ui_items[:10]
                elif new_model:
                    # 非當前 provider：沿用原 config 歷史，僅插入新模型
                    history: list = list(providers[pkey].get("model_history", []))
                    if new_model in history:
                        history.remove(new_model)
                    history.insert(0, new_model)
                    providers[pkey]["model_history"] = history[:10]

        return providers

    # ─────────────────────────────────────────────
    #  重置 / 刪除供應商設定
    # ─────────────────────────────────────────────

    def _on_reset_provider(self) -> None:
        """重置當前供應商為預設值。"""
        from PySide6.QtWidgets import QMessageBox
        from utils.config import DEFAULT_PROVIDERS

        key = self._current_provider_key
        defaults = DEFAULT_PROVIDERS.get(key)
        if not defaults:
            return
        if QMessageBox.question(
            self, "確認重置", f"確定重置「{defaults['name']}」的所有設定？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._api_url_input.setText(defaults["api_url"])
        self._api_key_input.setText(defaults["api_key"])
        self._model_input.clear()
        self._model_input.setCurrentText(defaults["model"])
        # 清除快取（不修改 self._config，等 Save 時統一更新）
        self._provider_cache_store.pop(key, None)
        logger.info("已重置供應商: %s", key)

    def _on_delete_model_history(self) -> None:
        """刪除當前選中的模型歷史記錄（只更新 UI，不修改 live config）。"""
        idx = self._model_input.currentIndex()
        if idx < 0:
            return
        removed = self._model_input.currentText()
        self._model_input.removeItem(idx)
        # 不直接修改 self._config.llm.providers — Save 時由
        # _build_updated_providers 從 UI 下拉內容重建 model_history
        logger.info("已刪除模型歷史: %s (provider=%s)", removed, self._current_provider_key)

    # ─────────────────────────────────────────────
    #  測試 LLM 連接
    # ─────────────────────────────────────────────

    def _on_api_key_changed(self) -> None:
        """API Key 輸入變更時更新狀態（已停用內建額度）。"""
        pass  # 內建免費額度已停用，無需特殊處理

    def _on_test_connection(self) -> None:
        """用當前 UI 中的設定測試 LLM API 連接。"""
        api_key = self._api_key_input.text().strip()
        model = self._model_input.currentText().strip()

        provider_key = self._provider_combo.get_provider_key()
        api_url = self._api_url_input.text().strip()

        if not api_key:
            self._test_result_label.setStyleSheet("font-size: 11px; color: #d32f2f;")
            self._test_result_label.setText("請先輸入 API Key")
            return

        if not api_url:
            self._test_result_label.setStyleSheet("font-size: 11px; color: #d32f2f;")
            self._test_result_label.setText("服務商 API URL 為空")
            return

        if not model:
            self._test_result_label.setStyleSheet("font-size: 11px; color: #d32f2f;")
            self._test_result_label.setText("請先輸入模型名稱")
            return

        self._test_btn.setEnabled(False)
        self._test_result_label.setStyleSheet("font-size: 11px; color: #888;")
        self._test_result_label.setText("正在測試...")

        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, lambda: self._run_test(api_url, api_key, model))

    def _run_test(self, api_url: str, api_key: str, model: str) -> None:
        """在下一輪事件循環中執行 LLM 連接測試。"""
        try:
            from llm.client import LLMClient
            from llm.provider import ProviderInfo

            provider_key = self._provider_combo.get_provider_key()
            provider = ProviderInfo(
                key=provider_key,
                name="test",
                api_url=api_url,
                api_key=api_key,
                model=model,
                enabled=True,
            )
            client = LLMClient(provider, timeout=10)
            success, message = client.test_connection(timeout=10)

            if success:
                self._test_result_label.setStyleSheet(
                    "font-size: 11px; color: #4CAF50;"
                )
            else:
                self._test_result_label.setStyleSheet(
                    "font-size: 11px; color: #d32f2f;"
                )
            self._test_result_label.setText(message)

        except Exception as err:
            self._test_result_label.setStyleSheet(
                "font-size: 11px; color: #d32f2f;"
            )
            self._test_result_label.setText(f"測試異常：{err}")
        finally:
            self._test_btn.setEnabled(True)

    # ─────────────────────────────────────────────
    #  API Key 顯示 / 隱藏
    # ─────────────────────────────────────────────

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        """切換 API Key 輸入框的可見性。"""
        if checked:
            self._api_key_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._toggle_key_btn.setText("隱藏")
        else:
            self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._toggle_key_btn.setText("顯示")

    def _toggle_repolish_api_key_visibility(self, checked: bool) -> None:
        """切換重新潤色 API Key 輸入框的可見性。"""
        if checked:
            self._repolish_api_key_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._toggle_repolish_key_btn.setText("隱藏")
        else:
            self._repolish_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._toggle_repolish_key_btn.setText("顯示")

    # ─────────────────────────────────────────────
    #  重新潤色服務商切換
    # ─────────────────────────────────────────────

    def _on_repolish_provider_index_changed(self, index: int) -> None:
        """切換重新潤色服務商時，更新字段可見性和值。"""
        provider_key = self._repolish_provider_combo.itemData(index) or ""
        if not provider_key:
            # 選擇「與主服務商相同」時隱藏字段
            self._repolish_fields_widget.setVisible(False)
        else:
            # 顯示字段並填入該服務商的值（唯讀，修改需到主服務商設定）
            self._repolish_fields_widget.setVisible(True)
            provider = self._config.llm.providers.get(provider_key, {})
            self._repolish_api_url_input.setText(provider.get("api_url", ""))
            self._repolish_api_url_input.setReadOnly(True)
            self._repolish_api_key_input.setText(provider.get("api_key", ""))
            self._repolish_api_key_input.setReadOnly(True)

            # 重建模型下拉（API 清單 + 歷史）
            self._populate_model_combo(
                self._repolish_model_input, provider_key,
                list(provider.get("model_history", [])),
                provider.get("model", ""),
            )
