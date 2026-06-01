"""
Bug 6 修復：ASR 模型切換使用 stop-before-stage 策略

修復前（RAM doubling）：
1. 先創建新進程（不停止舊的）
2. 成功後停止舊進程
3. 失敗時 rollback 到舊 config

修復後（避免 RAM doubling）：
1. 先停止舊進程（釋放記憶體）
2. 再創建新進程
3. 失敗時不 rollback（config 已更新，用戶需重啟應用）
4. 添加 unregister_shutdown() 防止 callback leaks

Tests:
  T1 - 停止舊進程後，_init_asr() 失敗返回 False
  T2 - _init_asr() 失敗顯示錯誤狀態
  T3 - _init_asr() 成功返回 True
  T4 - 舊進程被正確停止
  T5 - unregister_shutdown() 移除 callback
  T6 - 停止舊進程時調用 unregister_shutdown()
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_va_for_recreate():
    """Create a minimal VoiceApp with mocked subsystems for ASR recreate testing."""
    from app.app import VoiceApp

    va = object.__new__(VoiceApp)

    # Minimal init - avoid full __init__ which starts real subsystems
    va._config = MagicMock(name="old_config")
    va._config.asr = MagicMock(name="old_asr")
    va._asr_process = MagicMock(name="old_asr_process")
    va._asr_process.is_running = True
    va._invoke_gui = MagicMock(name="invoke_gui")
    va._lifecycle = MagicMock(name="lifecycle")

    new_config = MagicMock(name="new_config")
    new_config.asr = MagicMock(name="new_asr")

    return va, new_config


# ─── T1: _init_asr() failure triggers rollback of self._config ──────


class TestT1StopBeforeStage:
    """Bug 6 修復：先停止舊進程，再創建新進程（避免 RAM doubling）"""

    def test_old_process_stopped_before_init(self):
        va, new_config = _make_va_for_recreate()
        old_asr_process = va._asr_process
        old_config = va._config

        # 模擬 _init_asr() 成功創建新進程
        def fake_init_asr():
            va._asr_process = MagicMock(name="new_asr_process")
            va._asr_process.is_running = True

        va._init_asr = fake_init_asr

        result = va._apply_config_recreate_asr(new_config)

        # Bug 6 修復：舊進程應被停止
        old_asr_process.stop.assert_called_once()
        # unregister_shutdown 也應被調用
        va._lifecycle.unregister_shutdown.assert_called_once_with(old_asr_process.stop)
        # 新進程應被創建
        assert va._asr_process is not old_asr_process
        assert result is True

    def test_config_rolled_back_on_failure(self):
        """Bug 6 修復：失敗時恢復 config（與 _apply_config 流程兼容）"""
        va, new_config = _make_va_for_recreate()
        old_config = va._config

        # 模擬 _init_asr() 失敗
        def fake_init_asr():
            va._asr_process = None

        va._init_asr = fake_init_asr

        result = va._apply_config_recreate_asr(new_config)

        # Bug 6 修復：失敗時恢復 config（與 _apply_config 流程兼容）
        assert va._config is old_config, (
            "失敗時應恢復 config（與 _apply_config 流程兼容）"
        )
        assert result is False


# ─── T2: _init_asr() failure shows error status, NOT "就緒" ──────────


class TestT2ErrorStatusOnSilentFailure:
    """When _init_asr() silently fails, GUI must show error status, not "就緒"."""

    def test_error_status_shown_not_ready(self):
        va, new_config = _make_va_for_recreate()

        def fake_init_asr():
            va._asr_process = None

        va._init_asr = fake_init_asr

        va._apply_config_recreate_asr(new_config)

        status_calls = [
            c for c in va._invoke_gui.call_args_list
            if len(c[0]) >= 2 and c[0][0] == "set_status"
        ]
        last_status = status_calls[-1] if status_calls else None

        assert last_status is not None, "At least one set_status call expected"
        # _invoke_gui("set_status", (str, "message")) => call_args[0] = ("set_status", (str, "message"))
        status_arg = last_status[0][1]
        # status_arg may be a tuple (str, "message") or a string
        status_text = status_arg[1] if isinstance(status_arg, tuple) else status_arg
        assert "失敗" in status_text or "重建" in status_text, (
            f"Last status should indicate failure, got: {status_text}"
        )
        assert status_text != "就緒", (
            "Status must NOT be '就緒' when ASR recreation failed"
        )


# ─── T3: _init_asr() failure returns False ──────────────────────────


class TestT3ReturnsFalseOnSilentFailure:
    """When _init_asr() silently fails, must return False."""

    def test_returns_false(self):
        va, new_config = _make_va_for_recreate()

        def fake_init_asr():
            va._asr_process = None

        va._init_asr = fake_init_asr

        result = va._apply_config_recreate_asr(new_config)
        assert result is False, "Must return False when ASR recreation fails"


# ─── T4: _init_asr() success returns True and commits new_config ────


class TestT4SuccessPath:
    """When _init_asr() succeeds, must return True and commit new_config."""

    def test_returns_true_and_commits(self):
        va, new_config = _make_va_for_recreate()

        def fake_init_asr():
            va._asr_process = MagicMock(name="new_asr_process")

        va._init_asr = fake_init_asr

        result = va._apply_config_recreate_asr(new_config)
        assert result is True, "Must return True on success"
        assert va._config is new_config, "Must commit new_config on success"

    def test_shows_ready_status(self):
        va, new_config = _make_va_for_recreate()

        def fake_init_asr():
            va._asr_process = MagicMock(name="new_asr_process")

        va._init_asr = fake_init_asr

        va._apply_config_recreate_asr(new_config)

        status_calls = [
            c for c in va._invoke_gui.call_args_list
            if len(c[0]) >= 2 and c[0][0] == "set_status"
        ]
        last_status = status_calls[-1] if status_calls else None
        assert last_status is not None
        status_arg = last_status[0][1]
        status_text = status_arg[1] if isinstance(status_arg, tuple) else status_arg
        assert status_text == "就緒", (
            f"Status should be '就緒' on success, got: {status_text}"
        )


# ─── T5: _init_asr() failure: old ASR process is restored ───────────


class TestT5LifecycleUnregister:
    """Bug 6 修復：unregister_shutdown() 防止 callback leaks"""

    def test_unregister_shutdown_called_on_old_process(self):
        """測試停止舊進程時應取消註冊其 shutdown callback"""
        va, new_config = _make_va_for_recreate()
        old_asr_process = va._asr_process

        # 模擬 _init_asr() 成功
        def fake_init_asr():
            va._asr_process = MagicMock(name="new_asr_process")
            va._asr_process.is_running = True

        va._init_asr = fake_init_asr

        va._apply_config_recreate_asr(new_config)

        # Bug 6 修復：應調用 unregister_shutdown
        va._lifecycle.unregister_shutdown.assert_called_once_with(old_asr_process.stop)

    def test_lifecycle_unregister_returns_true_for_registered(self):
        """測試 unregister_shutdown() 對已註冊的 callback 返回 True"""
        from app.lifecycle import LifecycleManager

        lifecycle = LifecycleManager()
        dummy = lambda: None

        lifecycle.register_shutdown(dummy)
        result = lifecycle.unregister_shutdown(dummy)

        assert result is True, "移除已註冊的 callback 應返回 True"


# ─── T6: _init_asr() failure logs rollback message ──────────────────


class TestT6FailureLogged:
    """Bug 6 修復：ASR 創建失敗時記錄錯誤"""

    def test_failure_is_logged(self):
        """測試 ASR 創建失敗時應記錄錯誤"""
        va, new_config = _make_va_for_recreate()

        def fake_init_asr():
            va._asr_process = None

        va._init_asr = fake_init_asr

        with patch("app.app.logger") as mock_logger:
            va._apply_config_recreate_asr(new_config)

            # Bug 6 修復：應記錄錯誤
            mock_logger.error.assert_called()
            error_msg = str(mock_logger.error.call_args)
            assert "ASR 重建失敗" in error_msg or "新進程未成功啟動" in error_msg

            error_calls = [
                c for c in mock_logger.error.call_args_list
                if "重建" in str(c) or "回滾" in str(c) or "rollback" in str(c).lower()
            ]
            assert len(error_calls) > 0, (
                "An error log about rollback/rebuild failure must be emitted"
            )


# ─── T7: _init_asr() raises exception (original path) also works ───


class TestT7ExceptionHandling:
    """Bug 6 修復：_init_asr() 異常時的處理"""

    def test_exception_returns_false_and_rolls_back(self):
        """測試 _init_asr() 異常時返回 False 並恢復 config"""
        va, new_config = _make_va_for_recreate()
        old_config = va._config
        old_asr_process = va._asr_process

        def fake_init_asr_raises():
            va._asr_process = None
            raise RuntimeError("ASR model failed to load")

        va._init_asr = fake_init_asr_raises

        result = va._apply_config_recreate_asr(new_config)
        assert result is False
        # Bug 6 修復：異常時恢復 config
        assert va._config is old_config, (
            "異常時應恢復 config"
        )
        # 舊進程應被停止
        old_asr_process.stop.assert_called_once()


if __name__ == "__main__":
    import traceback
    tests = [
        TestT1RollbackOnSilentInitFailure().test_config_rolled_back_when_init_asr_silently_fails,
        TestT2ErrorStatusOnSilentFailure().test_error_status_shown_not_ready,
        TestT3ReturnsFalseOnSilentFailure().test_returns_false,
        TestT4SuccessPath().test_returns_true_and_commits,
        TestT4SuccessPath().test_shows_ready_status,
        TestT5AsrProcessIsNullAfterFailure().test_asr_process_is_none,
        TestT6RollbackLogged().test_rollback_is_logged,
        TestT7ExceptionRaisingAlsoTriggersRollback().test_exception_path_also_rolls_back,
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
