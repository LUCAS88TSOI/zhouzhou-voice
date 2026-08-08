"""
審查修復 R9：config.json 損壞時靜默重置為預設值，一次執行內 .bak 也被蓋掉

原本的五步連鎖：
1. load() 解析失敗直接 return AppConfig()
2. 那行 logger.error 發生在 setup_logging() 之前，打包後既看不到也不落檔
3. 預設值的 setup_complete=False 觸發首次啟動精靈，用戶以為只是版本更新
4. 精靈結束 save() 把壞檔複製成 .bak、預設值寫進 config.json
5. 退出時再 save 一次，.bak 被預設值再蓋一次

→ 所有 API Key、自訂角色、快捷鍵設定兩份檔案都救不回來，UI 零提示。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.config import AppConfig, ConfigManager


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ConfigManager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ConfigManager, "CONFIG_FILE", tmp_path / "config.json")
    ConfigManager._load_failed = False
    return tmp_path


def _write_good(cfg_dir: Path) -> None:
    ConfigManager.save(AppConfig())


class TestCorruptQuarantine:
    def test_corrupt_file_is_quarantined(self, cfg_dir: Path) -> None:
        """壞檔要另存成帶時間戳的副本，不能被後續 save 蓋掉。"""
        (cfg_dir / "config.json").write_text('{"llm": broken', encoding="utf-8")

        ConfigManager.load()

        quarantined = list(cfg_dir.glob("config.corrupt.*.json"))
        assert len(quarantined) == 1
        assert "broken" in quarantined[0].read_text(encoding="utf-8")

    def test_quarantine_names_do_not_collide(self, cfg_dir: Path) -> None:
        """連續兩次損壞不得互相覆蓋。"""
        (cfg_dir / "config.json").write_text("bad-1", encoding="utf-8")
        ConfigManager.load()
        (cfg_dir / "config.json").write_text("bad-2", encoding="utf-8")
        ConfigManager.load()

        bodies = {
            p.read_text(encoding="utf-8")
            for p in cfg_dir.glob("config.corrupt.*.json")
        }
        assert bodies == {"bad-1", "bad-2"}


class TestBakRecovery:
    def test_recovers_api_key_from_bak(self, cfg_dir: Path) -> None:
        """壞檔時要先嘗試從 .bak 還原，而不是直接吐預設值。"""
        good = AppConfig()
        good = good.__class__(**{**good.__dict__})
        ConfigManager.save(good)
        # 手動造一份含 API Key 的 .bak
        data = json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))
        data["llm"]["providers"] = {
            "openai": {"api_key": "sk-RECOVER-ME", "model": "gpt-4o"},
        }
        (cfg_dir / "config.json.bak").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8",
        )
        (cfg_dir / "config.json").write_text("{{{corrupt", encoding="utf-8")

        restored = ConfigManager.load()

        assert restored.llm.providers["openai"]["api_key"] == "sk-RECOVER-ME"

    def test_falls_back_to_defaults_when_bak_also_bad(self, cfg_dir: Path) -> None:
        (cfg_dir / "config.json").write_text("{{{corrupt", encoding="utf-8")
        (cfg_dir / "config.json.bak").write_text("also-corrupt", encoding="utf-8")

        config = ConfigManager.load()

        assert isinstance(config, AppConfig)
        assert ConfigManager.load_failed() is True


class TestLoadFailedFlag:
    def test_flag_false_on_clean_load(self, cfg_dir: Path) -> None:
        _write_good(cfg_dir)
        ConfigManager.load()
        assert ConfigManager.load_failed() is False

    def test_flag_true_on_corrupt(self, cfg_dir: Path) -> None:
        (cfg_dir / "config.json").write_text("nope", encoding="utf-8")
        ConfigManager.load()
        assert ConfigManager.load_failed() is True

    def test_flag_false_when_bak_recovery_succeeds(self, cfg_dir: Path) -> None:
        """從 .bak 救回也算沒遺失，但仍要留下 quarantine 檔。"""
        _write_good(cfg_dir)
        (cfg_dir / "config.json.bak").write_text(
            (cfg_dir / "config.json").read_text(encoding="utf-8"), encoding="utf-8",
        )
        (cfg_dir / "config.json").write_text("bad", encoding="utf-8")

        ConfigManager.load()

        assert ConfigManager.load_failed() is False
        assert list(cfg_dir.glob("config.corrupt.*.json"))


class TestBakRollGuard:
    def test_save_does_not_overwrite_bak_after_failed_load(
        self, cfg_dir: Path,
    ) -> None:
        """載入失敗後的第一次 save 不得把預設值蓋掉 .bak。"""
        precious = (cfg_dir / "config.json.bak")
        _write_good(cfg_dir)
        precious.write_text('{"marker": "precious"}', encoding="utf-8")
        (cfg_dir / "config.json").write_text("corrupt", encoding="utf-8")

        ConfigManager.load()
        ConfigManager.save(AppConfig())

        assert precious.read_text(encoding="utf-8") == '{"marker": "precious"}'

    def test_save_rolls_bak_normally_after_good_load(self, cfg_dir: Path) -> None:
        _write_good(cfg_dir)
        ConfigManager.load()
        ConfigManager.save(AppConfig())
        assert (cfg_dir / "config.json.bak").exists()
