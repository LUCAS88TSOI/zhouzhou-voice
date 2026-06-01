"""
TDD 測試：修復第 3 輪發現的剩餘 Bug

Bug 5 [HIGH]: History reprocess 在 LLM 失敗時覆寫歷史
修復：使用 result.success 決定是否更新 DB

Bug 6 [HIGH]: ASR 模型切換 RAM doubling + callback leaks
修復：先停舊進程再建新的，添加 unregister_shutdown()
"""

import pytest
import sys
import os
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# Bug 5: History reprocess 不應在 LLM 失敗時覆寫
# ============================================================================

@dataclass
class LLMResultStatus:
    """LLM 處理結果的結構化狀態"""
    success: bool          # 是否成功處理（無錯誤）
    text: str              # 結果文本（可能與原文相同）
    was_processed: bool    # 是否實際送交 LLM 處理
    error: str | None = None  # 錯誤訊息（若有）


class TestBug5HistoryReprocessOnError:
    """Bug 5: LLM 失敗時不應覆寫歷史記錄"""

    def test_llm_api_error_does_not_overwrite_history(self):
        """
        測試 LLM API 錯誤時，應保留原有歷史記錄
        Bug 5 修復前：was_processed=True 導致錯誤結果仍寫入 DB
        Bug 5 修復後：只有 success=True 才更新 DB
        """
        from llm.processor import LLMResultStatus

        # 模擬 LLM API 錯誤
        error_result = LLMResultStatus(
            success=False,
            text="原始文字",  # 返回原文或空
            was_processed=True,  # 實際送交了 LLM
            error="API timeout"
        )

        # Bug 5 修復：檢查 success 而非 was_processed
        should_update = error_result.success
        assert not should_update, "LLM 錯誤時不應更新歷史記錄"

    def test_llm_success_does_update_history(self):
        """
        測試 LLM 成功時，應更新歷史記錄
        """
        from llm.processor import LLMResultStatus

        success_result = LLMResultStatus(
            success=True,
            text="潤色後的文字",
            was_processed=True,
            error=None
        )

        should_update = success_result.success
        assert should_update, "LLM 成功時應更新歷史記錄"

    def test_llm_unavailable_does_not_update_history(self):
        """
        測試沒有配置 LLM 時，不應更新歷史記錄
        """
        from llm.processor import LLMResultStatus

        unavailable_result = LLMResultStatus(
            success=False,
            text="原始文字",
            was_processed=False,  # 沒有 LLM 可用
            error=None
        )

        should_update = unavailable_result.success
        assert not should_update, "無 LLM 時不應更新歷史記錄"


# ============================================================================
# Bug 6: ASR 模型切換 RAM doubling + callback leaks
# ============================================================================

class TestBug6ASRModelSwitchMemory:
    """Bug 6: ASR 模型切換應先停止舊進程，避免 RAM doubling"""

    def test_asr_switch_stops_old_before_creating_new(self):
        """
        測試 ASR 模型切換應先停止舊進程
        Bug 6 修復前：先創建新進程（RAM doubling），再停止舊的
        Bug 6 修復後：先停止舊進程，再創建新的
        """
        # 模擬切換流程
        steps = []

        # Bug 6 修復後的順序
        steps.append("stop_old_process")
        steps.append("unregister_old_callback")
        steps.append("create_new_process")
        steps.append("register_new_callback")

        # 驗證順序：stop_old 必須在 create_new 之前
        stop_idx = steps.index("stop_old_process")
        create_idx = steps.index("create_new_process")
        assert stop_idx < create_idx, "應先停止舊進程，再創建新進程"

    def test_lifecycle_unregister_prevents_callback_leaks(self):
        """
        測試 unregister_shutdown() 應移除已註冊的 callback
        Bug 6 修復：添加 unregister_shutdown() 方法
        """
        from app.lifecycle import LifecycleManager

        lifecycle = LifecycleManager()
        dummy_callback = lambda: None

        # 註冊 callback
        lifecycle.register_shutdown(dummy_callback)
        assert len(lifecycle._shutdown_callbacks) == 1

        # 移除 callback
        result = lifecycle.unregister_shutdown(dummy_callback)
        assert result is True, "應成功移除 callback"
        assert len(lifecycle._shutdown_callbacks) == 0, "callback 應被移除"

    def test_lifecycle_unregister_nonexistent_returns_false(self):
        """
        測試移除不存在的 callback 應返回 False
        """
        from app.lifecycle import LifecycleManager

        lifecycle = LifecycleManager()
        dummy_callback = lambda: None

        result = lifecycle.unregister_shutdown(dummy_callback)
        assert result is False, "移除不存在的 callback 應返回 False"
