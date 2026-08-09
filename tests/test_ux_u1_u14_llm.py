"""
U1 + U14：LLM 的兩種「靜默」—— 一種太吵，一種太安靜

U1  首次使用未設 API Key 時，每一句話都彈一次「潤色失敗，請檢查網絡或 API Key」。
    內建免費 key 已停用，所以 _is_ready() 恆 False，新用戶連續口述十句就被打斷十次，
    訊息還把他往「檢查網絡」的錯方向推。
U14 主服務商失敗時，同一份 messages（含系統提示詞、熱詞、完整逐字稿）
    被原封不動重送給下一個有 key 的服務商，UI 完全無提示。
    用戶為隱私刻意選了自架 endpoint，卻可能把客戶資料送去 api.openai.com。

鎖住的行為：
- 「從未配置」與「配置了但失敗」是兩種結果，前者不得彈失敗警告
- 引導提示每個 session 只出現一次
- 跨服務商降級預設關閉；關閉時絕不重送給別家
- 開啟時降級成功必須回報實際用了誰
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.processor import LLMProcessor, LLMResult, RoleConfig
from llm.provider import ProviderInfo, get_active_provider
from utils.config import AppConfig, LLMConfig


def _provider(key: str, api_key: str = "sk-test") -> dict:
    return {
        "name": f"{key} 服務",
        "api_url": f"https://{key}.test/v1/chat/completions",
        "api_key": api_key,
        "model": "m",
        "enabled": True,
    }


def _config(active: str = "alpha", **llm_kw) -> AppConfig:
    llm = LLMConfig(
        active_provider=active,
        providers={"alpha": _provider("alpha"), "beta": _provider("beta")},
        **llm_kw,
    )
    return replace(AppConfig(), llm=llm)


# ─────────────────────── U1：未配置 ≠ 失敗 ───────────────────────

class TestU1NotConfigured:
    def test_result_flags_missing_configuration(self) -> None:
        """沒有任何 client 時，這不是「失敗」，是「還沒設定」。"""
        cfg = _config(active="alpha")
        cfg = replace(cfg, llm=replace(cfg.llm, providers={}))
        proc = LLMProcessor(cfg)

        result = proc.process(text="今天天氣不錯", role=RoleConfig())

        assert result.not_configured is True
        assert result.text == "今天天氣不錯"

    def test_real_failure_is_not_marked_as_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """配置了但 API 回 401 —— 這是真失敗，該提示檢查 Key。"""
        proc = LLMProcessor(_config())
        monkeypatch.setattr(
            proc, "_stream_chat",
            lambda **kw: LLMResult(text="", error="401 Unauthorized"),
        )
        monkeypatch.setattr(proc, "_failover", lambda **kw: LLMResult(
            text="", error="401 Unauthorized",
        ))

        result = proc.process(text="今天天氣不錯", role=RoleConfig())
        assert result.not_configured is False
        assert result.error

    def test_status_carries_flag_to_the_app_layer(self) -> None:
        """app 層靠這個旗標決定要不要吵人。"""
        from llm.processor import LLMResultStatus

        assert LLMResultStatus(
            success=False, text="x", was_processed=False,
        ).not_configured is False


class TestU1HintOncePerSession:
    @pytest.fixture
    def app(self, monkeypatch):
        from app.app import VoiceApp

        a = VoiceApp.__new__(VoiceApp)
        a._llm_hint_shown = False
        a._config = _config()
        sent: list[str] = []
        monkeypatch.setattr(
            a, "_invoke_gui",
            lambda method, *args: sent.append(
                f"{method}:{args[0][1] if args else ''}"
            ),
        )
        a._sent = sent
        return a

    def test_first_unconfigured_result_shows_one_hint(self, app) -> None:
        app._notify_llm_not_configured()
        hints = [m for m in app._sent if m.startswith("notify")]
        assert len(hints) == 1

    def test_hint_points_at_settings_not_at_the_network(self, app) -> None:
        app._notify_llm_not_configured()
        text = app._sent[0]
        assert "設定" in text
        assert "網絡" not in text and "网络" not in text

    def test_repeated_utterances_stay_quiet(self, app) -> None:
        for _ in range(10):
            app._notify_llm_not_configured()
        assert len([m for m in app._sent if m.startswith("notify")]) == 1

    def test_flag_starts_false_on_a_fresh_app(self) -> None:
        from app.app import VoiceApp

        assert VoiceApp._llm_hint_shown is False


# ─────────────────── U14：跨服務商降級要明示同意 ───────────────────

class TestU14FailoverDefaultsOff:
    def test_config_default_is_off(self) -> None:
        """資料離開原本的收件方，必須是用戶主動開啟的。"""
        assert LLMConfig().allow_provider_failover is False

    def test_failover_does_not_resend_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc = LLMProcessor(_config())
        tried: list[str] = []
        monkeypatch.setattr(
            proc, "_build_client",
            lambda prov, **kw: tried.append(prov.key) or object(),
        )

        result = proc._failover(
            messages=[{"role": "user", "content": "客戶合約金額"}],
            failed_provider=ProviderInfo(
                key="alpha", name="alpha", api_url="u", api_key="k",
                model="m", enabled=True,
            ),
            first_error="500 Internal Server Error",
            on_token=None,
            should_stop=None,
        )

        assert tried == [], "關閉降級時不得把逐字稿送給任何其他服務商"
        assert result.error == "500 Internal Server Error"

    def test_failover_resends_when_explicitly_enabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc = LLMProcessor(_config(allow_provider_failover=True))
        monkeypatch.setattr(proc, "_build_client", lambda prov, **kw: prov.key)
        monkeypatch.setattr(
            proc, "_stream_chat",
            lambda client, **kw: LLMResult(text="潤色結果"),
        )

        result = proc._failover(
            messages=[{"role": "user", "content": "x"}],
            failed_provider=ProviderInfo(
                key="alpha", name="alpha", api_url="u", api_key="k",
                model="m", enabled=True,
            ),
            first_error="500 Internal Server Error",
            on_token=None,
            should_stop=None,
        )
        assert result.text == "潤色結果"


class TestU14ReportsWhoActuallyAnswered:
    def test_successful_failover_names_the_provider(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc = LLMProcessor(_config(allow_provider_failover=True))
        monkeypatch.setattr(proc, "_build_client", lambda prov, **kw: prov.key)
        monkeypatch.setattr(
            proc, "_stream_chat", lambda client, **kw: LLMResult(text="ok"),
        )

        result = proc._failover(
            messages=[],
            failed_provider=ProviderInfo(
                key="alpha", name="alpha", api_url="u", api_key="k",
                model="m", enabled=True,
            ),
            first_error="500",
            on_token=None,
            should_stop=None,
        )
        assert result.used_provider == "beta"

    def test_normal_path_reports_the_configured_provider(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc = LLMProcessor(_config())
        monkeypatch.setattr(
            proc, "_stream_chat", lambda **kw: LLMResult(text="潤色結果"),
        )
        result = proc.process(text="原文", role=RoleConfig())
        assert result.used_provider == "alpha"

    def test_app_warns_when_another_provider_answered(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.app import VoiceApp

        a = VoiceApp.__new__(VoiceApp)
        a._config = _config(allow_provider_failover=True)
        sent: list[str] = []
        monkeypatch.setattr(
            a, "_invoke_gui",
            lambda method, *args: sent.append(args[0][1] if args else ""),
        )

        a._notify_provider_switch("beta")

        assert sent and "beta" in sent[0]


class TestU14ActiveProviderDoesNotSwapSilently:
    def test_unconfigured_active_provider_returns_none_by_default(self) -> None:
        """選了 custom 卻沒填 key，不該偷偷改用還留著 key 的 openai。"""
        cfg = _config(active="alpha")
        cfg = replace(cfg, llm=replace(cfg.llm, providers={
            "alpha": _provider("alpha", api_key=""),
            "beta": _provider("beta"),
        }))
        assert get_active_provider(cfg) is None

    def test_fallback_allowed_when_user_opted_in(self) -> None:
        cfg = _config(active="alpha", allow_provider_failover=True)
        cfg = replace(cfg, llm=replace(cfg.llm, providers={
            "alpha": _provider("alpha", api_key=""),
            "beta": _provider("beta"),
        }))
        info = get_active_provider(cfg)
        assert info is not None and info.key == "beta"

    def test_configured_provider_is_always_returned_untouched(self) -> None:
        info = get_active_provider(_config(active="beta"))
        assert info is not None and info.key == "beta"


class TestU14SettingsToggleExists:
    def test_llm_tab_round_trips_the_flag(self, tmp_path, monkeypatch) -> None:
        """純 config 旗標等於沒有 —— 用戶必須有地方開它。"""
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("未安裝 PySide6")
        from core.recording_db import RecordingDatabase

        monkeypatch.setattr(RecordingDatabase, "DB_PATH", tmp_path / "h.db")
        QApplication.instance() or QApplication(sys.argv)

        from gui.settings_panel import SettingsPanel

        panel = SettingsPanel(AppConfig())
        try:
            cfg = _config(allow_provider_failover=True)
            panel.load_config(cfg)
            assert panel.get_config().llm.allow_provider_failover is True

            panel.load_config(_config(allow_provider_failover=False))
            assert panel.get_config().llm.allow_provider_failover is False
        finally:
            panel.deleteLater()
