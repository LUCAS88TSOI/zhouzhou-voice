r"""
Windows 開機啟動註冊表管理

提供讀寫 HKCU\Run 註冊表的功能，用於設定開機自動啟動。
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

from utils.paths import APP_ROOT, IS_PACKAGED

APP_NAME = "CCVoice"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command() -> str:
    """組出寫入註冊表 Run 的完整啟動指令（已加引號，含必要參數）。

    - 打包後：sys.executable 即 App 本體，直接使用。
    - 原始碼執行：需「直譯器 + main.py」，並優先用 pythonw.exe 避免開機彈出主控台黑窗。

    修復前只寫入 sys.executable，在原始碼模式下等同裸 python.exe（無腳本參數），
    開機只會彈出一個空的 Python 互動視窗而不啟動本程式。
    """
    if IS_PACKAGED:
        return f'"{Path(sys.executable).resolve()}"'

    interpreter = Path(sys.executable).resolve()
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    return f'"{interpreter}" "{(APP_ROOT / "main.py").resolve()}"'


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
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass  # 已經不存在，忽略
