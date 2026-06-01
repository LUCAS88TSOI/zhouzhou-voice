"""
DISCOVERY.md 修復回歸測試：
  P1 - ASR 未就緒時 TranscribeTab busy 狀態需正確清理
  P2 - send_and_wait timeout 需包含鎖等待時間
  P3 - AudioConfig 對 None/空字串/非數字的容錯
  P4 - probe_duration 無 ffprobe 時 fallback 到 ffmpeg -i
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── P1: ASR 未就緒 → TranscribeTab 卡死 ─────────────────

def test_p1_files_dropped_asr_none_resets_busy():
    """_asr_process 為 None 時，_on_files_dropped 需呼叫
    on_transcribe_batch_finished 解除 TranscribeTab 忙碌狀態。"""
    from unittest.mock import MagicMock
    from app.app import VoiceApp

    app = object.__new__(VoiceApp)
    app._asr_process = None
    app._config = MagicMock()
    app._is_transcribing = False
    app._transcribing_lock = threading.Lock()  # iter 3 Bug E 新欄位
    mock_window = MagicMock()
    mock_window._tray = MagicMock()
    app._main_window = mock_window

    gui_calls: list[str] = []
    app._invoke_gui = lambda method, *args: gui_calls.append(method)

    app._on_files_dropped(["file1.mp3"], file_cfg=MagicMock())

    assert "on_transcribe_batch_finished" in gui_calls, (
        f"ASR 為 None 時必須呼叫 on_transcribe_batch_finished，實得: {gui_calls}"
    )


def test_p1_files_dropped_asr_not_running_resets_busy():
    """_asr_process.is_running=False 時同樣需要清理。"""
    from unittest.mock import MagicMock
    from app.app import VoiceApp

    app = object.__new__(VoiceApp)
    mock_asr = MagicMock()
    mock_asr.is_running = False
    app._asr_process = mock_asr
    app._config = MagicMock()
    app._is_transcribing = False
    app._transcribing_lock = threading.Lock()  # iter 3 Bug E 新欄位
    mock_window = MagicMock()
    mock_window._tray = MagicMock()
    app._main_window = mock_window

    gui_calls: list[str] = []
    app._invoke_gui = lambda method, *args: gui_calls.append(method)

    app._on_files_dropped(["file1.mp3"], file_cfg=MagicMock())

    assert "on_transcribe_batch_finished" in gui_calls


# ─── P2: ASR timeout 含鎖等待 ────────────────────────────

def test_p2_send_and_wait_timeout_includes_lock_wait():
    """send_and_wait 的 timeout 應包含等待 _call_lock 的時間。

    線程 A 持鎖 1.5s，線程 B 用 timeout=0.5s 呼叫 → 應在 ~1.5s 超時，
    而非鎖釋放後再等 0.5s（共 ~2.0s）。
    """
    import queue as _queue
    from core.asr_process import ASRProcess, ASRRequest

    asr = ASRProcess(model_dir="nonexistent")
    asr._queue_in = _queue.Queue()
    asr._queue_out = _queue.Queue()

    class _FakeProc:
        def is_alive(self):
            return True

    asr._process = _FakeProc()

    lock_held_duration = 1.5

    def hold_lock():
        with asr._call_lock:
            time.sleep(lock_held_duration)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    time.sleep(0.05)  # 確保 holder 已拿到鎖

    timeout = 0.5
    start = time.monotonic()
    timed_out = False
    try:
        req = ASRRequest(task_id="test-timeout", audio_data=b"\x00" * 16)
        asr.send_and_wait(req, timeout=timeout)
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    holder.join(timeout=3)

    assert timed_out, "應該拋出 TimeoutError"
    # 修復後：elapsed ≈ 1.5s（鎖釋放後立即發現 deadline 已過）
    # 修復前：elapsed ≈ 2.0s（鎖釋放後才開始 0.5s timeout）
    assert elapsed < lock_held_duration + timeout - 0.2, (
        f"timeout 含鎖等待時間後 elapsed={elapsed:.2f}s 應 < {lock_held_duration + timeout - 0.2:.1f}s"
    )


# ─── P3: AudioConfig 壞資料容錯 ──────────────────────────

def test_p3_audio_config_none_max_recording():
    """None (JSON null) 不應拋例外。"""
    from utils.config import AudioConfig
    cfg = AudioConfig(max_recording_seconds=None)
    assert cfg.max_recording_seconds >= 1


def test_p3_audio_config_empty_string_segment():
    """空字串不應拋例外。"""
    from utils.config import AudioConfig
    cfg = AudioConfig(segment_seconds="")
    assert cfg.segment_seconds >= 1.0


def test_p3_audio_config_none_threshold():
    from utils.config import AudioConfig
    cfg = AudioConfig(long_audio_threshold=None)
    assert cfg.long_audio_threshold >= 1.0


def test_p3_audio_config_none_overlap():
    from utils.config import AudioConfig
    cfg = AudioConfig(segment_overlap=None)
    assert cfg.segment_overlap >= 0.0


def test_p3_audio_config_non_numeric_string():
    """非數字字串不應拋例外，應退回預設值。"""
    from utils.config import AudioConfig
    cfg = AudioConfig(max_recording_seconds="abc", segment_seconds="xyz")
    assert cfg.max_recording_seconds >= 1
    assert cfg.segment_seconds >= 1.0


# ─── P4: probe_duration ffmpeg -i fallback ────────────────

def test_p4_parse_ffmpeg_duration():
    """_parse_ffmpeg_duration 純函數正確解析 ffmpeg stderr。"""
    from transcribe.file_transcriber import _parse_ffmpeg_duration

    assert abs(_parse_ffmpeg_duration(
        "  Duration: 00:02:30.50, start: 0.000000, bitrate: 128 kb/s"
    ) - 150.5) < 0.01
    assert abs(_parse_ffmpeg_duration(
        "  Duration: 01:00:00.00, start: 0.0"
    ) - 3600.0) < 0.01
    assert _parse_ffmpeg_duration("no duration info here") == 0.0
    assert _parse_ffmpeg_duration("") == 0.0


def test_p4_probe_duration_ffmpeg_fallback():
    """無 ffprobe 但有 ffmpeg 時，用 ffmpeg -i 取時長。"""
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from transcribe import file_transcriber as ft

    ft._reset_ffmpeg_cache()

    fake_stderr = "  Duration: 00:02:30.50, start: 0.000000, bitrate: 128 kb/s\n"
    mock_result = MagicMock()
    mock_result.stderr = fake_stderr
    mock_result.stdout = ""

    with patch("transcribe.file_transcriber.shutil.which", return_value=None), \
         patch("transcribe.file_transcriber.resolve_ffmpeg_path",
               return_value=r"C:\fake\ffmpeg.exe"), \
         patch("transcribe.file_transcriber.subprocess.run",
               return_value=mock_result):
        duration = ft.probe_duration(Path("test.mp4"))

    assert abs(duration - 150.5) < 0.1, f"應為 ~150.5s，實得 {duration}"


def test_p4_probe_duration_none_when_nothing_available():
    """ffprobe 和 ffmpeg 都不可用時返回 0.0。"""
    from pathlib import Path
    from unittest.mock import patch
    from transcribe import file_transcriber as ft

    ft._reset_ffmpeg_cache()

    with patch("transcribe.file_transcriber.shutil.which", return_value=None), \
         patch("transcribe.file_transcriber.resolve_ffmpeg_path",
               return_value=None):
        duration = ft.probe_duration(Path("test.mp4"))

    assert duration == 0.0


def test_p4_probe_duration_prefers_ffprobe():
    """ffprobe 可用時優先使用（原行為不變）。"""
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from transcribe import file_transcriber as ft

    ft._reset_ffmpeg_cache()

    mock_result = MagicMock()
    mock_result.stdout = "150.5\n"

    with patch("transcribe.file_transcriber.shutil.which",
               return_value=r"C:\tools\ffprobe.exe"), \
         patch("transcribe.file_transcriber.subprocess.run",
               return_value=mock_result):
        duration = ft.probe_duration(Path("test.mp4"))

    assert abs(duration - 150.5) < 0.1


if __name__ == "__main__":
    import traceback

    tests = [
        test_p1_files_dropped_asr_none_resets_busy,
        test_p1_files_dropped_asr_not_running_resets_busy,
        test_p2_send_and_wait_timeout_includes_lock_wait,
        test_p3_audio_config_none_max_recording,
        test_p3_audio_config_empty_string_segment,
        test_p3_audio_config_none_threshold,
        test_p3_audio_config_none_overlap,
        test_p3_audio_config_non_numeric_string,
        test_p4_parse_ffmpeg_duration,
        test_p4_probe_duration_ffmpeg_fallback,
        test_p4_probe_duration_none_when_nothing_available,
        test_p4_probe_duration_prefers_ffprobe,
    ]
    failed = 0
    for tcase in tests:
        try:
            tcase()
            print(f"  PASS  {tcase.__name__}")
        except Exception as err:  # noqa: BLE001
            print(f"  FAIL  {tcase.__name__}: {err}")
            traceback.print_exc()
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
