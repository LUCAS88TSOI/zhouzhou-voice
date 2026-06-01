"""
內建 API Key 管理。

BigModel（智譜 AI）免費額度已停用，
用戶需自行在設定中配置 API Key。
"""

from __future__ import annotations


# 免費額度鎖定的模型（已停用）
BUILTIN_MODEL = "glm-4-flash-250414"


def get_builtin_key() -> str:
    """返回內建 BigModel API key，已停用。"""
    # 內建免費額度已停用，用戶需自行提供 API Key
    return ""
