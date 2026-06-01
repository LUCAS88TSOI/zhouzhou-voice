"""
州州語音 - 桌面流程狀態指示器

語音處理全流程期間（錄音 → 識別 → LLM 潤色 → 完成）在桌面指定位置顯示
半透明浮窗（脈衝圓點 + 狀態文字），流程結束時自動隱藏。支援拖動設定位置，
位置變更後發出 position_changed 信號。

狀態切換由 set_state() 驅動，顏色與文字會依狀態自動更新。
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from utils.logger import get_logger

logger = get_logger("recording_indicator")

_WIDTH = 180
_HEIGHT = 36
_TIMER_MS = 50   # 20fps
_PULSE_SPEED = 0.15  # 每幀相位增量

# ─── 狀態常數 ────────────────────────────────────────────────
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_POLISHING = "polishing"
STATE_DONE = "done"
STATE_HIDDEN = "hidden"

# ─── 狀態 → 顏色映射（脈衝圓點基礎色，alpha 由動畫計算）────────
_STATE_COLORS = {
    STATE_RECORDING: QColor(255, 60, 60),   # 紅
    STATE_PROCESSING: QColor(255, 165, 0),  # 橙
    STATE_POLISHING: QColor(80, 160, 255),  # 藍
    STATE_DONE: QColor(60, 200, 100),       # 綠
}

# ─── 狀態 → 預設顯示文字 ─────────────────────────────────────
_STATE_DEFAULT_TEXT = {
    STATE_RECORDING: "錄音中...",
    STATE_PROCESSING: "識別中...",
    STATE_POLISHING: "潤色中...",
    STATE_DONE: "完成",
}


class RecordingIndicator(QWidget):
    """
    桌面浮動流程狀態指示器。

    特性：
    - 半透明深色圓角背景 + 脈衝圓點 + 狀態文字
    - 錄音/識別/潤色全流程持續顯示，由 set_state() 切換顏色與文字
    - 可拖動：按住左鍵拖動到任意位置
    - 位置變動後發出 position_changed(x, y) 信號
    - 流程開始調用 show_recording()，流程結束調用 hide_recording()

    Args:
        x: 初始 X 座標（螢幕絕對座標）
        y: 初始 Y 座標（螢幕絕對座標）
    """

    position_changed = Signal(int, int)

    def __init__(self, x: int = 100, y: int = 100, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._drag_pos: QPoint | None = None
        self._dragged: bool = False
        self._pulse_phase: float = 0.0

        # 當前顯示狀態與文字
        self._state: str = STATE_RECORDING
        self._text: str = _STATE_DEFAULT_TEXT[STATE_RECORDING]

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(_TIMER_MS)
        self._pulse_timer.timeout.connect(self._tick)

        self._font = QFont("Microsoft YaHei", 10)
        self._text_rect = QRect(32, 0, _WIDTH - 36, _HEIGHT)

        self.setFixedSize(_WIDTH, _HEIGHT)
        self.move(x, y)
        self.hide()

        logger.debug("流程狀態指示器已建立，初始位置 (%d, %d)", x, y)

    # ─── 公開方法 ────────────────────────────────────────────

    def show_recording(self) -> None:
        """開始流程 → 顯示浮窗並啟動脈衝動畫。

        錄音/處理/潤色全程顯示，內容由 set_state() 切換。
        不再強制重置脈衝相位，確保狀態切換時動畫連續。
        """
        if not self._pulse_timer.isActive():
            self._pulse_timer.start()
        self.show()
        self.raise_()
        logger.debug("流程狀態指示器顯示（state=%s）", self._state)

    def hide_recording(self) -> None:
        """結束流程 → 停止動畫並隱藏浮窗。

        整個語音處理流程結束（或發生錯誤中斷）時呼叫。
        """
        self._pulse_timer.stop()
        self.hide()
        logger.debug("流程狀態指示器隱藏")

    def set_state(self, state: str, text: str | None = None) -> None:
        """切換顯示狀態（顏色 + 文字）。

        Args:
            state: 狀態常數，允許值為 STATE_RECORDING、STATE_PROCESSING、
                STATE_POLISHING、STATE_DONE。未知值會 fallback 到 STATE_PROCESSING。
            text: 自訂顯示文字；為 None 時使用該狀態的預設文字
                （例：STATE_POLISHING → "潤色中..."）。
                可傳入帶進度的字串如「LLM 潤色中... (3/5)」。

        Note:
            本方法應從主線程（GUI 線程）呼叫。若需從 worker 線程觸發，
            請透過 QMetaObject.invokeMethod() 或信號槽跨線程派發。
        """
        if state not in _STATE_COLORS:
            logger.warning("未知指示器狀態 '%s'，fallback 到 STATE_PROCESSING", state)
            state = STATE_PROCESSING

        self._state = state
        self._text = text if text is not None else _STATE_DEFAULT_TEXT[state]
        self.update()

    def get_position(self) -> tuple[int, int]:
        """返回當前位置 (x, y)。"""
        p = self.pos()
        return p.x(), p.y()

    # ─── 私有方法 ────────────────────────────────────────────

    @Slot()
    def _tick(self) -> None:
        self._pulse_phase = (self._pulse_phase + _PULSE_SPEED) % (2 * math.pi)
        self.update()

    # ─── 繪製 ────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景：半透明深色圓角矩形
        bg = QColor(30, 30, 30, 210)
        path = QPainterPath()
        path.addRoundedRect(0, 0, _WIDTH, _HEIGHT, 8, 8)
        painter.fillPath(path, bg)

        # 脈衝圓點（顏色依當前狀態）
        pulse = (math.sin(self._pulse_phase) + 1) / 2  # 0.0 ~ 1.0
        radius = 5 + pulse * 2          # 5 ~ 7 px
        alpha = int(180 + pulse * 75)   # 180 ~ 255
        base_color = _STATE_COLORS.get(self._state, _STATE_COLORS[STATE_PROCESSING])
        dot_color = QColor(base_color.red(), base_color.green(), base_color.blue(), alpha)
        painter.setBrush(dot_color)
        painter.setPen(Qt.PenStyle.NoPen)
        cx, cy = 18, _HEIGHT // 2
        painter.drawEllipse(
            int(cx - radius), int(cy - radius),
            int(radius * 2), int(radius * 2),
        )

        # 文字（顯示當前狀態文字）
        painter.setPen(QColor(255, 255, 255, 220))
        painter.setFont(self._font)
        painter.drawText(
            self._text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )

    # ─── 拖動事件 ────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            self._dragged = True

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragged:
            p = self.pos()
            logger.debug("指示器拖動到 (%d, %d)", p.x(), p.y())
            self.position_changed.emit(p.x(), p.y())
        self._drag_pos = None
        self._dragged = False
