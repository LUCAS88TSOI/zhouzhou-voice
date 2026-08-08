"""
CRITICAL: _apply_config 不可逆操作 rollback 必須保持子系統一致。

Bug: 當 ASR 重建失敗時，_apply_config 若順序錯誤會：
  1. 先 commit self._config = new_config
  2. 更新 text_processor / hotword / llm 等子系統
  3. 之後才發現 ASR 重建失敗，被迫逐一 rollback
這樣會有短暫的不一致窗口，且 rollback 邏輯容易漏。

Fix: 把不可逆操作（ASR 重建）放最前面，只有成功後才 commit config
並更新其他子系統。失敗時直接 return，不需要 rollback 任何東西，
因為根本未曾改變。

v3.9.3 遷移說明：本檔案原本用 `audio_changed`（AudioConfig 任一欄位改變）
觸發上述 rollback 安全測試。已確認 AudioConfig 四個欄位對 ASR 子進程完全
無影響，`_apply_config()` 已移除 audio_changed 觸發 restart 的分支，故本檔案
全部改用僅存的不可逆操作 `asr_changed`（ASR 模型切換，經
`_apply_config_recreate_asr()`）重新觸發同一組安全性斷言，測試語意不變。

Tests:
  T1 - ASR 重建失敗不 mutate text_processor
  T2 - ASR 重建失敗不 mutate hotword manager
  T3 - ASR 重建失敗不 mutate LLM processor
  T4 - ASR 重建失敗不把壞 config 存檔
  T5 - ASR 重建失敗不 raise（signal handler 唔會 crash）
  T6 - ASR 重建失敗喺 GUI 顯示錯誤狀態
  T7 - ASR 重建成功套用全部子系統更新
  T8 - asr 未變更時其他 config 改動仍套用，且唔會嘗試重建 ASR
  T9 - 已退役（見下方說明），is_running 閘門覆蓋範圍已併入
       test_asr_recreate_rollback.py
  T10 - 混合變更：asr + output 都變，重建失敗 => output 唔變
  T11 - rollback 後 config 保持一致
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

    old_config = MagicMock()
    old_config.shortcut = MagicMock()
    old_config.shortcut.key = "caps_lock"
    old_config.shortcut.threshold = 0.3
    old_config.shortcut.suppress = False
    old_config.shortcut.repolish_key = ""
    old_config.shortcut.repolish_instant = False
    old_config.asr = MagicMock(name="old_asr")
    shared_audio = MagicMock(name="shared_audio")
    old_config.audio = shared_audio
    old_config.output = MagicMock(name="old_output")
    old_config.hotword = MagicMock(name="old_hotword")
    old_config.llm = MagicMock(name="old_llm")
    old_config.llm.enabled = False
    old_config.llm.active_role = "default"
    old_config.llm.custom_roles = []
    old_config.llm.builtin_overrides = {}

    new_config = MagicMock()
    new_config.shortcut = old_config.shortcut  # shortcut unchanged
    new_config.asr = MagicMock(name="new_asr")  # asr CHANGED -> triggers recreate
    new_config.audio = shared_audio  # audio unchanged（現已與 restart 無關）
    new_config.output = MagicMock(name="new_output")  # output CHANGED
    new_config.hotword = MagicMock(name="new_hotword")  # hotword CHANGED
    new_config.llm = MagicMock(name="new_llm")  # llm CHANGED
    new_config.llm.enabled = False
    new_config.llm.active_role = "default"
    new_config.llm.custom_roles = []
    new_config.llm.builtin_overrides = {}

    va._config = old_config

    mock_hotword = MagicMock()
    mock_llm = MagicMock()
    mock_asr = MagicMock(name="old_asr_process")
    mock_asr.is_running = True
    mock_invoke = MagicMock()

    va._hotword = mock_hotword
    va._llm = mock_llm
    va._asr_process = mock_asr
    va._invoke_gui = mock_invoke
    va._hotkey = None
    va._text_processor = MagicMock(name="old_text_processor")
    va._lifecycle = MagicMock(name="lifecycle")

    # 預設「成功」的 _init_asr：建立新的、is_running=True 的子進程。
    # 失敗情境的測試會覆寫成 side_effect=Exception。
    def _default_init_asr():
        va._asr_process = MagicMock(name="new_asr_process")
        va._asr_process.is_running = True

    va._init_asr = MagicMock(side_effect=_default_init_asr)

    return va, old_config, new_config, mock_hotword, mock_llm, mock_asr, mock_invoke


# ─── T1: ASR 重建失敗不 mutate text_processor ────────────────────


class TestT1TextProcessorNotMutatedOnASRFailure:
    """When ASR recreate fails, text_processor must remain unchanged."""

    def test_text_processor_not_replaced(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("model missing"))

        original_tp = va._text_processor

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        assert va._text_processor is original_tp, (
            "text_processor was replaced despite ASR recreate failure"
        )


# ─── T2: ASR 重建失敗不 mutate hotword manager ───────────────────


class TestT2HotwordNotMutatedOnASRFailure:
    """When ASR recreate fails, hotword manager must not be reloaded."""

    def test_hotword_not_reloaded(self):
        va, old_config, new_config, mock_hotword, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("model missing"))

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        mock_hotword.reload.assert_not_called(), (
            "hotword.reload was called despite ASR recreate failure"
        )


# ─── T3: ASR 重建失敗不 mutate LLM processor ─────────────────────


class TestT3LLMNotMutatedOnASRFailure:
    """When ASR recreate fails, LLM processor must not be updated."""

    def test_llm_not_updated(self):
        va, old_config, new_config, _, mock_llm, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("model missing"))

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        mock_llm.update_config.assert_not_called(), (
            "llm.update_config was called despite ASR recreate failure"
        )


# ─── T4: ASR 重建失敗不持久化壞 config ───────────────────────────


class TestT4BrokenConfigNotPersisted:
    """When ASR recreate fails, new_config must NOT be saved to disk."""

    def test_new_config_not_saved(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("model missing"))

        with patch("app.app.ConfigManager") as MockCfgMgr:
            va._apply_config(new_config)

            for call_args in MockCfgMgr.save.call_args_list:
                saved_cfg = call_args[0][0]
                assert saved_cfg is not new_config, (
                    "new_config was persisted despite ASR recreate failure"
                )


# ─── T5: ASR 重建失敗不 raise ─────────────────────────────────────


class TestT5NoRaiseOnASRFailure:
    """When ASR recreate fails, _apply_config must NOT raise.

    Raising would crash the voice-worker thread or be silently swallowed
    by Qt signal handlers, leaving GUI in an inconsistent state.
    """

    def test_no_exception_raised(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("model missing"))

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)


# ─── T6: ASR 重建失敗喺 GUI 顯示錯誤狀態 ─────────────────────────


class TestT6ErrorStatusOnASRFailure:
    """When ASR recreate fails, GUI must show error status."""

    def test_error_status_shown(self):
        va, old_config, new_config, *_, mock_invoke = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("model missing"))

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        status_calls = [
            c for c in mock_invoke.call_args_list
            if len(c[0]) >= 2 and c[0][0] == "set_status"
        ]
        error_shown = any(
            "失敗" in str(c[0][1]) or "重啟" in str(c[0][1])
            for c in status_calls
        )
        assert error_shown, (
            f"GUI should show error status on ASR recreate failure, "
            f"got calls: {status_calls}"
        )


# ─── T7: ASR 重建成功套用全部子系統更新 ──────────────────────────


class TestT7SuccessPathAppliesAll:
    """When ASR recreate succeeds, all subsystem updates must be applied."""

    def test_all_subsystems_updated_on_success(self):
        va, old_config, new_config, mock_hotword, mock_llm, mock_asr, _ = _make_va_with_subsystems()
        # ASR recreate succeeds (default _init_asr behavior)

        with patch("app.app.ConfigManager") as MockCfgMgr:
            with patch("core.text_processor.TextProcessor") as MockTP:
                mock_tp_instance = MagicMock()
                MockTP.return_value = mock_tp_instance

                va._apply_config(new_config)

                assert va._config is new_config
                assert va._text_processor is mock_tp_instance
                mock_hotword.reload.assert_called_once_with(new_config.hotword)
                mock_llm.update_config.assert_called_once_with(new_config.llm)
                MockCfgMgr.save.assert_called_with(new_config)


# ─── T8: asr 未變更時其他改動仍套用，不嘗試重建 ASR ──────────────


class TestT8NonASRChangesStillApplied:
    """When asr config is unchanged, other changes must still be applied
    without attempting ASR recreation."""

    def test_output_and_hotword_updated_without_asr_recreate(self):
        va, old_config, new_config, mock_hotword, mock_llm, mock_asr, _ = _make_va_with_subsystems()
        new_config.asr = old_config.asr  # Same asr -> no recreate

        with patch("app.app.ConfigManager"):
            with patch("core.text_processor.TextProcessor") as MockTP:
                MockTP.return_value = MagicMock()

                va._apply_config(new_config)

                va._init_asr.assert_not_called()
                assert va._config is new_config
                mock_hotword.reload.assert_called_once()
                mock_llm.update_config.assert_called_once()


# ─── T9 已退役（v3.9.3）─────────────────────────────────────────
# 原測試「ASR not running: config applied without restart attempt」
# 測嘅係已移除嘅 audio_changed 分支入面
# `if self._asr_process and self._asr_process.is_running` 呢個 restart 閘門。
# 呢個分支已經隨 audio_changed 一齊移除（見上方模組 docstring）。
# is_running 閘門而家淨係喺 _apply_config_recreate_asr() 度用嚟決定要唔要
# stop 舊進程，呢個閘門已喺
# test_asr_recreate_rollback.py::TestT1StopBeforeStage::test_old_process_not_stopped_when_not_running
# 補齊覆蓋（code review MEDIUM-5），故呢度唔再重複測。


# ─── T10: 混合變更，重建失敗 => output 唔變 ──────────────────────


class TestT10MixedChangeRollback:
    """When both asr and output change, ASR recreate failure must
    prevent output change from being applied."""

    def test_output_not_updated_when_asr_fails(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("crash"))
        original_tp = va._text_processor

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        assert va._text_processor is original_tp
        assert va._config is old_config


# ─── T11: rollback 後 config 保持一致 ────────────────────────────


class TestT11ConfigConsistentAfterRollback:
    """After ASR recreate failure, self._config must equal old_config,
    and no subsystem should hold a reference to new_config."""

    def test_config_rolled_back(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("crash"))

        with patch("app.app.ConfigManager"):
            va._apply_config(new_config)

        assert va._config is old_config, (
            "self._config must be old_config after rollback"
        )

    def test_only_old_config_saved(self):
        va, old_config, new_config, *_ = _make_va_with_subsystems()
        va._init_asr = MagicMock(side_effect=RuntimeError("crash"))

        with patch("app.app.ConfigManager") as MockCfgMgr:
            va._apply_config(new_config)

            if MockCfgMgr.save.call_count > 0:
                last_saved = MockCfgMgr.save.call_args_list[-1][0][0]
                assert last_saved is old_config, (
                    "Last persisted config must be old_config"
                )
