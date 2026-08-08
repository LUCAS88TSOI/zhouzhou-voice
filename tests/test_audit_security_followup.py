"""
安全覆核追加修復（R9 / R10 / R15 的殘留缺口）

H1  剪貼簿輪詢期會讀到其他應用寫入的內容 → 密碼被送去雲端 LLM 並寫進 log
H2  config.json 讀取層 OSError 仍會導致完好的原檔被預設值覆蓋
M2  EnumClipboardFormats 回 0 同時代表「結束」與「失敗」，fail-open 會毀掉圖片
M3  非文字格式黑名單漏掉 >= 0xC000 的註冊格式（PNG、FileGroupDescriptorW）
M4  金鑰被貼進「API URL」欄位時，client.py 的 log 與錯誤訊息未 redact
M5  隔離檔含明文 API Key 且無上限累積
L1  redact 的長度門檻讓自架服務的短金鑰完全不被遮蔽
L3  被硬殺時殘留的 .config_*.tmp 含明文金鑰
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.config import AppConfig, ConfigManager
from utils.secrets import redact, safe_url


class TestRedact:
    def test_short_secret_is_still_redacted(self) -> None:
        """自架 Ollama / one-api 常見 sk-1234，洩漏後一樣可用。"""
        assert "sk-1234" not in redact("失敗：key=sk-1234", "sk-1234")

    def test_empty_secret_does_not_mangle_message(self) -> None:
        assert redact("一切正常", "") == "一切正常"
        assert redact("一切正常", None) == "一切正常"

    def test_single_char_secret_ignored(self) -> None:
        """單字元金鑰若照替換會把整段訊息打爛。"""
        assert redact("abcabc", "a") == "abcabc"

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.test/v1/models?key=AIzaSyUNKNOWN123",
            "https://x.test/v1/models?api_key=sk-UNKNOWN123",
            "https://x.test/v1/models?access_token=UNKNOWN123",
        ],
    )
    def test_url_key_params_redacted_without_knowing_secret(self, url: str) -> None:
        """金鑰被貼進 API URL 欄位時，我們並不知道它的值。"""
        out = redact(f"網路連線失敗：Max retries with url: {url}", None)
        assert "UNKNOWN123" not in out


class TestSafeUrl:
    def test_query_stripped(self) -> None:
        assert safe_url("https://x.test/v1?key=SECRET") == "https://x.test/v1"

    def test_userinfo_stripped(self) -> None:
        assert "SECRET" not in safe_url("https://user:SECRET@x.test/v1")

    def test_plain_url_unchanged(self) -> None:
        assert safe_url("https://x.test/v1") == "https://x.test/v1"

    def test_empty_is_safe(self) -> None:
        assert safe_url("") == ""


class TestClientDoesNotLogKeyInUrl:
    def test_init_log_strips_query(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        from llm.client import LLMClient
        from llm.provider import ProviderInfo

        fake_client_key = "sk-real-key-value"  # 測試假值，非真實金鑰
        provider = ProviderInfo(
            key="custom", name="custom",
            api_url="https://x.test/v1?key=PASTED-SECRET-123",
            api_key=fake_client_key, model="m", enabled=True,
        )
        with caplog.at_level(logging.INFO):
            LLMClient(provider)

        assert "PASTED-SECRET-123" not in caplog.text


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ConfigManager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ConfigManager, "CONFIG_FILE", tmp_path / "config.json")
    ConfigManager._load_failed = False
    ConfigManager._read_failed = False
    ConfigManager._disk_trusted = True
    return tmp_path


class TestReadFailureDoesNotDestroyConfig:
    def test_unreadable_config_is_never_overwritten(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """防毒／OneDrive 短暫鎖檔不得讓完好的 config.json 被預設值蓋掉。"""
        ConfigManager.save(AppConfig())
        data = json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))
        data["llm"]["providers"] = {"openai": {"api_key": "sk-PRECIOUS"}}
        (cfg_dir / "config.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8",
        )

        monkeypatch.setattr(ConfigManager, "READ_RETRY_DELAY", 0)
        monkeypatch.setattr(
            Path, "read_text",
            lambda self, **kw: (_ for _ in ()).throw(OSError("被鎖住")),
        )

        ConfigManager.load()
        monkeypatch.undo()

        # 載入失敗後的 save 必須被拒絕，原檔保持完好
        ConfigManager.save(AppConfig())
        on_disk = (cfg_dir / "config.json").read_text(encoding="utf-8")
        assert "sk-PRECIOUS" in on_disk

    def test_read_failure_does_not_write_empty_quarantine(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """讀不到內容時沒東西可隔離，不該產生空的 corrupt 檔。"""
        (cfg_dir / "config.json").write_text("whatever", encoding="utf-8")
        monkeypatch.setattr(ConfigManager, "READ_RETRY_DELAY", 0)
        monkeypatch.setattr(
            Path, "read_text",
            lambda self, **kw: (_ for _ in ()).throw(OSError("被鎖住")),
        )

        ConfigManager.load()
        monkeypatch.undo()

        assert not list(cfg_dir.glob("config.corrupt.*.json"))

    def test_parse_failure_still_quarantines(self, cfg_dir: Path) -> None:
        """內容損壞（讀得到但解析失敗）仍要走隔離流程。"""
        (cfg_dir / "config.json").write_text("{{{broken", encoding="utf-8")
        ConfigManager.load()
        assert list(cfg_dir.glob("config.corrupt.*.json"))


class TestQuarantineHousekeeping:
    def test_only_recent_quarantine_files_kept(self, cfg_dir: Path) -> None:
        """每份隔離檔都含明文 API Key，不能無限累積。"""
        for i in range(6):
            (cfg_dir / "config.json").write_text(f"broken-{i}", encoding="utf-8")
            ConfigManager.load()

        kept = list(cfg_dir.glob("config.corrupt.*.json"))
        assert len(kept) <= ConfigManager.MAX_QUARANTINE_FILES

    def test_cleanup_temp_files_removes_residue(self, cfg_dir: Path) -> None:
        """被硬殺時殘留的 .config_*.tmp 含明文金鑰。"""
        residue = cfg_dir / ".config_abc.tmp"
        residue.write_text('{"api_key": "sk-LEAK"}', encoding="utf-8")

        ConfigManager.cleanup_temp_files()

        assert not residue.exists()


class TestClipboardFormatSafety:
    def test_registered_png_format_counts_as_non_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chrome/Figma 複製圖片用註冊格式 "PNG"，黑名單抓不到。"""
        from utils import clipboard

        monkeypatch.setattr(
            clipboard, "_enum_formats", lambda: frozenset({0xC1_00}),
        )
        assert clipboard._has_non_text_formats() is True

    def test_pure_text_clipboard_is_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from utils import clipboard

        monkeypatch.setattr(
            clipboard, "_enum_formats",
            lambda: frozenset({clipboard.CF_UNICODETEXT, clipboard.CF_LOCALE}),
        )
        assert clipboard._has_non_text_formats() is False

    def test_enum_failure_is_unknown_not_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """列舉失敗必須回 None（無法判斷），不得 fail-open 成「純文字」。"""
        from utils import clipboard

        monkeypatch.setattr(clipboard, "_enum_formats", lambda: None)
        assert clipboard._has_non_text_formats() is None


class TestClipboardSensitiveGuard:
    def test_sensitive_clipboard_aborts_capture(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """密碼管理員標記的內容絕不能被讀去送 LLM。"""
        from utils import clipboard

        monkeypatch.setattr(clipboard, "_is_sensitive_clipboard", lambda: True)
        called: list[int] = []
        monkeypatch.setattr(
            clipboard.ClipboardManager, "get_text",
            classmethod(lambda cls: called.append(1) or "密碼"),
        )

        assert clipboard.ClipboardManager.capture_selection() is None
        assert not called, "偵測到敏感標記後不該再讀取剪貼簿"

    def test_unknown_sensitivity_also_aborts(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """判斷不到時保守處理（當成敏感）。"""
        from utils import clipboard

        monkeypatch.setattr(clipboard, "_is_sensitive_clipboard", lambda: None)
        assert clipboard.ClipboardManager.capture_selection() is None
