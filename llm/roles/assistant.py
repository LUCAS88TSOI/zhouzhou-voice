# 州州語音 - 大助理角色
# 通用助手，幫助用戶解答問題

from __future__ import annotations

from llm.processor import RoleConfig

# ---------------------------------------------------------------------------
# System prompt（系統提示詞）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT: str = """\
你是一個助手，幫助用戶解答問題。所有輸出必須使用繁體中文。按用戶要求輸出內容，不要添加額外說明。
"""

# ---------------------------------------------------------------------------
# Role config（角色配置）
# ---------------------------------------------------------------------------

ASSISTANT_ROLE: RoleConfig = RoleConfig(
    name="大助理",
    system_prompt=_SYSTEM_PROMPT,
    output_mode="typing",
    enable_history=True,   # 保留對話歷史，支援多輪問答
    enable_hotwords=False,  # 通用助手不需要熱詞
)
