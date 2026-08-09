"""
U4 + U18：錄音歷史分頁的效能與可用性

U4 每次語音輸入完成都在主線程重載 100 筆含完整 WAV 的歷史並重建 200+ 個按鈕。
    即使設定頁從沒打開過也照做 —— 講完話貼上之後 GUI 頓一下。
U18 寫「共 843 筆」但表格永遠只顯示 100 筆，沒有分頁也沒有搜尋，
    識別結果欄硬截斷成「...」且沒有 tooltip。「找回某段文字」幾乎不可用。

鎖住的行為：
- get_recent_meta() 絕不 SELECT audio_data，播放時才按 id 取單筆 blob
- keyword / limit / offset 全部下推到 SQL，不在 Python 端過濾
- count() 有快取，寫入操作會失效
- 分頁隱藏時只標記 dirty，可見時（或 showEvent）才真的重建表格
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.recording_db import RecordingDatabase


def _pcm(n: int = 8) -> bytes:
    """產生 n 個 sample 的 float32 PCM。"""
    return struct.pack(f"{n}f", *([0.1] * n))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "history.db")
    d = RecordingDatabase()
    yield d
    d.close()


# ─────────────────────────── U4：不再載入 BLOB ───────────────────────────

class TestU4MetaExcludesAudio:
    def test_meta_has_no_audio_field(self, db: RecordingDatabase) -> None:
        """RecordingMeta 根本不該有 audio_data 欄位，杜絕誤用。"""
        db.insert(_pcm(), 1.0, asr_text="你好")
        meta = db.get_recent_meta(limit=10)[0]
        assert not hasattr(meta, "audio_data")

    def test_meta_query_never_selects_audio_data(self, db: RecordingDatabase) -> None:
        """真正的成本在 SQL —— SELECT * 會把 WAV 一起撈上來。"""
        db.insert(_pcm(4096), 2.0, asr_text="長錄音")

        seen: list[str] = []
        db._conn.set_trace_callback(seen.append)
        try:
            db.get_recent_meta(limit=10)
        finally:
            db._conn.set_trace_callback(None)

        assert seen, "應該有查詢發生"
        for sql in seen:
            assert "audio_data" not in sql, f"查詢仍撈了 BLOB: {sql}"
            assert "SELECT *" not in " ".join(sql.upper().split())

    def test_meta_keeps_the_fields_the_table_needs(self, db: RecordingDatabase) -> None:
        db.insert(_pcm(), 1.5, asr_text="原文", llm_text="潤色後", role_id="writer")
        meta = db.get_recent_meta(limit=10)[0]
        assert (meta.duration, meta.asr_text, meta.llm_text, meta.role_id) == (
            1.5, "原文", "潤色後", "writer",
        )
        assert meta.id > 0 and meta.timestamp is not None

    def test_audio_still_reachable_by_id(self, db: RecordingDatabase) -> None:
        """播放時才取 blob —— 資料必須仍然拿得到。"""
        rid = db.insert(_pcm(), 1.0, asr_text="播放我")
        assert db.get_by_id(rid).audio_data


# ─────────────────────────── U18：搜尋與分頁 ───────────────────────────

class TestU18SearchAndPaging:
    def test_keyword_filter_is_pushed_down_to_sql(self, db: RecordingDatabase) -> None:
        db.insert(_pcm(), 1.0, asr_text="今天的會議記錄")
        db.insert(_pcm(), 1.0, asr_text="買菜清單")
        db.insert(_pcm(), 1.0, asr_text="無關", llm_text="會議紀要潤色版")

        hits = db.get_recent_meta(limit=100, keyword="會議")
        assert {m.asr_text for m in hits} == {"今天的會議記錄", "無關"}

    def test_keyword_matches_llm_text_too(self, db: RecordingDatabase) -> None:
        """用戶記得的是貼出去的那一版（llm_text），不是原始識別結果。"""
        db.insert(_pcm(), 1.0, asr_text="ㄨㄤˊ總", llm_text="王總的報價")
        assert len(db.get_recent_meta(keyword="王總")) == 1

    def test_keyword_special_chars_are_escaped(self, db: RecordingDatabase) -> None:
        """LIKE 的 % 與 _ 是萬用字元，用戶輸入它們時必須當字面值。"""
        db.insert(_pcm(), 1.0, asr_text="折扣 50%")
        db.insert(_pcm(), 1.0, asr_text="完全無關的內容")

        assert len(db.get_recent_meta(keyword="50%")) == 1
        assert len(db.get_recent_meta(keyword="%")) == 1

    def test_offset_pages_through_results(self, db: RecordingDatabase) -> None:
        for i in range(5):
            db.insert(_pcm(), 1.0, asr_text=f"第{i}筆")

        page1 = db.get_recent_meta(limit=2, offset=0)
        page2 = db.get_recent_meta(limit=2, offset=2)
        assert len(page1) == len(page2) == 2
        assert not {m.id for m in page1} & {m.id for m in page2}

    def test_count_respects_keyword(self, db: RecordingDatabase) -> None:
        db.insert(_pcm(), 1.0, asr_text="會議")
        db.insert(_pcm(), 1.0, asr_text="其他")
        assert db.count() == 2
        assert db.count(keyword="會議") == 1


class TestU18CountCache:
    def test_repeated_count_hits_db_once(self, db: RecordingDatabase) -> None:
        db.insert(_pcm(), 1.0)
        db.count()

        calls: list[str] = []
        db._conn.set_trace_callback(calls.append)
        try:
            db.count()
            db.count()
        finally:
            db._conn.set_trace_callback(None)
        assert not calls, "無鍵字的總數應直接讀快取"

    def test_insert_invalidates_count_cache(self, db: RecordingDatabase) -> None:
        assert db.count() == 0
        db.insert(_pcm(), 1.0)
        assert db.count() == 1

    def test_delete_invalidates_count_cache(self, db: RecordingDatabase) -> None:
        rid = db.insert(_pcm(), 1.0)
        assert db.count() == 1
        db.delete(rid)
        assert db.count() == 0

    def test_cleanup_old_invalidates_count_cache(self, db: RecordingDatabase) -> None:
        rid = db.insert(_pcm(), 1.0)
        db._conn.execute(
            "UPDATE recordings SET timestamp = '2020-01-01T00:00:00' WHERE id = ?",
            (rid,),
        )
        db._conn.commit()
        assert db.count() == 1        # 先讓快取生效

        assert db.cleanup_old(days=30) == 1
        assert db.count() == 0

    def test_keyword_count_is_not_cached_as_total(self, db: RecordingDatabase) -> None:
        """帶關鍵字的計數不可以污染總數快取。"""
        db.insert(_pcm(), 1.0, asr_text="會議")
        db.insert(_pcm(), 1.0, asr_text="其他")
        assert db.count(keyword="會議") == 1
        assert db.count() == 2


# ─────────────────────────── U4：可見性守門 ───────────────────────────

@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("未安裝 PySide6")
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def tab(qapp, db):
    from gui.widgets.history_tab import HistoryTab

    t = HistoryTab(db)
    yield t
    t.deleteLater()


class TestU4VisibilityGating:
    def test_hidden_tab_only_marks_dirty(self, tab, monkeypatch) -> None:
        """設定頁從沒打開過時，講一句話不該重建 100 列表格。"""
        called: list[int] = []
        monkeypatch.setattr(tab, "_refresh_list", lambda: called.append(1))

        tab.hide()
        tab.mark_dirty()

        assert not called
        assert tab._dirty is True

    def test_show_event_flushes_pending_refresh(self, tab, monkeypatch) -> None:
        from PySide6.QtGui import QShowEvent

        tab.hide()
        tab.mark_dirty()

        called: list[int] = []
        monkeypatch.setattr(tab, "_refresh_list", lambda: called.append(1))
        tab.showEvent(QShowEvent())

        assert called, "分頁變可見時要補上這次刷新"

    def test_visible_tab_refreshes_immediately(self, tab, monkeypatch) -> None:
        called: list[int] = []
        monkeypatch.setattr(tab, "_refresh_list", lambda: called.append(1))
        monkeypatch.setattr(tab, "isVisible", lambda: True)

        tab.mark_dirty()
        assert called

    def test_show_event_without_dirty_does_not_refresh(self, tab, monkeypatch) -> None:
        """反覆切分頁不該每次都重查資料庫。"""
        from PySide6.QtGui import QShowEvent

        tab._dirty = False
        called: list[int] = []
        monkeypatch.setattr(tab, "_refresh_list", lambda: called.append(1))
        tab.showEvent(QShowEvent())
        assert not called

    def test_refresh_clears_dirty_flag(self, tab) -> None:
        tab.mark_dirty()
        tab._refresh_list()
        assert tab._dirty is False

    def test_settings_panel_refresh_history_does_not_hit_db(
        self, qapp, tmp_path, monkeypatch,
    ) -> None:
        """app.py 的每句話回呼最終走到這裡，必須是零成本的。"""
        monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "h.db")
        from gui.settings_panel import SettingsPanel
        from utils.config import AppConfig

        panel = SettingsPanel(AppConfig())
        try:
            calls: list[int] = []
            monkeypatch.setattr(
                panel._tab_history, "_refresh_list", lambda: calls.append(1),
            )
            panel._tab_history.hide()
            panel.refresh_history()
            assert not calls
            assert panel._tab_history._dirty is True
        finally:
            panel.deleteLater()


# ─────────────────────────── U4/U18：表格行為 ───────────────────────────

class TestHistoryTableBehaviour:
    def test_list_uses_meta_query(self, tab, monkeypatch) -> None:
        """表格重建絕不能走 get_recent()（會載入全部 WAV）。"""
        monkeypatch.setattr(
            tab._db, "get_recent",
            lambda *a, **kw: pytest.fail("表格不該呼叫 get_recent（含 BLOB）"),
        )
        tab._refresh_list()

    def test_play_fetches_blob_lazily(self, tab, db, monkeypatch) -> None:
        rid = db.insert(_pcm(), 1.0, asr_text="播放我")
        tab._refresh_list()

        fetched: list[int] = []
        monkeypatch.setattr(
            db, "get_by_id",
            lambda r: fetched.append(r) or type("R", (), {"audio_data": b"WAV"})(),
        )
        monkeypatch.setattr(tab._player, "load_wav", lambda data: None)
        monkeypatch.setattr(tab._player, "_toggle_play", lambda: None)

        tab._on_play(tab._records[0])
        assert fetched == [rid]

    def test_play_survives_deleted_record(self, tab, db, monkeypatch) -> None:
        """列表是快照，記錄可能已被別處刪掉 —— 不可以炸掉。"""
        db.insert(_pcm(), 1.0)
        tab._refresh_list()
        monkeypatch.setattr(db, "get_by_id", lambda r: None)
        tab._on_play(tab._records[0])   # 不拋例外即通過

    def test_truncated_cell_carries_full_text_tooltip(self, tab, db) -> None:
        long_text = "字" * 120
        db.insert(_pcm(), 1.0, asr_text=long_text)
        tab._refresh_list()

        cell = tab._table.item(0, 2)
        assert cell.text().endswith("...")
        assert cell.toolTip() == long_text

    def test_count_label_admits_it_is_showing_a_subset(self, tab, db) -> None:
        """「共 843 筆」配 100 列表格，用戶會以為資料丟了。"""
        for i in range(5):
            db.insert(_pcm(), 1.0, asr_text=f"第{i}筆")
        tab._page_size = tab._shown = 2
        tab._refresh_list()

        label = tab._count_label.text()
        assert "2" in label and "5" in label

    def test_load_more_extends_the_window(self, tab, db) -> None:
        for i in range(5):
            db.insert(_pcm(), 1.0, asr_text=f"第{i}筆")
        tab._page_size = tab._shown = 2
        tab._refresh_list()
        assert tab._table.rowCount() == 2

        tab._on_load_more()
        assert tab._table.rowCount() == 4

    def test_load_more_hidden_when_everything_is_shown(self, tab, db) -> None:
        db.insert(_pcm(), 1.0)
        tab._refresh_list()
        assert not tab._more_btn.isVisible() or not tab._more_btn.isEnabled()

    def test_search_filters_the_table(self, tab, db) -> None:
        db.insert(_pcm(), 1.0, asr_text="今天的會議記錄")
        db.insert(_pcm(), 1.0, asr_text="買菜清單")

        tab._search_edit.setText("會議")
        tab._apply_search()

        assert tab._table.rowCount() == 1
        assert "會議" in tab._table.item(0, 2).text()

    def test_search_resets_paging(self, tab, db) -> None:
        """搜尋後 offset 必須歸零，否則會顯示空白頁。"""
        for i in range(5):
            db.insert(_pcm(), 1.0, asr_text=f"會議{i}")
        tab._page_size = tab._shown = 2
        tab._refresh_list()
        tab._on_load_more()
        assert tab._shown > tab._page_size

        tab._search_edit.setText("會議")
        tab._apply_search()
        assert tab._shown == tab._page_size
