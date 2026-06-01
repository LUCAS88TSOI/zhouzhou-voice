"""
DISCOVERY.md 第三輪 review — 修復驗證測試

測試覆蓋：
  F1  — 非字串 shortcut key 崩潰啟動 (Critical)
  F4  — 空/雜訊 ASR 不清 _last_result
  F5  — repolish API URL/Key 欄位誤導（read-only）
  F6  — repolish hotkey mode/threshold 不即時生效
  F8  — audio_player mkstemp fd 洩漏
"""
from __future__ import annotations

import os
import tempfile


# ─── F1: 非字串 shortcut key 崩潰 ──────────────────────────


class TestF1ShortcutKeyTypeSafety:
    """F1: 非字串 shortcut key 不應崩潰。"""

    def test_resolve_keyboard_key_none(self):
        """_resolve_keyboard_key(None) 不應拋異常。"""
        from utils.hotkey import _resolve_keyboard_key

        result = _resolve_keyboard_key(None)
        assert result is None

    def test_resolve_keyboard_key_int(self):
        """_resolve_keyboard_key(123) 不應拋異常。"""
        from utils.hotkey import _resolve_keyboard_key

        result = _resolve_keyboard_key(123)
        assert result is None

    def test_shortcut_config_coerces_none_key(self):
        """ShortcutConfig(key=None) 應退回預設 key。"""
        from utils.config import _dict_to_config

        cfg = _dict_to_config({"shortcut": {"key": None}})
        assert isinstance(cfg.shortcut.key, str)
        assert len(cfg.shortcut.key) > 0

    def test_shortcut_config_coerces_int_key(self):
        """ShortcutConfig(key=42) 應退回預設 key。"""
        from utils.config import _dict_to_config

        cfg = _dict_to_config({"shortcut": {"key": 42}})
        assert isinstance(cfg.shortcut.key, str)
        assert len(cfg.shortcut.key) > 0


# ─── F4: 空/雜訊 ASR 不清 _last_result ─────────────────────


class TestF4ClearStaleResult:
    """F4: 空/雜訊 ASR 結果應清空 _last_result。"""

    def test_empty_asr_clears_last_result(self):
        """ASR 回傳空字串時 _last_result 應被清空。"""
        from unittest.mock import MagicMock, patch
        from app.app import VoiceApp
        from utils.config import AppConfig
        import numpy as np

        va = object.__new__(VoiceApp)
        va._config = AppConfig()
        va._last_result = "之前的結果"
        va._last_pre_llm_text = "之前的文字"
        va._is_processing = False
        va._processing_lock = MagicMock()
        va._main_window = None
        va._asr_process = MagicMock()
        va._text_processor = None
        va._hotword = None
        va._llm = None
        va._recorder = None
        va._recording_db = None

        # 模擬 ASR 返回空結果
        va._asr_process.is_running = True
        with patch.object(va, "_try_recognize", return_value=""):
            # 產生 0.5 秒 float32 音頻
            audio = np.zeros(8000, dtype=np.float32).tobytes()
            va._process_audio(audio)

        assert va._last_result == "", f"_last_result 應清空，實際: {va._last_result!r}"
        assert va._last_pre_llm_text == "", f"_last_pre_llm_text 應清空，實際: {va._last_pre_llm_text!r}"

    def test_noise_only_clears_last_result(self):
        """ASR 回傳純標點時 _last_result 應被清空。"""
        from unittest.mock import MagicMock, patch
        from app.app import VoiceApp
        from utils.config import AppConfig
        import numpy as np

        va = object.__new__(VoiceApp)
        va._config = AppConfig()
        va._last_result = "之前的結果"
        va._last_pre_llm_text = "之前的文字"
        va._is_processing = False
        va._processing_lock = MagicMock()
        va._main_window = None
        va._asr_process = MagicMock()
        va._text_processor = None
        va._hotword = None
        va._llm = None
        va._recorder = None
        va._recording_db = None

        va._asr_process.is_running = True
        with patch.object(va, "_try_recognize", return_value="，。！？"):
            audio = np.zeros(8000, dtype=np.float32).tobytes()
            va._process_audio(audio)

        assert va._last_result == "", f"_last_result 應清空，實際: {va._last_result!r}"


# ─── F5: repolish API URL/Key 欄位 read-only ────────────────


class TestF5RepolishFieldsReadOnly:
    """F5: repolish 的 API URL 和 API Key 應為 read-only。"""

    def test_repolish_api_url_is_readonly(self):
        """repolish API URL 輸入框應不可編輯。"""
        from unittest.mock import MagicMock
        from utils.config import AppConfig

        # 需要完整建構 SettingsPanel 來檢查 widget 屬性
        # 改用直接呼叫 _build_llm_tab 太複雜，改為驗證 isReadOnly
        from gui.settings_panel import SettingsPanel

        panel = SettingsPanel.__new__(SettingsPanel)
        # 不呼叫 __init__，直接建構需要的 mock widgets
        # 改為檢查程式碼邏輯：_on_repolish_provider_index_changed 後 readonly
        # 太依賴 GUI — 改為 lightweight 斷言
        assert hasattr(SettingsPanel, "_on_repolish_provider_index_changed")

        # 真正的測試：呼叫方法時 setReadOnly 被調用
        panel = MagicMock()
        panel._repolish_provider_combo = MagicMock()
        panel._repolish_provider_combo.itemData.return_value = "bigmodel"
        panel._repolish_fields_widget = MagicMock()
        panel._config = AppConfig()
        panel._repolish_api_url_input = MagicMock()
        panel._repolish_api_key_input = MagicMock()
        panel._repolish_model_input = MagicMock()

        SettingsPanel._on_repolish_provider_index_changed(panel, 1)

        panel._repolish_api_url_input.setReadOnly.assert_called_with(True)
        panel._repolish_api_key_input.setReadOnly.assert_called_with(True)


# ─── F6: repolish hotkey mode/threshold 不即時生效 ──────────


class TestF6RepolishHotkeyRebuild:
    """F6: repolish_instant 或 threshold 變更應重建 listener。"""

    def test_repolish_instant_change_rebuilds_listener(self):
        """只改 repolish_instant（不改 key）應重建 repolish hotkey。"""
        from unittest.mock import MagicMock, patch, call
        from utils.config import AppConfig, ShortcutConfig
        from dataclasses import replace
        from app.app import VoiceApp

        old_sc = ShortcutConfig(repolish_key="f2", repolish_instant=True)
        new_sc = ShortcutConfig(repolish_key="f2", repolish_instant=False)  # 只改 instant
        old_cfg = replace(AppConfig(), shortcut=old_sc)
        new_cfg = replace(AppConfig(), shortcut=new_sc)

        va = object.__new__(VoiceApp)
        va._config = old_cfg
        va._hotkey = MagicMock()
        va._repolish_hotkey = MagicMock()
        va._llm = None
        va._main_window = None
        va._recording_db = None
        va._text_processor = None

        with patch("utils.config.ConfigManager.save"):
            with patch.object(va, "_make_repolish_hotkey", return_value=MagicMock()) as mock_make:
                va._apply_config(new_cfg)

        # 應重建 repolish hotkey（即使 key 沒變）
        mock_make.assert_called_once()

    def test_threshold_change_rebuilds_repolish(self):
        """只改 threshold（不改 key）也應重建。"""
        from unittest.mock import MagicMock, patch
        from utils.config import AppConfig, ShortcutConfig
        from dataclasses import replace
        from app.app import VoiceApp

        old_sc = ShortcutConfig(repolish_key="f2", threshold=0.3)
        new_sc = ShortcutConfig(repolish_key="f2", threshold=0.8)  # 只改 threshold
        old_cfg = replace(AppConfig(), shortcut=old_sc)
        new_cfg = replace(AppConfig(), shortcut=new_sc)

        va = object.__new__(VoiceApp)
        va._config = old_cfg
        va._hotkey = MagicMock()
        va._repolish_hotkey = MagicMock()
        va._llm = None
        va._main_window = None
        va._recording_db = None
        va._text_processor = None

        with patch("utils.config.ConfigManager.save"):
            with patch.object(va, "_make_repolish_hotkey", return_value=MagicMock()) as mock_make:
                va._apply_config(new_cfg)

        mock_make.assert_called_once()


# ─── F8: audio_player mkstemp fd 洩漏 ───────────────────────


class TestF8MkstempFdLeak:
    """F8: tempfile.mkstemp() 的 fd 應被關閉。"""

    def test_fd_is_closed_after_load_wav(self):
        """load_wav 後 mkstemp 返回的 fd 不應洩漏。"""
        from unittest.mock import MagicMock, patch
        import os

        closed_fds = []
        original_close = os.close

        def tracking_close(fd):
            closed_fds.append(fd)
            original_close(fd)

        from gui.widgets.audio_player import AudioPlayerWidget

        widget = AudioPlayerWidget.__new__(AudioPlayerWidget)
        widget._player = MagicMock()
        widget._play_btn = MagicMock()
        widget._slider = MagicMock()
        widget._temp_file = None

        wav_header = (
            b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
            b"\x01\x00\x01\x00\x80\x3e\x00\x00\x00\x7d\x00\x00"
            b"\x02\x00\x10\x00data\x00\x00\x00\x00"
        )

        with patch("os.close", side_effect=tracking_close) as mock_close:
            with patch("gui.widgets.audio_player.QUrl"):
                widget.load_wav(wav_header)

        assert len(closed_fds) >= 1, "mkstemp fd 應被 os.close 關閉"

        # 清理 temp file
        if widget._temp_file and widget._temp_file.exists():
            widget._temp_file.unlink()
