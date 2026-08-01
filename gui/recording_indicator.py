"""
州州語音 - 桌面流程狀態指示器

語音處理全流程期間（錄音 → 識別 → LLM 潤色 → 完成）在桌面顯示半透明浮窗
（脈衝圓點 + 狀態文字），流程結束時自動隱藏。

定位有兩種模式：
  自動（預設）— 每次由隱藏轉為顯示時，貼齊「游標所在螢幕」的底部中央
  自訂         — 用戶拖動過，記住座標；但每次顯示前都經 gui.screen_utils
                 校驗，離開所有螢幕時自動退回底部中央並發出 auto_center_requested

所有絕對座標一律經 gui.screen_utils 取得，不可自行 move()（見該模組說明）。
狀態切換由 set_state() 驅動，顏色與文字會依狀態自動更新。
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from gui import screen_utils
from utils.logger import get_logger

logger = get_logger("recording_indicator")

# ─── 尺寸與繪製常數（paintEvent 內禁止出現 magic number）──────────
_WIDTH = 360
_HEIGHT = 72
_CORNER_RADIUS = 16
_DOT_CX = 36          # 脈衝圓點中心 X
_DOT_BASE_R = 10      # 圓點基礎半徑
_DOT_PULSE_R = 4      # 脈衝額外半徑（實際 10 ~ 14）
_TEXT_LEFT = 64       # 文字左邊界，須 >= _DOT_CX + _DOT_BASE_R + _DOT_PULSE_R
_TEXT_RIGHT_PAD = 16
_FONT_PT = 18

_BG_COLOR = QColor(30, 30, 30, 210)       # 半透明深色背景
_TEXT_COLOR = QColor(255, 255, 255, 220)
_DOT_ALPHA_MIN = 180                      # 圓點透明度 180 ~ 255
_DOT_ALPHA_RANGE = 75

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
    - 可拖動：按住左鍵拖動到任意位置（放手後座標會 clamp 回可視範圍）
    - 位置變動後發出 position_changed(x, y) 信號
    - 座標失效時發出 auto_center_requested，讓上層清掉壞設定
    - 流程開始調用 show_recording()，流程結束調用 hide_recording()

    所有公開方法只准在主線程（GUI 線程）呼叫 —— 定位會讀 QCursor.pos()，
    在 worker 線程是未定義行為。跨線程請走 VoiceApp._invoke_gui()。

    Args:
        saved_pos: 用戶自訂座標 (x, y)；None（預設）表示自動貼齊底部中央
    """

    position_changed = Signal(int, int)
    auto_center_requested = Signal()

    def __init__(
        self,
        saved_pos: tuple[int, int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
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
        self._auto_center: bool = saved_pos is None

        # 當前顯示狀態與文字
        self._state: str = STATE_RECORDING
        self._text: str = _STATE_DEFAULT_TEXT[STATE_RECORDING]

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(_TIMER_MS)
        self._pulse_timer.timeout.connect(self._tick)

        self._font = QFont("Microsoft YaHei", _FONT_PT)
        self._text_rect = QRect(
            _TEXT_LEFT, 0, _WIDTH - _TEXT_LEFT - _TEXT_RIGHT_PAD, _HEIGHT,
        )

        self.setFixedSize(_WIDTH, _HEIGHT)
        if saved_pos is not None:
            self.move(*saved_pos)
        # 建構期無人接得住信號，故只記錄「需要修復壞設定」，延到首次 show 才 emit
        self._heal_pending: bool = self._reposition()
        self.hide()

        p = self.pos()
        logger.debug(
            "流程狀態指示器已建立，位置 (%d, %d)，自動居中=%s",
            p.x(), p.y(), self._auto_center,
        )

    # ─── 公開方法 ────────────────────────────────────────────

    def show_recording(self, force_reposition: bool = False) -> None:
        """開始流程 → 顯示浮窗並啟動脈衝動畫。

        錄音/處理/潤色全程顯示，內容由 set_state() 切換。
        不再強制重置脈衝相位，確保狀態切換時動畫連續。

        預設只在「隱藏 → 顯示」的轉換時重新定位：流程中途重算會令浮窗在
        錄音→識別→潤色之間跟著游標跳來跳去。螢幕熱插拔 / 解析度變更 /
        游標換屏都靠這一步自然吸收，故不需監聽 screenAdded/Removed。

        Args:
            force_reposition: 新流程起點傳 True。上一次「完成」後浮窗有 800ms
                延遲隱藏，期間開始新錄音時 isVisible() 仍為 True，不強制重算
                就會留在上一塊螢幕。
        """
        if force_reposition or not self.isVisible():
            self._heal_pending = self._reposition() or self._heal_pending
        if self._heal_pending:
            self._heal_pending = False
            self.auto_center_requested.emit()
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

    def reset_to_auto_center(self) -> None:
        """清除自訂位置 → 改為自動貼齊游標所在螢幕底部中央（立即生效）。"""
        self._auto_center = True
        self.move(*screen_utils.auto_bottom_center(_WIDTH, _HEIGHT))
        logger.info("浮窗已重置為自動底部中央")

    # ─── 私有方法 ────────────────────────────────────────────

    def _reposition(self) -> bool:
        """重算並移動到安全位置（定位一律經 screen_utils.resolve_position）。

        Returns:
            True 表示原座標已離開所有螢幕、已強制退回自動居中
            （呼叫端應 emit auto_center_requested 讓上層清掉壞設定）。
        """
        saved = None if self._auto_center else self.get_position()
        pos, fell_back = screen_utils.resolve_position(_WIDTH, _HEIGHT, saved)
        self.move(*pos)
        if fell_back:
            self._auto_center = True
        return fell_back

    @Slot()
    def _tick(self) -> None:
        self._pulse_phase = (self._pulse_phase + _PULSE_SPEED) % (2 * math.pi)
        self.update()

    # ─── 繪製 ────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景：半透明深色圓角矩形
        path = QPainterPath()
        path.addRoundedRect(0, 0, _WIDTH, _HEIGHT, _CORNER_RADIUS, _CORNER_RADIUS)
        painter.fillPath(path, _BG_COLOR)

        # 脈衝圓點（顏色依當前狀態）
        pulse = (math.sin(self._pulse_phase) + 1) / 2  # 0.0 ~ 1.0
        radius = _DOT_BASE_R + pulse * _DOT_PULSE_R
        alpha = int(_DOT_ALPHA_MIN + pulse * _DOT_ALPHA_RANGE)
        base_color = _STATE_COLORS.get(self._state, _STATE_COLORS[STATE_PROCESSING])
        dot_color = QColor(base_color.red(), base_color.green(), base_color.blue(), alpha)
        painter.setBrush(dot_color)
        painter.setPen(Qt.PenStyle.NoPen)
        cx, cy = _DOT_CX, _HEIGHT // 2
        painter.drawEllipse(
            int(cx - radius), int(cy - radius),
            int(radius * 2), int(radius * 2),
        )

        # 文字（過長時省略號截斷，避免未來新增長進度文案被無聲切掉）
        painter.setPen(_TEXT_COLOR)
        painter.setFont(self._font)
        painter.drawText(
            self._text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            QFontMetrics(self._font).elidedText(
                self._text, Qt.TextElideMode.ElideRight, self._text_rect.width(),
            ),
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
            pos, fell_back = screen_utils.resolve_position(
                _WIDTH, _HEIGHT, self.get_position(),
            )
            self.move(*pos)
            self._auto_center = fell_back
            if fell_back:
                # 理論上拖動終點必在螢幕內；螢幕於拖動途中被拔掉才會走此路
                self.auto_center_requested.emit()
            else:
                logger.debug("指示器拖動到 (%d, %d)", *pos)
                self.position_changed.emit(*pos)
        self._drag_pos = None
        self._dragged = False
