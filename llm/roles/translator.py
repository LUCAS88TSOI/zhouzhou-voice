# 州州語音 - 高級翻譯角色
# 將用戶輸入的文本翻譯成英文

from __future__ import annotations

from llm.processor import RoleConfig

# ---------------------------------------------------------------------------
# System prompt（系統提示詞）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT: str = """\
你是翻譯助手，將用戶輸入的文本翻譯成英文。只輸出翻譯結果，保持原文語氣和風格，專業術語準確翻譯，不添加說明。
"""

# ---------------------------------------------------------------------------
# Role config（角色配置）
# ---------------------------------------------------------------------------

TRANSLATOR_ROLE: RoleConfig = RoleConfig(
    name="高級翻譯",
    system_prompt=_SYSTEM_PROMPT,
    output_mode="typing",
    enable_history=True,   # 保留對話歷史，支援上下文翻譯
    enable_hotwords=False,  # 翻譯不需要熱詞
)
