"""
ProviderCombo 供應商白名單測試。

把供應商下拉收窄到 Google + SiliconFlow（隱藏其餘，但 config／key 不刪、可逆）。
純函數 visible_provider_entries 決定顯示哪些供應商，可獨立單元測試。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gui.widgets.provider_combo import VISIBLE_PROVIDERS, visible_provider_entries

_PROVIDERS = {
    "openai": {"name": "OpenAI"},
    "google": {"name": "Google"},
    "siliconflow": {"name": "SiliconFlow"},
    "bigmodel": {"name": "BigModel"},
    "custom": {"name": "自訂"},
}


def test_visible_keeps_only_whitelist_in_config_order():
    keys = [k for k, _ in visible_provider_entries(_PROVIDERS)]
    assert keys == ["google", "siliconflow"]


def test_visible_preserves_display_name():
    entries = dict(visible_provider_entries(_PROVIDERS))
    assert entries["google"] == "Google"
    assert entries["siliconflow"] == "SiliconFlow"


def test_empty_whitelist_shows_all():
    keys = [k for k, _ in visible_provider_entries(_PROVIDERS, whitelist=frozenset())]
    assert set(keys) == set(_PROVIDERS)


def test_falls_back_to_key_when_name_missing():
    entries = dict(visible_provider_entries({"google": {}}))
    assert entries["google"] == "google"


def test_default_whitelist_is_google_and_siliconflow():
    assert VISIBLE_PROVIDERS == frozenset({"google", "siliconflow"})


# ─── always_include（避免隱藏使用中的 active_provider）────────

def test_always_include_reveals_provider_outside_whitelist():
    keys = [k for k, _ in visible_provider_entries(
        _PROVIDERS, always_include=("bigmodel",)
    )]
    assert keys == ["google", "siliconflow", "bigmodel"]


def test_always_include_empty_string_ignored():
    keys = [k for k, _ in visible_provider_entries(_PROVIDERS, always_include=("",))]
    assert keys == ["google", "siliconflow"]


def test_always_include_already_whitelisted_no_duplicate():
    keys = [k for k, _ in visible_provider_entries(
        _PROVIDERS, always_include=("google",)
    )]
    assert keys == ["google", "siliconflow"]
