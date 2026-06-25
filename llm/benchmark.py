"""
州州語音 - LLM 批量測速純邏輯

把多個模型的「測試連接」結果排名，方便用戶揀出回應最快的模型。
此模組無 Qt 依賴，純函數可獨立單元測試；GUI 對話框（背景測試、
表格顯示）負責 IO，再呼叫此處排序。

設計原則：
- 不可變：BenchmarkRow 為 frozen dataclass
- 純函數：排序 / 揀最快無副作用
"""

from __future__ import annotations

from dataclasses import dataclass
from math import inf


@dataclass(frozen=True)
class BenchmarkRow:
    """單一模型的測速結果（不可變）。

    Attributes:
        model:   模型名稱
        success: 測試是否成功
        elapsed: 網絡往返耗時（秒）；失敗時為直到失敗為止的耗時
        message: 人類可讀結果訊息（成功回覆預覽或錯誤原因）
    """

    model: str
    success: bool
    elapsed: float
    message: str = ""


def format_elapsed(elapsed: float) -> str:
    """格式化耗時為「X.XX 秒」。"""
    return f"{elapsed:.2f} 秒"


def is_rate_limited(message: str) -> bool:
    """測試結果訊息是否屬「請求過密／限流」（HTTP 429）。"""
    low = (message or "").lower()
    return "429" in low or "過於頻繁" in message or "rate limit" in low


def sort_benchmark_rows(rows: list[BenchmarkRow]) -> list[BenchmarkRow]:
    """按「回應最快」排序：成功者依耗時升序在前，失敗者排最後。

    失敗行的次要鍵統一為 inf（不按耗時排），靠 Python sorted() 的穩定性
    維持原本輸入順序。如日後需「失敗行按耗時排序以診斷最慢端點」，
    須同步更新此鍵與 test_sort_is_stable_for_failures 測試。
    """
    return sorted(
        rows,
        key=lambda r: (not r.success, r.elapsed if r.success else inf),
    )


def fastest_model(rows: list[BenchmarkRow]) -> str | None:
    """回傳回應最快（耗時最短）的成功模型名稱；全部失敗則回 None。"""
    successful = [r for r in rows if r.success]
    if not successful:
        return None
    return min(successful, key=lambda r: r.elapsed).model
