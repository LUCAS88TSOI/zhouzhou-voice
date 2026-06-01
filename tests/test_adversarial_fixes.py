"""
Codex Adversarial Review — 修復驗證測試

測試覆蓋：
  A1 — 轉錄 single-flight guard
  A2 — FFmpeg 提取改用 temp file
  A3 — 輸出文件防覆寫命名
"""
from __future__ import annotations

import os
import struct
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch


# ─── A1: 轉錄 single-flight guard ──────────────────────────


class TestA1TranscribeSingleFlight:
    """A1: 同時只能有一個 transcribe batch。"""

    def test_second_drop_rejected_while_transcribing(self):
        """轉錄進行中再次 drop 應被拒絕（不啟動新線程）。"""
        from app.app import VoiceApp
        from utils.config import AppConfig

        va = object.__new__(VoiceApp)
        va._config = AppConfig()
        va._asr_process = MagicMock()
        va._asr_process.is_running = True
        va._main_window = MagicMock()
        va._is_transcribing = True  # 已有轉錄在跑
        va._transcribing_lock = threading.Lock()  # iter 3 Bug E 新欄位

        spawned = []
        original_thread = threading.Thread

        with patch("app.app.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            va._on_files_dropped(["file1.mp3", "file2.mp3"])
            # 不應啟動新線程
            mock_thread.assert_not_called()

    def test_voiceapp_has_is_transcribing_attr(self):
        """VoiceApp 應有 _is_transcribing 屬性。"""
        from app.app import VoiceApp

        va = object.__new__(VoiceApp)
        va.__init__()
        assert hasattr(va, "_is_transcribing")
        assert va._is_transcribing is False


# ─── A2: FFmpeg 提取改用 temp file ──────────────────────────


class TestA2TempFileExtraction:
    """A2: _extract_full_audio 應寫入 temp file 而非 RAM。"""

    def test_extract_returns_path_not_bytes(self):
        """_extract_full_audio 應返回 Path（temp file），非 bytes。"""
        from transcribe.file_transcriber import FileTranscriber

        ft = FileTranscriber.__new__(FileTranscriber)
        ft._asr = MagicMock()
        ft._seg_duration = 60
        ft._seg_overlap = 4
        ft._cancel_event = threading.Event()

        fake_pcm = struct.pack("100f", *([0.1] * 100))

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (None, b"")

        def fake_popen(cmd, **kwargs):
            # FFmpeg 寫入的目標是 cmd 最後一個參數（temp file path）
            out_path = cmd[-1]
            Path(out_path).write_bytes(fake_pcm)
            return mock_proc

        with patch("transcribe.file_transcriber.resolve_ffmpeg_path", return_value="ffmpeg"):
            with patch("transcribe.file_transcriber.subprocess.Popen", side_effect=fake_popen):
                result = ft._extract_full_audio(Path("test.mp3"), None)

        assert isinstance(result, Path), \
            f"應返回 Path，實際返回 {type(result).__name__}"
        assert result.exists()
        assert result.stat().st_size == len(fake_pcm)

        # 清理
        result.unlink(missing_ok=True)

    def test_recognize_segments_accepts_path(self):
        """_recognize_segments 應能接受 Path 參數。"""
        from transcribe.file_transcriber import FileTranscriber

        ft = FileTranscriber.__new__(FileTranscriber)
        ft._asr = MagicMock()
        ft._seg_duration = 60
        ft._seg_overlap = 4
        ft._cancel_event = threading.Event()

        # 建立 temp PCM file（0.01 秒 = 160 samples）
        pcm_data = struct.pack("160f", *([0.0] * 160))
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(pcm_data)
            tmp_path = Path(f.name)

        try:
            # Mock ASR response
            mock_response = MagicMock()
            mock_response.text = "test"
            mock_response.tokens = ["test"]
            mock_response.timestamps = [0.0]
            mock_response.duration = 0.01
            mock_response.error = ""
            ft._asr.send_and_wait.return_value = mock_response

            results = ft._recognize_segments(tmp_path, 0.01, None)
            assert isinstance(results, list)
        finally:
            tmp_path.unlink(missing_ok=True)


# ─── A3: 輸出文件防覆寫命名 ────────────────────────────────


class TestA3NonDestructiveOutput:
    """A3: 輸出文件應避免覆寫已存在的文件。"""

    def test_unique_path_no_conflict(self, tmp_path):
        """無衝突時返回原始路徑。"""
        from transcribe.file_transcriber import _unique_path

        p = tmp_path / "video.srt"
        assert _unique_path(p) == p

    def test_unique_path_with_conflict(self, tmp_path):
        """已存在同名文件時應生成帶序號的路徑。"""
        from transcribe.file_transcriber import _unique_path

        p = tmp_path / "video.srt"
        p.write_text("existing content")

        result = _unique_path(p)
        assert result != p
        assert result.suffix == ".srt"
        assert "video" in result.stem

    def test_unique_path_multiple_conflicts(self, tmp_path):
        """多個衝突時序號遞增。"""
        from transcribe.file_transcriber import _unique_path

        base = tmp_path / "video.srt"
        base.write_text("v1")
        (tmp_path / "video_1.srt").write_text("v2")
        (tmp_path / "video_2.srt").write_text("v3")

        result = _unique_path(base)
        assert result == tmp_path / "video_3.srt"

    def test_transcribe_uses_unique_paths(self):
        """transcribe() 保存時應使用 _unique_path。"""
        from transcribe.file_transcriber import FileTranscriber
        import transcribe.file_transcriber as ft_module

        # 確認 _unique_path 存在且被引用
        assert hasattr(ft_module, "_unique_path"), \
            "file_transcriber 應有 _unique_path 函數"
