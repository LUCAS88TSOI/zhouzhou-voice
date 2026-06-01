"""
CRITICAL: _apply_config ASR restart rollback leaves subsystems inconsistent.

Bug: When ASR restart fails, _apply_config does:
  1. self._config = new_config   (line 1395)
  2. Updates text_processor with new_config.output   (line 1437-1438)
  3. HotwordManager.reload with new_config.hotword   (line 1443-1444)
  4. LLM update_config with new_config.llm           (line 1428)
  5. Then ASR restart fails                           (line 1454)
  6. Rolls back self._config = old_config             (line 1458)
  7. Rolls back ConfigManager.save(old_config)        (line 1459)
  8. raise                                            (line 1461)

After step 6-8: self._config is old_config, but text_processor, hotword,
and LLM are all configured with new_config. The raise also propagates to
the Qt signal handler which silently swallows it.

Fix: Reorder _apply_config so ASR restart (irreversible) happens FIRST.
Only commit config + update other subsystems after ASR restart succeeds.
This way, on failure, nothing needs to be rolled back because nothing
was changed yet.

Tests:
  T1 - ASR restart failure does NOT mutate text_processor
  T2 - ASR restart failure does NOT mutate hotword manager
  T3 - ASR restart failure does NOT mutate LLM processor
  T4 - ASR restart failure does NOT persist broken config to disk
  T5 - ASR restart failure does NOT raise (no crash in signal handler)
  T6 - ASR restart failure shows error status in GUI
  T7 - ASR restart success applies ALL subsystem updates
  T8 - Non-ASR config changes still applied when audio unchanged
  T9 - ASR not running: config applied without restart attempt
  T10 - Mixed change: audio + output change, restart fails => output NOT changed
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_va_with_subsystems():
    """Create a VoiceApp with mocked subsystems for rollback testing."""
    from app.app import VoiceApp

    va = object.__new__(VoiceApp)
    VoiceApp.__init__(va)

    # Create old and new configs that differ in audio, output, hotword, and llm
    old_config = MagicMock()
    old_config.shortcut = MagicMock()
    old_config.shortcut.key = "caps_lock"
    old_config.shortcut.threshold = 0.3
    old_config.shortcut.suppress = False
    old_config.shortcut.repolish_key = ""
    old_config.shortcut.repolish_instant = False
    shared_asr = MagicMock(name="shared_asr")
    old_config.asr = shared_asr
    old_config.audio = MagicMock(name="old_audio")
    old_config.output = MagicMock(name="old_output")
    old_config.hotword = MagicMock(name="old_hotword")
    old_config.llm = MagicMock(name="old_llm")
    old_config.llm.enabled = False
    old_config.llm.active_role = "default"
    old_config.llm.custom_roles = []
    old_config.llm.builtin_overrides = {}

    new_config = MagicMock()
    new_config.shortcut = old_config.shortcut  # shortcut unchanged
    new_config.asr = shared_asr  # asr unchanged -> goes to audio-only branch
    new_config.audio = MagicMock(name="new_audio")   # audio CHANGED
    new_config.output = MagicMock(name="new_output")  # output CHANGED
    new_config.hotword = MagicMock(name="new_hotword")  # hotword CHANGED
    new_config.llm = MagicMock(name="new_llm")  # llm CHANGED
    new_config.llm.enabled = False
    new_config.llm.active_role = "default"
    new_config.llm.custom_roles = []
    new_config.llm.builtin_overrides = {}

    va._config = old_config

    # Mock subsystems
    mock_hotword = MagicMock()
    mock_llm = MagicMock()
    mock_asr = MagicMock()
    mock_asr.is_running = True
    mock_invoke = MagicMock()

    va._hotword = mock_hotword
    va._llm = mock_llm
    va._asr_process = mock_asr
    va._invoke_gui = mock_invoke
    va._hotkey = None
    va._text_processor = MagicMock(name="old_text_processor")

    return va, old_config, new_config, mock_hotword, mock_llm, mock_asr, mock_invoke


# ─── T1: ASR restart failure does NOT mutate text_processor ─────────


class TestT1TextProcessorNotMutatedOnASRFailure:
    """When ASR restart fails, text_processor must remain unchanged."""

    def test_text_processor_not_replaced(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("model missing")

        original_tp = va._text_processor

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        assert va._text_processor is original_tp, (
            "text_processor was replaced despite ASR restart failure"
        )


# ─── T2: ASR restart failure does NOT mutate hotword manager ────────


class TestT2HotwordNotMutatedOnASRFailure:
    """When ASR restart fails, hotword manager must not be reloaded."""

    def test_hotword_not_reloaded(self):
        va, old_config, new_config, mock_hotword, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("model missing")

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        mock_hotword.reload.assert_not_called(), (
            "hotword.reload was called despite ASR restart failure"
        )


# ─── T3: ASR restart failure does NOT mutate LLM processor ─────────


class TestT3LLMNotMutatedOnASRFailure:
    """When ASR restart fails, LLM processor must not be updated."""

    def test_llm_not_updated(self):
        va, old_config, new_config, _, mock_llm, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("model missing")

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        mock_llm.update_config.assert_not_called(), (
            "llm.update_config was called despite ASR restart failure"
        )


# ─── T4: ASR restart failure does NOT persist broken config ─────────


class TestT4BrokenConfigNotPersisted:
    """When ASR restart fails, new_config must NOT be saved to disk."""

    def test_new_config_not_saved(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("model missing")

        with patch("app.app.ConfigManager") as MockCfgMgr:
            va._apply_config(new_config)

            # ConfigManager.save should only be called with old_config (rollback)
            # or not called at all if we reorder correctly
            for call_args in MockCfgMgr.save.call_args_list:
                saved_cfg = call_args[0][0]
                assert saved_cfg is not new_config, (
                    "new_config was persisted despite ASR restart failure"
                )


# ─── T5: ASR restart failure does NOT raise ────────────────────────


class TestT5NoRaiseOnASRFailure:
    """When ASR restart fails, _apply_config must NOT raise.

    Raising would crash the voice-worker thread or be silently swallowed
    by Qt signal handlers, leaving GUI in an inconsistent state.
    The method should handle the error gracefully instead.
    """

    def test_no_exception_raised(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("model missing")

        with patch("app.app.ConfigManager"):
            # Must NOT raise
            va._apply_config(new_config)


# ─── T6: ASR restart failure shows error status in GUI ──────────────


class TestT6ErrorStatusOnASRFailure:
    """When ASR restart fails, GUI must show error status."""

    def test_error_status_shown(self):
        va, old_config, new_config, *_, mock_invoke = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("model missing")

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        # Should show "ASR 重啟失敗" or similar error status
        status_calls = [
            c for c in mock_invoke.call_args_list
            if len(c[0]) >= 2 and c[0][0] == "set_status"
        ]
        error_shown = any(
            "失敗" in str(c[0][1]) or "重啟" in str(c[0][1])
            for c in status_calls
        )
        assert error_shown, (
            f"GUI should show error status on ASR restart failure, "
            f"got calls: {status_calls}"
        )


# ─── T7: ASR restart success applies ALL subsystem updates ──────────


class TestT7SuccessPathAppliesAll:
    """When ASR restart succeeds, all subsystem updates must be applied."""

    def test_all_subsystems_updated_on_success(self):
        va, old_config, new_config, mock_hotword, mock_llm, mock_asr, _ = _make_va_with_subsystems()
        # ASR restart succeeds (default)

        with patch("app.app.ConfigManager") as MockCfgMgr:
            with patch("core.text_processor.TextProcessor") as MockTP:
                mock_tp_instance = MagicMock()
                MockTP.return_value = mock_tp_instance

                va._apply_config(new_config)

                # Config committed
                assert va._config is new_config
                # Text processor updated
                assert va._text_processor is mock_tp_instance
                # Hotword reloaded
                mock_hotword.reload.assert_called_once_with(new_config.hotword)
                # LLM updated
                mock_llm.update_config.assert_called_once_with(new_config.llm)
                # Config saved
                MockCfgMgr.save.assert_called_with(new_config)


# ─── T8: Non-ASR config changes applied when audio unchanged ────────


class TestT8NonAudioChangesStillApplied:
    """When audio config is the same, other changes must still be applied."""

    def test_output_and_hotword_updated_without_asr_restart(self):
        va, old_config, new_config, mock_hotword, mock_llm, mock_asr, _ = _make_va_with_subsystems()
        # Same audio config -> no ASR restart
        new_config.audio = old_config.audio

        with patch("app.app.ConfigManager"):
            with patch("core.text_processor.TextProcessor") as MockTP:
                MockTP.return_value = MagicMock()

                va._apply_config(new_config)

                mock_asr.restart.assert_not_called()
                assert va._config is new_config
                mock_hotword.reload.assert_called_once()
                mock_llm.update_config.assert_called_once()


# ─── T9: ASR not running: config applied without restart attempt ────


class TestT9ASRNotRunningNoRestart:
    """When ASR process exists but is not running, skip restart."""

    def test_no_restart_when_not_running(self):
        va, old_config, new_config, mock_hotword, mock_llm, mock_asr, _ = _make_va_with_subsystems()
        mock_asr.is_running = False

        with patch("app.app.ConfigManager"):
            with patch("core.text_processor.TextProcessor") as MockTP:
                MockTP.return_value = MagicMock()

                va._apply_config(new_config)

                mock_asr.restart.assert_not_called()
                assert va._config is new_config


# ─── T10: Mixed change, restart fails => output NOT changed ─────────


class TestT10MixedChangeRollback:
    """When both audio and output change, ASR restart failure must
    prevent output change from being applied."""

    def test_output_not_updated_when_asr_fails(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("crash")
        original_tp = va._text_processor

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        # text_processor must NOT have been replaced
        assert va._text_processor is original_tp
        # config must be old_config
        assert va._config is old_config


# ─── T11: Config is consistent after rollback ───────────────────────


class TestT11ConfigConsistentAfterRollback:
    """After ASR restart failure, self._config must equal old_config,
    and no subsystem should hold a reference to new_config."""

    def test_config_rolled_back(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("crash")

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        assert va._config is old_config, (
            "self._config must be old_config after rollback"
        )

    def test_only_old_config_saved(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._asr_process.restart.side_effect = RuntimeError("crash")

        with patch("app.app.ConfigManager") as MockCfgMgr:
            va._apply_config(new_config)

            # The last save must be old_config (rollback)
            if MockCfgMgr.save.call_count > 0:
                last_saved = MockCfgMgr.save.call_args_list[-1][0][0]
                assert last_saved is old_config, (
                    "Last persisted config must be old_config"
                )
