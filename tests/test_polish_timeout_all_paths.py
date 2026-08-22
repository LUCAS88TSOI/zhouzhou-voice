"""
潤色逾時保護：所有呼叫路徑都必須有上限（v3.9.6 修完 client 層後的殘留卡死）。

v3.9.6 修好咗 client 層「keep-alive 心跳令 should_stop 冇機會被檢查」，
但只有主語音管線傳 `enforce_timeout=True`。實際日誌（2026-08-22）顯示：

    17:05:40  LLM 處理開始: 角色=泰翻 …   ← 冇「處理完成」，永遠卡住
    17:11:34  LLM 處理開始: 角色=泰翻 …   ← 同樣卡住

而同一時段主管線（角色=預設潤色）準時喺 10.00s 中止。分別就係重新潤色／
潤色選取／歷史重處理／檔案轉譯呢五個呼叫點全部用預設 `enforce_timeout=False`
—— `should_stop=None` + `request_timeout=None` → client 走預設 30s read
timeout，而心跳令每次 socket read 都成功，read timeout 永遠唔會觸發 →
**無限卡死**，卡死後 `_is_repolishing` 永遠 True，重新潤色功能直到重啟為止報廢。

呢個檔案鎖三層防線：
  1. 呼叫層：每個 `_try_llm_polish()` 呼叫點都套 polish_timeout（批次路徑放大倍數）
  2. Client 層：即使呼叫端完全冇傳 should_stop，串流總時長仍有硬上限
  3. 連線衛生：任何提早中止（含外層 break）都要 close() 而唔係放返連線池
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import llm.client as client_mod
from llm.client import LLMClient
from llm.processor import LLMResultStatus
from llm.provider import ProviderInfo
from utils.config import AppConfig

# 測試用假金鑰（變數形式，避免被 pre-commit secret 掃描誤報）
_FAKE_KEY = "fake-test-api-key"


# ─── 共用 fixture ──────────────────────────────────────────

class _CapturingProcessor:
    """假 LLMProcessor：記錄 process() 收到的逾時相關參數。"""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def process(self, **kwargs):
        from llm.processor import LLMResult

        self.kwargs = kwargs
        return LLMResult(text="潤色後")


def _app():
    from app.app import VoiceApp
    from dataclasses import replace

    app = object.__new__(VoiceApp)
    # paste_mode 預設 True：唔關就會真係寫用戶剪貼板 + 向前景視窗送 Ctrl+V
    # （_invoke_gui 係 no-op，例外全被吞，副作用完全隱形）
    cfg = AppConfig()
    app._config = replace(cfg, output=replace(cfg.output, paste_mode=False))
    app._processing_lock = threading.Lock()
    app._is_processing = False
    app._is_repolishing = False
    app._recorder = None
    app._hotword = None
    app._llm = None
    app._target_hwnd = None
    app._last_result = "上一次的結果"
    app._last_pre_llm_text = "上一次的結果"
    app._invoke_gui = lambda *a: None
    app._shutting_down = False
    return app


# ─── 第 1 層：每個呼叫點都要套逾時 ──────────────────────────

def test_try_llm_polish_enforces_timeout_by_default():
    """預設就要套 polish_timeout —— 唔可以再有『忘記傳旗標＝無限等』。"""
    app = _app()
    proc = _CapturingProcessor()

    result = app._try_llm_polish("測試文字", llm_processor=proc)

    assert result.text == "潤色後"
    assert proc.kwargs.get("should_stop") is not None, (
        "預設呼叫冇傳 should_stop：keep-alive 心跳會令呢條路徑永遠卡死"
    )
    assert proc.kwargs.get("request_timeout") == app._config.llm.polish_timeout


def test_try_llm_polish_scales_timeout_for_batch_paths():
    """檔案轉譯逐段潤色輸出長，要更大預算 —— 但仍然必須有限。"""
    app = _app()
    proc = _CapturingProcessor()

    app._try_llm_polish("長逐字稿", llm_processor=proc, timeout_scale=6.0)

    assert proc.kwargs["request_timeout"] == app._config.llm.polish_timeout * 6.0
    assert proc.kwargs["should_stop"] is not None


def test_repolish_call_site_passes_timeout():
    """重新潤色（F2）：實際卡死嘅路徑，必須帶逾時。"""
    app = _app()
    proc = _CapturingProcessor()
    app._build_repolish_processor = lambda: (proc, "")

    app._run_repolish()

    assert proc.kwargs.get("should_stop") is not None, (
        "重新潤色路徑冇逾時保護（日誌 17:05:40／17:11:34 卡死點）"
    )
    assert proc.kwargs.get("request_timeout")


def test_history_reprocess_call_site_passes_timeout():
    """錄音歷史重新處理：同樣係用戶等結果嘅互動路徑。"""
    app = _app()
    proc = _CapturingProcessor()
    app._llm = proc

    record = mock.MagicMock()
    record.asr_text = "原始文字"
    app._recording_db = mock.MagicMock()
    app._recording_db.get_by_id.return_value = record

    app._run_history_reprocess(1, "default")

    assert proc.kwargs.get("should_stop") is not None
    assert proc.kwargs.get("request_timeout")


def test_file_polish_call_site_uses_scaled_timeout():
    """檔案轉譯潤色：用放大倍數，但唔可以係無限。"""
    app = _app()
    captured: dict = {}

    def fake_polish(text, **kwargs):
        captured.update(kwargs)
        return LLMResultStatus(success=True, text=text, was_processed=True)

    app._try_llm_polish = fake_polish
    app._polish_transcription_text("短文本", mock.MagicMock())

    scale = captured.get("timeout_scale")
    assert scale is not None and scale > 1.0, (
        "檔案轉譯路徑冇傳 timeout_scale，會退回無限等"
    )


def test_chunked_polish_helper_bounds_each_chunk():
    """逐段潤色 helper：每段都要有獨立逾時，一段卡死唔可以拖死成個轉譯。"""
    from transcribe.file_transcriber import polish_transcription_with_context
    from llm.processor import RoleConfig

    seen: list[dict] = []

    class _Proc:
        def process(self, **kwargs):
            from llm.processor import LLMResult

            seen.append(kwargs)
            return LLMResult(text="潤色後")

    polish_transcription_with_context(
        ["第一段", "第二段"],
        _Proc(),
        RoleConfig(name="潤色", system_prompt="潤色"),
        request_timeout=30.0,
    )

    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs.get("should_stop") is not None, (
            "逐段潤色冇逾時：一段撞到 keep-alive 就永遠卡住成個檔案轉譯"
        )
        assert kwargs.get("request_timeout") == 30.0
    # 每段各自計時（唔係共用一個全域截止時間，否則後面段會被誤殺）
    assert seen[0]["should_stop"] is not seen[1]["should_stop"]


def test_chunked_polish_falls_back_to_raw_on_timeout():
    """逾時中止的半截潤色唔可以照收 —— 用戶後半段講嘅話會憑空消失。"""
    from transcribe.file_transcriber import polish_transcription_with_context
    from llm.processor import RoleConfig

    class _StoppedProc:
        def process(self, **kwargs):
            from llm.processor import LLMResult

            return LLMResult(text="潤色到一半就", was_stopped=True)

    out = polish_transcription_with_context(
        ["完整原文一二三"],
        _StoppedProc(),
        RoleConfig(name="潤色", system_prompt="潤色"),
        request_timeout=30.0,
    )

    assert out == ["完整原文一二三"], "半截潤色被當成成功結果收下"


def test_polish_selection_call_site_passes_timeout():
    """潤色選取文字：長選取最易逾時，必須帶預算（且失敗唔可以覆寫選取內容）。"""
    app = _app()
    proc = _CapturingProcessor()
    app._build_repolish_processor = lambda: (proc, "")
    app._target_hwnd = None

    with mock.patch(
        "utils.clipboard.ClipboardManager.capture_selection",
        return_value="選取的文字",
    ):
        app._run_polish_selection()

    assert proc.kwargs.get("should_stop") is not None
    assert proc.kwargs.get("request_timeout")


def test_polish_selection_timeout_does_not_overwrite_selection():
    """逾時時貼返一模一樣的純文字，只會摧毀用戶選取內容的格式，零收益。

    其他失敗（API／網絡）維持既有「貼返原文、唔靜默中斷」行為，見
    tests/test_polish_selection_worker.py::test_llm_failure_shows_warning。
    """
    app = _app()
    app._build_repolish_processor = lambda: (mock.MagicMock(), "")
    app._target_hwnd = None
    app._try_llm_polish = lambda *a, **k: LLMResultStatus(
        success=False, text="選取的文字", was_processed=True, error="潤色逾時",
    )

    with mock.patch(
        "utils.clipboard.ClipboardManager.capture_selection",
        return_value="選取的文字",
    ), mock.patch(
        "utils.clipboard.ClipboardManager.paste_text", return_value=True,
    ) as paste:
        app._run_polish_selection()

    paste.assert_not_called()


def test_long_text_gets_bigger_budget_than_short_text():
    """長文本要自動放寬預算，否則長選取／長逐字稿 100% 逾時失敗。"""
    app = _app()

    short = app._polish_timeout_for("你好")
    long = app._polish_timeout_for("字" * 2000)

    assert short == app._config.llm.polish_timeout
    assert long > short, "長文本仍然只得設定值嘅預算，必定逾時"
    assert long <= app._POLISH_TIMEOUT_MAX, "預算冇絕對上限"


def test_zero_polish_timeout_is_not_unlimited():
    """polish_timeout=0（舊註解寫「不限制」）唔可以退化成無限等。"""
    from dataclasses import replace

    app = _app()
    app._config = replace(
        app._config, llm=replace(app._config.llm, polish_timeout=0.0),
    )

    budget = app._polish_timeout_for("你好")
    assert budget == app._POLISH_TIMEOUT_UNLIMITED
    assert 0 < budget <= app._POLISH_TIMEOUT_MAX


def test_bad_polish_timeout_in_config_does_not_break_polish():
    """手改壞的 config（null／字串）唔可以令每一次潤色都拋 TypeError。"""
    from utils.config import LLMConfig

    assert LLMConfig(polish_timeout=None).polish_timeout == 10.0  # type: ignore[arg-type]
    assert LLMConfig(polish_timeout="oops").polish_timeout == 10.0  # type: ignore[arg-type]


def test_chunked_file_polish_passes_request_timeout():
    """長文本走 polish_transcription_with_context 那條分支同樣要帶預算。"""
    app = _app()
    app._llm = _CapturingProcessor()
    app._invoke_gui = lambda *a: None

    captured: dict = {}

    def fake_helper(**kwargs):
        captured.update(kwargs)
        return ["潤色後"]

    with mock.patch(
        "transcribe.file_transcriber.polish_transcription_with_context",
        fake_helper,
    ):
        app._polish_chunked("句子。" * 800, 1500)

    assert captured.get("request_timeout"), "長文本分支冇傳 request_timeout"
    assert captured["request_timeout"] <= app._POLISH_TIMEOUT_MAX


# ─── 第 2 層：client 硬上限（呼叫端完全冇傳 should_stop）────

class _InfiniteKeepAlive:
    """永遠只送 SSE 心跳行、從不送內容的假回應（真實服務商的思考階段）。"""

    def __init__(self) -> None:
        self.status = 200
        self.consumed = 0
        self.released = False
        self.closed = False

    def __iter__(self):
        while True:
            self.consumed += 1
            if self.consumed > 100_000:  # 防止修復失敗時測試真的無限跑
                raise AssertionError(
                    "client 冇硬上限：純 keep-alive 串流讀足 10 萬行仍未中止"
                )
            yield b": ping\n"

    def release_conn(self) -> None:
        self.released = True

    def close(self) -> None:
        self.closed = True


def _make_client(timeout: int = 10) -> LLMClient:
    provider = ProviderInfo(
        key="test",
        name="test",
        api_url="https://api.example.com/v1",
        api_key=_FAKE_KEY,
        model="fake-model",
        enabled=True,
    )
    return LLMClient(provider, timeout=timeout)


def test_client_aborts_keepalive_stream_without_should_stop(monkeypatch):
    """最後防線：冇 should_stop 時，串流總時長仍受硬上限約束。

    read timeout 只約束「單次 socket read」；心跳令每次 read 都成功，
    所以總時長可以無限 —— 必須另有 wall-clock 上限。
    """
    fake_response = _InfiniteKeepAlive()
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    # 模擬時間流逝：每次讀取推進 1 秒（不真的等）。
    # 注入假的 time 模組而唔係 setattr(client_mod.time, ...) —— 後者會全進程
    # 換掉 time.monotonic，任何背景 thread／logging 都會觀測到並推進假時鐘。
    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(
        client_mod, "time", SimpleNamespace(monotonic=fake_monotonic),
    )

    client = _make_client(timeout=10)
    meta: dict = {}
    gen, _warnings = client.chat_with_warnings(
        [{"role": "user", "content": "hi"}], stream=True, meta=meta,
    )
    chunks = list(gen)

    assert chunks == []
    assert meta.get("stopped") is True, "硬上限到期未標記 stopped"
    # 中止後連線要關掉，唔可以將未讀完的髒連線放返池
    assert fake_response.closed is True
    assert fake_response.released is False


def test_client_hard_budget_does_not_kill_normal_stream(monkeypatch):
    """硬上限唔可以誤殺正常串流（回歸保護）。"""

    class _Normal:
        def __init__(self) -> None:
            self.status = 200
            self.released = False
            self.closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
            yield b"data: [DONE]\n"

        def release_conn(self) -> None:
            self.released = True

        def close(self) -> None:
            self.closed = True

    fake_response = _Normal()
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    client = _make_client(timeout=10)
    meta: dict = {}
    gen, _warnings = client.chat_with_warnings(
        [{"role": "user", "content": "hi"}], stream=True, meta=meta,
    )

    assert list(gen) == ["hi"]
    assert meta.get("stopped") is None
    assert fake_response.released is True
    assert fake_response.closed is False


# ─── 真 urllib3 行為：假物件測唔到的那一層 ──────────────────

def test_real_urllib3_response_without_newline_still_aborts():
    r"""對端持續送「唔含換行」的 bytes 時必須中止 —— 唔可以無限卡死。

    上面的假物件都係逐行 yield，繞過咗 urllib3 `HTTPResponse.__iter__` 的
    內部緩衝：它見唔到 `\n` 就淨係 append 唔 yield，令迴圈本體永遠唔執行、
    deadline 同 should_stop 一次都檢查唔到。呢個測試用真 urllib3 回應把
    嗰一層鎖住 —— 唔止防惡意端點，SSE 規範容許裸 `\r`，合法服務商都會踩中。
    """
    import urllib3

    class _NoNewlineForever(io.RawIOBase):
        def __init__(self) -> None:
            self.reads = 0

        def readable(self) -> bool:
            return True

        def read(self, amt=-1):
            self.reads += 1
            if self.reads > 500_000:
                raise AssertionError("讀足 50 萬次仍未中止 —— 硬上限失效")
            return b"x" * 64  # 永遠冇換行

    response = urllib3.HTTPResponse(
        body=_NoNewlineForever(), preload_content=False, headers={}, status=200,
    )

    meta: dict = {}
    started = time.monotonic()
    chunks = list(
        LLMClient._parse_sse_stream(
            response, meta, None, deadline=time.monotonic() + 0.5,
        )
    )
    elapsed = time.monotonic() - started

    assert chunks == []
    assert meta.get("stopped") is True, "無換行資料流未被標記中止"
    assert elapsed < 15.0, f"耗時 {elapsed:.1f}s —— 中止唔夠及時"


def test_sse_lines_separated_by_bare_cr_are_parsed():
    r"""SSE 規範容許裸 `\r` 做行結束符，唔可以解析唔到內容。"""

    class _CrSeparated:
        status = 200

        def __iter__(self):
            yield (
                b'data: {"choices":[{"delta":{"content":"\xe5\x89\x8d"}}]}\r'
                b'data: {"choices":[{"delta":{"content":"\xe5\xbe\x8c"}}]}\r'
                b"data: [DONE]\r"
            )

        def release_conn(self) -> None:
            pass

        def close(self) -> None:
            pass

    assert list(LLMClient._parse_sse_stream(_CrSeparated())) == ["前", "後"]


def test_crlf_is_treated_as_single_separator():
    r"""`\r\n` 唔可以當成兩個分隔符（否則會多出空行，雖然無害但語意錯）。"""

    class _Crlf:
        status = 200

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\r\n'
            yield b"data: [DONE]\r\n"

        def release_conn(self) -> None:
            pass

        def close(self) -> None:
            pass

    assert list(LLMClient._parse_sse_stream(_Crlf())) == ["hi"]


# ─── 第 3 層：外層 break 也要關連線 ─────────────────────────

def test_outer_break_closes_connection_not_release(monkeypatch):
    """processor 收到內容後才 should_stop → 外層 break，連線同樣未讀完。

    舊行為：generator 被 GC 觸發 GeneratorExit → finally 走 release_conn()，
    把「仲有未讀 SSE bytes」的髒連線放返池，下個請求可能解析到殘留內容。
    """

    class _ContentThenForever:
        def __init__(self) -> None:
            self.status = 200
            self.released = False
            self.closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"aaa"}}]}\n'
            for _ in range(10_000):
                yield b'data: {"choices":[{"delta":{"content":"bbb"}}]}\n'

        def release_conn(self) -> None:
            self.released = True

        def close(self) -> None:
            self.closed = True

    fake_response = _ContentThenForever()
    monkeypatch.setattr(
        client_mod._POOL_MANAGER, "urlopen", lambda *a, **k: fake_response,
    )

    from llm.processor import LLMProcessor, RoleConfig

    provider = ProviderInfo(
        key="test", name="test", api_url="https://api.example.com/v1",
        api_key=_FAKE_KEY, model="fake-model", enabled=True,
    )
    monkeypatch.setattr("llm.processor.get_active_provider", lambda cfg: provider)
    monkeypatch.setattr(
        "llm.processor.list_available_providers", lambda cfg: [provider],
    )

    class _Cfg:
        max_tokens = 1024
        temperature = 0.3
        top_p = 1.0
        frequency_penalty = 0.0
        presence_penalty = 0.0
        do_sample = True
        enabled = True
        allow_provider_failover = False

    processor = LLMProcessor(_Cfg())

    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3  # 收到內容 chunk 之後才觸發 → 走外層 break

    result = processor.process(
        text="測試文字",
        role=RoleConfig(name="潤色", system_prompt="潤色一下"),
        should_stop=should_stop,
        request_timeout=10,
    )

    assert result.was_stopped is True
    assert fake_response.closed is True, (
        "外層 break 後連線被 release_conn() 放返池：髒連線會污染下個請求"
    )
    assert fake_response.released is False
