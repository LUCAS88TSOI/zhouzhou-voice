"""
州州語音 - 單一入口

Windows 離線語音輸入工具。
雙擊此文件或執行 python main.py 啟動應用。
"""

import ctypes
import multiprocessing

from app.app import VoiceApp

_MUTEX_NAME = "Global\\ZhouZhouVoice_SingleInstance"


def _ensure_single_instance() -> bool:
    """確保只有一個實例在運行。返回 True 表示可以繼續。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        ctypes.windll.user32.MessageBoxW(
            None,
            "州州語音已在運行中。\n請先關閉已開啟的程序再嘗試。",
            "州州語音",
            0x40,  # MB_ICONINFORMATION
        )
        return False
    # 不關閉 handle，讓 Mutex 隨進程存活，退出時自動釋放
    return True


def main():
    if not _ensure_single_instance():
        return
    app = VoiceApp()
    app.run()


if __name__ == "__main__":
    # Nuitka 打包和 Windows 子進程必需
    multiprocessing.freeze_support()
    main()
