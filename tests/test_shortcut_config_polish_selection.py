"""ShortcutConfig.polish_selection_key / polish_selection_instant 欄位測試。

涵蓋預設值、非字串防呆、config.json save/load round-trip。
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import config as cfgmod
from utils.config import ShortcutConfig


def test_default_is_disabled():
    sc = ShortcutConfig()
    assert sc.polish_selection_key == ""
    assert sc.polish_selection_instant is True


def test_non_string_key_falls_back_to_empty():
    sc = ShortcutConfig(polish_selection_key=None)  # type: ignore[arg-type]
    assert sc.polish_selection_key == ""


def _isolate(monkeypatch, tmp_path):
    CM = cfgmod.ConfigManager
    monkeypatch.setattr(CM, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(CM, "CONFIG_FILE", tmp_path / "config.json")
    return CM


def test_roundtrips_through_save_load(tmp_path, monkeypatch):
    CM = _isolate(monkeypatch, tmp_path)
    cfg = cfgmod.AppConfig()
    cfg2 = dataclasses.replace(
        cfg,
        shortcut=dataclasses.replace(
            cfg.shortcut,
            polish_selection_key="f3",
            polish_selection_instant=False,
        ),
    )

    CM.save(cfg2)
    loaded = CM.load()

    assert loaded.shortcut.polish_selection_key == "f3"
    assert loaded.shortcut.polish_selection_instant is False
