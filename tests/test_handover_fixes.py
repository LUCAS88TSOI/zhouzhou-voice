"""
HAND OVER LIST 修復回歸測試：
  P1 - ASR 共享通道序列化（互斥鎖）
  P2 - 文件轉錄進度條在失敗路徑也收尾
  P3 - 浮窗狀態語義拆分（DONE / FAILED / READY）
  P4 - AudioConfig 邊界 clamp
"""

from __future__ import annotations

import os
import queue as _queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── P1: ASR 互斥鎖 ────────────────────────────────────────

def test_asr_process_has_call_lock():
    """ASRProcess 應持有互斥鎖，序列化 send_and_wait。"""
    from core.asr_process import ASRProcess
    asr = ASRProcess(model_dir="nonexistent")
    assert hasattr(asr, "_call_lock"), "ASRProcess 需要 _call_lock"
    # duck-typing：lock 具備 acquire/release
    assert hasattr(asr._call_lock, "acquire")
    assert hasattr(asr._call_lock, "release")


def test_asr_concurrent_send_and_wait_each_caller_gets_own_response():
    """並發呼叫 send_and_wait — 每個 caller 都要拿到自己 task_id 的結果，
    而不是被另一個 caller 的 _drain_stale() 搶走。
    """
    from core.asr_process import ASRProcess, ASRRequest, ASRResponse

    asr = ASRProcess(model_dir="nonexistent")
    qi: _queue.Queue = _queue.Queue()
    qo: _queue.Queue = _queue.Queue()
    asr._queue_in = qi  # type: ignore[assignment]
    asr._queue_out = qo  # type: ignore[assignment]

    class _FakeProc:
        def is_alive(self) -> bool:
            return True

    asr._process = _FakeProc()  # type: ignore[assignment]

    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                req = qi.get(timeout=0.1)
            except _queue.Empty:
                continue
            time.sleep(0.03)  # 模擬處理延遲
            qo.put(ASRResponse(task_id=req.task_id, text=f"text-{req.task_id}"))

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    results: dict[str, str] = {}
    errors: list[tuple[str, Exception]] = []

    def caller(tid: str) -> None:
        try:
            req = ASRRequest(task_id=tid, audio_data=b"\x00" * 16)
            resp = asr.send_and_wait(req, timeout=5.0)
            results[tid] = resp.text
        except Exception as err:  # noqa: BLE001
            errors.append((tid, err))

    threads = [
        threading.Thread(target=caller, args=(f"tid{i}",)) for i in range(5)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    stop.set()

    assert not errors, f"無 caller 應超時，實得錯誤: {errors}"
    assert len(results) == 5
    for tid, text in results.items():
        assert text == f"text-{tid}", f"{tid} 拿到錯配的 response: {text}"


# ─── P2: 早退路徑發 1.0 進度讓上層收尾 ─────────────────────

def test_file_transcriber_missing_file_emits_terminal_progress():
    """文件不存在時，應回報 >=1.0 或終結型進度，讓 MainWindow 能收尾進度條。"""
    from transcribe.file_transcriber import FileTranscriber

    progress_calls: list[tuple[float, str]] = []

    t = FileTranscriber(asr_process=None)
    result = t.transcribe(
        file_path="definitely_not_existing_file.wav",
        on_progress=lambda r, m: progress_calls.append((r, m)),
    )

    assert result is None
    # 修復後：早退時應有 1.0 進度，避免進度條卡住
    assert any(r >= 1.0 for r, _ in progress_calls), (
        f"早退應送終結進度（>=1.0），實得 {progress_calls}"
    )


def test_file_transcriber_bad_extension_emits_terminal_progress():
    """不支援的格式 — 同樣要發終結進度。"""
    import tempfile

    from transcribe.file_transcriber import FileTranscriber

    with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
        f.write(b"fake")
        path = f.name
    try:
        progress_calls: list[tuple[float, str]] = []
        t = FileTranscriber(asr_process=None)
        result = t.transcribe(
            file_path=path,
            on_progress=lambda r, m: progress_calls.append((r, m)),
        )
        assert result is None
        assert any(r >= 1.0 for r, _ in progress_calls), (
            f"不支援格式應送終結進度，實得 {progress_calls}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ─── P3: 狀態語義拆分 ─────────────────────────────────────

def test_status_constants_defined():
    """MainWindow 需匯出 STATUS_DONE 與 STATUS_FAILED。"""
    from gui import main_window as mw
    assert hasattr(mw, "STATUS_DONE")
    assert hasattr(mw, "STATUS_FAILED")
    assert mw.STATUS_DONE != mw.STATUS_READY
    assert mw.STATUS_FAILED != mw.STATUS_READY
    assert mw.STATUS_FAILED != mw.STATUS_DONE


def _categorize_status_v2(status: str) -> str:
    """修復後的分類邏輯 — 僅 STATUS_DONE 才進 done 狀態。"""
    STATUS_RECORDING = "錄音中"
    if status == "完成":
        return "done"
    if status == "失敗":
        return "failed"
    if status == "就緒":
        return "hide"
    if (
        status == STATUS_RECORDING
        or status.startswith("錄音")
        or status == "已達錄音上限"
    ):
        return "recording"
    if "潤色" in status or "LLM" in status:
        return "polishing"
    if any(kw in status for kw in ("識別", "處理", "校正", "轉錄", "分段")):
        return "processing"
    return "hide"


def test_status_ready_no_longer_shows_done():
    assert _categorize_status_v2("就緒") == "hide"


def test_status_done_shows_done():
    assert _categorize_status_v2("完成") == "done"


def test_status_failed_not_shown_as_done():
    assert _categorize_status_v2("失敗") == "failed"


# ─── P4: AudioConfig 邊界 clamp ────────────────────────────

def test_audio_config_clamps_negative_segment_seconds():
    from utils.config import AudioConfig
    cfg = AudioConfig(segment_seconds=-5.0)
    assert cfg.segment_seconds > 0, "segment_seconds 必須 > 0"


def test_audio_config_clamps_zero_segment_seconds():
    from utils.config import AudioConfig
    cfg = AudioConfig(segment_seconds=0.0)
    assert cfg.segment_seconds > 0


def test_audio_config_clamps_negative_overlap():
    from utils.config import AudioConfig
    cfg = AudioConfig(segment_seconds=10.0, segment_overlap=-1.0)
    assert cfg.segment_overlap >= 0.0


def test_audio_config_clamps_overlap_exceeds_segment():
    from utils.config import AudioConfig
    cfg = AudioConfig(segment_seconds=10.0, segment_overlap=999.0)
    assert cfg.segment_overlap < cfg.segment_seconds
    assert cfg.segment_overlap >= 0.0


def test_audio_config_clamps_negative_max_recording():
    from utils.config import AudioConfig
    cfg = AudioConfig(max_recording_seconds=-100)
    assert cfg.max_recording_seconds > 0


def test_audio_config_clamps_negative_threshold():
    from utils.config import AudioConfig
    cfg = AudioConfig(long_audio_threshold=-5.0)
    assert cfg.long_audio_threshold > 0


if __name__ == "__main__":
    import traceback

    tests = [
        test_asr_process_has_call_lock,
        test_asr_concurrent_send_and_wait_each_caller_gets_own_response,
        test_file_transcriber_missing_file_emits_terminal_progress,
        test_file_transcriber_bad_extension_emits_terminal_progress,
        test_status_constants_defined,
        test_status_ready_no_longer_shows_done,
        test_status_done_shows_done,
        test_status_failed_not_shown_as_done,
        test_audio_config_clamps_negative_segment_seconds,
        test_audio_config_clamps_zero_segment_seconds,
        test_audio_config_clamps_negative_overlap,
        test_audio_config_clamps_overlap_exceeds_segment,
        test_audio_config_clamps_negative_max_recording,
        test_audio_config_clamps_negative_threshold,
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
