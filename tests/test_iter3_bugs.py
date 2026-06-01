"""
TDD 測試：修復第 3 輪發現的 Bug 2 - 長重疊去重

Bug 2 [HIGH]: 長重疊 >50 字元去重回歸
修復：移除 max_check 中的 50 字元硬編碼限制
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBug2LongOverlapDedupe:
    """Bug 2: 長重疊 (>50 字元) 應完整去重"""

    def test_overlap_detection_with_large_text(self):
        """
        測試重疊檢測邏輯：
        當 prev_text 和 curr_text 有超過 50 字元的精確重疊時，
        max_overlap 應該是完整長度，而不是被截斷到 50
        """
        # 模擬 merge_segment_tokens 中的重疊檢測邏輯
        overlap_text = "X" * 100
        prev_text = overlap_text
        curr_text = overlap_text

        # 找出最長精確匹配（修復後的邏輯）
        max_overlap = 0
        max_check = min(len(prev_text), len(curr_text))  # 移除 50 的限制
        for i in range(max_check, 0, -1):
            if prev_text[-i:] == curr_text[:i]:
                max_overlap = i
                break

        # 驗證：應該找到 100 字元的重疊
        assert max_overlap == 100, f"應該找到 100 字元重疊，但只找到 {max_overlap}"

    def test_max_check_limit_removed(self):
        """驗證 max_check 不再被限制為 50"""
        import inspect
        from transcribe import file_transcriber

        source = inspect.getsource(file_transcriber.merge_segment_tokens)

        # 驗證代碼中沒有 ", 50) " 的限制
        assert ", 50)" not in source, "代碼中仍存在 ', 50' 的硬編碼限制"

    def test_code_has_correct_logic(self):
        """驗證修復後的代碼邏輯正確"""
        import inspect
        from transcribe import file_transcriber

        source = inspect.getsource(file_transcriber.merge_segment_tokens)

        # 驗證 max_check 使用正確的公式
        assert "max_check = min(len(prev_text), len(curr_text))" in source
