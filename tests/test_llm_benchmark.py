"""
llm.benchmark 純邏輯測試（排名 / 格式化 / 揀最快）。

批量測速的核心：把多個模型的測試結果按「回應最快」排序，
成功者依耗時升序在前，失敗者排最後；並能挑出最快的成功模型。
無 Qt 依賴，純函數可獨立測試。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.benchmark import (
    BenchmarkRow,
    fastest_model,
    format_elapsed,
    is_rate_limited,
    sort_benchmark_rows,
)


# ─── format_elapsed ───────────────────────────────────────

def test_format_elapsed_two_decimals():
    assert format_elapsed(1.834) == "1.83 秒"


def test_format_elapsed_zero():
    assert format_elapsed(0.0) == "0.00 秒"


# ─── sort_benchmark_rows ──────────────────────────────────

def test_sort_puts_fastest_success_first():
    rows = [
        BenchmarkRow(model="slow", success=True, elapsed=3.0),
        BenchmarkRow(model="fast", success=True, elapsed=0.5),
        BenchmarkRow(model="mid", success=True, elapsed=1.5),
    ]
    ordered = [r.model for r in sort_benchmark_rows(rows)]
    assert ordered == ["fast", "mid", "slow"]


def test_sort_puts_failures_last_regardless_of_elapsed():
    rows = [
        BenchmarkRow(model="failfast", success=False, elapsed=0.1),
        BenchmarkRow(model="ok", success=True, elapsed=2.0),
    ]
    ordered = [r.model for r in sort_benchmark_rows(rows)]
    assert ordered == ["ok", "failfast"]


def test_sort_is_stable_for_failures():
    # 失敗者之間維持原本出現順序（穩定排序）
    rows = [
        BenchmarkRow(model="f1", success=False, elapsed=0.1),
        BenchmarkRow(model="f2", success=False, elapsed=0.2),
    ]
    ordered = [r.model for r in sort_benchmark_rows(rows)]
    assert ordered == ["f1", "f2"]


# ─── fastest_model ────────────────────────────────────────

def test_fastest_model_returns_quickest_success():
    rows = [
        BenchmarkRow(model="a", success=True, elapsed=2.0),
        BenchmarkRow(model="b", success=True, elapsed=0.7),
        BenchmarkRow(model="c", success=False, elapsed=0.1),
    ]
    assert fastest_model(rows) == "b"


def test_fastest_model_none_when_all_fail():
    rows = [
        BenchmarkRow(model="a", success=False, elapsed=0.1),
        BenchmarkRow(model="b", success=False, elapsed=0.2),
    ]
    assert fastest_model(rows) is None


def test_fastest_model_none_for_empty():
    assert fastest_model([]) is None


# ─── is_rate_limited ──────────────────────────────────────

def test_rate_limited_detects_429():
    assert is_rate_limited("HTTP 429 錯誤（詳見日誌）") is True


def test_rate_limited_detects_chinese_phrase():
    assert is_rate_limited("請求過於頻繁（HTTP 429）：請稍後重試") is True


def test_rate_limited_detects_english_phrase():
    assert is_rate_limited("Error: rate limit exceeded") is True


def test_rate_limited_false_for_success():
    assert is_rate_limited("連接成功！模型回應：Hi") is False


def test_rate_limited_false_for_empty():
    assert is_rate_limited("") is False
