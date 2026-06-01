"""
DISCOVERY.md 第二輪 review — 修復驗證測試

測試覆蓋：
  F1  — _dict_to_config 壞 JSON 崩潰 (Critical)
  F2  — enabled=bool(api_key) 停用 BigModel 內建額度
  F3  — UI 特例化 siliconflow/bigmodel 不一致
  F8  — hotkey threshold/suppress 修改不生效
  F11 — RecordingDatabase SQLite 無線程同步
  F12 — settings_panel reset/delete 突變 live config
"""
from __future__ import annotations

import threading


# ─── F1: _dict_to_config 壞 JSON 崩潰 ──────────────────────


class TestF1ConfigMalformedJson:
    """F1: 畸形但合法 JSON 不應讓 _dict_to_config 拋 AttributeError。"""

    def test_llm_as_list(self):
        """{"llm": []} — list 沒有 .get()，不應拋異常。"""
        from utils.config import _dict_to_config, AppConfig

        cfg = _dict_to_config({"llm": []})
        assert isinstance(cfg, AppConfig)

    def test_shortcut_as_string(self):
        """{"shortcut": "bad"} — str 沒有 .items()。"""
        from utils.config import _dict_to_config, AppConfig

        cfg = _dict_to_config({"shortcut": "bad"})
        assert isinstance(cfg, AppConfig)

    def test_output_as_int(self):
        """{"output": 42}。"""
        from utils.config import _dict_to_config, AppConfig

        cfg = _dict_to_config({"output": 42})
        assert isinstance(cfg, AppConfig)

    def test_audio_as_none(self):
        """{"audio": null}。"""
        from utils.config import _dict_to_config, AppConfig

        cfg = _dict_to_config({"audio": None})
        assert isinstance(cfg, AppConfig)

    def test_nested_bad_shapes(self):
        """多個區段同時畸形。"""
        from utils.config import _dict_to_config, AppConfig

        cfg = _dict_to_config({
            "llm": [],
            "shortcut": "bad",
            "output": 42,
            "audio": None,
            "hotword": True,
            "file": 3.14,
            "ui": [1, 2],
            "history": "nope",
        })
        assert isinstance(cfg, AppConfig)

    def test_llm_providers_as_string(self):
        """{"llm": {"providers": "garbage"}} — 嵌套值非 dict。"""
        from utils.config import _dict_to_config, AppConfig

        cfg = _dict_to_config({"llm": {"providers": "garbage"}})
        assert isinstance(cfg, AppConfig)
        assert len(cfg.llm.providers) > 0  # 退回預設 providers

    def test_config_manager_load_catches_attribute_error(self, tmp_path, monkeypatch):
        """ConfigManager.load() 對畸形 JSON 應退回預設，不崩潰。"""
        from utils.config import ConfigManager, AppConfig

        bad_json = tmp_path / "config.json"
        bad_json.write_text('{"llm": [], "hotword": "bad"}', encoding="utf-8")

        monkeypatch.setattr(ConfigManager, "CONFIG_FILE", bad_json)
        monkeypatch.setattr(ConfigManager, "CONFIG_DIR", tmp_path)
        cfg = ConfigManager.load()
        assert isinstance(cfg, AppConfig)


# ─── F2: enabled=bool(api_key) 停用 BigModel ────────────────


class TestF2EnabledNotDerivedFromKey:
    """F2: 保存設定時不應用 bool(api_key) 推導 enabled。"""

    def test_blank_key_does_not_set_enabled_false(self):
        """api_key 為空時 enabled 不應被設為 False。"""
        from unittest.mock import MagicMock
        from utils.config import AppConfig

        panel = MagicMock()
        panel._config = AppConfig()
        panel._current_provider_key = "bigmodel"
        panel._provider_cache_store = {}
        panel._api_key_input = MagicMock()
        panel._api_key_input.text.return_value = "  "  # 空 key（含空格）
        panel._model_input = MagicMock()
        panel._model_input.currentText.return_value = "glm-4-flash"
        panel._api_url_input = MagicMock()
        panel._api_url_input.text.return_value = "https://open.bigmodel.cn/api/paas/v4"

        from gui.settings_panel import SettingsPanel
        SettingsPanel._save_current_provider_fields(panel)

        cached = panel._provider_cache_store["bigmodel"]
        # 核心斷言：enabled 鍵不應存在或不應為 False
        assert "enabled" not in cached or cached["enabled"] is not False


# ─── F3: BigModel 內建 key 已停用 ────────────────────────────


class TestF3BuiltinKeyDisabled:
    """F3: BigModel 內建 key 已停用，需用戶自行提供 API Key。"""

    def test_bigmodel_requires_api_key(self):
        """BigModel 無 API Key 時不可用。"""
        import dataclasses
        from llm.provider import get_active_provider
        from utils.config import AppConfig

        config = AppConfig()
        config = dataclasses.replace(config, llm=dataclasses.replace(
            config.llm,
            active_provider="bigmodel",
            providers={**config.llm.providers, "bigmodel": {**config.llm.providers["bigmodel"], "api_key": ""}}
        ))

        provider = get_active_provider(config)
        assert provider is None  # 無 key 時應返回 None

    def test_bigmodel_with_api_key_available(self):
        """BigModel 有 API Key 時可用。"""
        import dataclasses
        from llm.provider import get_active_provider
        from utils.config import AppConfig

        config = AppConfig()
        config = dataclasses.replace(config, llm=dataclasses.replace(
            config.llm,
            active_provider="bigmodel",
            providers={**config.llm.providers, "bigmodel": {**config.llm.providers["bigmodel"], "api_key": "test-key-12345"}}
        ))

        provider = get_active_provider(config)
        assert provider is not None
        assert provider.is_available


# ─── F8: hotkey threshold/suppress 修改不生效 ────────────────


class TestF8HotkeyConfigApply:
    """F8: 修改 threshold/suppress 後應即時生效。"""

    def test_update_config_method_exists(self):
        """HotkeyListener 應有 update_config 方法。"""
        from utils.hotkey import HotkeyListener

        listener = HotkeyListener(
            key="f1", threshold=0.3, suppress=False,
            on_activate=None, on_deactivate=None,
        )
        assert hasattr(listener, "update_config"), "缺少 update_config 方法"

    def test_update_config_changes_all_params(self):
        """update_config 應更新 key、threshold 和 suppress。"""
        from utils.hotkey import HotkeyListener

        listener = HotkeyListener(
            key="f1", threshold=0.3, suppress=False,
            on_activate=None, on_deactivate=None,
        )
        listener.update_config(key="f2", threshold=0.8, suppress=True)

        assert listener._key_name == "f2"
        assert listener._threshold == 0.8
        assert listener._suppress is True

    def test_app_apply_config_calls_update_config(self):
        """VoiceApp._apply_config 應調用 update_config 傳遞全部快捷鍵參數。"""
        from unittest.mock import MagicMock, patch, call
        from utils.config import AppConfig, ShortcutConfig
        from dataclasses import replace
        from app.app import VoiceApp

        old_cfg = AppConfig()
        new_sc = ShortcutConfig(key="caps_lock", threshold=0.8, suppress=True)
        new_cfg = replace(old_cfg, shortcut=new_sc)

        mock_hotkey = MagicMock()

        va = object.__new__(VoiceApp)
        va._config = old_cfg
        va._hotkey = mock_hotkey
        va._repolish_hotkey = None
        va._llm = None
        va._main_window = None
        va._recording_db = None
        va._text_processor = None

        with patch("utils.config.ConfigManager.save"):
            va._apply_config(new_cfg)

        # 應調用 update_config（而非只 update_key）
        mock_hotkey.update_config.assert_called_once()


# ─── F11: RecordingDatabase SQLite 無線程同步 ────────────────


class TestF11SqliteThreadSafety:
    """F11: RecordingDatabase 應有線程鎖保護 SQLite 操作。"""

    def test_constructor_creates_lock(self, tmp_path, monkeypatch):
        """RecordingDatabase.__init__ 應建立 _lock。"""
        from core.recording_db import RecordingDatabase

        db_path = tmp_path / "test.db"
        monkeypatch.setattr(RecordingDatabase, "DB_PATH", db_path)
        db = RecordingDatabase()

        assert hasattr(db, "_lock"), "RecordingDatabase 缺少 _lock 屬性"
        assert isinstance(db._lock, type(threading.Lock()))
        db.close()

    def test_concurrent_insert_no_crash(self, tmp_path, monkeypatch):
        """多線程同時 insert 不應崩潰。"""
        import struct
        from core.recording_db import RecordingDatabase

        db_path = tmp_path / "test_history.db"
        monkeypatch.setattr(RecordingDatabase, "DB_PATH", db_path)
        db = RecordingDatabase()

        audio = struct.pack("10f", *([0.1] * 10))
        errors = []

        def worker(n):
            try:
                for _ in range(5):
                    db.insert(audio_bytes=audio, duration=0.1, asr_text=f"thread-{n}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        db.close()
        assert not errors, f"並發 insert 拋出異常: {errors}"

        monkeypatch.setattr(RecordingDatabase, "DB_PATH", db_path)
        db2 = RecordingDatabase()
        assert db2.count() == 20
        db2.close()


# ─── F12: settings_panel reset/delete 突變 live config ───────


class TestF12ConfigMutationGuard:
    """F12: reset/delete 不應直接修改 self._config.llm.providers。"""

    def test_reset_does_not_mutate_config(self):
        """重置後 self._config.llm.providers 不應被修改。"""
        from unittest.mock import MagicMock, patch
        from utils.config import AppConfig

        cfg = AppConfig()
        cfg.llm.providers["bigmodel"]["model_history"] = ["glm-4-flash"]

        panel = MagicMock()
        panel._config = cfg
        panel._current_provider_key = "bigmodel"
        panel._api_url_input = MagicMock()
        panel._api_key_input = MagicMock()
        panel._model_input = MagicMock()
        panel._provider_cache_store = {}

        before = dict(cfg.llm.providers["bigmodel"])

        from gui.settings_panel import SettingsPanel

        # QMessageBox 是在方法內局部 import 的，需要 patch PySide6.QtWidgets
        with patch("PySide6.QtWidgets.QMessageBox") as mock_mb:
            mock_mb.question.return_value = mock_mb.StandardButton.Yes
            SettingsPanel._on_reset_provider(panel)

        after = dict(cfg.llm.providers["bigmodel"])
        assert after == before, f"config 被突變: {before} → {after}"

    def test_delete_model_history_does_not_mutate_config(self):
        """刪除模型歷史不應修改 self._config.llm.providers。"""
        from unittest.mock import MagicMock
        from utils.config import AppConfig

        cfg = AppConfig()
        cfg.llm.providers["bigmodel"]["model_history"] = ["model-a", "model-b"]

        panel = MagicMock()
        panel._config = cfg
        panel._current_provider_key = "bigmodel"
        panel._model_input = MagicMock()
        panel._model_input.currentIndex.return_value = 0
        panel._model_input.currentText.return_value = "model-a"

        before_history = list(cfg.llm.providers["bigmodel"]["model_history"])

        from gui.settings_panel import SettingsPanel
        SettingsPanel._on_delete_model_history(panel)

        after_history = cfg.llm.providers["bigmodel"]["model_history"]
        assert after_history == before_history, f"config 被突變: {before_history} → {after_history}"

    def test_delete_model_persists_after_build_providers(self):
        """刪除模型歷史後 _build_updated_providers 不應恢復被刪項。"""
        from unittest.mock import MagicMock
        from utils.config import AppConfig

        cfg = AppConfig()
        cfg.llm.providers["bigmodel"]["model_history"] = ["model-a", "model-b", "model-c"]

        panel = MagicMock()
        panel._config = cfg
        panel._current_provider_key = "bigmodel"
        # 預填充 cache（模擬 _save_current_provider_fields 的結果）
        panel._provider_cache_store = {
            "bigmodel": {
                "api_key": "sk-test",
                "model": "model-b",
                "api_url": "https://api.example.com",
            },
        }

        # 模擬 QComboBox：刪除了 "model-a"，剩下 "model-b", "model-c"
        panel._model_input = MagicMock()
        panel._model_input.currentText.return_value = "model-b"
        panel._model_input.count.return_value = 2
        panel._model_input.itemText.side_effect = lambda i: ["model-b", "model-c"][i]
        panel._api_key_input = MagicMock()
        panel._api_key_input.text.return_value = "sk-test"
        panel._api_url_input = MagicMock()
        panel._api_url_input.text.return_value = "https://api.example.com"

        from gui.settings_panel import SettingsPanel
        result = SettingsPanel._build_updated_providers(panel)

        bigmodel_history = result["bigmodel"].get("model_history", [])
        assert "model-a" not in bigmodel_history, \
            f"被刪除的 model-a 不應出現在歷史中: {bigmodel_history}"

    def test_reset_provider_clears_model_history_on_save(self):
        """重置供應商後 _build_updated_providers 不應保留舊 model_history。"""
        from unittest.mock import MagicMock
        from utils.config import AppConfig

        cfg = AppConfig()
        cfg.llm.providers["openai"]["model_history"] = ["gpt-4", "gpt-3.5"]

        panel = MagicMock()
        panel._config = cfg
        panel._current_provider_key = "openai"
        # 預填充 cache（重置後 api_key 為空，model 為預設）
        panel._provider_cache_store = {
            "openai": {
                "api_key": "",
                "model": "gpt-4o-mini",
                "api_url": "https://api.openai.com/v1",
            },
        }

        # 重置後 QComboBox 被清空
        panel._model_input = MagicMock()
        panel._model_input.currentText.return_value = "gpt-4o-mini"
        panel._model_input.count.return_value = 0
        panel._model_input.itemText.side_effect = lambda i: [][i]
        panel._api_key_input = MagicMock()
        panel._api_key_input.text.return_value = ""
        panel._api_url_input = MagicMock()
        panel._api_url_input.text.return_value = "https://api.openai.com/v1"

        from gui.settings_panel import SettingsPanel
        result = SettingsPanel._build_updated_providers(panel)

        history = result["openai"].get("model_history", [])
        assert "gpt-4" not in history, f"重置後 gpt-4 不應保留: {history}"
        assert "gpt-3.5" not in history, f"重置後 gpt-3.5 不應保留: {history}"
