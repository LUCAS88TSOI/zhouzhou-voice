"""
Windows 開機啟動註冊表管理

提供讀寫 HKCU\Run 註冊表的功能，用於設定開機自動啟動。
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

APP_NAME = "CCVoice"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_startup_enabled() -> bool:
    """
    檢查是否已註冊開機啟動。

    Returns:
        True 如果註冊表中有 CC語音 的啟動項
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ
        ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except (FileNotFoundError, OSError):
        return False


def set_startup(enable: bool) -> None:
    """
    設定或取消開機自動啟動。

    Args:
        enable: True 啟用開機啟動，False 取消
    """
    exe_path = Path(sys.executable).resolve()

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, str(exe_path))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass  # 已經不存在，忽略
