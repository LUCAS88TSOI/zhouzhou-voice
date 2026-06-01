"""
州州語音 - 系統托盤圖標

提供系統托盤圖標，含右鍵選單和氣泡通知。
所有使用者操作透過 Signal 發射，由外部（MainWindow / AppController）連接處理。

用法：
    tray = VoiceTrayIcon(parent=main_window)
    tray.quit_requested.connect(app.quit)
    tray.show()
"""

from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING  # noqa: F401 — used for UpdateInfo

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from utils.logger import get_logger
from utils.paths import ICON_PATH as _ICON_PATH

if TYPE_CHECKING:
    from utils.updater import UpdateInfo

logger = get_logger("tray")

# 回退用的彩色方塊尺寸和顏色
_FALLBACK_ICON_SIZE = 64
_FALLBACK_ICON_COLOR = QColor(0, 150, 136)  # Teal 綠


class VoiceTrayIcon(QSystemTrayIcon):
    """
    州州語音系統托盤圖標。

    Signals:
        copy_result_requested  — 使用者點擊「複製結果」
        add_hotword_requested  — 使用者點擊「添加熱詞」
        add_rectify_requested  — 使用者點擊「添加糾錯」
        clear_memory_requested — 使用者點擊「清除記憶」
        settings_requested     — 使用者點擊「設置」
        quit_requested         — 使用者點擊「退出」
        show_window_requested  — 使用者雙擊托盤圖標
    """

    # ─── 信號定義 ──────────────────────────────────────────

    copy_result_requested = Signal()
    add_hotword_requested = Signal()
    add_rectify_requested = Signal()
    clear_memory_requested = Signal()
    transcribe_requested = Signal()
    role_switch_requested = Signal(str)  # role_id
    settings_requested = Signal()
    startup_toggle_requested = Signal(bool)  # enable
    quit_requested = Signal()
    show_window_requested = Signal()
    update_dialog_requested = Signal(object)  # UpdateInfo

    # ─── 初始化 ────────────────────────────────────────────

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._icon = self._load_icon()
        self.setIcon(self._icon)
        self.setToolTip("州州語音 — 就緒")

        self._menu = self._build_context_menu()
        self.setContextMenu(self._menu)

        # 雙擊托盤圖標 → 顯示主視窗
        self.activated.connect(self._on_activated)

        # 新版本通知選單項（有新版本時動態插入）
        self._action_update: QAction | None = None
        self._update_info = None  # 儲存 UpdateInfo 供選單重開對話框

        logger.info("系統托盤圖標已建立")

    # ─── 公開方法 ──────────────────────────────────────────

    def update_status(self, status: str) -> None:
        """
        更新托盤圖標的滑鼠懸停提示文字。

        Args:
            status: 當前狀態描述（如「錄音中...」）
        """
        tooltip = f"州州語音 — {status}"
        self.setToolTip(tooltip)
        logger.debug("托盤提示更新: %s", tooltip)

    def update_roles(
        self,
        roles: list[tuple[str, str, bool]],
        active_role_id: str,
    ) -> None:
        """
        更新角色切換子選單的內容。

        Args:
            roles: [(role_id, display_name, is_builtin), ...]
            active_role_id: 當前使用的角色 ID
        """
        self._role_menu.clear()

        for role_id, display_name, _is_builtin in roles:
            action = QAction(display_name, self._role_menu)
            action.setCheckable(True)
            action.setChecked(role_id == active_role_id)
            action.triggered.connect(
                lambda checked, rid=role_id: self.role_switch_requested.emit(rid)
            )
            self._role_menu.addAction(action)

        logger.debug("角色子選單已更新，共 %d 個角色", len(roles))

    def show_message(self, title: str, message: str) -> None:
        """
        顯示系統氣泡通知（Windows 10+ 為 Toast 通知）。

        Args:
            title: 通知標題
            message: 通知內容
        """
        self.showMessage(
            title,
            message,
            self._icon,
            3000,  # 顯示 3 秒
        )
        logger.debug("氣泡通知: [%s] %s", title, message)

    def show_update_available(self, info: UpdateInfo) -> None:
        """
        在選單最頂部插入更新入口。點擊時發射 update_dialog_requested。

        幂等：多次呼叫不會重複新增選單項。

        Args:
            info: UpdateInfo 物件
        """
        if self._action_update is not None:
            return  # 已顯示過，不重複

        self._update_info = info

        # 在右鍵選單頂部插入高亮選項
        self._action_update = QAction(
            f"⭐ 有新版本 v{info.remote_version}", self._menu,
        )
        self._action_update.triggered.connect(
            lambda: self.update_dialog_requested.emit(self._update_info)
        )

        actions = self._menu.actions()
        if actions:
            self._menu.insertAction(actions[0], self._action_update)
            self._menu.insertSeparator(actions[0])
        else:
            self._menu.addAction(self._action_update)

        logger.info("托盤選單：新版本提示已插入 v%s", info.remote_version)

    @staticmethod
    def _open_url(url: str) -> None:
        """在預設瀏覽器中打開 URL。"""
        webbrowser.open(url)

    def get_icon(self) -> QIcon:
        """取得當前使用的圖標（供 MainWindow 共用）。"""
        return self._icon

    def set_startup_checked(self, checked: bool) -> None:
        """設定開機啟動選單項的勾選狀態。"""
        self._action_startup.setChecked(checked)

    # ─── 選單建構 ──────────────────────────────────────────

    def _build_context_menu(self) -> QMenu:
        """建構右鍵選單，回傳 QMenu。"""
        menu = QMenu()

        # 功能項目
        self._action_copy = self._add_action(
            menu, "複製結果", self.copy_result_requested
        )
        self._action_hotword = self._add_action(
            menu, "添加熱詞", self.add_hotword_requested
        )
        self._action_rectify = self._add_action(
            menu, "添加糾錯", self.add_rectify_requested
        )
        self._action_clear = self._add_action(
            menu, "清除記憶", self.clear_memory_requested
        )

        menu.addSeparator()

        # 文件轉錄
        self._action_transcribe = self._add_action(
            menu, "文件轉錄...", self.transcribe_requested
        )

        menu.addSeparator()

        # 角色切換子選單
        self._role_menu = QMenu("切換角色", menu)
        menu.addMenu(self._role_menu)

        menu.addSeparator()

        # 設置
        self._action_settings = self._add_action(
            menu, "設置", self.settings_requested
        )

        # 開機自動啟動（可勾選）
        self._action_startup = QAction("開機自動啟動", self)
        self._action_startup.setCheckable(True)
        self._action_startup.triggered.connect(
            lambda: self.startup_toggle_requested.emit(self._action_startup.isChecked())
        )
        menu.addAction(self._action_startup)

        menu.addSeparator()

        # 退出
        self._action_quit = self._add_action(
            menu, "退出", self.quit_requested
        )

        logger.debug("右鍵選單已建構，共 %d 個動作", len(menu.actions()))
        return menu

    @staticmethod
    def _add_action(menu: QMenu, text: str, signal: Signal) -> QAction:
        """
        向選單添加一個動作，點擊時發射對應信號。

        Args:
            menu: 目標選單
            text: 顯示文字
            signal: 點擊時發射的 Signal

        Returns:
            建立的 QAction
        """
        action = QAction(text, menu)
        action.triggered.connect(signal.emit)
        menu.addAction(action)
        return action

    # ─── 圖標載入 ──────────────────────────────────────────

    @staticmethod
    def _load_icon() -> QIcon:
        """
        嘗試載入 assets/icon.ico，失敗時生成彩色方塊作為回退。

        Returns:
            QIcon 實例
        """
        if _ICON_PATH.exists():
            icon = QIcon(str(_ICON_PATH))
            if not icon.isNull():
                logger.info("圖標已載入: %s", _ICON_PATH)
                return icon
            logger.warning("圖標文件存在但無法載入: %s", _ICON_PATH)

        logger.info("使用回退生成圖標（彩色方塊）")
        return _create_fallback_icon()

    # ─── 事件處理 ──────────────────────────────────────────

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """
        處理托盤圖標的啟動事件。

        雙擊時發射 show_window_requested 信號。

        Args:
            reason: 啟動原因（單擊 / 雙擊 / 中鍵 / 右鍵等）
        """
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            logger.debug("托盤圖標被雙擊")
            self.show_window_requested.emit()


# ─── 模組級工具函數 ────────────────────────────────────────


def _create_fallback_icon() -> QIcon:
    """
    生成一個純色方塊圖標作為回退，中央帶有「州」字。

    Returns:
        生成的 QIcon
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QPainter

    size = _FALLBACK_ICON_SIZE
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        # 繪製圓角背景
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(_FALLBACK_ICON_COLOR)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = size // 8
        painter.drawRoundedRect(0, 0, size, size, radius, radius)

        # 繪製「州」字
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Microsoft YaHei", size // 3, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(
            pixmap.rect(),
            Qt.AlignmentFlag.AlignCenter,
            "州",
        )
    finally:
        painter.end()

    return QIcon(pixmap)
