"""
州州語音 - 熱詞系統

子模組：
- phoneme: 音素模糊匹配（拼音相似度）
- rules: 使用者定義的規則替換
- rectify: 糾錯歷史存儲（LLM 上下文注入）
- manager: 統一管理器（協調子系統 + 文件監控）
"""

from hotword.manager import HotwordManager
from hotword.phoneme import PhonemeIndex
from hotword.rectify import RectifyStore
from hotword.rules import RuleEngine

__all__ = [
    "HotwordManager",
    "PhonemeIndex",
    "RectifyStore",
    "RuleEngine",
]
