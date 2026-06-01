"""
CC語音 - 主視窗

提供主應用視窗，內含兩個頁面（QStackedWidget）：
  - 頁面 0（語音頁）：狀態列、識別結果、進度條、版本號
  - 頁面 1（設定頁）：SettingsPanel + 返回/儲存按鈕

按右上角 ⚙ 按鈕或托盤「設置」可切換到設定頁。
設定頁按「儲存」發出 settings_save_requested 信號，由 VoiceApp 接收並套用。

用法：
    window = MainWindow(app_controller=voice_app)
    window.show()
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui.tray_icon import VoiceTrayIcon
from utils.logger import get_logger

if TYPE_CHECKING:
    from app.app import VoiceApp

logger = get_logger("main_window")

# 圖標路徑（統一路徑解析）
from utils.paths import APP_VERSION, ICON_PATH as _ICON_PATH

# 顯示結果的最大行數（超過時裁剪舊內容）
_MAX_RESULT_LINES = 20

# 狀態文字對應
# 語義區分：
#   STATUS_READY  — 空閒/清理結束（不顯示浮窗完成態）
#   STATUS_DONE   — 明確成功完成（綠色浮窗短暫顯示後隱藏）
#   STATUS_FAILED — 明確失敗（直接隱藏浮窗，避免誤導為成功）
STATUS_READY = "就緒"
STATUS_DONE = "完成"
STATUS_FAILED = "失敗"
STATUS_RECORDING = "錄音中..."
STATUS_RECOGNIZING = "識別中..."
STATUS_PROCESSING = "處理中..."
STATUS_CORRECTING = "校正中..."
STATUS_POLISHING = "潤色中..."
STATUS_LLM = "LLM 處理中..."

# QStackedWidget 頁面索引
_PAGE_VOICE = 0
_PAGE_SETTINGS = 1
_PAGE_TRANSCRIBE = 2

# 錄音中不允許開設定頁的狀態集合（含完整流水線狀態）
_BUSY_STATUSES = {
    STATUS_RECORDING, STATUS_RECOGNIZING, STATUS_PROCESSING,
    STATUS_CORRECTING, STATUS_POLISHING, STATUS_LLM,
}

# 完成態浮窗顯示時長（毫秒），結束後自動隱藏
_DONE_HIDE_DELAY_MS = 800

# 設定頁淺色主題樣式（避免繼承 Windows 深色模式）
_SETTINGS_STYLESHEET = (
    "QWidget { background-color: #ffffff; color: #333333; }"
    "QTabWidget::pane { border: 1px solid #d0d0d0; background-color: #ffffff; }"
    "QTabBar::tab { background-color: #f5f5f5; color: #555555; padding: 5px 10px;"
    "               border: 1px solid #d0d0d0; border-bottom: none;"
    "               border-top-left-radius: 3px; border-top-right-radius: 3px; }"
    "QTabBar::tab:selected { background-color: #ffffff; color: #333333; }"
    "QTabBar::tab:hover { background-color: #ebebeb; }"
    "QLineEdit, QTextEdit, QTextBrowser { background-color: #fafafa; color: #333333;"
    "    border: 1px solid #d0d0d0; border-radius: 3px; padding: 3px 5px; }"
    "QComboBox { background-color: #fafafa; color: #333333;"
    "    border: 1px solid #d0d0d0; border-radius: 3px; padding: 3px 6px; }"
    "QSpinBox, QDoubleSpinBox { background-color: #fafafa; color: #333333;"
    "    border: 1px solid #d0d0d0; border-radius: 3px; }"
    "QCheckBox { color: #333333; }"
    "QLabel { color: #333333; background: transparent; }"
    "QGroupBox { color: #555555; border: 1px solid #d0d0d0;"
    "    border-radius: 4px; margin-top: 8px; padding-top: 4px; }"
    "QGroupBox::title { subcontrol-origin: margin; padding: 0 4px; color: #555555; }"
    "QPushButton { background-color: #f5f5f5; color: #333333;"
    "    border: 1px solid #d0d0d0; border-radius: 3px; padding: 4px 8px; }"
    "QPushButton:hover { background-color: #e8e8e8; }"
    "QScrollArea { background-color: #ffffff; border: none; }"
    "QScrollBar:vertical { background: #f0f0f0; width: 8px; border: none; }"
    "QScrollBar::handle:vertical { background: #c0c0c0; border-radius: 4px; min-height: 20px; }"
)


class MainWindow(QMainWindow):
    """
    CC語音主視窗。

    功能：
    - 語音頁：頂部狀態列 + ⚙ 按鈕、中央結果區、轉錄進度條、版本號
    - 設定頁：SettingsPanel（六頁籤）+ 返回 / 儲存按鈕
    - 支援拖放音視頻文件進行轉錄
    - 最小化 / 關閉時隱藏到系統托盤
    - 托盤「退出」→ 真正關閉

    Args:
        app_controller: VoiceApp 實例（可為 None，用於獨立測試）
    """

    # 拖放文件信號：發射 Path 列表和 FileConfig（第二參數為可選）
    files_dropped = Signal(list, object)  # list[Path], FileConfig | None

    # 設定儲存信號：發射新的 AppConfig，由 VoiceApp._apply_config 接收
    settings_save_requested = Signal(object)  # AppConfig

    # 錄音歷史重新處理信號（record_id, role_id）
    reprocess_requested = Signal(int, str)

    def __init__(self, app_controller: Optional[VoiceApp] = None) -> None:
        super().__init__()

        self._app_controller = app_controller
        self._force_quit: bool = False
        self._current_status: str = STATUS_READY

        self._version = APP_VERSION

        # 建構 UI
        self._setup_window()
        self._setup_tray()
        self._setup_ui()
        self._connect_tray_signals()

        # 啟用拖放
        self.setAcceptDrops(True)

        # 桌面錄音指示器
        self._recording_indicator = self._create_recording_indicator()
        # 浮窗「完成態」延遲隱藏定時器（重複狀態變更時自動取消）
        self._indicator_hide_timer: Optional[QTimer] = None
        # 進度條批次結束後的單一可取消隱藏定時器
        # （先前 QTimer.singleShot 不可取消，會在下一個檔案還在跑時誤藏）
        self._progress_hide_timer: Optional[QTimer] = None

        logger.info("主視窗已建立 (版本 %s)", self._version)

    # ─── 公開方法 ──────────────────────────────────────────

    @Slot(str)
    def append_result(self, text: str) -> None:
        """
        添加一條識別結果到文字區域（帶時間戳）。

        自動裁剪超過 _MAX_RESULT_LINES 的舊內容。

        Args:
            text: 識別結果文字
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"

        from html import escape as _html_escape
        self._text_area.append(_html_escape(line))
        self._trim_results()

        # 自動滾動到底部
        scrollbar = self._text_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        logger.debug("結果已添加: %s", text[:50])

    @Slot()
    def refresh_history(self) -> None:
        """外部通知新錄音已儲存，刷新歷史頁籤。"""
        if self._settings_panel is not None:
            self._settings_panel.refresh_history()

    @Slot(str)
    def set_status(self, status: str) -> None:
        """
        更新狀態列文字，同步更新托盤提示。

        若設定頁正在顯示且狀態進入「錄音中」，自動切回語音頁。

        Args:
            status: 狀態描述（如 STATUS_READY、STATUS_RECORDING 等）
        """
        self._current_status = status
        self._status_label.setText(f"  {status}")
        self._tray.update_status(status)

        # 桌面錄音指示器：整流程持續顯示，依狀態切換顏色與文字
        if self._recording_indicator is not None:
            self._sync_indicator_state(status)

        logger.debug("狀態更新: %s", status)

    @Slot(str)
    def notify_warning(self, message: str) -> None:
        """彈出托盤警告通知（潤色失敗等需用戶察覺的情況，唔好靜默）。"""
        if self._tray is not None:
            self._tray.show_message("CC語音", message)
        logger.warning("用戶提示: %s", message)

    def _sync_indicator_state(self, status: str) -> None:
        """根據主視窗狀態同步浮窗顏色與文字。

        語義：
          STATUS_DONE    → 綠色「完成」短暫顯示後隱藏
          STATUS_FAILED  → 直接隱藏（不再誤顯為成功綠色）
          STATUS_READY   → 直接隱藏（空閒狀態不展示 DONE）
          錄音中         → 紅
          識別/處理/校正/轉錄/分段 → 橙
          潤色 / LLM     → 藍
        """
        from gui.recording_indicator import (
            STATE_DONE, STATE_POLISHING, STATE_PROCESSING, STATE_RECORDING,
        )

        # 取消任何待執行的隱藏定時器（避免新狀態被舊定時器擦掉）
        if self._indicator_hide_timer is not None:
            self._indicator_hide_timer.stop()
            self._indicator_hide_timer = None

        if status == STATUS_DONE:
            self._recording_indicator.set_state(STATE_DONE, "完成")
            self._recording_indicator.show_recording()
            self._indicator_hide_timer = QTimer(self)
            self._indicator_hide_timer.setSingleShot(True)
            self._indicator_hide_timer.timeout.connect(
                self._recording_indicator.hide_recording
            )
            self._indicator_hide_timer.start(_DONE_HIDE_DELAY_MS)
            return

        if status in (STATUS_READY, STATUS_FAILED):
            # 空閒 / 失敗：直接隱藏浮窗，避免「失敗卻顯示綠色完成」
            self._recording_indicator.hide_recording()
            return

        if status == STATUS_RECORDING or status.startswith("錄音") or status == "已達錄音上限":
            self._recording_indicator.set_state(STATE_RECORDING, status)
        elif "潤色" in status or "LLM" in status:
            self._recording_indicator.set_state(STATE_POLISHING, status)
        elif any(kw in status for kw in ("識別", "處理", "校正", "轉錄", "分段")):
            self._recording_indicator.set_state(STATE_PROCESSING, status)
        else:
            # 未知狀態：直接隱藏
            self._recording_indicator.hide_recording()
            return

        self._recording_indicator.show_recording()

    def _create_recording_indicator(self):
        """建立桌面錄音指示器，讀取 config 中儲存的位置。失敗時靜默返回 None。"""
        try:
            from gui.recording_indicator import RecordingIndicator
            ui_cfg = self._app_controller.config.ui if self._app_controller is not None else None
            x = ui_cfg.indicator_x if ui_cfg is not None else 100
            y = ui_cfg.indicator_y if ui_cfg is not None else 100
            indicator = RecordingIndicator(x=x, y=y)
            indicator.position_changed.connect(self._on_indicator_moved)
            return indicator
        except Exception as err:
            logger.error("無法建立錄音指示器: %s", err)
            return None

    @Slot(int, int)
    def _on_indicator_moved(self, x: int, y: int) -> None:
        """指示器被拖動後，通知 app_controller 儲存新位置。"""
        if self._app_controller is not None:
            self._app_controller.update_indicator_position(x, y)

    def force_close(self) -> None:
        """
        強制關閉視窗（跳過「隱藏到托盤」邏輯）。
        從托盤「退出」動作調用。
        """
        logger.info("強制關閉主視窗")
        self._force_quit = True
        self._tray.hide()
        self.close()

    # ─── 設定頁導航 ────────────────────────────────────────

    def show_settings_page(self) -> None:
        """
        顯示主視窗並切換到設定頁（供外部調用）。
        """
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self._navigate_to_settings()

    def _navigate_to_settings(self) -> None:
        """切換到設定頁。錄音/識別/LLM 進行中時拒絕。"""
        if self._current_status in _BUSY_STATUSES:
            self._tray.show_message("CC語音", "請等待目前操作完成再開啟設置")
            logger.warning("狀態 %s 下拒絕開啟設定頁", self._current_status)
            return

        # 刷新 SettingsPanel 為最新 config
        if (
            self._app_controller is not None
            and self._settings_panel is not None
        ):
            try:
                self._settings_panel.load_config(self._app_controller.config)
                # 傳遞 HotwordManager 引用給熱詞分頁
                hotword_mgr = getattr(self._app_controller, "_hotword", None)
                self._settings_panel._tab_hotword.set_manager(hotword_mgr)
            except Exception as err:
                logger.error("刷新設定面板失敗: %s", err)

        self._stack.setCurrentIndex(_PAGE_SETTINGS)
        logger.debug("切換到設定頁")

    def _navigate_to_voice(self) -> None:
        """切換回語音頁（不儲存）。"""
        self._stack.setCurrentIndex(_PAGE_VOICE)
        logger.debug("切換到語音頁")

    @Slot()
    def _on_settings_save(self) -> None:
        """設定頁「儲存」按鈕：讀取 config，發出信號，返回語音頁。"""
        if self._settings_panel is None:
            return
        try:
            new_config = self._settings_panel.get_config()
            logger.info("設定已儲存，發出 settings_save_requested")
            self.settings_save_requested.emit(new_config)
            self._navigate_to_voice()
        except Exception as err:
            logger.error("儲存設定失敗: %s", err, exc_info=True)
            self._tray.show_message("CC語音", f"儲存失敗：{err}")

    def _on_mic_test(self) -> None:
        """從設定頁觸發麥克風測試。"""
        if self._app_controller is not None:
            self._app_controller._show_mic_test()

    # ─── 視窗設定 ──────────────────────────────────────────

    def _setup_window(self) -> None:
        """設定視窗基本屬性：標題、大小、圖標、居中。"""
        self.setWindowTitle("CC語音")
        self.resize(840, 780)
        self.setMinimumSize(780, 720)

        self._window_icon = self._load_window_icon()
        self.setWindowIcon(self._window_icon)

        self._center_on_screen()

    def _setup_ui(self) -> None:
        """建構完整的 UI 佈局（QStackedWidget 雙頁）。"""
        central = QWidget()
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._stack = QStackedWidget()
        root_layout.addWidget(self._stack)

        # 頁面 0：語音頁
        self._stack.addWidget(self._build_voice_page())

        # 頁面 1：設定頁
        self._settings_panel = None  # 在 _build_settings_page 中賦值
        self._stack.addWidget(self._build_settings_page())

        # 頁面 2：文件轉錄頁
        self._tab_transcribe = None  # 在 _build_transcribe_page 中賦值
        self._stack.addWidget(self._build_transcribe_page())

        self._stack.setCurrentIndex(_PAGE_VOICE)

    def _setup_tray(self) -> None:
        """建立系統托盤圖標。"""
        self._tray = VoiceTrayIcon(parent=self)
        self._tray.show()
        logger.info("系統托盤已啟動")

    # ─── 頁面建構 ──────────────────────────────────────────

    def _build_voice_page(self) -> QWidget:
        """
        建構語音頁（原有 UI 封裝 + 右上角齒輪按鈕）。

        Returns:
            語音頁 QWidget
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # ── 頂部列：狀態（左）+ 齒輪按鈕（右）────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(4)

        # 狀態容器
        status_container = QWidget()
        status_container.setStyleSheet(
            "background-color: #f0f0f0;"
            "color: #333333;"
            "border: 1px solid #d0d0d0;"
            "border-radius: 4px;"
        )
        status_inner = QHBoxLayout(status_container)
        status_inner.setContentsMargins(8, 4, 8, 4)

        dot = QLabel("\u25CF")  # ●
        dot.setStyleSheet("color: #4CAF50; font-size: 14px; border: none;")
        dot.setFixedWidth(20)
        self._status_dot = dot

        label = QLabel(f"  {STATUS_READY}")
        label.setStyleSheet("font-size: 13px; border: none; color: #333333;")
        font = QFont("Microsoft YaHei", 10)
        label.setFont(font)
        self._status_label = label

        status_inner.addWidget(dot)
        status_inner.addWidget(label)
        status_inner.addStretch()

        # 文件轉錄按鈕
        _btn_style = (
            "QToolButton {"
            "  border: 1px solid #d0d0d0;"
            "  border-radius: 4px;"
            "  font-size: 14px;"
            "  color: #666666;"
            "  background-color: #f0f0f0;"
            "}"
            "QToolButton:hover {"
            "  background-color: #e0e0e0;"
            "  color: #333333;"
            "}"
        )
        transcribe_btn = QToolButton()
        transcribe_btn.setText("📄")
        transcribe_btn.setToolTip("文件轉錄")
        transcribe_btn.setFixedSize(30, 30)
        transcribe_btn.setStyleSheet(_btn_style)
        transcribe_btn.clicked.connect(self._navigate_to_transcribe)

        # 齒輪按鈕
        gear_btn = QToolButton()
        gear_btn.setText("⚙")
        gear_btn.setToolTip("設置")
        gear_btn.setFixedSize(30, 30)
        gear_btn.setStyleSheet(_btn_style)
        gear_btn.clicked.connect(self._navigate_to_settings)

        top_bar.addWidget(status_container, stretch=1)
        top_bar.addWidget(transcribe_btn)
        top_bar.addWidget(gear_btn)
        layout.addLayout(top_bar)

        # ── 中央文字區域 ──────────────────────────
        self._text_area = self._create_text_area()
        layout.addWidget(self._text_area, stretch=1)

        # ── 轉錄進度條（預設隱藏）────────────────
        self._progress_bar = self._create_progress_bar()
        layout.addWidget(self._progress_bar)

        # ── 底部版本標籤 ──────────────────────────
        self._version_label = QLabel(f"v{self._version}")
        self._version_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._version_label.setStyleSheet("color: #999999; font-size: 11px;")
        ver_font = QFont("Microsoft YaHei", 8)
        self._version_label.setFont(ver_font)
        layout.addWidget(self._version_label)

        return page

    def _build_settings_page(self) -> QWidget:
        """
        建構設定頁（SettingsPanel + 頂部返回按鈕 + 底部儲存按鈕）。

        Returns:
            設定頁 QWidget
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(4)

        # ── 頂部列：返回按鈕 + 標題 ──────────────
        top_bar = QHBoxLayout()

        back_btn = QPushButton("← 返回")
        back_btn.setFixedWidth(80)
        back_btn.setStyleSheet(
            "QPushButton {"
            "  font-size: 13px;"
            "  color: #1565C0;"
            "  border: 1px solid #90CAF9;"
            "  border-radius: 4px;"
            "  padding: 4px 8px;"
            "  background-color: #E3F2FD;"
            "}"
            "QPushButton:hover { background-color: #BBDEFB; color: #0D47A1; }"
            "QPushButton:pressed { background-color: #90CAF9; }"
            "QPushButton:focus { outline: none; background-color: #BBDEFB; }"
        )
        back_btn.clicked.connect(self._navigate_to_voice)

        settings_title = QLabel("設置")
        settings_title.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #333333;"
        )
        title_font = QFont("Microsoft YaHei", 11)
        settings_title.setFont(title_font)

        top_bar.addWidget(back_btn)
        top_bar.addSpacing(8)
        top_bar.addWidget(settings_title)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        # ── SettingsPanel ─────────────────────────
        if self._app_controller is not None:
            try:
                from gui.settings_panel import SettingsPanel
                self._settings_panel = SettingsPanel(
                    self._app_controller.config,
                    parent=page,
                    recording_db=getattr(self._app_controller, '_recording_db', None),
                )
                layout.addWidget(self._settings_panel, stretch=1)
                # 轉發錄音歷史重新處理信號
                self._settings_panel._tab_history.reprocess_requested.connect(
                    self.reprocess_requested
                )
                # 轉發麥克風測試信號
                self._settings_panel.mic_test_requested.connect(self._on_mic_test)
                self._settings_panel.setStyleSheet(_SETTINGS_STYLESHEET)
            except Exception as err:
                logger.error("無法建立 SettingsPanel: %s", err, exc_info=True)
                placeholder = QLabel(f"設置面板初始化失敗：{err}")
                placeholder.setWordWrap(True)
                layout.addWidget(placeholder, stretch=1)
        else:
            # 獨立測試模式（無 controller）
            placeholder = QLabel("設置不可用（無 app_controller）")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(placeholder, stretch=1)

        # ── 底部列：儲存按鈕 ──────────────────────
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch()

        save_btn = QPushButton("儲存")
        save_btn.setFixedWidth(80)
        save_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #4CAF50;"
            "  color: white;"
            "  border: none;"
            "  border-radius: 4px;"
            "  padding: 5px 16px;"
            "  font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #43A047; }"
            "QPushButton:pressed { background-color: #388E3C; }"
        )
        save_btn.clicked.connect(self._on_settings_save)
        bottom_bar.addWidget(save_btn)
        layout.addLayout(bottom_bar)

        return page

    def _build_transcribe_page(self) -> QWidget:
        """建構文件轉錄頁（TranscribeTab + 頂部返回按鈕）。"""
        from gui.widgets.transcribe_tab import TranscribeTab

        page = QWidget()
        page.setStyleSheet(_SETTINGS_STYLESHEET)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(4)

        # ── 頂部列：返回按鈕 + 標題 ──────────────
        top_bar = QHBoxLayout()

        back_btn = QPushButton("← 返回")
        back_btn.setFixedWidth(80)
        back_btn.setStyleSheet(
            "QPushButton {"
            "  font-size: 13px;"
            "  color: #1565C0;"
            "  border: 1px solid #90CAF9;"
            "  border-radius: 4px;"
            "  padding: 4px 8px;"
            "  background-color: #E3F2FD;"
            "}"
            "QPushButton:hover { background-color: #BBDEFB; color: #0D47A1; }"
            "QPushButton:pressed { background-color: #90CAF9; }"
            "QPushButton:focus { outline: none; background-color: #BBDEFB; }"
        )
        back_btn.clicked.connect(self._navigate_to_voice)

        title = QLabel("文件轉錄")
        title.setStyleSheet("font-size: 14px; font-weight: bold; color: #333333;")
        title.setFont(QFont("Microsoft YaHei", 11))

        top_bar.addWidget(back_btn)
        top_bar.addSpacing(8)
        top_bar.addWidget(title)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        # ── TranscribeTab 主體 ────────────────────
        file_cfg = self._app_controller.config.file if self._app_controller is not None else None
        from utils.config import FileConfig
        self._tab_transcribe = TranscribeTab(file_cfg or FileConfig(), parent=page)
        layout.addWidget(self._tab_transcribe, stretch=1)

        return page

    def _navigate_to_transcribe(self) -> None:
        """切換到文件轉錄頁。錄音/識別/LLM 進行中時拒絕。"""
        if self._current_status in _BUSY_STATUSES:
            self._tray.show_message("CC語音", "請等待目前操作完成再開啟文件轉錄")
            logger.warning("狀態 %s 下拒絕開啟轉錄頁", self._current_status)
            return
        self._stack.setCurrentIndex(_PAGE_TRANSCRIBE)
        logger.debug("切換到文件轉錄頁")

    # ─── UI 元件建構 ───────────────────────────────────────

    def _create_text_area(self) -> QTextEdit:
        """建構中央識別結果文字區域（唯讀）。"""
        text_area = QTextEdit()
        text_area.setReadOnly(True)
        text_area.setPlaceholderText("識別結果將顯示在這裡...")

        font = QFont("Microsoft YaHei", 10)
        text_area.setFont(font)
        text_area.setStyleSheet(
            "QTextEdit {"
            "  border: 1px solid #d0d0d0;"
            "  border-radius: 4px;"
            "  padding: 8px;"
            "  background-color: #fafafa;"
            "  color: #333333;"
            "}"
        )
        return text_area

    def _create_progress_bar(self) -> QProgressBar:
        """建構底部轉錄進度條（預設隱藏）。"""
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFormat("%p% — 準備中")
        bar.setFixedHeight(22)
        bar.setStyleSheet(
            "QProgressBar {"
            "  border: 1px solid #d0d0d0;"
            "  border-radius: 4px;"
            "  text-align: center;"
            "  background-color: #f5f5f5;"
            "  color: #333333;"
            "}"
            "QProgressBar::chunk {"
            "  background-color: #4CAF50;"
            "  border-radius: 3px;"
            "}"
        )
        bar.hide()
        return bar

    # ─── 轉錄進度 ─────────────────────────────────────────

    @Slot(float, str)
    def update_transcribe_progress(self, ratio: float, message: str) -> None:
        """
        更新轉錄進度條。

        進度條的顯示 / 隱藏改由 `show_progress()` 與
        `on_transcribe_batch_finished()` 集中管理，避免多檔時
        前一檔 100% 觸發的 hide 把後續檔案的進度條藏掉。

        Args:
            ratio: 0.0 ~ 1.0 進度比例
            message: 進度訊息
        """
        percent = int(ratio * 100)
        self._progress_bar.setValue(percent)
        self._progress_bar.setFormat(f"%p% — {message}")
        if ratio <= 0:
            self._progress_bar.show()

    @Slot(str, str)
    def update_file_status(self, file_path: str, status: str) -> None:
        """轉發文件狀態更新到轉錄頁籤。"""
        if self._tab_transcribe is not None:
            self._tab_transcribe.update_file_status(file_path, status)

    @Slot()
    def show_progress(self) -> None:
        """顯示轉錄進度條，並取消任何待執行的隱藏定時器。"""
        if self._progress_hide_timer is not None:
            self._progress_hide_timer.stop()
            self._progress_hide_timer = None
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("%p% — 準備中")
        self._progress_bar.show()
        self.showNormal()
        self.activateWindow()

    def hide_progress(self) -> None:
        """隱藏轉錄進度條。"""
        if self._progress_hide_timer is not None:
            self._progress_hide_timer.stop()
            self._progress_hide_timer = None
        self._progress_bar.hide()

    @Slot()
    def on_transcribe_batch_finished(self) -> None:
        """整批轉錄結束：解除 TranscribeTab 忙碌狀態，延遲 3s 收起進度條。"""
        if self._tab_transcribe is not None:
            self._tab_transcribe.set_busy(False)
        if self._progress_hide_timer is not None:
            self._progress_hide_timer.stop()
        self._progress_hide_timer = QTimer(self)
        self._progress_hide_timer.setSingleShot(True)
        self._progress_hide_timer.timeout.connect(self._progress_bar.hide)
        self._progress_hide_timer.start(3000)

    # ─── 文件選擇 ──────────────────────────────────────────

    def open_file_dialog(self) -> None:
        """開啟文件選擇對話框（供托盤選單調用）。"""
        from transcribe.file_transcriber import MEDIA_EXTENSIONS

        ext_list = " ".join(f"*{ext}" for ext in sorted(MEDIA_EXTENSIONS))
        filter_str = f"Media Files ({ext_list});;All Files (*)"

        files, _ = QFileDialog.getOpenFileNames(
            self, "選擇音視頻文件", "", filter_str,
        )
        if files:
            paths = [Path(f) for f in files]
            logger.info("使用者選擇了 %d 個文件", len(paths))
            self.files_dropped.emit(paths, None)

    # ─── 拖放事件 ──────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """拖入事件：檢查是否包含支援的媒體文件。"""
        from transcribe.file_transcriber import MEDIA_EXTENSIONS

        mime_data = event.mimeData()
        if mime_data.hasUrls():
            for url in mime_data.urls():
                if url.isLocalFile():
                    suffix = Path(url.toLocalFile()).suffix.lower()
                    if suffix in MEDIA_EXTENSIONS:
                        event.acceptProposedAction()
                        return

        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """放下事件：收集媒體文件並發射 files_dropped 信號。"""
        from transcribe.file_transcriber import MEDIA_EXTENSIONS

        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.suffix.lower() in MEDIA_EXTENSIONS:
                    paths.append(path)

        if paths:
            logger.info("拖放接收 %d 個媒體文件", len(paths))
            event.acceptProposedAction()
            self.files_dropped.emit(paths, None)
        else:
            event.ignore()

    # ─── 托盤信號連接 ─────────────────────────────────────

    def _connect_tray_signals(self) -> None:
        """將托盤圖標的信號連接到對應的槽。

        僅連接純 UI 信號（顯示視窗、退出、設置頁切換）。
        業務邏輯信號（複製、熱詞、糾錯、清除記憶等）由 VoiceApp._init_gui() 連接。
        """
        self._tray.show_window_requested.connect(self._on_show_window)
        self._tray.quit_requested.connect(self._on_quit_requested)
        self._tray.settings_requested.connect(self._on_settings)

    # ─── 托盤信號槽 ───────────────────────────────────────

    @Slot()
    def _on_show_window(self) -> None:
        """托盤雙擊：顯示並激活主視窗。"""
        self.showNormal()
        self.activateWindow()
        self.raise_()
        logger.debug("主視窗已顯示")

    @Slot()
    def _on_quit_requested(self) -> None:
        """托盤退出：強制關閉視窗，通知 controller。"""
        logger.info("使用者請求退出")

        if self._app_controller is not None:
            self._app_controller.shutdown()

        self.force_close()

    @Slot()
    def _on_settings(self) -> None:
        """托盤「設置」→ 顯示視窗並切換到設定頁。"""
        logger.info("托盤設置請求")
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self._navigate_to_settings()

    # ─── 事件覆寫 ──────────────────────────────────────────

    def changeEvent(self, event: object) -> None:
        """視窗狀態變更事件。最小化時隱藏到托盤。"""
        super().changeEvent(event)  # type: ignore[arg-type]

        if self.isMinimized():
            self.hide()
            self._tray.show_message("CC語音", "已最小化到系統托盤")
            logger.debug("視窗最小化到托盤")

    def closeEvent(self, event: QCloseEvent) -> None:
        """視窗關閉事件。非強制退出時隱藏到托盤而非真正關閉。"""
        if self._force_quit:
            logger.info("視窗正在關閉（強制退出）")
            self._tray.hide()
            event.accept()
            return

        event.ignore()
        self.hide()
        self._tray.show_message("CC語音", "已隱藏到系統托盤，雙擊圖標顯示")
        logger.debug("視窗隱藏到托盤（關閉按鈕）")

    # ─── 內部工具 ──────────────────────────────────────────

    def _trim_results(self) -> None:
        """裁剪文字區域，只保留最近 _MAX_RESULT_LINES 行。"""
        text = self._text_area.toPlainText()
        lines = text.split("\n")

        if len(lines) > _MAX_RESULT_LINES:
            trimmed = "\n".join(lines[-_MAX_RESULT_LINES:])
            self._text_area.setPlainText(trimmed)

            cursor = self._text_area.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self._text_area.setTextCursor(cursor)

    def _center_on_screen(self) -> None:
        """將視窗居中放置在主螢幕上。"""
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            x = (geometry.width() - self.width()) // 2 + geometry.x()
            y = (geometry.height() - self.height()) // 2 + geometry.y()
            self.move(x, y)

    @staticmethod
    def _load_window_icon() -> QIcon:
        """載入視窗圖標，失敗時返回空圖標。"""
        if _ICON_PATH.exists():
            icon = QIcon(str(_ICON_PATH))
            if not icon.isNull():
                return icon
        return QIcon()
