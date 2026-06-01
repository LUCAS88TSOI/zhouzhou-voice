"""
Codex-discovered HIGH severity bug fixes — TDD tests.

Bug 1 — SettingsPanel._sync_provider_fields() calls nonexistent _update_builtin_lock()
Bug 2 — HotwordManager.reload() calls nonexistent _load_hotword() (typo: should be _load_hotwords)
Bug 3 — _apply_config() persists config before ASR restart, leaving broken state on failure
Bug 4 — merge_segment_tokens() drops non-overlapping text when tokens are multi-character
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Bug 1: _update_builtin_lock() AttributeError ────────────


class TestBug1UpdateBuiltinLock:
    """SettingsPanel._sync_provider_fields() calls self._update_builtin_lock()
    which does not exist.  Opening settings or switching providers raises
    AttributeError.
    """

    def test_sync_provider_fields_does_not_call_nonexistent_method(self):
        """_sync_provider_fields must not call _update_builtin_lock().
        The method was removed but the call site was left behind.
        After fix, the method body must not contain '_update_builtin_lock'.
        """
        import inspect
        from gui.settings_panel import SettingsPanel

        source = inspect.getsource(SettingsPanel._sync_provider_fields)
        assert "_update_builtin_lock" not in source, (
            "Bug 1 still present: _sync_provider_fields() still calls "
            "nonexistent _update_builtin_lock()"
        )

    def test_no_update_builtin_lock_method_exists(self):
        """Verify that _update_builtin_lock is not a method on SettingsPanel.
        If the method was never meant to exist, it should not be defined.
        """
        from gui.settings_panel import SettingsPanel

        assert not hasattr(SettingsPanel, "_update_builtin_lock"), (
            "Bug 1: _update_builtin_lock should not exist as a method"
        )


# ─── Bug 2: _load_hotword() typo ─────────────────────────────


class TestBug2HotwordReloadTypo:
    """HotwordManager.reload() calls _load_hotword() but the method
    is _load_hotwords() (plural).  First config save changing hotword
    settings triggers AttributeError.
    """

    def test_reload_calls_correct_method(self):
        """reload(new_config) must call _load_hotwords() (plural), not
        _load_hotword() (singular).
        """
        from hotword.manager import HotwordManager
        from hotword.manager import HotwordConfig

        mgr = object.__new__(HotwordManager)

        # Minimal stubs so reload() can execute
        mgr._config = HotwordConfig()
        mgr._hotword_path = Path(os.devnull)
        mgr._rule_path = Path(os.devnull)
        mgr._rectify_path = Path(os.devnull)
        mgr._phoneme_index = MagicMock()
        mgr._rule_engine = MagicMock()
        mgr._rectify_store = MagicMock()
        mgr._hotword_mtime = 0.0
        mgr._rule_mtime = 0.0
        mgr._rectify_mtime = 0.0

        new_cfg = HotwordConfig(threshold=0.8)

        try:
            mgr.reload(new_cfg)
        except AttributeError as e:
            if "_load_hotword" in str(e):
                pytest.fail(
                    f"Bug 2 still present: reload() calls _load_hotword "
                    f"instead of _load_hotwords: {e}"
                )
            raise


# ─── Bug 3: ASR config committed before restart ──────────────


class TestBug3ASRConfigRollback:
    """_apply_config() attempts ASR restart BEFORE committing config.
    If restart fails, the method returns gracefully without changing
    any state (no raise, no config mutation, no subsystem updates).
    This prevents partial state inconsistency and avoids crashing
    Qt signal handlers that silently swallow exceptions.
    """

    def test_apply_config_no_mutation_on_asr_restart_failure(self):
        """When ASR restart fails, _config must remain old_config
        and the broken config must NOT be persisted.
        The method must NOT raise (Qt signal handler swallows exceptions).
        """
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
        old_config.asr = MagicMock(name="shared_asr")
        old_config.audio = MagicMock()

        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.asr = old_config.asr  # asr unchanged -> audio-only branch
        # Only audio differs -> triggers restart
        new_config.audio = MagicMock()

        va._config = old_config
        va._hotkey = None
        va._llm = None
        va._hotword = None

        # Mock ASR process that fails on restart
        mock_asr = MagicMock()
        mock_asr.is_running = True
        mock_asr.restart.side_effect = RuntimeError("Model files missing")
        va._asr_process = mock_asr

        mock_invoke = MagicMock()
        va._invoke_gui = mock_invoke

        with patch("app.app.ConfigManager") as MockCfgMgr:
            # _apply_config must NOT raise (graceful return instead)
            va._apply_config(new_config)

            # Config must remain old_config (never mutated)
            assert va._config is old_config, (
                "Bug 3: _config was mutated despite ASR restart failure"
            )

            # ConfigManager.save must NOT have been called at all
            # (since we never committed the new config)
            MockCfgMgr.save.assert_not_called()


# ─── Bug 4: Segment merge drops non-overlapping text ─────────


class TestBug4SegmentMergeMulticharToken:
    """merge_segment_tokens() computes max_overlap in characters but
    advances curr_start in whole-token steps.  When the overlap boundary
    falls inside a multi-character token, the entire token is skipped,
    silently losing content at segment boundaries.
    """

    def test_multichar_token_not_dropped(self):
        """When overlap boundary falls mid-token, the non-overlapping
        portion of that token must still appear in merged output.

        Scenario:
          prev tokens: ["你好", "世界", "今天"]
          curr tokens: ["今天", "天氣", "不錯"]
          Overlap is "今天" (2 chars).
          Expected merged: "你好世界今天天氣不錯"
          Buggy merged (drops "今天"): "你好世界天氣不錯"
        """
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        prev = SegmentResult(
            offset=0.0,
            text="你好世界今天",
            tokens=["你好", "世界", "今天"],
            timestamps=[0.0, 0.5, 1.0],
            duration=2.0,
        )
        # curr starts at 1.0, overlap = 0.5s
        # "今天" falls in the overlap zone
        curr = SegmentResult(
            offset=1.0,
            text="今天天氣不錯",
            tokens=["今天", "天氣", "不錯"],
            timestamps=[0.0, 0.5, 1.0],
            duration=2.0,
        )

        merged_text, tokens, ts = merge_segment_tokens([prev, curr], overlap=0.5)

        assert "今天" in merged_text, (
            f"Bug 4: merged text lost '今天': got '{merged_text}'"
        )
        assert "天氣" in merged_text, (
            f"Bug 4: merged text lost '天氣': got '{merged_text}'"
        )
        assert "不錯" in merged_text, (
            f"Bug 4: merged text lost '不錯': got '{merged_text}'"
        )

    def test_exact_overlap_multichar_preserves_all_content(self):
        """Exact full-token overlap: entire first token of curr overlaps
        with last token of prev.  Must not double-count or drop.
        """
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        prev = SegmentResult(
            offset=0.0,
            text="ABCD",
            tokens=["AB", "CD"],
            timestamps=[0.0, 0.5],
            duration=1.0,
        )
        curr = SegmentResult(
            offset=0.5,
            text="CDEF",
            tokens=["CD", "EF"],
            timestamps=[0.0, 0.5],
            duration=1.0,
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.5)

        # "CD" appears once (overlap), "EF" appended
        assert merged_text == "ABCDEF", (
            f"Expected 'ABCDEF', got '{merged_text}'"
        )

    def test_partial_multichar_overlap_preserves_suffix(self):
        """When overlap is a suffix of prev's last token but only a prefix
        of curr's first token, the merged result must keep all unique content.

        Example:
          prev: ["ABC"]  -> "ABC"
          curr: ["BCD"]  -> "BCD"
          Overlap in characters: "BC" (2 chars)
          Expected: "ABC" + "D" = "ABCD"

        The bug: token "BCD" is skipped entirely because its cumulative
        length (3) >= max_overlap (2), causing curr_start = 1, skipping it.
        After fix: only the overlap portion "BC" is removed, "D" is kept.
        """
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        prev = SegmentResult(
            offset=0.0,
            text="ABC",
            tokens=["ABC"],
            timestamps=[0.0],
            duration=0.5,
        )
        curr = SegmentResult(
            offset=0.0,
            text="BCD",
            tokens=["BCD"],
            timestamps=[0.0],
            duration=0.5,
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.5)

        assert "D" in merged_text, (
            f"Bug 4: suffix 'D' of partially-overlapping token was dropped. "
            f"Got '{merged_text}'"
        )

    def test_no_overlap_preserves_all_tokens(self):
        """When there is no overlap at all, every token must appear."""
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        prev = SegmentResult(
            offset=0.0,
            text="Hello",
            tokens=["Hello"],
            timestamps=[0.0],
            duration=1.0,
        )
        curr = SegmentResult(
            offset=1.0,
            text="World",
            tokens=["World"],
            timestamps=[0.0],
            duration=1.0,
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.0)

        assert merged_text == "HelloWorld", (
            f"Expected 'HelloWorld', got '{merged_text}'"
        )

    def test_single_char_tokens_no_data_loss(self):
        """Single-character tokens must all be preserved even with overlap."""
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        prev = SegmentResult(
            offset=0.0,
            text="ABCDE",
            tokens=["A", "B", "C", "D", "E"],
            timestamps=[0.0, 0.1, 0.2, 0.3, 0.4],
            duration=0.5,
        )
        curr = SegmentResult(
            offset=0.2,
            text="CDEFG",
            tokens=["C", "D", "E", "F", "G"],
            timestamps=[0.0, 0.1, 0.2, 0.3, 0.4],
            duration=0.5,
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.3)

        assert merged_text == "ABCDEFG", (
            f"Expected 'ABCDEFG', got '{merged_text}'"
        )

    def test_empty_segments(self):
        """Empty segment list returns empty result."""
        from transcribe.file_transcriber import merge_segment_tokens

        merged_text, tokens, ts = merge_segment_tokens([], overlap=0.5)
        assert merged_text == ""
        assert tokens == []
        assert ts == []

    def test_single_segment(self):
        """Single segment returns its own data with global timestamps."""
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        seg = SegmentResult(
            offset=1.5,
            text="Hello",
            tokens=["Hello"],
            timestamps=[0.0],
            duration=1.0,
        )

        merged_text, tokens, ts = merge_segment_tokens([seg], overlap=0.5)

        assert merged_text == "Hello"
        assert ts == [1.5]  # offset + local timestamp

    def test_three_segments_with_partial_overlaps(self):
        """Three segments with partial overlaps must chain correctly."""
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        s1 = SegmentResult(
            offset=0.0,
            text="ABCDE",
            tokens=["AB", "CDE"],
            timestamps=[0.0, 0.5],
            duration=1.0,
        )
        s2 = SegmentResult(
            offset=0.5,
            text="DEFGH",
            tokens=["DE", "FGH"],
            timestamps=[0.0, 0.5],
            duration=1.0,
        )
        s3 = SegmentResult(
            offset=1.0,
            text="GHIJK",
            tokens=["GH", "IJK"],
            timestamps=[0.0, 0.5],
            duration=1.0,
        )

        merged_text, _, _ = merge_segment_tokens([s1, s2, s3], overlap=0.5)

        # All unique content must appear
        for expected in ["ABCDE", "FGH", "IJK"]:
            assert expected[:2] in merged_text or expected in merged_text, (
                f"Expected content from '{expected}' in merged: '{merged_text}'"
            )

    def test_curr_empty_tokens_no_crash(self):
        """A segment with empty tokens must not crash the merge."""
        from transcribe.file_transcriber import SegmentResult, merge_segment_tokens

        prev = SegmentResult(
            offset=0.0,
            text="Hello",
            tokens=["Hello"],
            timestamps=[0.0],
            duration=1.0,
        )
        curr = SegmentResult(
            offset=0.5,
            text="",
            tokens=[],
            timestamps=[],
            duration=0.5,
        )

        merged_text, _, _ = merge_segment_tokens([prev, curr], overlap=0.5)

        assert "Hello" in merged_text


# ─── Bug 3 Edge Cases ─────────────────────────────────────────


class TestBug3ASRConfigRollbackEdgeCases:
    """Edge cases for ASR config rollback behavior."""

    def test_apply_config_succeeds_normally_when_no_restart_needed(self):
        """When audio config hasn't changed, no restart is attempted
        and the new config is persisted successfully.
        """
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        VoiceApp.__init__(va)

        shared_audio = MagicMock()
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
        old_config.asr = MagicMock(name="shared_asr")
        old_config.audio = shared_audio

        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.asr = old_config.asr  # Same asr -> no recreate
        new_config.audio = shared_audio  # Same audio -> no restart

        va._config = old_config
        va._hotkey = None
        va._llm = None
        va._hotword = None
        va._asr_process = MagicMock()

        with patch("app.app.ConfigManager") as MockCfgMgr:
            va._apply_config(new_config)

            # Config must be new_config (no rollback needed)
            assert va._config is new_config
            # restart must NOT have been called
            va._asr_process.restart.assert_not_called()

    def test_apply_config_no_restart_when_asr_not_running(self):
        """When ASR process is not running, restart is skipped."""
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
        old_config.asr = MagicMock(name="shared_asr")
        old_config.audio = MagicMock()

        new_config = MagicMock()
        new_config.shortcut = old_config.shortcut
        new_config.llm = old_config.llm
        new_config.output = old_config.output
        new_config.hotword = old_config.hotword
        new_config.asr = old_config.asr  # Same asr -> audio-only branch
        new_config.audio = MagicMock()  # Different -> would trigger restart

        va._config = old_config
        va._hotkey = None
        va._llm = None
        va._hotword = None

        # ASR not running
        mock_asr = MagicMock()
        mock_asr.is_running = False
        va._asr_process = mock_asr

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

            mock_asr.restart.assert_not_called()
            assert va._config is new_config

