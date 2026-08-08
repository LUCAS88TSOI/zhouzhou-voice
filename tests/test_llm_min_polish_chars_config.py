"""
LLMConfig.min_polish_chars —— 語音識別後字數門檻設定。

字數未達門檻直接輸出 ASR 原文，跳過 LLM 潤色（沿用舊硬編碼 _MIN_LLM_LENGTH=4
的預設值，向下相容）。不設上限 clamp（用戶要求「無上限」），只做下限防禦，
仿 AudioConfig 手法防止壞 config.json 造成崩潰。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config import LLMConfig


def test_default_is_four():
    assert LLMConfig().min_polish_chars == 4


def test_accepts_custom_value():
    assert LLMConfig(min_polish_chars=20).min_polish_chars == 20


def test_no_upper_clamp():
    assert LLMConfig(min_polish_chars=999999).min_polish_chars == 999999


def test_negative_clamps_to_one():
    assert LLMConfig(min_polish_chars=-5).min_polish_chars == 1


def test_zero_clamps_to_one():
    assert LLMConfig(min_polish_chars=0).min_polish_chars == 1


def test_bad_string_falls_back_to_default():
    assert LLMConfig(min_polish_chars="abc").min_polish_chars == 4


def test_none_falls_back_to_default():
    assert LLMConfig(min_polish_chars=None).min_polish_chars == 4


def test_infinity_falls_back_to_default():
    """code review MEDIUM-3：int(float('inf')) 拋 OverflowError，
    原本只 catch (TypeError, ValueError) 冇接住，壞 config.json 寫入
    Infinity（json 合法字面值）會令啟動崩潰。"""
    assert LLMConfig(min_polish_chars=float("inf")).min_polish_chars == 4
