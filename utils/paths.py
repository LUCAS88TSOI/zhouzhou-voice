"""
州州語音 - 統一路徑解析

偵測打包環境（Nuitka / PyInstaller）並提供穩定的路徑常數。
所有模組應從此處 import 路徑，避免各自計算。

注意：此模組不可 import config.py 或 logger.py，避免循環依賴。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _is_packaged() -> bool:
    """偵測是否在打包環境中運行（Nuitka / PyInstaller）。"""
    # Nuitka: __compiled__ 存在於已編譯模組的 globals
    # PyInstaller: sys.frozen = True
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def _get_app_root() -> Path:
    """取得應用根目錄。"""
    if _is_packaged():
        # 打包後：exe 所在的 .dist 資料夾
        return Path(sys.executable).resolve().parent
    # 開發模式：utils/paths.py → 上兩層 = 專案根目錄
    return Path(__file__).resolve().parent.parent


IS_PACKAGED: bool = _is_packaged()

APP_ROOT: Path = _get_app_root()
MODELS_DIR: Path = APP_ROOT / "models"
ASSETS_DIR: Path = APP_ROOT / "assets"
ICON_PATH: Path = ASSETS_DIR / "icon.ico"

# 使用者資料目錄（%APPDATA%/zhouzhou-voice/）
# 直接計算，不 import config.py 避免循環依賴
APP_DATA_DIR: Path = Path.home() / "AppData" / "Roaming" / "zhouzhou-voice"
LOG_DIR: Path = APP_DATA_DIR / "logs"

# 版本號（單一來源：根目錄 VERSION 文件）
APP_VERSION: str = (APP_ROOT / "VERSION").read_text("utf-8").strip() if (APP_ROOT / "VERSION").exists() else "0.0.0"
