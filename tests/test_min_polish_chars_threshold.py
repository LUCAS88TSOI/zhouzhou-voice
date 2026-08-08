"""
_process_audio() 的 LLM 潤色字數門檻應讀取 LLMConfig.min_polish_chars（可配置），
不再寫死 _MIN_LLM_LENGTH=4。
"""
import dataclasses
import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.app import AsrOutcome, VoiceApp
from utils.config import AppConfig


def _make_va(min_chars: int) -> VoiceApp:
    cfg = AppConfig()
    cfg = dataclasses.replace(
        cfg,
        llm=dataclasses.replace(cfg.llm, enabled=True, min_polish_chars=min_chars),
        output=dataclasses.replace(cfg.output, paste_mode=False),  # 避免測試真的操作剪貼板
    )
    va = object.__new__(VoiceApp)
    va._config = cfg
    va._last_result = ""
    va._last_pre_llm_text = ""
    va._is_processing = False
    va._processing_lock = MagicMock()
    va._main_window = None
    va._asr_process = MagicMock()
    va._asr_process.is_running = True
    va._text_processor = None
    va._hotword = None
    va._llm = MagicMock()
    va._recorder = None
    va._recording_db = None
    return va


def _run_with_text(va: VoiceApp, text: str) -> MagicMock:
    """跑一次 _process_audio，回傳被 patch 的 _try_llm_polish mock 供斷言。"""
    audio = np.zeros(8000, dtype=np.float32).tobytes()
    mock_result = MagicMock(text="潤色後文字", success=True, error="")
    with patch.object(va, "_try_recognize", return_value=AsrOutcome(text=text)):
        with patch.object(va, "_try_llm_polish", return_value=mock_result) as mock_polish:
            va._process_audio(audio)
    return mock_polish


def test_text_below_threshold_skips_llm():
    va = _make_va(min_chars=10)
    mock_polish = _run_with_text(va, "五個字剛好")  # 5 字 < 10
    mock_polish.assert_not_called()
    assert va._last_result == "五個字剛好"


def test_text_at_threshold_calls_llm():
    va = _make_va(min_chars=5)
    mock_polish = _run_with_text(va, "五個字剛好")  # 5 字 == 5
    mock_polish.assert_called_once()


def test_text_one_below_threshold_skips_llm():
    va = _make_va(min_chars=5)
    mock_polish = _run_with_text(va, "四個字啦")  # 4 字 < 5
    mock_polish.assert_not_called()


def test_threshold_twenty_skips_ten_char_text():
    va = _make_va(min_chars=20)
    text = "一二三四五六七八九十"  # 10 字
    mock_polish = _run_with_text(va, text)
    mock_polish.assert_not_called()
    assert va._last_result == text
