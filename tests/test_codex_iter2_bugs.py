"""
Codex iter 2 HIGH severity bug fixes -- TDD tests.

Bug A (app/app.py): ASR model changes persisted but never reach the live recognizer.
  - _apply_config() only checks old_config.audio != new_config.audio before restart.
  - When user changes ASR model, new_config.asr differs but new_config.audio is the same.
  - ASRProcess.restart() only stop/starts existing process, never replaces model binding.
  - Fix: detect asr config change and do a full stop + recreate with new model_dir.

Bug B (gui/update_dialog.py): Packaged updater aborts on GitHub 302 redirect.
  - _download_worker() uses redirect=False and treats non-200 as fatal.
  - GitHub releases URLs return HTTP 302, so download fails with "HTTP 302".
  - Fix: follow redirects manually or re-request the Location header.
"""
from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Bug A: ASR model switch not applied to live recognizer ────────


class TestBugAASRModelSwitchNotApplied:
    """When user switches ASR model in settings, _apply_config() must
    detect the asr config change and recreate the ASR process with the
    new model.  Simply calling restart() on the existing process keeps
    the old model loaded.
    """

    def _make_va(self):
        """Build a minimal VoiceApp with mocked internals."""
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)

        old_config = MagicMock()
        old_config.shortcut = MagicMock()
        old_config.shortcut.key = "caps_lock"
        old_config.shortcut.threshold = 0.3
        old_config.shortcut.suppress = False
        old_config.shortcut.repolish_key = ""
        old_config.shortcut.repolish_instant = False
        old_config.llm = MagicMock()
        old_config.output = MagicMock()
        old_config.hotword = MagicMock()
        old_config.audio = MagicMock()

        va._config = old_config
        va._hotkey = None
        va._llm = None
        va._hotword = None
        va._main_window = None

        # ASR process is running
        mock_asr = MagicMock()
        mock_asr.is_running = True
        va._asr_process = mock_asr

        return va, old_config

    def test_asr_model_change_triggers_recreation(self):
        """When asr config differs (model changed), _apply_config must
        detect the change and recreate the ASR process, not just restart.
        Simply calling restart() keeps the old model.

        Bug 6 修復：使用 stop-before-stage 策略，先停止舊進程再創建新的（避免 RAM doubling）。
        """
        from utils.config import ASRConfig

        va, old_config = self._make_va()

        # Build a new_config where ONLY asr differs
        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.audio = old_config.audio  # Same audio
        new_config.asr = ASRConfig(model="sensevoice", language="auto")  # Different model
        old_config.asr = ASRConfig(model="nemo-parakeet", language="auto")

        mock_invoke = MagicMock()
        va._invoke_gui = mock_invoke

        # Save reference to the original mock ASR before _init_asr replaces it
        original_asr_mock = va._asr_process

        # Mock _init_asr to simulate successful ASR creation
        def fake_init_asr():
            new_asr = MagicMock()
            new_asr.is_running = True
            va._asr_process = new_asr

        with patch("app.app.ConfigManager") as MockCfgMgr, \
             patch.object(va, "_init_asr", fake_init_asr):
            va._apply_config(new_config)

            # Bug 6 修復後：舊進程先被停止（在 _init_asr 之前）
            original_asr_mock.stop.assert_called()

    def test_asr_model_unchanged_does_not_recreate(self):
        """When asr config is the same, no ASR recreation should occur."""
        from utils.config import ASRConfig

        va, old_config = self._make_va()

        same_asr = ASRConfig(model="nemo-parakeet", language="auto")
        old_config.asr = same_asr

        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.audio = old_config.audio
        new_config.asr = same_asr  # Identical

        va._invoke_gui = MagicMock()

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

            # Should NOT have stopped/recreated ASR
            va._asr_process.stop.assert_not_called()
            va._asr_process.restart.assert_not_called()

    def test_asr_recreation_failure_rolls_back_config(self):
        """Bug 6 修復：ASR 創建失敗時回滾 config（先停舊進程，再建新的）"""
        from utils.config import ASRConfig

        va, old_config = self._make_va()
        old_asr_process = va._asr_process  # 保存舊進程引用

        old_config.asr = ASRConfig(model="old_model", language="auto")

        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.audio = old_config.audio
        new_config.asr = ASRConfig(model="new_model", language="auto")

        va._invoke_gui = MagicMock()

        # _init_asr will fail (e.g. model files missing)
        with patch("app.app.ConfigManager") as MockCfgMgr, \
             patch.object(va, "_init_asr", side_effect=RuntimeError("model missing")):
            va._apply_config(new_config)

            # Bug 6 修復：失敗時回滾 config
            MockCfgMgr.save.assert_not_called()
            assert va._config is old_config
            # 舊進程應被停止（使用保存的引用，因為 va._asr_process 在異常後變成 None）
            old_asr_process.stop.assert_called_once()

    def test_both_audio_and_asr_change_triggers_recreation(self):
        """When both audio and asr config change, ASR must be recreated.

        Bug 6 修復：使用 stop-before-stage 策略，先停止舊進程再創建新的（避免 RAM doubling）。
        """
        from utils.config import ASRConfig

        va, old_config = self._make_va()

        old_config.asr = ASRConfig(model="old_model", language="auto")

        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.audio = MagicMock()  # Different audio too
        new_config.asr = ASRConfig(model="new_model", language="auto")

        va._invoke_gui = MagicMock()

        # Save reference before _init_asr replaces it
        original_asr_mock = va._asr_process

        # Mock _init_asr to simulate successful ASR creation
        def fake_init_asr():
            new_asr = MagicMock()
            new_asr.is_running = True
            va._asr_process = new_asr

        with patch("app.app.ConfigManager"), \
             patch.object(va, "_init_asr", fake_init_asr):
            va._apply_config(new_config)

            # 修復後（Bug 1）：_init_asr 成功後，舊進程才會被停止
            original_asr_mock.stop.assert_called()


# ─── Bug B: Updater aborts on HTTP 302 redirect ──────────────────


class TestBugBUpdaterHTTP302Redirect:
    """_download_worker() disables redirects and treats any non-200 as
    fatal.  GitHub release URLs return HTTP 302, so the download fails.
    Fix: the worker must follow the redirect Location header and
    download from the resolved URL.
    """

    def test_download_follows_302_redirect(self):
        """When download_url returns 302, _download_worker must follow
        the Location header and download from the redirected URL.
        """
        from gui.update_dialog import UpdateDialog, _DownloadRelay

        # Build a mock 302 response then a 200 response
        redirect_resp = MagicMock()
        redirect_resp.status = 302
        redirect_resp.headers = {"Location": "https://objects.githubusercontent.com/real.zip"}

        final_resp = MagicMock()
        final_resp.status = 200
        final_resp.headers = {"Content-Length": "2048"}  # Bug 1 修復：需要 >= 1024
        # Simulate streaming response
        final_resp.stream = MagicMock(return_value=[b"data"])
        final_resp.release_conn = MagicMock()

        mock_http = MagicMock()
        # First call returns 302, second call returns 200
        mock_http.request.side_effect = [redirect_resp, final_resp]

        relay = MagicMock(spec=_DownloadRelay)
        cancel = MagicMock()
        cancel.is_set.return_value = False

        with patch("gui.update_dialog.urllib3.PoolManager", return_value=mock_http), \
             patch("gui.update_dialog.APP_DATA_DIR", Path("/tmp/_test_update")):
            UpdateDialog._download_worker(
                "https://github.com/repo/releases/download/v1/file.zip",
                relay,
                cancel,
            )

        # Must have emitted finished with success (True)
        relay.finished.emit.assert_called_once()
        call_args = relay.finished.emit.call_args[0]
        assert call_args[0] is True, (
            f"Expected download success, but got: {call_args}"
        )

    def test_download_treats_200_as_success(self):
        """When the initial URL returns 200 (no redirect), download
        should succeed normally without extra requests.
        """
        from gui.update_dialog import UpdateDialog, _DownloadRelay

        ok_resp = MagicMock()
        ok_resp.status = 200
        ok_resp.headers = {"Content-Length": "2048"}  # Bug 1 修復：需要 >= 1024
        ok_resp.stream = MagicMock(return_value=[b"data"])
        ok_resp.release_conn = MagicMock()

        mock_http = MagicMock()
        mock_http.request.return_value = ok_resp

        relay = MagicMock(spec=_DownloadRelay)
        cancel = MagicMock()
        cancel.is_set.return_value = False

        with patch("gui.update_dialog.urllib3.PoolManager", return_value=mock_http), \
             patch("gui.update_dialog.APP_DATA_DIR", Path("/tmp/_test_update")):
            UpdateDialog._download_worker(
                "https://objects.githubusercontent.com/direct.zip",
                relay,
                cancel,
            )

        relay.finished.emit.assert_called_once()
        call_args = relay.finished.emit.call_args[0]
        assert call_args[0] is True

    def test_download_reports_error_on_final_non_200(self):
        """If after following redirects the final response is not 200,
        the download must fail with an error message.
        """
        from gui.update_dialog import UpdateDialog, _DownloadRelay

        redirect_resp = MagicMock()
        redirect_resp.status = 302
        redirect_resp.headers = {"Location": "https://objects.githubusercontent.com/real.zip"}

        error_resp = MagicMock()
        error_resp.status = 403

        mock_http = MagicMock()
        mock_http.request.side_effect = [redirect_resp, error_resp]

        relay = MagicMock(spec=_DownloadRelay)
        cancel = MagicMock()
        cancel.is_set.return_value = False

        with patch("gui.update_dialog.urllib3.PoolManager", return_value=mock_http), \
             patch("gui.update_dialog.APP_DATA_DIR", Path("/tmp/_test_update")):
            UpdateDialog._download_worker(
                "https://github.com/repo/releases/download/v1/file.zip",
                relay,
                cancel,
            )

        relay.finished.emit.assert_called_once()
        call_args = relay.finished.emit.call_args[0]
        assert call_args[0] is False
        assert "403" in call_args[1]

    def test_download_redirect_limit_prevents_infinite_loop(self):
        """If the server keeps redirecting, there must be a max redirect
        limit to prevent infinite loops.
        """
        from gui.update_dialog import UpdateDialog, _DownloadRelay

        # Infinite 302 loop
        loop_resp = MagicMock()
        loop_resp.status = 302
        loop_resp.headers = {"Location": "https://github.com/loop"}

        mock_http = MagicMock()
        mock_http.request.return_value = loop_resp

        relay = MagicMock(spec=_DownloadRelay)
        cancel = MagicMock()
        cancel.is_set.return_value = False

        with patch("gui.update_dialog.urllib3.PoolManager", return_value=mock_http), \
             patch("gui.update_dialog.APP_DATA_DIR", Path("/tmp/_test_update")):
            UpdateDialog._download_worker(
                "https://github.com/repo/releases/download/v1/file.zip",
                relay,
                cancel,
            )

        relay.finished.emit.assert_called_once()
        call_args = relay.finished.emit.call_args[0]
        assert call_args[0] is False, (
            f"Expected download to fail due to redirect loop, but succeeded"
        )
