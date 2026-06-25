"""
ConfigManager.save 原子寫測試。

防止「進程中途被殺／crash 留下半截 config.json → 下次載入失敗 → 設定
（含 API Key）靜默遺失」。原子寫保證：要麼完整寫入、要麼原檔不變。

涵蓋：
  - API Key 等設定 save→load 完整 round-trip
  - 置換前保留一份 .bak 滾動備份
  - 不留 .tmp 臨時垃圾檔
"""

import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import config as cfgmod

# 測試用假金鑰（變數形式，避免被 pre-commit secret 掃描誤報）
_FAKE_KEY = "fake-test-api-key"


def _isolate(monkeypatch, tmp_path):
    CM = cfgmod.ConfigManager
    monkeypatch.setattr(CM, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(CM, "CONFIG_FILE", tmp_path / "config.json")
    return CM


def test_save_roundtrips_api_key(tmp_path, monkeypatch):
    CM = _isolate(monkeypatch, tmp_path)
    cfg = cfgmod.AppConfig()
    prov = dict(cfg.llm.providers)
    prov["custom"] = {**prov.get("custom", {}), "api_key": _FAKE_KEY}
    cfg2 = dataclasses.replace(
        cfg, llm=dataclasses.replace(cfg.llm, providers=prov)
    )

    CM.save(cfg2)
    loaded = CM.load()

    assert loaded.llm.providers["custom"]["api_key"] == _FAKE_KEY


def test_save_creates_rolling_backup(tmp_path, monkeypatch):
    CM = _isolate(monkeypatch, tmp_path)
    CM.save(cfgmod.AppConfig())  # 第一次：尚無舊檔可備份
    CM.save(cfgmod.AppConfig())  # 第二次：應保留上次良好副本

    assert (tmp_path / "config.json.bak").exists()


def test_save_leaves_no_temp_files(tmp_path, monkeypatch):
    CM = _isolate(monkeypatch, tmp_path)
    CM.save(cfgmod.AppConfig())

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_produces_valid_json(tmp_path, monkeypatch):
    import json
    CM = _isolate(monkeypatch, tmp_path)
    CM.save(cfgmod.AppConfig())

    # 完整可解析（原子寫保證唔會半截）
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "llm" in data
