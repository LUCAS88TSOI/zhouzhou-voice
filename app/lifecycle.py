"""
州州語音 - 生命週期管理

處理應用的啟動、關閉和系統信號。
提供統一的關閉入口和 LIFO 清理回調機制。

用法：
    lifecycle = LifecycleManager()
    lifecycle.initialize()
    lifecycle.register_shutdown(cleanup_fn)
    # ... 應用運行 ...
    lifecycle.request_shutdown("用戶退出")
    lifecycle.cleanup()
"""

from __future__ import annotations

import atexit
import signal
import sys
import time
from typing import Callable, List

from utils.logger import get_logger

logger = get_logger("lifecycle")


class LifecycleManager:
    """
    應用生命週期管理器。

    職責：
    - 註冊和執行關閉回調（LIFO 順序，後註冊的先執行）
    - 處理系統信號（SIGINT / SIGTERM，雙擊確認退出）
    - 提供統一的關閉入口
    - atexit 安全網，確保資源一定被清理
    """

    def __init__(self) -> None:
        self._shutdown_callbacks: List[Callable] = []
        self._is_shutting_down: bool = False
        self._is_cleaned_up: bool = False
        self._last_signal_time: float = 0.0

    # ─── 公開 API ──────────────────────────────────────────

    def initialize(self) -> None:
        """
        初始化生命週期管理。
        註冊 SIGINT/SIGTERM 處理器和 atexit 安全網。
        """
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        atexit.register(self.cleanup)
        logger.info("生命週期管理器已初始化")

    def register_shutdown(self, callback: Callable) -> None:
        """
        註冊關閉回調。回調以 LIFO（後進先出）順序執行。

        Args:
            callback: 無參數的可調用對象
        """
        name = _get_callable_name(callback)
        self._shutdown_callbacks.append(callback)
        logger.debug("已註冊關閉回調: %s", name)

    def unregister_shutdown(self, callback: Callable) -> bool:
        """
        移除已註冊的關閉回調。

        Args:
            callback: 要移除的可調用對象

        Returns:
            True 如果成功移除，False 如果找不到該回調
        """
        try:
            self._shutdown_callbacks.remove(callback)
            logger.debug("已移除關閉回調: %s", _get_callable_name(callback))
            return True
        except ValueError:
            return False

    def request_shutdown(self, reason: str = "unknown") -> None:
        """
        請求關閉應用。設置關閉標記，主循環會檢測到並退出。

        Args:
            reason: 關閉原因（記錄到日誌）
        """
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        logger.info("正在關閉應用，原因: %s", reason)

    @property
    def is_shutting_down(self) -> bool:
        """是否正在關閉中。主循環應輪詢此屬性決定是否退出。"""
        return self._is_shutting_down

    def cleanup(self) -> None:
        """
        執行所有關閉回調（LIFO 順序）。

        保證只執行一次，重複調用無效。
        每個回調獨立 try/except，一個失敗不影響其他回調。
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        count = len(self._shutdown_callbacks)
        logger.info("開始清理資源（%d 個回調）", count)

        for callback in reversed(self._shutdown_callbacks):
            name = _get_callable_name(callback)
            try:
                logger.debug("執行清理: %s", name)
                callback()
            except Exception as err:
                logger.error("清理回調失敗 [%s]: %s", name, err)

        logger.info("資源清理完成")

    # ─── 信號處理 ──────────────────────────────────────────

    def _handle_signal(self, signum: int, _frame: object) -> None:
        """
        信號處理器。

        防誤觸設計：
        - 第一次 Ctrl+C：提示用戶再按一次
        - 1 秒內第二次 Ctrl+C：真正觸發關閉
        - 已在關閉中再收到信號：強制退出
        """
        sig_name = signal.Signals(signum).name

        # 已在關閉流程中 → 強制退出
        if self._is_shutting_down:
            # signal handler — print() is safe here; logger may deadlock
            print(f"\n強制退出（{sig_name}）...")
            sys.exit(1)

        now = time.monotonic()

        if now - self._last_signal_time > 1.0:
            # 第一次：重置計時，提示用戶
            self._last_signal_time = now
            # signal handler — print() is safe here; logger may deadlock
            print(f"\n收到 {sig_name}，再按一次確認退出")
            return

        # 1 秒內第二次：觸發關閉
        # signal handler — print() is safe here; logger may deadlock
        print(f"\n收到第二次 {sig_name}，正在退出...")
        self.request_shutdown(reason=f"Signal {sig_name}")


# ─── 工具函數 ──────────────────────────────────────────────

def _get_callable_name(callback: Callable) -> str:
    """安全地取得可調用對象的名稱。"""
    return getattr(callback, "__name__", str(callback))
