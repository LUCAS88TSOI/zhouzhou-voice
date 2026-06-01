"""FFmpeg 路徑解析器測試

驗證混合策略：
1. 系統 PATH 有 ffmpeg → 回傳系統路徑
2. 系統 PATH 沒有 → fallback 到 imageio-ffmpeg
3. 兩者都沒有 → 回傳 None
4. 重複呼叫使用快取，不重複查 PATH
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from transcribe import file_transcriber as ft


@pytest.fixture(autouse=True)
def _clear_cache():
    """每個測試前清空解析快取，避免狀態污染。"""
    ft._reset_ffmpeg_cache()
    yield
    ft._reset_ffmpeg_cache()


def test_resolve_uses_system_ffmpeg_when_in_path():
    """系統 PATH 有 ffmpeg 時，應優先回傳系統路徑。"""
    with patch("transcribe.file_transcriber.shutil.which",
               return_value=r"C:\tools\ffmpeg.exe") as mock_which:
        result = ft.resolve_ffmpeg_path()

    assert result == r"C:\tools\ffmpeg.exe"
    mock_which.assert_called_with("ffmpeg")


def test_resolve_falls_back_to_imageio_when_not_in_path():
    """系統 PATH 沒有 ffmpeg 時，應 fallback 到 imageio-ffmpeg。"""
    fake_bundled = r"C:\fake\imageio\ffmpeg.exe"

    with patch("transcribe.file_transcriber.shutil.which", return_value=None), \
         patch("transcribe.file_transcriber._get_imageio_ffmpeg_exe",
               return_value=fake_bundled):
        result = ft.resolve_ffmpeg_path()

    assert result == fake_bundled


def test_resolve_returns_none_when_both_missing():
    """系統與 imageio-ffmpeg 都沒有時，應回傳 None。"""
    with patch("transcribe.file_transcriber.shutil.which", return_value=None), \
         patch("transcribe.file_transcriber._get_imageio_ffmpeg_exe",
               return_value=None):
        result = ft.resolve_ffmpeg_path()

    assert result is None


def test_resolve_is_cached():
    """重複呼叫應使用快取，不重複查 PATH。"""
    with patch("transcribe.file_transcriber.shutil.which",
               return_value=r"C:\tools\ffmpeg.exe") as mock_which:
        ft.resolve_ffmpeg_path()
        ft.resolve_ffmpeg_path()
        ft.resolve_ffmpeg_path()

    assert mock_which.call_count == 1


def test_check_ffmpeg_true_when_resolvable():
    """check_ffmpeg 有解析結果時回 True。"""
    with patch("transcribe.file_transcriber.shutil.which",
               return_value=r"C:\tools\ffmpeg.exe"):
        assert ft.check_ffmpeg() is True


def test_check_ffmpeg_false_when_not_resolvable():
    """check_ffmpeg 兩者都找不到時回 False。"""
    with patch("transcribe.file_transcriber.shutil.which", return_value=None), \
         patch("transcribe.file_transcriber._get_imageio_ffmpeg_exe",
               return_value=None):
        assert ft.check_ffmpeg() is False


def test_imageio_ffmpeg_exe_returns_none_when_package_missing():
    """_get_imageio_ffmpeg_exe 遇到 ImportError 時應回傳 None，不得拋例外。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "imageio_ffmpeg":
            raise ImportError("simulated missing package")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = ft._get_imageio_ffmpeg_exe()

    assert result is None


def test_extract_audio_pcm_uses_resolved_path():
    """extract_audio_pcm 應呼叫 resolve_ffmpeg_path 取得路徑，而非硬編碼 'ffmpeg'。"""
    from pathlib import Path

    fake_exe = r"C:\fake\bundled\ffmpeg.exe"
    fake_path = Path("dummy.mp3")

    with patch("transcribe.file_transcriber.resolve_ffmpeg_path",
               return_value=fake_exe), \
         patch("transcribe.file_transcriber.subprocess.Popen") as mock_popen:
        mock_popen.return_value = object()
        ft.extract_audio_pcm(fake_path)

    # 驗證 Popen 被呼叫且第一個參數（cmd list）的開頭是解析後的路徑
    assert mock_popen.called
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == fake_exe
