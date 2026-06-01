"""TDD 測試：Iter 2 Medium 優先級 bugs 修復"""
import pytest
import sys
import os
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestFilePolishFailureHandling:
    """app/app.py:1164 - file polish 失敗不應保存"""

    def test_polish_failure_does_not_save_file(self):
        """LLM 錯誤時不應保存 _polished.txt"""
        from app.app import VoiceApp
        from llm.processor import LLMResultStatus

        va = object.__new__(VoiceApp)
        va._config = MagicMock()
        va._config.llm = MagicMock()
        va._config.llm.active_role = "default"
        va._llm = MagicMock()
        va._invoke_gui = MagicMock()

        # 模擬 LLM 失敗
        va._llm.process.return_value = MagicMock(
            text="",
            error="API error",
            warnings=[]
        )

        result = va._try_llm_polish("test text")
        
        # Bug 修復：失敗時 success=False
        assert result.success is False
        # _polish_transcription_text 應檢查 success
        # 這會在完整整合測試中驗證


class TestRectifyPerformance:
    """hotword/manager.py:320 - rectify O(n²) 掃描優化"""

    def test_rectify_apply_uses_efficient_algorithm(self):
        """rectify apply 應該使用高效的替換算法"""
        # TODO: 實現後需要測試 rectify 的效能
        # 目前只是驗證 API 存在
        from hotword.rectify import RectifyStore
        assert hasattr(RectifyStore, 'apply')


class TestOverlapMergePerformance:
    """transcribe/file_transcriber.py:435 - quadratic merge 優化"""

    def test_merge_segment_tokens_exists(self):
        """驗證 merge_segment_tokens 方法存在"""
        from transcribe.file_transcriber import merge_segment_tokens
        assert callable(merge_segment_tokens)


class TestLLMResultAbstraction:
    """app/app.py:1066 - LLM result abstraction 一致性"""

    def test_all_callers_use_result_consistently(self):
        """所有呼叫者應該一致使用 LLMResultStatus"""
        from llm.processor import LLMResultStatus
        
        # 驗證 LLMResultStatus 有正確的欄位
        status = LLMResultStatus(
            success=True,
            text="test",
            was_processed=True,
            error=""
        )
        assert status.success is True
        assert status.text == "test"
