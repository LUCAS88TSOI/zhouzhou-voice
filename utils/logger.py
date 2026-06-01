"""
州州語音 - 日誌系統

提供統一的日誌管理，支持文件輪轉和控制台輸出。
用法：
    from utils.logger import setup_logging, get_logger

    setup_logging()                    # 應用啟動時調用一次
    logger = get_logger("module_name") # 各模組取得自己的日誌器
"""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


# ─── 模組狀態 ──────────────────────────────────────────────

_loggers: Dict[str, logging.Logger] = {}
_log_dir: Optional[Path] = None
_log_level: int = logging.INFO
_initialized: bool = False

# ─── 格式常數 ──────────────────────────────────────────────

_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s "
    "- [%(filename)s:%(lineno)d] - %(message)s"
)
_CONSOLE_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# 每個日誌文件最大 10 MB，保留 5 個備份
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

# 需要抑制的第三方庫日誌
_NOISY_LOGGERS = ("urllib3", "PySide6", "pynput", "httpx", "httpcore")


# ─── 公開 API ──────────────────────────────────────────────

def setup_logging(
    log_dir: Optional[str | Path] = None,
    level: str = "INFO",
) -> None:
    """
    初始化日誌系統。應用啟動時調用一次。

    Args:
        log_dir: 日誌目錄路徑，預設為工作目錄下 logs/
        level: 日誌等級 (DEBUG / INFO / WARNING / ERROR)
    """
    global _log_dir, _log_level, _initialized

    _log_level = getattr(logging, level.upper(), logging.INFO)

    if log_dir is None:
        from utils.paths import LOG_DIR
        _log_dir = LOG_DIR
    else:
        _log_dir = Path(log_dir)
    _log_dir.mkdir(parents=True, exist_ok=True)

    # 抑制第三方庫的噪音日誌
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _initialized = True

    # 為已經存在的日誌器補上文件處理器
    _update_existing_loggers()


def get_logger(name: str) -> logging.Logger:
    """
    取得具名日誌器（單例模式）。

    每個名稱只會創建一次，後續調用返回同一實例。
    若在 setup_logging() 之前調用，日誌只輸出到控制台；
    setup_logging() 之後會自動補上文件處理器。

    Args:
        name: 日誌器名稱（例如 "app"、"asr"、"config"）

    Returns:
        配置好的 Logger 實例
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(f"zhouzhou.{name}")
    logger.setLevel(_log_level)
    logger.propagate = False

    # 控制台處理器（始終添加）
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(console)

    # 文件處理器（僅在 setup_logging 已調用時添加）
    if _initialized and _log_dir is not None:
        logger.addHandler(_create_file_handler(name))

    _loggers[name] = logger
    return logger


# ─── 內部函數 ──────────────────────────────────────────────

def _create_file_handler(name: str) -> logging.Handler:
    """創建旋轉文件處理器。"""
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = _log_dir / f"{name}_{date_str}.log"
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    return handler


def _update_existing_loggers() -> None:
    """為在 setup_logging() 之前創建的日誌器補上文件處理器。"""
    for name, logger in _loggers.items():
        logger.setLevel(_log_level)
        has_file = any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in logger.handlers
        )
        if not has_file and _log_dir is not None:
            logger.addHandler(_create_file_handler(name))
