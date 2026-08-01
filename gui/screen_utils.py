"""
州州語音 - 螢幕幾何工具（絕對座標定位的唯一收口）

**約定：任何需要 `move()` 到絕對螢幕座標的 GUI 元件，都必須經本模組取得座標，
不可自行計算後直接 move()。**

由來：桌面浮窗曾把位置存成 (1574, 683)，而用戶是雙螢幕
    主屏 1440x2560 @ (0, 0)   副屏 1920x1080 @ (1440, 820)
該座標 x 已離開主屏、y 又未到副屏 —— 落在兩屏之間的空隙，不屬於任何螢幕。
`move()` 照樣執行、動畫照樣跑，浮窗卻永久看不到且無任何錯誤。根因是
「沒有任何一層負責校驗目標座標是否可見」，所以抽出本模組統一收口。

分兩層：
  純幾何層 —— clamp_point_to_rects / bottom_center_in_rect
      只收 plain tuple，零 Qt 呼叫，可在無 GUI 環境下單元測試
  Qt 層 —— available_rects / cursor_screen_rect / auto_bottom_center / resolve_position
      讀真實螢幕狀態，**只准主線程（GUI 線程）呼叫**（QCursor.pos() 在
      worker 線程是未定義行為）

Qt import 刻意寫在函式內，令純幾何層在無 PySide6 的環境仍可 import 測試。
"""

from __future__ import annotations

from utils.logger import get_logger

logger = get_logger("screen_utils")

# (x, y, width, height) —— 與 QRect 同一坐標系（Qt 邏輯像素）
Rect = tuple[int, int, int, int]

# 貼底邊距（邏輯像素）。取 48 是為了大於標準 Windows 工作列高度（約 40），
# 令「工作列設為自動隱藏」時彈出的工作列也不會蓋住浮窗。
BOTTOM_MARGIN = 48


# ─── 純幾何層（零 Qt 依賴，可離線測試）────────────────────

def _overlap_area(x: int, y: int, w: int, h: int, rect: Rect) -> int:
    """widget 矩形與 rect 的交疊面積（無交疊回 0）。"""
    rx, ry, rw, rh = rect
    dx = min(x + w, rx + rw) - max(x, rx)
    dy = min(y + h, ry + rh) - max(y, ry)
    return dx * dy if dx > 0 and dy > 0 else 0


def clamp_point_to_rects(
    x: int, y: int, w: int, h: int, rects: list[Rect],
) -> tuple[int, int] | None:
    """把 (x, y, w, h) 推入與其交疊面積最大的 rect 內。

    Args:
        x, y: widget 左上角座標
        w, h: widget 尺寸
        rects: 可用螢幕矩形清單（通常來自 available_rects()）

    Returns:
        clamp 後的 (x, y)；**若與所有 rect 完全無交疊則回 None**，代表
        該座標不屬於任何螢幕，呼叫端應改用 bottom_center 之類的安全位置。
    """
    best = max(rects, key=lambda r: _overlap_area(x, y, w, h, r), default=None)
    if best is None or _overlap_area(x, y, w, h, best) <= 0:
        return None
    rx, ry, rw, rh = best
    # 外層 max(rx, ...) 處理 widget 比螢幕大的情況：此時 rx+rw-w < rx，
    # 內層 min 會回負偏移，須夾回 rx 以保證左上角仍可見。
    return max(rx, min(x, rx + rw - w)), max(ry, min(y, ry + rh - h))


def bottom_center_in_rect(
    rect: Rect, w: int, h: int, margin: int = BOTTOM_MARGIN,
) -> tuple[int, int]:
    """rect 內「水平居中 + 貼近底部」的左上角座標。

    rect 應為 availableGeometry（已扣除工作列），故底邊即可用底邊。
    螢幕比 widget + margin 還細時夾回 rect 左上角，不可回負偏移。
    """
    rx, ry, rw, rh = rect
    return max(rx, rx + (rw - w) // 2), max(ry, ry + rh - h - margin)


# ─── Qt 層（讀真實螢幕，只准主線程呼叫）───────────────────

def available_rects() -> list[Rect]:
    """所有螢幕的 availableGeometry（已扣除工作列）。"""
    from PySide6.QtGui import QGuiApplication

    return [
        (g.x(), g.y(), g.width(), g.height())
        for g in (s.availableGeometry() for s in QGuiApplication.screens())
    ]


def cursor_screen_rect() -> Rect | None:
    """游標所在螢幕的 availableGeometry；取不到則退回主螢幕，仍無則 None。"""
    from PySide6.QtGui import QCursor, QGuiApplication

    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    g = screen.availableGeometry()
    return g.x(), g.y(), g.width(), g.height()


def auto_bottom_center(w: int, h: int) -> tuple[int, int]:
    """游標所在螢幕的底部中央座標。

    取不到任何螢幕時回 (0, 0) 並記錄警告 —— 浮窗是非關鍵功能，
    絕不可拋異常拖死語音流程。
    """
    rect = cursor_screen_rect()
    if rect is None:
        logger.warning("取不到任何螢幕，浮窗定位退回 (0, 0)")
        return 0, 0
    return bottom_center_in_rect(rect, w, h)


def resolve_position(
    w: int, h: int, saved: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], bool]:
    """決定 widget 的安全顯示位置 —— 所有定位需求的統一入口。

    Args:
        saved: 用戶自訂座標；None 代表自動模式

    Returns:
        ((x, y), fell_back)。自動模式 → 游標所在螢幕底部中央、fell_back 為
        False；自訂模式 → clamp 回可視範圍；座標已離開所有螢幕時退回底部中央
        並回 fell_back=True，呼叫端應順手清掉那份壞設定。
    """
    if saved is not None:
        pos = clamp_point_to_rects(saved[0], saved[1], w, h, available_rects())
        if pos is not None:
            return pos, False
        logger.warning("浮窗座標 %s 不在任何螢幕範圍內，改用底部中央", saved)
    return auto_bottom_center(w, h), saved is not None
