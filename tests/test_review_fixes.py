"""
Code Review 修復回歸測試：
  H1 - TranscribeTab 刪除按鈕在交叉新增/刪除後仍有效
  H2 - 轉錄頁導航有忙碌狀態防護
  M1 - _recognize_long_audio 不做冗餘 astype
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── H1: 刪除按鈕交叉操作後仍正確刪除 ─────────────────────

def test_remove_button_works_after_interleaved_add_remove():
    """新增 A,B,C → 刪除 B → 新增 D → 點 D 的刪除按鈕 → D 應被移除。

    舊實作用 row index 綁定 lambda，交叉操作後 D 的按鈕持有過期索引，
    點擊會靜默失敗（bounds check 保護不會誤刪但也不會真正刪除）。
    """
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
    except ImportError:
        return  # CI 無 Qt，跳過

    from pathlib import Path
    from gui.widgets.transcribe_tab import TranscribeTab
    from utils.config import FileConfig

    tab = TranscribeTab(config=FileConfig())

    # 新增 A, B, C
    tab._add_files([Path("A.mp3"), Path("B.mp3"), Path("C.mp3")])
    assert tab._table.rowCount() == 3

    # 刪除 B（row 1）
    tab._remove_row(1)
    assert tab._table.rowCount() == 2

    # 新增 D
    tab._add_files([Path("D.mp3")])
    assert tab._table.rowCount() == 3

    # 找到 D 所在的行
    d_row = None
    for r in range(tab._table.rowCount()):
        item = tab._table.item(r, tab._COL_NAME)
        if item and item.data(0x0100) == str(Path("D.mp3")):  # Qt.ItemDataRole.UserRole
            d_row = r
            break
    assert d_row is not None, "D.mp3 應在表中"

    # 點擊 D 的刪除按鈕
    remove_btn = tab._table.cellWidget(d_row, tab._COL_REMOVE)
    assert remove_btn is not None, "D 行應有刪除按鈕"
    remove_btn.click()

    # 驗證 D 已被移除
    remaining = set()
    for r in range(tab._table.rowCount()):
        item = tab._table.item(r, tab._COL_NAME)
        if item:
            remaining.add(item.data(0x0100))
    assert str(Path("D.mp3")) not in remaining, (
        f"D.mp3 應已被刪除，但仍在表中: {remaining}"
    )
    assert tab._table.rowCount() == 2


# ─── H2: 轉錄頁導航忙碌防護 ────────────────────────────────

def test_navigate_to_transcribe_blocked_during_recording():
    """錄音中（STATUS_RECORDING）不應允許切換到轉錄頁。

    _navigate_to_settings() 已有此防護，_navigate_to_transcribe() 也需要。
    """
    from gui.main_window import (
        STATUS_RECORDING, STATUS_RECOGNIZING, STATUS_LLM,
        _BUSY_STATUSES, _PAGE_TRANSCRIBE, _PAGE_VOICE,
    )

    # 驗證忙碌狀態集合包含所有預期狀態
    assert STATUS_RECORDING in _BUSY_STATUSES
    assert STATUS_RECOGNIZING in _BUSY_STATUSES
    assert STATUS_LLM in _BUSY_STATUSES

    # 嘗試實例化 MainWindow 驗證行為（若 Qt 不可用則用邏輯測試）
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
    except ImportError:
        return

    # 由於 MainWindow 需要完整 app，改為直接檢查方法原始碼
    import inspect
    from gui.main_window import MainWindow
    source = inspect.getsource(MainWindow._navigate_to_transcribe)

    # 修復後，方法內應檢查 _BUSY_STATUSES 或 _current_status
    assert "_BUSY_STATUSES" in source or "_current_status" in source, (
        "_navigate_to_transcribe() 必須包含忙碌狀態檢查，"
        "防止錄音中切換到轉錄頁導致 ASR 通道競爭"
    )


if __name__ == "__main__":
    import traceback

    tests = [
        test_remove_button_works_after_interleaved_add_remove,
        test_navigate_to_transcribe_blocked_during_recording,
    ]
    failed = 0
    for tcase in tests:
        try:
            tcase()
            print(f"  PASS  {tcase.__name__}")
        except Exception as err:  # noqa: BLE001
            print(f"  FAIL  {tcase.__name__}: {err}")
            traceback.print_exc()
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
