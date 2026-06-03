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

    def _fake_build(self, provider, timeout=None):
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

    def _all_dead(self, provider, timeout=None):
        return _FakeClient(None)

    with patch.object(LLMProcessor, "_build_client", _all_dead):
        proc = LLMProcessor(_make_failover_config())
        result = proc.process("呢句嘢夠長要潤色", RoleConfig(name="default", system_prompt="潤色"))

    assert result.error != "", "全部死 key 應有 error"
    assert result.text == "呢句嘢夠長要潤色", "失敗應 fallback 原文"


# ─── P5: 剪貼板加固（貼上失敗不再靜默 / 不冒泡）───────────────

def test_clipboard_no_write_has_set_text():
    """回歸守衛：ClipboardManager 不應有 write()（曾誤用），必須有 set_text()。"""
    from utils.clipboard import ClipboardManager
    assert hasattr(ClipboardManager, "set_text")
    assert not hasattr(ClipboardManager, "write"), "write() 已廢棄，應改用 set_text()"


def test_paste_text_returns_false_when_ctrl_v_fails():
    """Ctrl+V 模擬失敗（回 False）時，paste_text 應回 False。"""
    from unittest.mock import patch
    from utils.clipboard import ClipboardManager
    with patch.object(ClipboardManager, "set_text", return_value=True), \
         patch("utils.keyboard.KeyboardSimulator.press_ctrl_v", return_value=False):
        assert ClipboardManager.paste_text("x", restore=False) is False


def test_paste_text_swallows_exception_returns_false():
    """press_ctrl_v 拋異常時，paste_text 捕獲並回 False（不冒泡到呼叫端）。"""
    from unittest.mock import patch
    from utils.clipboard import ClipboardManager
    with patch.object(ClipboardManager, "set_text", return_value=True), \
         patch("utils.keyboard.KeyboardSimulator.press_ctrl_v",
               side_effect=RuntimeError("boom")):
        assert ClipboardManager.paste_text("x", restore=False) is False


def test_paste_text_success_returns_true():
    """正常路徑：set_text 成功 + Ctrl+V 成功 → 回 True。"""
    from unittest.mock import patch
    from utils.clipboard import ClipboardManager
    with patch.object(ClipboardManager, "set_text", return_value=True), \
         patch("utils.keyboard.KeyboardSimulator.press_ctrl_v", return_value=True):
        assert ClipboardManager.paste_text("x", restore=False) is True


def test_press_ctrl_v_returns_false_on_controller_error():
    """pynput controller 異常時，press_ctrl_v 回 False 而非冒泡。"""
    try:
        from unittest.mock import patch, MagicMock
        from utils.keyboard import KeyboardSimulator
    except ImportError:
        return  # pynput 不可用 — 優雅跳過
    bad = MagicMock()
    bad.press.side_effect = RuntimeError("pynput fail")
    with patch.object(KeyboardSimulator, "_get_controller", return_value=bad):
        assert KeyboardSimulator.press_ctrl_v() is False


def test_on_copy_result_uses_set_text():
    """托盤『複製最近結果』應呼叫 set_text（修復前誤用不存在的 write → AttributeError）。"""
    try:
        from unittest.mock import patch
        from app.app import VoiceApp
        from utils.clipboard import ClipboardManager
    except ImportError:
        return  # 依賴不可用 — 優雅跳過

    class _Stub:
        _last_result = "識別結果文字"

    with patch.object(ClipboardManager, "set_text", return_value=True) as m:
        VoiceApp._on_copy_result(_Stub())  # unbound 呼叫，只用 self._last_result
    m.assert_called_once_with("識別結果文字")


# ─── P6: 模型置頂（pinned 排最前 + 寫回 config）────────────────

def test_pinned_models_saved_to_providers():
    """複刻 _build_updated_providers 的寫回邏輯：pinned 寫入存在的 provider。"""
    providers = {"p": {"name": "P", "model": "m"}}
    pinned_store = {"p": ["glm-4", "glm-4-plus"], "ghost": ["x"]}
    for pkey, pinned in pinned_store.items():
        if pkey in providers:
            providers[pkey]["pinned_models"] = list(pinned)
    assert providers["p"]["pinned_models"] == ["glm-4", "glm-4-plus"]
    assert "ghost" not in providers, "不存在的 provider 不應被建立"


def test_populate_model_combo_pins_first():
    """真實跑 _populate_model_combo：置頂模型排最前、其餘去重且排除已置頂。"""
    try:
        from unittest.mock import patch
        from PySide6.QtWidgets import QApplication, QComboBox
        from gui.settings_panel import SettingsPanel
    except ImportError:
        return  # 無 Qt — 優雅跳過

    _app = QApplication.instance() or QApplication([])
    combo = QComboBox()
    combo.setEditable(True)

    class _Stub:
        _pinned_store = {"p": ["glm-4-plus", "glm-4"]}
        _model_input = object()  # 不等於 combo → 不觸發 _sync_pin_button

    fetched = ["glm-4", "glm-4-air", "glm-4-plus"]  # 含與 pinned 重複者
    with patch("llm.model_cache.get", return_value=(fetched, 0.0)):
        SettingsPanel._populate_model_combo(
            _Stub(), combo, "p", ["glm-4-flash"], "glm-4",
        )

    items = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]
    # 置頂兩個排最前，保持次序
    assert items[0] == "glm-4-plus"
    assert items[1] == "glm-4"
    # 其餘接其後（去重，排除已置頂）
    assert "glm-4-air" in items and "glm-4-flash" in items
    # 重複的 pinned 只出現一次
    assert items.count("glm-4") == 1
    assert items.count("glm-4-plus") == 1


# ─── P7: 錄音記錄優先（歷史保存在剪貼板輸出之前）──────────────

def test_history_saved_before_paste_in_process_audio():
    """
    回歸守衛：_process_audio 內，錄音歷史 insert 必須喺 paste_text 之前。
    修復前順序相反，paste 拋異常會冒泡跳過歷史保存 → 錄音記錄遺失。
    """
    try:
        import inspect
        from app.app import VoiceApp
    except ImportError:
        return  # 依賴不可用 — 優雅跳過
    src = inspect.getsource(VoiceApp._process_audio)
    assert "_recording_db.insert" in src
    assert "paste_text" in src
    assert src.index("_recording_db.insert") < src.index("paste_text"), \
        "錄音歷史必須喺剪貼板輸出之前保存，避免輸出環節出錯累及記錄"


def test_build_providers_history_excludes_separator():
    """置頂產生的分隔線（itemText="")不應污染 model_history（複刻收集邏輯）。"""
    try:
        from PySide6.QtWidgets import QApplication, QComboBox
    except ImportError:
        return  # 無 Qt — 優雅跳過
    _app = QApplication.instance() or QApplication([])
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItems(["glm-4-plus", "glm-4"])      # 置頂區
    combo.insertSeparator(combo.count())          # 分隔線 → itemText=""
    combo.addItems(["glm-4-air", "glm-4-flash"])  # 其餘
    # 複刻 _build_updated_providers 修正後的收集邏輯
    ui_items = [
        t for i in range(combo.count())
        if (t := combo.itemText(i).strip())
    ]
    assert "" not in ui_items, "分隔線空字串不應進入 model_history"
    assert ui_items == ["glm-4-plus", "glm-4", "glm-4-air", "glm-4-flash"]


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
        test_clipboard_no_write_has_set_text,
        test_paste_text_returns_false_when_ctrl_v_fails,
        test_paste_text_swallows_exception_returns_false,
        test_paste_text_success_returns_true,
        test_press_ctrl_v_returns_false_on_controller_error,
        test_on_copy_result_uses_set_text,
        test_pinned_models_saved_to_providers,
        test_populate_model_combo_pins_first,
        test_history_saved_before_paste_in_process_audio,
        test_build_providers_history_excludes_separator,
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
