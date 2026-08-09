"""VoiceApp._run_polish_selection() worker 測試。

涵蓋：鎖互斥、無選取提示、未配置 LLM、成功貼上、LLM 潤色失敗提示、
貼上失敗提示、以及 _on_polish_selection_activate() 錄音中忽略觸發。
"""
from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch


def _valid_providers():
    from utils.config import DEFAULT_PROVIDERS

    providers = {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()}
    providers["bigmodel"] = {**providers["bigmodel"], "api_key": "test-key-1234567890"}
    return providers


def _make_app(llm_config=None, output_config=None):
    from app.app import VoiceApp
    from utils.config import AppConfig, LLMConfig

    va = object.__new__(VoiceApp)
    cfg = AppConfig()
    llm = llm_config or LLMConfig(
        enabled=True, active_provider="bigmodel", providers=_valid_providers(),
    )
    va._config = replace(cfg, llm=llm, output=output_config or cfg.output)
    va._llm = None
    va._processing_lock = threading.Lock()
    # R7：鎖只守這兩個布林，I/O 一律在鎖外進行
    va._is_processing = False
    va._is_repolishing = False
    va._target_hwnd = 0          # U9：貼上前的目標視窗比對（0 = 不檢查）
    va._hotword = None
    va._recorder = None
    return va


class TestPolishSelectionActivateGuard:
    def test_ignored_while_recording(self):
        va = _make_app()
        va._recorder = MagicMock(is_recording=True)

        with patch.object(va, "_spawn_worker") as mock_spawn:
            va._on_polish_selection_activate()

        mock_spawn.assert_not_called()

    def test_spawns_worker_when_idle(self):
        va = _make_app()

        with patch.object(va, "_spawn_worker") as mock_spawn:
            va._on_polish_selection_activate()

        mock_spawn.assert_called_once()
        assert mock_spawn.call_args.kwargs["name"] == "polish-selection-worker"


class TestRunPolishSelection:
    def test_no_selection_shows_tray_warning_and_skips_llm(self):
        va = _make_app()

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch("utils.clipboard.ClipboardManager.capture_selection", return_value=None), \
                patch.object(va, "_build_repolish_processor") as mock_build:
            va._run_polish_selection()

        mock_build.assert_not_called()
        warnings = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "notify_warning"]
        assert any("未偵測到選取文字" in w for w in warnings)
        # 未偵測到選取文字唔應該改動狀態列（唔應該彈綠色「完成」指示燈）
        assert not any(c.args[0] == "set_status" for c in mock_gui.call_args_list)

    def test_locked_when_already_processing(self):
        # R7：忙碌狀態改由 _is_processing 旗標表示，而非長時間持鎖
        # （持鎖跨 LLM I/O 會把 pynput 鉤子線程卡住數十秒）
        va = _make_app()
        va._is_processing = True
        with patch("utils.clipboard.ClipboardManager.capture_selection") as mock_capture:
            va._run_polish_selection()
        mock_capture.assert_not_called()

    def test_skipped_when_repolish_in_flight(self):
        va = _make_app()
        va._is_repolishing = True
        with patch("utils.clipboard.ClipboardManager.capture_selection") as mock_capture:
            va._run_polish_selection()
        mock_capture.assert_not_called()

    def test_unconfigured_llm_sets_status(self):
        va = _make_app()

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch("utils.clipboard.ClipboardManager.capture_selection", return_value="選取的文字"), \
                patch.object(va, "_build_repolish_processor", return_value=(None, "")):
            va._run_polish_selection()

        statuses = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "set_status"]
        assert "未配置 LLM" in statuses

    def test_success_pastes_polished_text(self):
        va = _make_app()
        mock_processor = MagicMock()

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch("utils.clipboard.ClipboardManager.capture_selection", return_value="原文"), \
                patch.object(va, "_build_repolish_processor", return_value=(mock_processor, "")), \
                patch.object(va, "_try_llm_polish") as mock_polish, \
                patch("utils.clipboard.ClipboardManager.paste_text", return_value=True) as mock_paste:
            from llm.processor import LLMResultStatus
            mock_polish.return_value = LLMResultStatus(
                success=True, text="潤色後文字", was_processed=True, error="",
            )
            va._run_polish_selection()

        mock_polish.assert_called_once()
        assert mock_polish.call_args.args[0] == "原文"
        mock_paste.assert_called_once_with(
            "潤色後文字",
            restore=va._config.output.restore_clip,
            expect_hwnd=va._target_hwnd,
        )
        statuses = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "set_status"]
        assert statuses[-1] == "完成"

    def test_llm_failure_shows_warning(self):
        va = _make_app()
        mock_processor = MagicMock()

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch("utils.clipboard.ClipboardManager.capture_selection", return_value="原文"), \
                patch.object(va, "_build_repolish_processor", return_value=(mock_processor, "")), \
                patch.object(va, "_try_llm_polish") as mock_polish, \
                patch("utils.clipboard.ClipboardManager.paste_text", return_value=True) as mock_paste:
            from llm.processor import LLMResultStatus
            mock_polish.return_value = LLMResultStatus(
                success=False, text="原文", was_processed=False, error="連線逾時",
            )
            va._run_polish_selection()

        warnings = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "notify_warning"]
        assert any("潤色選取文字失敗" in w for w in warnings)
        # LLM 失敗仍會貼返原文（result.text 喺失敗時等於選取原文），唔係靜默中斷
        mock_paste.assert_called_once_with(
            "原文",
            restore=va._config.output.restore_clip,
            expect_hwnd=va._target_hwnd,
        )
        # 記錄現行行為：LLM 失敗仍以「完成」收尾（狀態列語意屬既有 code review 記錄，非本測試範圍）
        statuses = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "set_status"]
        assert statuses[-1] == "完成"

    def test_paste_failure_shows_warning(self):
        va = _make_app()
        mock_processor = MagicMock()

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch("utils.clipboard.ClipboardManager.capture_selection", return_value="原文"), \
                patch.object(va, "_build_repolish_processor", return_value=(mock_processor, "")), \
                patch.object(va, "_try_llm_polish") as mock_polish, \
                patch("utils.clipboard.ClipboardManager.paste_text", return_value=False):
            from llm.processor import LLMResultStatus
            mock_polish.return_value = LLMResultStatus(
                success=True, text="潤色後文字", was_processed=True, error="",
            )
            va._run_polish_selection()

        warnings = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "notify_warning"]
        assert any("未能自動貼上" in w for w in warnings)

    def test_lock_released_after_run(self):
        va = _make_app()

        with patch.object(va, "_invoke_gui"), \
                patch("utils.clipboard.ClipboardManager.capture_selection", return_value=None):
            va._run_polish_selection()

        assert va._processing_lock.acquire(blocking=False)
        va._processing_lock.release()
        assert va._is_repolishing is False

    def test_exception_sets_failed_status_and_releases_lock(self):
        va = _make_app()

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch(
                    "utils.clipboard.ClipboardManager.capture_selection",
                    side_effect=RuntimeError("剪貼板讀取異常"),
                ):
            va._run_polish_selection()

        statuses = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "set_status"]
        assert statuses[-1] == "失敗"
        # finally 必須釋放鎖，否則後續錄音/重新潤色會被永久卡死
        assert va._processing_lock.acquire(blocking=False)
        va._processing_lock.release()
        assert va._is_repolishing is False
