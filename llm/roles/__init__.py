# 州州語音 - LLM 角色配置
# 匯出所有內建角色與查詢工具函數

from __future__ import annotations

from typing import Any, Dict, Sequence

from llm.processor import RoleConfig
from llm.roles.default import DEFAULT_ROLE
from llm.roles.dev_mode import DEV_MODE_ROLE
from llm.roles.writing_mode import WRITING_MODE_ROLE
from llm.roles.instruction_mode import INSTRUCTION_MODE_ROLE
from llm.roles.translator import TRANSLATOR_ROLE
from llm.roles.assistant import ASSISTANT_ROLE

# ---------------------------------------------------------------------------
# 內建角色查詢表：角色 ID → RoleConfig
# ---------------------------------------------------------------------------

BUILTIN_ROLES: dict[str, RoleConfig] = {
    "default": DEFAULT_ROLE,
    "書面模式": WRITING_MODE_ROLE,
    "開發模式": DEV_MODE_ROLE,
    "指令模式": INSTRUCTION_MODE_ROLE,
    "高級翻譯": TRANSLATOR_ROLE,
    "大助理": ASSISTANT_ROLE,
}

# 內建角色的原始提示詞快照（供「恢復預設」功能使用）
_BUILTIN_PROMPTS: dict[str, str] = {
    role_id: role.system_prompt
    for role_id, role in BUILTIN_ROLES.items()
}


def get_role(name: str) -> RoleConfig:
    """根據名稱取得角色配置，找不到則回傳預設角色。

    Parameters
    ----------
    name:
        角色 ID（例如 ``"default"``、``"開發模式"``）。

    Returns
    -------
    RoleConfig
        對應的角色配置。若名稱不在 ``BUILTIN_ROLES`` 中，回傳 ``DEFAULT_ROLE``。
    """
    return BUILTIN_ROLES.get(name, DEFAULT_ROLE)


def get_all_roles(
    custom_roles: Sequence[Dict[str, Any]] | None = None,
    builtin_overrides: Dict[str, str] | None = None,
) -> list[tuple[str, RoleConfig, bool]]:
    """取得所有角色（內建 + 用戶自訂）。

    Parameters
    ----------
    custom_roles:
        用戶自訂角色列表（來自 config）。
    builtin_overrides:
        用戶對內建角色提示詞的修改（role_id → 修改後的 prompt）。

    Returns
    -------
    list of (role_id, RoleConfig, is_builtin)
        排序：內建角色在前，自訂角色按順序在後。
    """
    result: list[tuple[str, RoleConfig, bool]] = []
    overrides = builtin_overrides or {}

    # 內建角色（套用用戶修改）
    for role_id, role in BUILTIN_ROLES.items():
        if role_id in overrides:
            from dataclasses import replace
            role = replace(role, system_prompt=overrides[role_id])
        result.append((role_id, role, True))

    # 用戶自訂角色
    if custom_roles:
        for entry in custom_roles:
            role_id = entry.get("id", "")
            if not role_id or role_id in BUILTIN_ROLES:
                continue  # 跳過無 ID 或與內建衝突的
            role = RoleConfig(
                name=entry.get("name", role_id),
                system_prompt=entry.get("system_prompt", ""),
                output_mode=entry.get("output_mode", "typing"),
                enable_history=entry.get("enable_history", False),
                enable_hotwords=entry.get("enable_hotwords", False),
            )
            result.append((role_id, role, False))

    return result


def resolve_role(
    role_id: str,
    custom_roles: Sequence[Dict[str, Any]] | None = None,
    builtin_overrides: Dict[str, str] | None = None,
) -> RoleConfig:
    """根據 role_id 解析角色，先查內建再查自訂。

    內建角色會套用 builtin_overrides 中的提示詞修改。

    Parameters
    ----------
    role_id:
        角色 ID。
    custom_roles:
        用戶自訂角色列表（來自 config）。
    builtin_overrides:
        用戶對內建角色提示詞的修改。

    Returns
    -------
    RoleConfig
    """
    overrides = builtin_overrides or {}

    # 內建（套用用戶修改）
    if role_id in BUILTIN_ROLES:
        role = BUILTIN_ROLES[role_id]
        if role_id in overrides:
            from dataclasses import replace
            role = replace(role, system_prompt=overrides[role_id])
        return role

    # 自訂
    if custom_roles:
        for entry in custom_roles:
            if entry.get("id") == role_id:
                return RoleConfig(
                    name=entry.get("name", role_id),
                    system_prompt=entry.get("system_prompt", ""),
                    output_mode=entry.get("output_mode", "typing"),
                    enable_history=entry.get("enable_history", False),
                    enable_hotwords=entry.get("enable_hotwords", False),
                )

    # fallback
    return DEFAULT_ROLE


def get_builtin_prompt(role_id: str) -> str:
    """取得內建角色的原始提示詞（用於「恢復預設」）。"""
    return _BUILTIN_PROMPTS.get(role_id, "")


__all__: list[str] = [
    "ASSISTANT_ROLE",
    "BUILTIN_ROLES",
    "DEFAULT_ROLE",
    "DEV_MODE_ROLE",
    "INSTRUCTION_MODE_ROLE",
    "TRANSLATOR_ROLE",
    "WRITING_MODE_ROLE",
    "get_all_roles",
    "get_builtin_prompt",
    "get_role",
    "resolve_role",
]
