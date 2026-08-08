"""
重新潤色快捷鍵不應受「LLM 潤色」總開關影響 — 修復驗證測試

背景：
  用戶喺設定關咗 `llm.enabled`（持久化落 config.json，啟動時就係 False），
  `_init_llm()` 直接 return，`self._llm` 全程留喺 None。
  之後撳重新潤色快捷鍵，`_build_repolish_processor()` 喺 `repolish_provider`
  為空字串時直接 `return self._llm`（None），`_run_repolish()` 收到 None
  就顯示「未配置 LLM」並放棄 —— 用戶明確主動觸發嘅動作被總開關無聲吞咗。

第一版修復嘅陷阱（CRITICAL，由 code-reviewer 揪出）：
  即場建立 processor 時如果原封不動傳 `self._config.llm`（`enabled=False`），
  `LLMProcessor._is_ready()` 會讀呢個 flag 並拒絕呼叫 —— processor 建到、
  client 都建到，但 `process()` 一律短路返原文，一個 HTTP request 都冇發出過。
  症狀由「明確講未配置」變成更誤導嘅「扮完成」（狀態列顯示完成，但貼返原文）。
  所以呢個測試檔嘅斷言刻意唔止於 `processor is not None`，仲要驗證
  `passed_cfg.enabled is True` / `processor._is_ready() is True`，並且用
  `llm.client.LLMClient.chat_with_warnings`（真正嘅網絡邊界）做端到端驗證，
  唔可以淨係 mock 走 `_try_llm_polish`。

預期：
  重新潤色係用戶主動觸發，唔應該依賴 `self._llm` 呢個「總開關開咗先建立」
  嘅 side-effect 物件，亦唔應該將 `enabled=False` 直接帶入即場建立嘅
  processor。`self._llm` 為 None、或者總開關喺 session 內被切走時，都應該
  用 `dataclasses.replace(self._config.llm, enabled=True)` 即場建一個可用嘅
  LLMProcessor；`self._llm` 已存在且總開關依然開住時則繼續復用（保留連線池 /
  對話歷史）；真係冇任何可用服務商（例如未填 API Key）時要優雅返 None，等
  `_run_repolish()` 如實顯示「未配置 LLM」，唔好靜默貼返原文扮成功。
"""
from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch


def _valid_providers():
    """帶有效 api_key 嘅 providers dict，確保 get_active_provider() 唔會因為
    「未填 API Key」而返 None —— 呢個測試檔要驗證嘅係總開關邏輯，唔係服務商
    驗證邏輯（嗰個由 llm/provider.py 自己嘅測試覆蓋）。"""
    from utils.config import DEFAULT_PROVIDERS

    providers = {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()}
    for key in ("bigmodel", "deepseek"):
        providers[key] = {**providers[key], "api_key": "test-key-1234567890"}
    return providers


def _make_app(llm_config, llm_instance=None):
    """建立一個唔行 __init__ 嘅 VoiceApp，只設 _build_repolish_processor 需要嘅屬性。"""
    from app.app import VoiceApp
    from utils.config import AppConfig

    va = object.__new__(VoiceApp)
    va._config = replace(AppConfig(), llm=llm_config)
    va._llm = llm_instance
    return va


def _off_config(**overrides):
    """總開關關咗（enabled=False）、冇單獨 repolish 服務商、帶有效 provider 嘅 LLMConfig。"""
    from utils.config import LLMConfig

    base = dict(
        enabled=False,
        active_provider="bigmodel",
        providers=_valid_providers(),
        repolish_provider="",
        repolish_model="",
        repolish_role="",
    )
    base.update(overrides)
    return LLMConfig(**base)


# ─── 核心 bug：總開關關咗 + self._llm is None ────────────────


class TestRepolishWhenGlobalToggleOff:
    """llm.enabled=False 且 self._llm is None 時仍要有真正可用嘅 processor。"""

    def test_returns_ready_processor(self):
        """總開關關咗、repolish_provider 空 → 唔止非 None，仲要真係可用（_is_ready）。"""
        va = _make_app(_off_config(), llm_instance=None)

        processor, _ = va._build_repolish_processor()

        assert processor is not None, "總開關關咗時重新潤色仍應建立 LLMProcessor"
        assert processor._is_ready() is True, (
            "processor 唔可以係『建到但用唔到』嘅空殼——"
            "呢個正正係第一版修復漏咗嘅 CRITICAL"
        )

    def test_built_from_active_provider_with_enabled_forced(self):
        """即場建立時應沿用原本 active_provider / providers，但強制 enabled=True。"""
        cfg = _off_config(active_provider="bigmodel")
        va = _make_app(cfg, llm_instance=None)

        with patch("llm.processor.LLMProcessor") as mock_cls:
            mock_cls.return_value = MagicMock(name="fallback-processor")
            processor, _ = va._build_repolish_processor()

        assert processor is mock_cls.return_value
        mock_cls.assert_called_once()
        passed_cfg = mock_cls.call_args[0][0]
        assert passed_cfg.active_provider == "bigmodel", "唔應該改 active_provider"
        assert passed_cfg.providers == cfg.providers
        assert passed_cfg.enabled is True, (
            "必須顯式繞過總開關，否則 LLMProcessor._is_ready() 會拒絕呼叫"
        )

    def test_repolish_role_still_returned(self):
        """即場建立分支仍要正確回傳 repolish_role。"""
        va = _make_app(_off_config(repolish_role="translate"), llm_instance=None)

        _, role = va._build_repolish_processor()

        assert role == "translate"

    def test_run_repolish_calls_llm_client_despite_toggle_off(self):
        """端到端：總開關關咗撳重新潤色，真正嘅 LLM client 都要被呼叫——
        唔止『唔顯示未配置』，而係實際打咗出去並攞到潤色結果。"""
        va = _make_app(_off_config(), llm_instance=None)
        va._processing_lock = threading.Lock()
        va._is_processing = False
        va._is_repolishing = False
        va._last_result = "原始文字"
        va._last_pre_llm_text = "原始文字"
        va._hotword = None
        va._config = replace(va._config, output=replace(va._config.output, paste_mode=False))

        with patch.object(va, "_invoke_gui") as mock_gui, \
                patch("llm.client.LLMClient.chat_with_warnings") as mock_chat:
            mock_chat.return_value = (iter(["潤色後文字"]), [])
            va._run_repolish()

        mock_chat.assert_called_once()
        statuses = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "set_status"]
        assert "未配置 LLM" not in statuses, f"仍然被總開關擋住: {statuses}"
        assert va._last_result == "潤色後文字"


# ─── 復用既有 processor（唔好斷連線池 / 對話歷史）──────────


class TestRepolishReusesExistingProcessor:
    """self._llm 已存在且總開關依然開住時要復用同一物件。"""

    def test_reuses_same_object(self):
        """repolish_provider 空、self._llm 非 None、enabled=True → 返回同一個物件（identity）。"""
        existing = MagicMock(name="existing-llm")
        va = _make_app(_off_config(enabled=True), llm_instance=existing)

        processor, _ = va._build_repolish_processor()

        assert processor is existing, "應復用既有 processor，唔好重新 new（會斷連線池/歷史）"

    def test_does_not_construct_new_processor(self):
        """復用分支唔應該再 new LLMProcessor。"""
        existing = MagicMock(name="existing-llm")
        va = _make_app(_off_config(enabled=True), llm_instance=existing)

        with patch("llm.processor.LLMProcessor") as mock_cls:
            va._build_repolish_processor()

        mock_cls.assert_not_called()

    def test_repeated_calls_return_same_object(self):
        """連續兩次呼叫應返回同一物件。"""
        existing = MagicMock(name="existing-llm")
        va = _make_app(_off_config(enabled=True), llm_instance=existing)

        first, _ = va._build_repolish_processor()
        second, _ = va._build_repolish_processor()

        assert first is second

    def test_does_not_reuse_when_toggle_off_in_session(self):
        """self._llm 已存在，但總開關依家關咗（session 內由開切關）→ 唔可以復用。

        self._llm 同主語音管線共用，若喺呢度偷偷幫佢 replace(enabled=True)，
        會令正常錄音都跟住恢復潤色，所以總開關關咗一律行 fallback 即場建立。
        """
        existing = MagicMock(name="existing-llm")
        va = _make_app(_off_config(enabled=False), llm_instance=existing)

        with patch("llm.processor.LLMProcessor") as mock_cls:
            mock_cls.return_value = MagicMock(name="fresh-processor")
            processor, _ = va._build_repolish_processor()

        assert processor is not existing, "總開關關咗唔可以復用共用嘅 self._llm"
        assert processor is mock_cls.return_value
        passed_cfg = mock_cls.call_args[0][0]
        assert passed_cfg.enabled is True


# ─── 邊界 / 錯誤路徑 ────────────────────────────────────────


class TestRepolishProcessorEdgeCases:
    """空配置、冇可用服務商、建構失敗等邊界情況要優雅降級，唔可以崩潰或假成功。"""

    def test_no_config_returns_none_gracefully(self):
        """self._config is None → 返回 (None, "")，唔拋異常。"""
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        va._config = None
        va._llm = None

        processor, role = va._build_repolish_processor()

        assert processor is None
        assert role == ""

    def test_construction_failure_returns_none(self):
        """LLMProcessor 建構拋異常 → 優雅返回 None，唔向上拋。"""
        va = _make_app(_off_config(), llm_instance=None)

        with patch("llm.processor.LLMProcessor", side_effect=RuntimeError("boom")):
            processor, role = va._build_repolish_processor()

        assert processor is None
        assert role == ""

    def test_no_available_provider_returns_none(self):
        """真係冇任何可用服務商（providers 全空）→ 返 None，唔好起個唔會用嘅 processor。"""
        va = _make_app(_off_config(providers={}), llm_instance=None)

        processor, _ = va._build_repolish_processor()

        assert processor is None, "冇可用服務商時應如實返 None，唔好靜默貼返原文扮成功"

    def test_run_repolish_reports_unconfigured_when_truly_no_provider(self):
        """真係冇配置任何服務商 → _run_repolish() 應如實顯示「未配置 LLM」，
        唔好貼返原文扮完成。"""
        va = _make_app(_off_config(providers={}), llm_instance=None)
        va._processing_lock = threading.Lock()
        va._is_processing = False
        va._is_repolishing = False
        va._last_result = "原始文字"
        va._last_pre_llm_text = "原始文字"

        with patch.object(va, "_invoke_gui") as mock_gui:
            va._run_repolish()

        statuses = [c.args[1][1] for c in mock_gui.call_args_list if c.args[0] == "set_status"]
        assert "未配置 LLM" in statuses
        assert va._last_result == "原始文字", "唔應該被靜默改成扮潤色成功"


# ─── repolish_provider 覆蓋分支 ──────────────────────────────


class TestRepolishProviderOverrideUnchanged:
    """repolish_provider 有值嗰個分支：沿用原有邏輯，但一樣要強制 enabled=True。"""

    def test_override_builds_temp_processor_with_enabled_forced(self):
        """repolish_provider 有值 → 用該服務商建立 temp processor，且強制 enabled=True。"""
        cfg = _off_config(
            active_provider="bigmodel",
            repolish_provider="deepseek",
            repolish_model="deepseek-chat",
        )
        va = _make_app(cfg, llm_instance=MagicMock(name="existing-llm"))

        with patch("llm.processor.LLMProcessor") as mock_cls:
            mock_cls.return_value = MagicMock(name="temp-processor")
            processor, _ = va._build_repolish_processor()

        assert processor is mock_cls.return_value
        passed_cfg = mock_cls.call_args[0][0]
        assert passed_cfg.active_provider == "deepseek"
        assert passed_cfg.enabled is True, (
            "總開關關咗都要強制 enabled=True，否則 _is_ready() 會擋住"
        )

    def test_override_processor_is_ready_for_real(self):
        """唔 mock LLMProcessor：驗證覆蓋分支起出嚟嘅 processor 真係可用，唔止『非 None』。"""
        cfg = _off_config(repolish_provider="deepseek")
        va = _make_app(cfg, llm_instance=None)

        processor, _ = va._build_repolish_processor()

        assert processor is not None
        assert processor._is_ready() is True

    def test_override_unusable_provider_returns_none(self):
        """repolish_provider 指向唔存在嘅服務商 key → 如實返 None，唔好貼原文扮成功。

        get_active_provider() 對「key 唔存在」冇 fallback-to-other-provider 邏輯
        （同「key 存在但缺 API Key」唔同），所以呢種情況必須喺呢度自己攔截。
        """
        cfg = _off_config(repolish_provider="ghost_provider_that_does_not_exist")
        va = _make_app(cfg, llm_instance=None)

        processor, _ = va._build_repolish_processor()

        assert processor is None

    def test_override_failure_falls_back_to_ad_hoc_processor(self):
        """覆蓋分支建構失敗 → 唔再退返可能唔可用嘅 self._llm，改行
        _make_fallback_repolish_processor() 即場再試一次（用 active_provider）。"""
        cfg = _off_config(repolish_provider="deepseek")
        va = _make_app(cfg, llm_instance=MagicMock(name="existing-llm-should-not-be-used"))

        fallback_processor = MagicMock(name="fallback-processor")
        with patch(
            "llm.processor.LLMProcessor",
            side_effect=[RuntimeError("boom"), fallback_processor],
        ):
            processor, _ = va._build_repolish_processor()

        assert processor is fallback_processor

    def test_override_and_fallback_both_fail_returns_none(self):
        """覆蓋分支同 fallback 都建構失敗 → 優雅返 None，唔向上拋。"""
        cfg = _off_config(repolish_provider="deepseek")
        va = _make_app(cfg, llm_instance=MagicMock(name="existing-llm"))

        with patch("llm.processor.LLMProcessor", side_effect=RuntimeError("boom")):
            processor, _ = va._build_repolish_processor()

        assert processor is None
