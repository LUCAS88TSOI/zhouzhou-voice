"""
桌面浮窗定位回歸測試

核心 bug（P0）：浮窗座標存到「兩塊螢幕之間的無效區域」，`move()` 照樣執行，
浮窗永久看不到卻無任何錯誤。用戶真實佈局：

    主屏 DISPLAY2  1440 x 2560 @ (0, 0)        （直立屏）
    副屏 DISPLAY1  1920 x 1080 @ (1440, 820)

儲存座標 (1574, 683)：x 已離開主屏右邊界 1440，y 又未到副屏頂部 820
→ 不屬於任何螢幕。舊版 `gui/recording_indicator.py` 直接 `self.move(x, y)`，
全專案零螢幕範圍校驗。

測試分三層：
  1. 純幾何（零 Qt 依賴）—— 主力回歸鎖
  2. config 遷移與 app 寫入路徑（stub，不起 GUI）
  3. Qt offscreen widget（無 PySide6 時自動跳過）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 用戶真實雙螢幕佈局（availableGeometry 形式：x, y, w, h）
USER_SCREENS = [(0, 0, 1440, 2560), (1440, 820, 1920, 1080)]

# 浮窗新尺寸
W, H = 360, 72


def _qt_app():
    """取得（或建立）QApplication；無 PySide6 或建不起時回 None 讓測試跳過。"""
    try:
        from PySide6.QtWidgets import QApplication
        return QApplication.instance() or QApplication(sys.argv)
    except Exception:
        return None


# ─── 第 1 層：純幾何（零 Qt） ──────────────────────────────

def test_gap_coordinate_detected_as_invalid():
    """回歸鎖：用戶儲存的 (1574, 683) 落在雙屏空隙，必須判定為無效。

    這是本次 P0 bug 的唯一根因。若此測試變綠燈以外的任何結果，
    代表 clamp 又重新接受了螢幕外座標。
    """
    from gui.screen_utils import clamp_point_to_rects

    assert clamp_point_to_rects(1574, 683, W, H, USER_SCREENS) is None


def test_saved_position_fully_on_screen_is_unchanged():
    """完全落在螢幕內的座標不應被改動。"""
    from gui.screen_utils import clamp_point_to_rects

    assert clamp_point_to_rects(200, 300, W, H, USER_SCREENS) == (200, 300)


def test_clamp_pushes_offscreen_right_inside():
    """右邊界溢出 → 推回，使右緣剛好貼齊螢幕右邊界。"""
    from gui.screen_utils import clamp_point_to_rects

    # 主屏寬 1440，x=1300 時右緣 1660 溢出 220px
    assert clamp_point_to_rects(1300, 300, W, H, USER_SCREENS) == (1440 - W, 300)


def test_clamp_pushes_offscreen_bottom_inside():
    """底部溢出 → 推回，使下緣剛好貼齊螢幕底邊。"""
    from gui.screen_utils import clamp_point_to_rects

    # 副屏 y 範圍 820..1900，y=1880 時下緣 1952 溢出
    assert clamp_point_to_rects(1500, 1880, W, H, USER_SCREENS) == (1500, 1900 - H)


def test_clamp_picks_screen_with_largest_overlap():
    """橫跨兩屏時，應推入交疊面積較大的那一屏。"""
    from gui.screen_utils import clamp_point_to_rects

    # y=1000 兩屏都覆蓋；x=1400 時只有 40px 在主屏、320px 在副屏 → 選副屏
    x, _y = clamp_point_to_rects(1400, 1000, W, H, USER_SCREENS)
    assert x == 1440, "應被推入副屏（交疊面積較大），而非留在主屏"


def test_negative_origin_screen_supported():
    """副屏排在主屏左邊時座標為負，必須視為合法。

    這正是不可以用 `indicator_x = -1` 之類數值 sentinel 表達
    「自動居中」的原因 —— 負座標在多螢幕虛擬桌面是完全合法的。
    """
    from gui.screen_utils import clamp_point_to_rects

    screens = [(-1920, 0, 1920, 1080), (0, 0, 1920, 1080)]
    assert clamp_point_to_rects(-1800, 500, W, H, screens) == (-1800, 500)


def test_empty_rects_returns_none():
    """取不到任何螢幕時回 None，不可拋異常。"""
    from gui.screen_utils import clamp_point_to_rects

    assert clamp_point_to_rects(100, 100, W, H, []) is None


def test_widget_larger_than_screen_keeps_topleft_visible():
    """widget 比螢幕大時，左上角仍必須可見（不可回負偏移）。"""
    from gui.screen_utils import clamp_point_to_rects

    tiny = [(0, 0, 200, 50)]
    assert clamp_point_to_rects(10, 10, W, H, tiny) == (0, 0)


def test_bottom_center_is_horizontally_centered_and_above_margin():
    """底部中央：水平居中，下緣距螢幕底邊剛好一個邊距。"""
    from gui.screen_utils import BOTTOM_MARGIN, bottom_center_in_rect

    rect = (1440, 820, 1920, 1080)
    x, y = bottom_center_in_rect(rect, W, H)
    assert x == 1440 + (1920 - W) // 2
    assert y + H == 820 + 1080 - BOTTOM_MARGIN


def test_bottom_center_respects_custom_margin():
    from gui.screen_utils import bottom_center_in_rect

    _x, y = bottom_center_in_rect((0, 0, 1440, 2560), W, H, margin=0)
    assert y + H == 2560


def test_bottom_center_clamps_when_screen_too_small():
    """螢幕比 widget + 邊距還細時，不可回到螢幕上方（負 y 偏移）。"""
    from gui.screen_utils import bottom_center_in_rect

    x, y = bottom_center_in_rect((0, 0, 200, 50), W, H)
    assert (x, y) == (0, 0)


def test_bottom_center_avoids_taskbar_via_available_geometry():
    """傳入的是 availableGeometry（已扣掉工作列），底邊即為可用底邊。"""
    from gui.screen_utils import BOTTOM_MARGIN, bottom_center_in_rect

    # 工作列在底部佔 48px → availableGeometry 高度 1032
    _x, y = bottom_center_in_rect((0, 0, 1920, 1032), W, H)
    assert y + H == 1032 - BOTTOM_MARGIN


def test_resolve_position_falls_back_to_bottom_center_when_saved_invalid():
    """儲存座標無效 → 改用底部中央並回報 fell_back，而非照 move 過去。"""
    from gui import screen_utils

    with patch.object(screen_utils, "available_rects", return_value=USER_SCREENS), \
         patch.object(screen_utils, "cursor_screen_rect", return_value=USER_SCREENS[1]):
        pos, fell_back = screen_utils.resolve_position(W, H, (1574, 683))

    assert pos == screen_utils.bottom_center_in_rect(USER_SCREENS[1], W, H)
    assert fell_back is True


def test_resolve_position_valid_saved_reports_no_fallback():
    from gui import screen_utils

    with patch.object(screen_utils, "available_rects", return_value=USER_SCREENS):
        assert screen_utils.resolve_position(W, H, (200, 300)) == ((200, 300), False)


def test_resolve_position_none_saved_uses_bottom_center():
    """自動模式不算 fallback —— 否則每次顯示都會誤觸自我修復信號。"""
    from gui import screen_utils

    with patch.object(screen_utils, "cursor_screen_rect", return_value=USER_SCREENS[0]):
        pos, fell_back = screen_utils.resolve_position(W, H, None)

    assert pos == screen_utils.bottom_center_in_rect(USER_SCREENS[0], W, H)
    assert fell_back is False


def test_auto_bottom_center_without_any_screen_returns_origin():
    """極端情況（取不到螢幕）：回 (0, 0) 且不可拋異常拖死語音流程。"""
    from gui import screen_utils

    with patch.object(screen_utils, "cursor_screen_rect", return_value=None):
        assert screen_utils.auto_bottom_center(W, H) == (0, 0)


def test_screen_utils_has_no_print():
    """專案硬規則：禁止 print 調試，統一用 logger。"""
    src = Path(__file__).resolve().parent.parent / "gui" / "screen_utils.py"
    assert "print(" not in src.read_text(encoding="utf-8")


# ─── 第 2 層：config 遷移與 app 寫入路徑 ───────────────────

def test_ui_config_defaults_to_auto_center():
    from utils.config import UIConfig

    assert UIConfig().indicator_auto_center is True


def test_ui_config_coerces_broken_coordinates():
    """手改壞的 config.json 不可令 move() 拋 TypeError → 浮窗永久靜默停用。"""
    from utils.config import UIConfig

    cfg = UIConfig(indicator_x="abc", indicator_y=None)  # type: ignore[arg-type]
    assert (cfg.indicator_x, cfg.indicator_y) == (100, 100)
    assert UIConfig(indicator_x="700").indicator_x == 700  # type: ignore[arg-type]


def test_ui_config_coerces_broken_flags():
    """非布林旗標要收斂；None（缺值）取安全預設 True。"""
    from utils.config import UIConfig

    assert UIConfig(show_indicator=0).show_indicator is False  # type: ignore[arg-type]
    assert UIConfig(show_indicator=None).show_indicator is True  # type: ignore[arg-type]
    assert UIConfig(indicator_auto_center="yes").indicator_auto_center is True  # type: ignore[arg-type]


def test_legacy_config_migrates_to_auto_center():
    """遷移鎖：舊 config（只有壞座標、無新欄位）必須自動變成底部中央模式。

    這令用戶不需要手改 config.json 就修好 P0 bug。
    """
    from utils.config import _dict_to_config

    cfg = _dict_to_config(
        {"ui": {"indicator_x": 1574, "indicator_y": 683, "show_indicator": True}}
    )
    assert cfg.ui.indicator_auto_center is True
    assert cfg.ui.indicator_x == 1574  # 舊值保留，只是不再被使用


def _make_voice_app(config):
    """建立 VoiceApp 但不呼叫 run() / _initialize()，故不會起 GUI 或 ASR。"""
    from app.app import VoiceApp

    va = VoiceApp()
    va._config = config
    va._main_window = None
    va._asr_process = None
    return va


def test_update_indicator_position_disables_auto_center():
    """拖動 = 明確自訂位置 → 必須關掉自動居中，否則下次啟動又跳回中央。"""
    from utils.config import AppConfig

    va = _make_voice_app(AppConfig())
    with patch("app.app.ConfigManager.save"):
        va.update_indicator_position(700, 900)

    assert va.config.ui.indicator_auto_center is False
    assert (va.config.ui.indicator_x, va.config.ui.indicator_y) == (700, 900)


def test_reset_indicator_position_enables_auto_center():
    from dataclasses import replace

    from utils.config import AppConfig, UIConfig

    cfg = replace(AppConfig(), ui=UIConfig(indicator_x=700, indicator_y=900,
                                           indicator_auto_center=False))
    va = _make_voice_app(cfg)
    calls = []
    va._invoke_gui = lambda method, *a: calls.append(method)
    with patch("app.app.ConfigManager.save"):
        va.reset_indicator_position()

    assert va.config.ui.indicator_auto_center is True
    assert calls == ["reset_recording_indicator"], "設定頁按鈕路徑要通知 GUI 歸位"


def test_self_heal_reset_does_not_command_gui():
    """浮窗自我修復時 widget 已歸位；再排一次 queued move 會令浮窗顯示後跳屏。"""
    from utils.config import AppConfig

    va = _make_voice_app(AppConfig())
    calls = []
    va._invoke_gui = lambda method, *a: calls.append(method)
    with patch("app.app.ConfigManager.save"):
        va.reset_indicator_position(notify_gui=False)

    assert va.config.ui.indicator_auto_center is True
    assert calls == []


def _live_and_stale():
    """(live, stale)：live 已被重置為自動居中，stale 是設定頁的過期快照。"""
    from dataclasses import replace

    from utils.config import AppConfig, UIConfig

    live = replace(AppConfig(), ui=UIConfig(indicator_x=700, indicator_y=900,
                                            indicator_auto_center=True))
    stale = replace(AppConfig(), ui=UIConfig(indicator_x=100, indicator_y=100,
                                             indicator_auto_center=False,
                                             show_indicator=False))
    return live, stale


def test_merge_live_indicator_position_keeps_live_coords():
    """設定頁快照不可覆蓋 live 浮窗座標；show_indicator 仍歸設定頁擁有。"""
    from utils.config import merge_live_indicator_position

    live, stale = _live_and_stale()
    merged = merge_live_indicator_position(stale, live)

    assert (merged.ui.indicator_x, merged.ui.indicator_y) == (700, 900)
    assert merged.ui.indicator_auto_center is True
    assert merged.ui.show_indicator is False


def test_settings_save_emits_live_indicator_position():
    """Race 端到端鎖：驅動真正的儲存路徑，驗證發出的 config 帶 live 座標。

    情境：開設定頁（快照 auto_center=False, (100,100)）→ 按「重置到底部中央」
    （live 即時變成 auto_center=True, (700,900)）→ 按儲存。若直接提交快照，
    剛剛的重置會被靜默 clobber。

    刻意驅動 MainWindow._on_settings_save 本身，而非用 inspect.getsource 檢查
    原始碼字串 —— 後者在「參數傳反」或「忘記接回傳值」時依然綠燈，是假安全感。
    """
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from PySide6.QtCore import QObject, Signal

    from gui.main_window import MainWindow

    live, stale = _live_and_stale()

    class _Owner(QObject):
        settings_save_requested = Signal(object)
        _settings_panel = type("_Panel", (), {"get_config": lambda self: stale})()
        _app_controller = type("_Ctrl", (), {"config": live})()

        def _navigate_to_voice(self) -> None:
            pass

    owner = _Owner()
    emitted: list = []
    owner.settings_save_requested.connect(emitted.append)
    MainWindow._on_settings_save(owner)

    assert emitted, "應發出 settings_save_requested"
    ui = emitted[0].ui
    assert (ui.indicator_x, ui.indicator_y) == (700, 900)
    assert ui.indicator_auto_center is True
    assert ui.show_indicator is False, "設定頁仍應擁有 show_indicator"


def test_apply_config_rebuilds_indicator_when_show_toggled():
    """show_indicator 變更 → 通知 GUI 重建浮窗（不需重啟程式）。"""
    from dataclasses import replace

    from utils.config import AppConfig, UIConfig

    va = _make_voice_app(replace(AppConfig(), ui=UIConfig(show_indicator=True)))
    calls = []
    va._invoke_gui = lambda method, *a: calls.append(method)
    with patch("app.app.ConfigManager.save"):
        va._apply_config(replace(AppConfig(), ui=UIConfig(show_indicator=False)))

    assert "rebuild_recording_indicator" in calls


def test_create_indicator_returns_none_when_disabled():
    """show_indicator=False → 完全不建立浮窗（設定真正生效）。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from dataclasses import replace

    from gui.main_window import MainWindow

    from utils.config import AppConfig, UIConfig

    class _Stub:
        config = replace(AppConfig(), ui=UIConfig(show_indicator=False))

    class _Owner:
        _app_controller = _Stub()

    assert MainWindow._create_recording_indicator(_Owner()) is None


# ─── 第 3 層：Qt offscreen widget ─────────────────────────

def test_indicator_size_is_360x72():
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui.recording_indicator import RecordingIndicator

    ind = RecordingIndicator()
    assert (ind.width(), ind.height()) == (W, H)


def test_text_rect_does_not_overlap_pulse_dot():
    """放大後文字左邊界必須避開脈衝圓點最大半徑，且右緣不可超出浮窗。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui import recording_indicator as ri

    assert ri._TEXT_LEFT >= ri._DOT_CX + ri._DOT_BASE_R + ri._DOT_PULSE_R
    ind = ri.RecordingIndicator()
    assert ind._text_rect.right() <= ri._WIDTH


def test_show_recording_positions_widget_inside_a_screen():
    """顯示後浮窗必須落在某塊真實螢幕內 —— 本次 bug 的端到端鎖。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui import screen_utils
    from gui.recording_indicator import RecordingIndicator

    # 模擬用戶的壞座標
    ind = RecordingIndicator(saved_pos=(1574, 683))
    ind.show_recording()
    p = ind.pos()
    assert screen_utils.clamp_point_to_rects(
        p.x(), p.y(), W, H, screen_utils.available_rects()
    ) == (p.x(), p.y())
    ind.hide_recording()


def test_bad_saved_position_requests_auto_center_heal():
    """壞座標首次顯示時應發出 auto_center_requested，讓上層清掉壞設定。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui.recording_indicator import RecordingIndicator

    ind = RecordingIndicator(saved_pos=(1574, 683))
    seen = []
    ind.auto_center_requested.connect(lambda: seen.append(1))
    ind.show_recording()
    ind.hide_recording()
    assert seen, "壞座標必須觸發自我修復信號"


def test_no_reposition_while_already_visible():
    """狀態切換（錄音→識別→潤色）期間浮窗已可見，不可重新定位（否則會跳）。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui.recording_indicator import STATE_PROCESSING, RecordingIndicator

    ind = RecordingIndicator(saved_pos=(200, 300))
    ind.show_recording()
    before = ind.pos()
    ind.set_state(STATE_PROCESSING)
    ind.show_recording()
    assert ind.pos() == before
    ind.hide_recording()


def test_force_reposition_moves_even_while_visible():
    """新流程起點必須重新定位：上次「完成」後有 800ms 延遲隱藏，期間開始
    新錄音時浮窗仍可見，不強制重算就會留在上一塊螢幕。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui import screen_utils
    from gui.recording_indicator import RecordingIndicator

    ind = RecordingIndicator(saved_pos=(200, 300))
    ind.show_recording()
    assert (ind.pos().x(), ind.pos().y()) == (200, 300)

    ind.reset_to_auto_center()
    ind.move(200, 300)                      # 模擬停留在上一塊螢幕的舊位置
    ind.show_recording(force_reposition=True)
    assert (ind.pos().x(), ind.pos().y()) == screen_utils.auto_bottom_center(W, H)
    ind.hide_recording()


def test_flow_start_forces_reposition_from_idle_states():
    """_sync_indicator_state 由空閒 / 完成 / 失敗進入流程時要傳 force_reposition。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui.main_window import (
        STATUS_DONE, STATUS_FAILED, STATUS_READY, STATUS_RECORDING, MainWindow,
    )

    seen: list = []

    class _Ind:
        def set_state(self, *a): pass
        def hide_recording(self): pass
        def show_recording(self, force_reposition=False):
            seen.append(force_reposition)

    class _Owner:
        _recording_indicator = _Ind()
        _indicator_hide_timer = type("_T", (), {"stop": lambda self: None})()
        _cancel_indicator_hide_timer = MainWindow._cancel_indicator_hide_timer

    for prev in (STATUS_READY, STATUS_DONE, STATUS_FAILED, ""):
        MainWindow._sync_indicator_state(_Owner(), STATUS_RECORDING, prev)
    assert seen == [True] * 4, "流程起點必須強制重新定位"

    seen.clear()
    MainWindow._sync_indicator_state(_Owner(), "識別中...", STATUS_RECORDING)
    assert seen == [False], "流程中途不可重新定位"


def test_reset_to_auto_center_moves_to_bottom_center():
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from gui import screen_utils
    from gui.recording_indicator import RecordingIndicator

    ind = RecordingIndicator(saved_pos=(200, 300))
    ind.reset_to_auto_center()
    p = ind.pos()
    assert (p.x(), p.y()) == screen_utils.auto_bottom_center(W, H)


def test_long_status_text_is_elided_not_silently_cut():
    """超長狀態文字要有省略號，不可無聲截斷。"""
    if _qt_app() is None:
        pytest.skip("無 PySide6")
    from PySide6.QtGui import QFontMetrics

    from gui import recording_indicator as ri

    ind = ri.RecordingIndicator()
    long_text = "分段識別中" * 20
    elided = QFontMetrics(ind._font).elidedText(
        long_text, ri.Qt.TextElideMode.ElideRight, ind._text_rect.width(),
    )
    assert elided.endswith("…")
    assert len(elided) < len(long_text)
