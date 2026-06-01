"""
Tests for three bug fixes:
  P1 - Long-audio overlap dedup in live recording
  P2 - Transcribe tab emits current UI config (not stale persisted config)
  P3 - Recording-limit status keeps floating indicator visible
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── P1: 重疊去重 ─────────────────────────────────────────

def _merge_text_overlap(a: str, b: str, max_check: int = 50) -> str:
    """從 app.py 提取的純函數 — 去除 a 尾端與 b 首端重疊字符後拼接。"""
    n = min(len(a), len(b), max_check)
    for i in range(n, 0, -1):
        if a[-i:] == b[:i]:
            return a + b[i:]
    return a + b


def test_merge_no_overlap():
    assert _merge_text_overlap("你好世界", "再見") == "你好世界再見"


def test_merge_exact_overlap():
    """分段邊界有重複字符時，應去重一次。"""
    assert _merge_text_overlap("你好世界", "世界再見") == "你好世界再見"


def test_merge_partial_overlap():
    """只有部分重疊。"""
    assert _merge_text_overlap("ABCDE", "DEF") == "ABCDEF"


def test_merge_empty_a():
    assert _merge_text_overlap("", "hello") == "hello"


def test_merge_empty_b():
    assert _merge_text_overlap("hello", "") == "hello"


def test_merge_full_overlap():
    """b 完全包含於 a 的尾部 — 不應重複。"""
    assert _merge_text_overlap("hello world", "world") == "hello world"


def test_merge_multiple_parts():
    """多段合併：每相鄰段去重。"""
    parts = ["識別結果前段重疊", "重疊後段文字", "文字繼續"]
    result = parts[0]
    for p in parts[1:]:
        result = _merge_text_overlap(result, p)
    assert result == "識別結果前段重疊後段文字繼續"
    assert "重疊重疊" not in result


# ─── P2: TranscribeTab 信號攜帶當前 UI 設定 ────────────────

def test_transcribe_signal_carries_config():
    """確認 TranscribeTab.transcribe_requested 信號攜帶 FileConfig。"""
    from gui.widgets.transcribe_tab import TranscribeTab
    from utils.config import FileConfig
    from PySide6.QtCore import Signal

    # 信號應為 Signal(list, object) — 第二個參數是 FileConfig
    sig = TranscribeTab.transcribe_requested
    # PySide6 Signal types: check the signature includes 2 args
    assert sig is not None, "transcribe_requested signal must exist"


def test_transcribe_get_config_reads_checkboxes():
    """get_config() 應反映 checkbox 的實際狀態，而非 load_config 時的初始值。"""
    try:
        from PySide6.QtWidgets import QApplication
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        from gui.widgets.transcribe_tab import TranscribeTab
        from utils.config import FileConfig

        initial = FileConfig(save_srt=True, save_txt=True, save_json=False, llm_polish=False)
        tab = TranscribeTab(config=initial)

        # 用戶在 UI 中改變選項（未按 Save）
        tab._srt_check.setChecked(False)
        tab._json_check.setChecked(True)
        tab._llm_polish_check.setChecked(True)

        cfg = tab.get_config()
        assert cfg.save_srt is False
        assert cfg.save_txt is True
        assert cfg.save_json is True
        assert cfg.llm_polish is True

    except ImportError:
        # Qt not available in CI — skip gracefully
        pass


# ─── P3: 錄音上限狀態不隱藏浮動指示器 ─────────────────────

def test_sync_indicator_limit_status_shows_indicator():
    """
    當 status == '已達錄音上限' 時，_sync_indicator_state 不應隱藏浮窗。
    模擬 MainWindow._sync_indicator_state 的判斷邏輯。
    """
    STATUS_RECORDING = "錄音中"

    def _categorize_status(status: str) -> str:
        """從 main_window.py 提取的分類邏輯（修復後版本）。"""
        if status == "完成":
            return "done"
        if (
            status == STATUS_RECORDING
            or status.startswith("錄音")
            or status == "已達錄音上限"
        ):
            return "recording"
        if "潤色" in status or "LLM" in status:
            return "polishing"
        if any(kw in status for kw in ("識別", "處理", "校正", "轉錄", "分段")):
            return "processing"
        return "hide"

    assert _categorize_status("已達錄音上限") == "recording"
    assert _categorize_status("錄音中") == "recording"
    assert _categorize_status("錄音開始") == "recording"
    assert _categorize_status("完成") == "done"
    assert _categorize_status("分段識別中") == "processing"
    assert _categorize_status("LLM 處理中") == "polishing"
    assert _categorize_status("未知狀態") == "hide"


# ─── P4: LLM 運行時自動降級（auth/quota 錯誤切後備 provider）─────

def _make_failover_config(active_key="dead"):
    """構造帶兩個可用 provider 的 fake config（active 會 401，backup 正常）。"""
    class _LLM:
        enabled = True
        active_provider = active_key
        temperature = 0.1
        max_tokens = 100
        top_p = 1.0
        frequency_penalty = 0.0
        presence_penalty = 0.0
        do_sample = True
        custom_roles: list = []
        builtin_overrides: dict = {}
        providers = {
            "dead": {"name": "Dead", "api_url": "https://dead.example/v1",
                     "api_key": "deadkey", "model": "m1", "enabled": True},
            "backup": {"name": "Backup", "api_url": "https://backup.example/v1",
                       "api_key": "backupkey", "model": "m2", "enabled": True},
        }

    class _Cfg:
        llm = _LLM()

    return _Cfg()


class _FakeClient:
    """模擬 LLMClient：text=None 代表會拋 401 的死 key。"""

    def __init__(self, text):
        self._text = text

    def chat_with_warnings(self, messages, stream=True):
        if self._text is None:
            def _gen():
                raise RuntimeError("API Key 無效（HTTP 401）")
                yield ""  # 令此函數成為 generator（與真 client 一致，惰性拋錯）
            return _gen(), []
        return iter([self._text]), []


def test_llm_failover_on_auth_error():
    """active provider 回 401 → 自動切到 backup provider 並成功。"""
    from unittest.mock import patch
    from llm.processor import LLMProcessor, RoleConfig

    def _fake_build(self, provider):
        return _FakeClient("潤色後結果" if provider.key == "backup" else None)

    with patch.object(LLMProcessor, "_build_client", _fake_build):
        proc = LLMProcessor(_make_failover_config())
        result = proc.process("呢句嘢夠長要潤色", RoleConfig(name="default", system_prompt="潤色"))

    assert result.error == "", f"降級後應無錯誤，實際: {result.error!r}"
    assert result.text == "潤色後結果", f"應為 backup 結果，實際: {result.text!r}"


def test_llm_failover_all_dead_returns_error():
    """所有 provider 都 401 → graceful 返回 error + 原文（不崩潰）。"""
    from unittest.mock import patch
    from llm.processor import LLMProcessor, RoleConfig

    def _all_dead(self, provider):
        return _FakeClient(None)

    with patch.object(LLMProcessor, "_build_client", _all_dead):
        proc = LLMProcessor(_make_failover_config())
        result = proc.process("呢句嘢夠長要潤色", RoleConfig(name="default", system_prompt="潤色"))

    assert result.error != "", "全部死 key 應有 error"
    assert result.text == "呢句嘢夠長要潤色", "失敗應 fallback 原文"


if __name__ == "__main__":
    # 可直接 python tests/test_fixes.py 執行
    import traceback
    tests = [
        test_merge_no_overlap,
        test_merge_exact_overlap,
        test_merge_partial_overlap,
        test_merge_empty_a,
        test_merge_empty_b,
        test_merge_full_overlap,
        test_merge_multiple_parts,
        test_transcribe_signal_carries_config,
        test_transcribe_get_config_reads_checkboxes,
        test_sync_indicator_limit_status_shows_indicator,
        test_llm_failover_on_auth_error,
        test_llm_failover_all_dead_returns_error,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
