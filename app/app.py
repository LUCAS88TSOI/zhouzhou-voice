"""
CC語音 - 應用主類

協調所有模組的啟動、運行和關閉。
這是整個應用的中樞，按正確順序初始化各子系統，
並在退出時按相反順序清理資源。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from app.lifecycle import LifecycleManager
from utils.config import AppConfig, ConfigManager
from utils.logger import get_logger, setup_logging

logger = get_logger("app")

# 模型基礎目錄（統一路徑解析）
from utils.paths import MODELS_DIR as _MODELS_BASE


def _merge_text_overlap_parts(parts: list[str], max_check: int = 50) -> str:
    """合併分段識別文字，去除相鄰段邊界的重疊字符。

    長錄音切段後各段首尾有重疊，直接拼接會重複。
    此函數逐對比較相鄰段的尾端與首端，找出最長精確匹配後去重拼接。
    """
    if not parts:
        return ""
    result = parts[0]
    for next_part in parts[1:]:
        if not next_part:
            continue
        n = min(len(result), len(next_part), max_check)
        overlap = 0
        for i in range(n, 0, -1):
            if result[-i:] == next_part[:i]:
                overlap = i
                break
        result = result + next_part[overlap:]
    return result


class VoiceApp:
    """
    CC語音應用主類。

    職責：
    - 按正確順序初始化所有模組
    - 管理應用生命週期
    - 協調模組間的交互
    """

    def __init__(self) -> None:
        self._config: AppConfig | None = None
        self._lifecycle = LifecycleManager()

        # Phase 2: ASR 核心
        self._recorder = None       # AudioRecorder
        self._asr_process = None    # ASRProcess
        self._text_processor = None # TextProcessor

        # Phase 3: 快捷鍵 + 輸出
        self._hotkey = None         # HotkeyListener（錄音）
        self._repolish_hotkey = None  # HotkeyListener（重新潤色，可選）

        # Phase 4: 熱詞
        self._hotword = None        # HotwordManager

        # Phase 5: LLM
        self._llm = None            # LLMProcessor

        # Phase 6: GUI
        self._qt_app = None         # QApplication
        self._main_window = None    # MainWindow

        # Phase 7: 錄音歷史
        self._recording_db = None   # RecordingDatabase

        # 最後一次識別結果（用於複製）
        self._last_result: str = ""
        self._last_pre_llm_text: str = ""  # LLM 前文字（供重新潤色用）

        # 背景處理防重入（用 Lock 確保原子性，防止雙次觸發）
        self._is_processing: bool = False
        self._processing_lock = threading.Lock()

        # 文件轉錄 single-flight guard
        # iter 3 Bug E：與 _is_processing 對稱，用 Lock 確保 check-and-set 原子性
        self._is_transcribing: bool = False
        self._transcribing_lock = threading.Lock()

        # In-flight worker registry：shutdown 時要等這些 daemon thread 結束，
        # 避免資料庫寫入/剪貼板操作被中途打斷。
        self._active_workers: set[threading.Thread] = set()
        self._active_workers_lock = threading.Lock()

        # Shutdown guard：shutdown() 將其設為 True，後續 _spawn_worker 會拒絕
        # 新 worker 請求（避免 hotkey listener 尚未停止時新錄音進入導致 race）。
        self._is_shutting_down: bool = False

    # ─── 公開屬性 ──────────────────────────────────────────

    @property
    def config(self) -> AppConfig:
        """當前配置（唯讀）。"""
        if self._config is None:
            raise RuntimeError("應用尚未初始化，配置不可用")
        return self._config

    @property
    def lifecycle(self) -> LifecycleManager:
        """生命週期管理器。"""
        return self._lifecycle

    def update_indicator_position(self, x: int, y: int) -> None:
        """更新錄音指示器位置並儲存（不重啟任何服務）。"""
        new_ui = replace(self._config.ui, indicator_x=x, indicator_y=y)
        self._config = replace(self._config, ui=new_ui)
        ConfigManager.save(self._config)
        logger.debug("指示器位置已更新: (%d, %d)", x, y)

    # ─── 啟動與關閉 ────────────────────────────────────────

    def run(self) -> None:
        """啟動應用。"""
        try:
            self._initialize()
            self._print_banner()

            # 有 GUI 時進入 Qt 事件循環，否則用等待循環
            if self._qt_app is not None:
                logger.info("進入 Qt 事件循環")
                sys.exit(self._qt_app.exec())
            else:
                self._wait_loop()

        except SystemExit:
            pass
        except Exception as err:
            logger.error("應用運行異常: %s", err, exc_info=True)
            _show_crash_message(str(err))
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """關閉應用 — 觸發生命週期清理。"""
        # iter 3 Bug A：_is_shutting_down 設為 True 必須在 _active_workers_lock 內，
        # 確保與 _spawn_worker 的 atomic check 之間有 happens-before 關係。
        # 這保證：flag 寫入對任何後續拿鎖的 spawn 都可見，不會有 worker 跨過
        # shutdown 快照漏 join。
        with self._active_workers_lock:
            self._is_shutting_down = True
        self._lifecycle.request_shutdown(reason="應用退出")
        # 先等 in-flight daemon workers 結束（資料庫寫入、剪貼板貼上等），
        # 再讓 lifecycle 依序釋放 ASR / DB / GUI 等資源。
        self._wait_active_workers(timeout=5.0)
        self._lifecycle.cleanup()

    # ─── 初始化 ────────────────────────────────────────────

    def _initialize(self) -> None:
        """按順序初始化所有模組。"""
        # Phase 1: 基礎
        self._config = ConfigManager.load()
        setup_logging()
        self._lifecycle.initialize()
        self._lifecycle.register_shutdown(self._on_shutdown_save_config)

        logger.info("CC語音 v%s 正在啟動...", self._config.version)

        # Phase 2: 錄音 + ASR + 文字處理
        self._init_recorder()
        self._init_asr()
        self._init_text_processor()

        # Phase 3: 快捷鍵
        self._init_hotkey()
        self._init_repolish_hotkey()
        self._lifecycle.register_shutdown(self._stop_repolish_hotkey)

        # Phase 4: 熱詞
        self._init_hotword()

        # Phase 5: LLM
        self._init_llm()

        # Phase 6: 錄音歷史（GUI 之前，供 SettingsPanel 共享）
        self._init_recording_db()

        # Phase 7: GUI（最後初始化）
        self._init_gui()

    def _init_recording_db(self) -> None:
        """初始化錄音歷史資料庫。"""
        try:
            from core.recording_db import RecordingDatabase
            self._recording_db = RecordingDatabase()
            self._lifecycle.register_shutdown(self._recording_db.close)
            logger.info("錄音歷史資料庫就緒")
        except Exception as err:
            logger.error("錄音歷史資料庫初始化失敗: %s", err, exc_info=True)

    def _init_recorder(self) -> None:
        """初始化錄音模組。"""
        try:
            from core.audio_recorder import AudioRecorder
            max_dur = float(self._config.audio.max_recording_seconds)
            self._recorder = AudioRecorder(max_duration=max_dur)
            self._recorder.set_limit_callback(self._on_recording_limit_reached)
            self._recorder.open()
            self._lifecycle.register_shutdown(self._recorder.close)
            logger.info("錄音模組就緒（上限 %.0f 秒）", max_dur)
        except Exception as err:
            logger.error("錄音模組初始化失敗: %s", err)

    def _on_recording_limit_reached(self) -> None:
        """錄音達到 max_recording_seconds 上限時的回調（在音頻線程中觸發）。

        - AudioRecorder 已將 _is_recording 設為 False，不會再追加新音頻
        - 透過 _invoke_gui 通知主線程更新浮窗狀態（會顯示「已達錄音上限」）
        - 主流程仍會在快捷鍵釋放時正常呼叫 stop_recording()，已錄音頻會走完整識別流程
        """
        logger.warning("錄音達到上限，自動停止追加")
        self._invoke_gui("set_status", (str, "已達錄音上限"))

    def _resolve_model_dir(self) -> Path:
        """根據配置中的模型 key 找到對應的模型目錄。"""
        from core.model_catalog import get_model_info

        model_key = self._config.asr.model
        info = get_model_info(model_key)
        if info is not None:
            return _MODELS_BASE / info.model_dir

        # 向後相容：若 key 不在目錄中，嘗試直接用 key 當子目錄名
        fallback = _MODELS_BASE / model_key
        if (
            fallback.resolve().is_relative_to(_MODELS_BASE.resolve())
            and fallback.exists()
        ):
            return fallback

        # 最終 fallback: 預設的 sensevoice
        return _MODELS_BASE / "sensevoice"

    def _init_asr(self) -> None:
        """初始化 ASR 子進程（載入模型）。"""
        model_dir = self._resolve_model_dir()

        # 嘗試從 model_catalog 取得文件名，否則用預設
        from core.model_catalog import get_model_info
        info = get_model_info(self._config.asr.model)
        model_filename = info.model_file if info else "model.onnx"
        tokens_filename = info.tokens_file if info else "tokens.txt"

        model_file = model_dir / model_filename
        tokens_file = model_dir / tokens_filename

        if not model_file.exists() or not tokens_file.exists():
            logger.warning(
                "配置的 ASR 模型不存在: %s，嘗試 fallback",
                self._config.asr.model,
            )
            # fallback: 掃描已安裝模型，用第一個可用的
            from core.model_catalog import get_installed_models
            installed = get_installed_models(_MODELS_BASE)
            if installed:
                info = installed[0]
                model_dir = _MODELS_BASE / info.model_dir
                model_file = model_dir / info.model_file
                tokens_file = model_dir / info.tokens_file
                logger.warning(
                    "自動切換到可用模型: %s (%s)",
                    info.name, info.key,
                )
            else:
                logger.warning(
                    "ASR 模型不存在: %s（需要 %s 和 %s）",
                    model_dir, model_filename, tokens_filename,
                )
                return

        try:
            from core.asr_process import ASRProcess

            model_name = info.name if info else self._config.asr.model
            logger.info("正在載入 ASR 模型 [%s]（首次約 10-30 秒）...", model_name)
            self._asr_process = ASRProcess(model_dir=model_dir, model_info=info)
            self._asr_process.start()
            self._lifecycle.register_shutdown(self._asr_process.stop)
            logger.info("ASR 子進程就緒，模型: %s", model_name)
        except Exception as err:
            logger.error("ASR 初始化失敗: %s", err, exc_info=True)
            self._asr_process = None

    def _init_text_processor(self) -> None:
        """初始化文字後處理。"""
        from core.text_processor import TextProcessor
        self._text_processor = TextProcessor(self._config.output)
        logger.info("文字處理模組就緒")

    def _init_hotkey(self) -> None:
        """初始化快捷鍵監聽。"""
        from utils.hotkey import HotkeyListener
        sc = self._config.shortcut

        self._hotkey = HotkeyListener(
            key=sc.key,
            threshold=sc.threshold,
            suppress=sc.suppress,
            on_activate=self._on_recording_start,
            on_deactivate=self._on_recording_stop,
        )
        self._hotkey.start()
        self._lifecycle.register_shutdown(self._hotkey.stop)

    def _make_repolish_hotkey(self, sc):
        """建立重新潤色 HotkeyListener（不自動 start）。"""
        from utils.hotkey import HotkeyListener

        if sc.repolish_instant:
            # 速發模式：鬆開時觸發
            return HotkeyListener(
                key=sc.repolish_key,
                threshold=0.05,  # 最小閾值防抖
                suppress=False,
                on_activate=None,
                on_deactivate=self._on_repolish_activate,
            )
        else:
            # 長按模式：長按觸發
            return HotkeyListener(
                key=sc.repolish_key,
                threshold=sc.threshold,
                suppress=False,
                on_activate=self._on_repolish_activate,
                on_deactivate=None,
            )

    def _init_repolish_hotkey(self) -> None:
        """初始化重新潤色快捷鍵（停用時直接返回）。"""
        if not self._config.shortcut.repolish_key:
            return
        self._repolish_hotkey = self._make_repolish_hotkey(self._config.shortcut)
        self._repolish_hotkey.start()
        logger.info("重新潤色快捷鍵已啟動: %s", self._config.shortcut.repolish_key)

    def _stop_repolish_hotkey(self) -> None:
        """停止重新潤色快捷鍵監聽（lifecycle 清理用）。"""
        if self._repolish_hotkey is not None:
            self._repolish_hotkey.stop()
            self._repolish_hotkey = None

    def _init_hotword(self) -> None:
        """初始化熱詞系統。"""
        if not self._config.hotword.enabled:
            logger.info("熱詞系統已停用")
            return

        try:
            from hotword.manager import HotwordManager
            self._hotword = HotwordManager(self._config.hotword)
            self._hotword.load_all()
            self._hotword.start_watcher()
            self._lifecycle.register_shutdown(self._hotword.stop_watcher)
            logger.info("熱詞系統就緒")
        except Exception as err:
            logger.error("熱詞系統初始化失敗: %s", err, exc_info=True)

    def _init_llm(self) -> None:
        """初始化 LLM 處理器。"""
        if not self._config.llm.enabled:
            logger.info("LLM 潤色已停用")
            return

        try:
            from llm.processor import LLMProcessor
            self._llm = LLMProcessor(self._config.llm)
            logger.info("LLM 處理器就緒")
        except Exception as err:
            logger.error("LLM 初始化失敗: %s", err, exc_info=True)

    def _init_gui(self) -> None:
        """初始化 PySide6 GUI。"""
        try:
            from PySide6.QtWidgets import QApplication
            from gui.main_window import MainWindow

            self._qt_app = QApplication.instance() or QApplication(sys.argv)
            self._main_window = MainWindow(app_controller=self)

            # 連接托盤信號
            tray = self._main_window._tray
            tray.quit_requested.connect(self._on_gui_quit)
            tray.copy_result_requested.connect(self._on_copy_result)
            tray.clear_memory_requested.connect(self._on_clear_memory)
            # settings_requested 由 MainWindow._on_settings 處理（切換到設定頁）
            tray.add_hotword_requested.connect(self._on_add_hotword)
            tray.add_rectify_requested.connect(self._on_add_rectify)
            tray.transcribe_requested.connect(self._on_transcribe_requested)
            tray.role_switch_requested.connect(self._on_role_switch)
            tray.startup_toggle_requested.connect(self._on_startup_toggle)
            tray.update_dialog_requested.connect(self._show_update_dialog)

            # 連接設定儲存信號：設定頁按「儲存」時套用新配置
            self._main_window.settings_save_requested.connect(self._apply_config)

            # 連接拖放信號
            self._main_window.files_dropped.connect(self._on_files_dropped)

            # 連接錄音歷史重新處理信號
            self._main_window.reprocess_requested.connect(self._on_history_reprocess)

            # 連接文件轉錄頁籤信號
            self._main_window._tab_transcribe.transcribe_requested.connect(self._on_files_dropped)

            # 初始化角色選單
            self._refresh_tray_roles()

            # 同步開機啟動狀態到托盤
            from utils.startup import is_startup_enabled
            tray.set_startup_checked(is_startup_enabled())

            # 不顯示主窗口（最小化到托盤）
            tray.show()
            tray.show_message("CC語音", "已啟動，按住 CapsLock 說話")

            # 背景檢查是否有新版本
            self._init_updater()

            # 首次啟動：麥克風測試
            if not self._config.setup_complete:
                self._show_mic_test()

            self._lifecycle.register_shutdown(self._cleanup_gui)
            logger.info("GUI 就緒（托盤模式）")
        except ImportError as exc:
            logger.warning("GUI 模組載入失敗，以終端模式運行: %s", exc, exc_info=True)
        except Exception as err:
            logger.error("GUI 初始化失敗: %s", err, exc_info=True)

    def _init_updater(self) -> None:
        """啟動背景版本檢查。網路失敗時靜默忽略，不影響正常使用。"""
        try:
            from utils.updater import check_for_update
            from utils.paths import APP_VERSION

            def on_update_result(info) -> None:
                if info is None:
                    logger.debug("版本檢查未能完成（網路問題，已忽略）")
                    return
                if info.available:
                    logger.info(
                        "發現新版本: %s（目前: %s）",
                        info.remote_version,
                        APP_VERSION,
                    )
                    tray = self._main_window._tray
                    tray.show_update_available(info)
                    self._show_update_dialog(info)
                else:
                    logger.debug("版本已是最新: %s", info.remote_version)

            check_for_update(on_update_result)
            logger.info("版本檢查線程已啟動")
        except Exception as err:
            logger.warning("版本檢查初始化失敗（已忽略）: %s", err)

    def _show_update_dialog(self, info: object) -> None:
        """彈出更新對話框。"""
        try:
            from gui.update_dialog import UpdateDialog
            dialog = UpdateDialog(info, parent=self._main_window)
            dialog.exec()
        except Exception as err:
            logger.warning("更新對話框顯示失敗: %s", err)

    def _show_mic_test(self) -> None:
        """顯示麥克風測試對話框（首次啟動或手動觸發）。"""
        try:
            from gui.mic_test_dialog import MicTestDialog
            dlg = MicTestDialog(self._recorder, self._asr_process, parent=None)
            dlg.exec()
            # 標記已完成首次設定
            if not self._config.setup_complete:
                self._config = replace(self._config, setup_complete=True)
                ConfigManager.save(self._config)
                logger.info("首次麥克風測試完成")
        except Exception as err:
            logger.warning("麥克風測試對話框失敗: %s", err, exc_info=True)

    # ─── 線程安全 GUI 更新 ────────────────────────────────

    def _invoke_gui(self, method: str, *args: tuple[type, object]) -> None:
        """從任意線程安全地調用 MainWindow 方法。

        Args:
            method: MainWindow 上的 slot 名稱
            *args: (type, value) 對，每對生成一個 Q_ARG
        """
        if not self._main_window:
            return
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        q_args = [Q_ARG(t, v) for t, v in args]
        QMetaObject.invokeMethod(
            self._main_window, method,
            Qt.ConnectionType.QueuedConnection,
            *q_args,
        )

    # ─── Worker registry（shutdown 時 join in-flight workers）────────

    def _spawn_worker(
        self, target, *, name: str = "worker", args: tuple = (),
    ) -> threading.Thread | None:
        """啟動 daemon worker thread 並加入 registry。

        Thread 執行結束時會自動從 registry 移除。
        shutdown 時 _wait_active_workers() 會 join 所有還活著的 workers。

        iter 3 Bug A 修復：atomic check-and-register —— 在同一把
        `_active_workers_lock` 內「檢查 _is_shutting_down + add registry」
        再 `thread.start()`。若 start 拋例外，rollback 時於 lock 內 discard。

        這消除了 iter 2 的 race window：start 後 register 前，shutdown 可
        snapshot 空 registry → worker 跑在已釋放資源上。

        行為：
        - shutdown 中（鎖內讀到 flag）：返回 None，不註冊、不 start
        - thread.start() 拋例外：在鎖內 discard 回滾，原例外上拋
        - 正常：register → start，返回 thread
        """
        def _runner():
            try:
                target(*args)
            finally:
                with self._active_workers_lock:
                    self._active_workers.discard(threading.current_thread())

        thread = threading.Thread(target=_runner, daemon=True, name=name)

        # Atomic: check flag + register 在同一把鎖內完成。
        # shutdown() 也必須在此鎖內設 _is_shutting_down=True，確保 happens-before。
        with self._active_workers_lock:
            if self._is_shutting_down:
                logger.warning("shutdown 中拒絕新 worker: %s", name)
                return None
            self._active_workers.add(thread)

        try:
            thread.start()
        except BaseException:
            # start 失敗 → 回滾 registry，讓呼叫端 finally reset flag
            with self._active_workers_lock:
                self._active_workers.discard(thread)
            raise
        return thread

    def _wait_active_workers(self, timeout: float = 5.0) -> None:
        """等待所有 in-flight workers 結束（最多 timeout 秒）。"""
        with self._active_workers_lock:
            workers = list(self._active_workers)
        if not workers:
            return
        logger.info("等待 %d 個背景 worker 結束（timeout=%.1fs）", len(workers), timeout)
        deadline = time.monotonic() + timeout
        for t in workers:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
            if t.is_alive():
                logger.warning("背景 worker 未在期限內結束: %s", t.name)

    # ─── 錄音回調 ──────────────────────────────────────────

    def _on_repolish_activate(self) -> None:
        """重新潤色快捷鍵觸發：在背景線程重新執行 LLM 潤色。"""
        if self._recorder is not None and self._recorder.is_recording:
            return
        if not self._last_result:
            return
        self._spawn_worker(self._run_repolish, name="repolish-worker")

    def _build_repolish_processor(self) -> tuple:
        """建立重新潤色用的 LLM 處理器和角色 ID。"""
        repolish_provider = self._config.llm.repolish_provider if self._config else ""
        repolish_model = self._config.llm.repolish_model if self._config else ""
        repolish_role = self._config.llm.repolish_role if self._config else ""

        if not repolish_provider:
            return self._llm, repolish_role

        try:
            from llm.processor import LLMProcessor

            temp_providers = dict(self._config.llm.providers)
            if repolish_provider in temp_providers:
                provider_dict = dict(temp_providers[repolish_provider])
                if repolish_model:
                    provider_dict["model"] = repolish_model
                temp_providers[repolish_provider] = provider_dict

            temp_llm_config = replace(
                self._config.llm,
                active_provider=repolish_provider,
                providers=temp_providers,
            )
            processor = LLMProcessor(temp_llm_config)
            logger.info("重新潤色使用服務商: %s, 模型: %s", repolish_provider, repolish_model or "預設")
            return processor, repolish_role
        except Exception as err:
            logger.error("建立重新潤色 LLM 處理器失敗: %s", err)
            return self._llm, repolish_role

    def _run_repolish(self) -> None:
        """背景線程：對 _last_result 重新執行 LLM 潤色並貼上。"""
        # Lock 防止雙次觸發（非阻塞嘗試，acquire/release 同在此線程）
        if not self._processing_lock.acquire(blocking=False):
            return
        final_status = "完成"
        try:
            source = self._last_pre_llm_text or self._last_result
            logger.info("重新潤色開始: %r", source[:60])

            llm_processor, repolish_role = self._build_repolish_processor()
            if llm_processor is None:
                final_status = "未配置 LLM"
                self._invoke_gui("set_status", (str, final_status))
                return

            self._invoke_gui("set_status", (str, "LLM 處理中..."))
            result = self._try_llm_polish(source, role_override=repolish_role, llm_processor=llm_processor)
            polished = result.text
            if not result.success:
                self._invoke_gui(
                    "notify_warning",
                    (str, "⚠ 重新潤色失敗（請檢查網絡或 API Key）"),
                )
            self._last_result = polished

            logger.info("重新潤色結果: %s", polished)
            self._invoke_gui("append_result", (str, f"[重新潤色] {polished}"))

            if self._config and self._config.output.paste_mode:
                from utils.clipboard import ClipboardManager
                if not ClipboardManager.paste_text(
                    polished, restore=self._config.output.restore_clip
                ):
                    self._invoke_gui(
                        "notify_warning",
                        (str, "⚠ 貼上失敗，結果已喺剪貼板，可手動 Ctrl+V"),
                    )
        except Exception as err:
            logger.error("重新潤色異常: %s", err, exc_info=True)
            final_status = "失敗"
        finally:
            self._processing_lock.release()
            self._invoke_gui("set_status", (str, final_status))

    def _on_history_reprocess(self, record_id: int, role_id: str) -> None:
        """錄音歷史重新處理：在背景線程對指定記錄重新執行 LLM 潤色。"""
        self._spawn_worker(
            self._run_history_reprocess,
            args=(record_id, role_id),
            name="history-reprocess",
        )

    def _run_history_reprocess(self, record_id: int, role_id: str) -> None:
        """背景線程：重新處理錄音歷史記錄。"""
        try:
            record = self._recording_db.get_by_id(record_id)
            if record is None:
                logger.warning("重新處理失敗: 找不到記錄 id=%d", record_id)
                return

            source = record.asr_text
            if not source:
                logger.warning("重新處理失敗: 記錄 id=%d 無 ASR 文字", record_id)
                return

            if not role_id:
                # 僅 ASR，清除 LLM 結果
                self._recording_db.update(record_id, llm_text="", role_id="")
                logger.info("重新處理(僅ASR): id=%d", record_id)
            else:
                result = self._try_llm_polish(source, role_override=role_id)
                # Bug 5 修復：只有成功處理才更新歷史記錄
                # 錯誤情況（API 失敗、超時）不覆蓋原有結果
                if result.success:
                    self._recording_db.update(
                        record_id, llm_text=result.text, role_id=role_id,
                    )
                    logger.info("重新處理完成: id=%d, role=%s", record_id, role_id)
                elif result.was_processed and result.error:
                    # LLM 處理失敗，保留原有記錄
                    logger.warning("重新處理失敗: id=%d, 錯誤: %s", record_id, result.error)
                else:
                    logger.info("重新處理跳過: id=%d, 無 LLM 可用", record_id)

            # 刷新歷史列表
            self._invoke_gui("refresh_history")
        except Exception as err:
            logger.error("重新處理異常: %s", err, exc_info=True)

    def _on_recording_start(self) -> None:
        """快捷鍵長按觸發：開始錄音。"""
        if self._recorder is None:
            return
        self._recorder.start_recording()
        logger.info("錄音開始")
        self._invoke_gui("set_status", (str, "錄音中..."))

    def _on_recording_stop(self) -> None:
        """快捷鍵鬆開觸發：停止錄音，交給背景線程處理。

        pynput 線程只做 stop_recording（非常快），其餘全部
        移到背景 daemon thread，避免阻塞快捷鍵監聽和 UI。

        Atomic guard：檢查 _is_processing 與設為 True 必須在同一把鎖下完成，
        否則兩次快速 release 會同時通過檢查並各 spawn 一個 worker。
        """
        if self._recorder is None:
            return

        audio_bytes = self._recorder.stop_recording()
        duration = len(audio_bytes) / 4 / 16000  # float32 = 4 bytes

        if duration < 0.1:
            logger.debug("錄音太短（%.2f 秒），已忽略", duration)
            self._invoke_gui("set_status", (str, "就緒"))
            return

        # Atomic check-and-set：同一把鎖內完成「guard 檢查 + 搶占」
        with self._processing_lock:
            if self._is_processing:
                logger.warning("上一次語音還在處理中，忽略本次")
                return
            self._is_processing = True

        logger.info("錄音完成: %.1f 秒, %d bytes", duration, len(audio_bytes))

        # 在背景線程中處理 ASR + 文字處理 + 熱詞 + LLM + 輸出。
        # spawn 失敗（shutdown 中或 thread.start 例外）必須 reset _is_processing，
        # 否則下次錄音被誤判為「仍在處理」永久卡住。
        try:
            worker = self._spawn_worker(
                lambda: self._process_audio(audio_bytes),
                name="voice-worker",
            )
            if worker is None:
                # shutdown 拒絕：reset flag 避免下次判重阻塞
                with self._processing_lock:
                    self._is_processing = False
        except BaseException:
            with self._processing_lock:
                self._is_processing = False
            raise

    def _process_audio(self, audio_bytes: bytes) -> None:
        """背景線程：完整的語音處理流水線。

        ASR 辨識 → 文字後處理 → 熱詞校正 → 過濾噪音 → LLM 潤色 → 輸出。
        所有 GUI 更新透過 _invoke_gui() 回到主線程。
        每個環節記錄耗時和中間結果，方便診斷。

        注意：_is_processing 在 _on_recording_stop 已 atomic 設為 True，
        本方法只負責在 finally 重置回 False。
        """
        timings: dict[str, float] = {}
        rec_duration = len(audio_bytes) / 4 / 16000  # float32 = 4 bytes
        # 終結狀態：成功 → "完成"、失敗 → "失敗"、空結果/過濾 → "就緒"
        final_status = "就緒"

        try:
            pipeline_start = time.monotonic()
            self._invoke_gui("set_status", (str, "識別中..."))

            # ── 1. ASR 識別（短音頻一次性 / 長音頻分段）─────
            t = time.monotonic()
            long_threshold = self._config.audio.long_audio_threshold
            if rec_duration > long_threshold:
                logger.info(
                    "長音頻 %.1fs > %.0fs，啟用分段識別",
                    rec_duration, long_threshold,
                )
                text = self._recognize_long_audio(audio_bytes)
            else:
                text = self._try_recognize(audio_bytes)
            timings["ASR"] = time.monotonic() - t

            if not text or not text.strip():
                self._last_result = ""
                self._last_pre_llm_text = ""
                self._print_timings(rec_duration, timings, final_text=None)
                return

            asr_raw_text = text  # 保存 ASR 原始結果
            logger.info("ASR 原始結果: %s", text)

            # ── 2. 文字後處理 ───────────────────────────────
            t = time.monotonic()
            self._invoke_gui("set_status", (str, "處理中..."))
            if self._text_processor:
                text = self._text_processor.process(text)
            timings["文字處理"] = time.monotonic() - t
            logger.debug("文字處理後: %s", text)

            # ── 3. 熱詞替換 ────────────────────────────────
            t = time.monotonic()
            self._invoke_gui("set_status", (str, "校正中..."))
            if self._hotword:
                text = self._hotword.correct(text)
            timings["熱詞"] = time.monotonic() - t
            logger.debug("熱詞校正後: %s", text)

            # ── 4. 過濾純標點 / 無實質內容 ──────────────────
            _NOISE_CHARS = "。，、！？.!?,;：:… \t\n"
            if not text.strip(_NOISE_CHARS):
                logger.debug("過濾純標點 ASR 結果: %r", text)
                self._last_result = ""
                self._last_pre_llm_text = ""
                self._print_timings(rec_duration, timings, final_text=None)
                return

            # ── 5. LLM 潤色（可選，短文本跳過）──────────────
            _MIN_LLM_LENGTH = 4  # 少於此字符數不調用 LLM（即 3 字以下跳過）
            skip_llm = len(text.strip()) < _MIN_LLM_LENGTH
            if skip_llm:
                logger.debug("短文本跳過 LLM: %r (%d 字符)", text, len(text.strip()))

            self._last_pre_llm_text = text

            t = time.monotonic()
            if self._config and self._config.llm.enabled and not skip_llm:
                self._invoke_gui("set_status", (str, "潤色中..."))
                result = self._try_llm_polish(text, enforce_timeout=True)
                text = result.text
                timings["LLM"] = time.monotonic() - t
                if result.success:
                    if timings["LLM"] > 0.01:
                        logger.info("LLM 潤色後: %s", text)
                elif result.error == "潤色逾時":
                    # 逾時 → 貼原文 + 專屬提示
                    _to = int(self._config.llm.polish_timeout)
                    self._invoke_gui(
                        "notify_warning",
                        (str, f"⚠ 潤色逾時（>{_to}s），已貼原文"),
                    )
                else:
                    # A2：潤色失敗唔好靜默 —— 貼出嘅係未潤色原文，彈托盤通知俾用戶知
                    logger.warning("LLM 潤色失敗，貼出未潤色原文（error=%s）", result.error)
                    self._invoke_gui(
                        "notify_warning",
                        (str, "⚠ 潤色失敗，已貼原文（請檢查網絡或 API Key）"),
                    )
            else:
                timings["LLM"] = 0.0  # 未調用 LLM

            # ── 計算總耗時 ──────────────────────────────────
            timings["總計"] = time.monotonic() - pipeline_start

            self._last_result = text
            logger.info("輸出: %s", text)

            # 輸出計時摘要（Console + GUI）
            self._print_timings(rec_duration, timings, final_text=text)

            # 更新 GUI：結果 + 計時 + 模型/角色資訊
            self._invoke_gui("append_result", (str, text))
            timing_line = self._format_timing_line(rec_duration, timings)
            self._invoke_gui("append_result", (str, timing_line))

            # 顯示模型和角色資訊
            model_info_line = self._get_model_role_info()
            if model_info_line:
                self._invoke_gui("append_result", (str, model_info_line))

            # ── 6. 儲存錄音歷史（記錄優先：先落庫，再做輸出）──────────
            #   置於剪貼板輸出之前，確保即使貼上/GUI 環節出錯，記錄都唔會遺失。
            if (
                self._recording_db
                and self._config
                and self._config.history.enabled
                and rec_duration >= self._config.history.min_duration
            ):
                try:
                    llm_text = text if self._config.llm.enabled else ""
                    role_id = self._config.llm.active_role if self._config.llm.enabled else ""
                    self._recording_db.insert(
                        audio_bytes=audio_bytes,
                        duration=rec_duration,
                        asr_text=asr_raw_text,
                        llm_text=llm_text,
                        role_id=role_id,
                        model_key=self._config.asr.model,
                    )
                    self._invoke_gui("refresh_history")
                except Exception as db_err:
                    logger.warning("儲存錄音歷史失敗: %s", db_err)

            # ── 7. 透過剪貼板粘貼（輸出，置於記錄之後）──────────────
            if self._config and self._config.output.paste_mode:
                from utils.clipboard import ClipboardManager
                if not ClipboardManager.paste_text(
                    text,
                    restore=self._config.output.restore_clip,
                ):
                    self._invoke_gui(
                        "notify_warning",
                        (str, "⚠ 貼上失敗，結果已喺剪貼板，可手動 Ctrl+V"),
                    )

            # 流水線完整走到底且 text 非空 → 明確成功
            final_status = "完成"

        except Exception as err:
            logger.error("語音處理異常: %s", err, exc_info=True)
            final_status = "失敗"
        finally:
            with self._processing_lock:
                self._is_processing = False
            self._invoke_gui("set_status", (str, final_status))

    # ─── 計時工具 ──────────────────────────────────────────

    def _get_model_role_info(self) -> str:
        """取得當前 ASR 模型和 LLM 角色的顯示名稱。"""
        from core.model_catalog import get_model_info
        from llm.roles import resolve_role

        # 獲取模型名稱
        model_key = self._config.asr.model if self._config else ""
        model_info = get_model_info(model_key)
        model_name = model_info.name if model_info else model_key

        # 獲取角色名稱
        if self._config and self._config.llm.enabled:
            role_key = self._config.llm.active_role
            role_config = resolve_role(
                role_key,
                custom_roles=self._config.llm.custom_roles,
                builtin_overrides=self._config.llm.builtin_overrides,
            )
            role_name = role_config.name
        else:
            role_name = "無"

        from llm.provider import get_active_provider
        provider = get_active_provider(self._config.llm) if self._config and self._config.llm.enabled else None
        llm_part = f" | LLM: {provider.model}" if provider else ""

        return f"[模型: {model_name} | 角色: {role_name}{llm_part}]"

    @staticmethod
    def _format_timing_line(
        rec_duration: float,
        timings: dict[str, float],
    ) -> str:
        """格式化一行計時摘要。"""
        parts = [f"錄音 {rec_duration:.1f}s"]
        for name, elapsed in timings.items():
            parts.append(f"{name} {elapsed:.2f}s")
        return "[計時] " + " | ".join(parts)

    def _print_timings(
        self,
        rec_duration: float,
        timings: dict[str, float],
        final_text: str | None,
    ) -> None:
        """將計時摘要印到 Console 和日誌。"""
        line = self._format_timing_line(rec_duration, timings)
        logger.info(
            "語音處理計時: %s (結果=%s)",
            line,
            repr(final_text[:50]) if final_text else "無",
        )

    def _try_recognize(self, audio_bytes: bytes) -> Optional[str]:
        """發送音頻到 ASR 子進程進行識別。"""
        if self._asr_process is None or not self._asr_process.is_running:
            logger.warning("ASR 未就緒，語音識別不可用")
            return None

        try:
            from core.asr_process import ASRRequest, new_task_id

            request = ASRRequest(
                task_id=new_task_id(),
                audio_data=audio_bytes,
                sample_rate=16000,
                is_final=True,
            )

            duration = len(audio_bytes) / 4 / 16000
            timeout = max(30.0, duration * 1.5)
            logger.info("識別中... (%.0f秒音頻, 超時%.0f秒)", duration, timeout)
            response = self._asr_process.send_and_wait(request, timeout=timeout)

            if response.error:
                logger.error("ASR 識別錯誤: %s", response.error)
                return None

            if not response.text:
                logger.info("未識別到語音內容")
                return None

            logger.info("ASR 識別: %s", response.text)
            return response.text

        except TimeoutError:
            logger.error("ASR 識別超時")
            return None
        except Exception as err:
            logger.error("ASR 識別異常: %s", err, exc_info=True)
            return None

    def _recognize_long_audio(self, audio_bytes: bytes) -> Optional[str]:
        """長音頻分段識別 → 文字拼接。

        將 float32 PCM 按 audio.segment_seconds 切片，串行送 ASR 子進程，
        每段識別後拼接結果。每段內部呼叫 _try_recognize() 重用既有重試/超時邏輯。

        Args:
            audio_bytes: float32 PCM bytes（16kHz 單聲道）

        Returns:
            拼接後的識別文字；任一段失敗仍嘗試後續段，全部失敗返回 None
        """
        if self._asr_process is None or not self._asr_process.is_running:
            logger.warning("ASR 未就緒，無法分段識別")
            return None

        import numpy as np

        audio_cfg = self._config.audio
        sample_rate = 16000
        # 保底檢查：AudioConfig.__post_init__ 已 clamp，但額外防禦以防未來回歸
        seg_seconds = max(1.0, float(audio_cfg.segment_seconds))
        overlap_seconds = max(0.0, float(audio_cfg.segment_overlap))
        if overlap_seconds >= seg_seconds:
            overlap_seconds = max(0.0, seg_seconds - 0.1)
        seg_samples = int(seg_seconds * sample_rate)
        overlap_samples = int(overlap_seconds * sample_rate)
        stride = max(1, seg_samples - overlap_samples)

        # float32 → numpy 陣列（4 bytes/sample）
        audio_arr = np.frombuffer(audio_bytes, dtype=np.float32)
        total_samples = len(audio_arr)

        # 切片計算
        slices: list[tuple[int, int]] = []
        pos = 0
        while pos < total_samples:
            end = min(pos + seg_samples, total_samples)
            slices.append((pos, end))
            if end >= total_samples:
                break
            pos += stride

        total_segs = len(slices)
        logger.info(
            "分段識別: 總長 %.1fs → %d 段（每段 %.0fs，重疊 %.1fs）",
            total_samples / sample_rate,
            total_segs,
            audio_cfg.segment_seconds,
            audio_cfg.segment_overlap,
        )

        parts: list[str] = []
        for idx, (start, end) in enumerate(slices, 1):
            self._invoke_gui(
                "set_status",
                (str, f"分段識別中 ({idx}/{total_segs})..."),
            )
            seg_arr = audio_arr[start:end]
            seg_bytes = seg_arr.tobytes()  # 已是 float32，無需轉型
            seg_text = self._try_recognize(seg_bytes)
            if seg_text:
                parts.append(seg_text.strip())
            else:
                logger.warning("分段 %d/%d 識別失敗或為空", idx, total_segs)

        if not parts:
            return None

        merged = _merge_text_overlap_parts(parts)
        logger.info("分段識別完成: %d 段拼接 → %d 字", total_segs, len(merged))
        return merged

    def _try_llm_polish(
        self, text: str, role_override: str = "", llm_processor=None,
        enforce_timeout: bool = False,
    ):
        """嘗試用 LLM 潤色文字。返回結構化狀態。

        Args:
            text: 要潤色的文字
            role_override: 覆蓋角色 ID，空字串使用預設角色
            llm_processor: 覆蓋 LLM 處理器，None 使用預設處理器
            enforce_timeout: True 時套用 polish_timeout 上限，逾時直接回原文
                             （僅主語音管線用；repolish / 檔案轉錄不套）

        Returns:
            LLMResultStatus: 結構化狀態（含 success, text, was_processed, error）
        """
        import time as _time

        from llm.processor import LLMResultStatus

        processor = llm_processor or self._llm
        if processor is None:
            return LLMResultStatus(
                success=False,
                text=text,
                was_processed=False,
                error=""
            )

        try:
            from llm.roles import resolve_role

            role_id = role_override or (self._config.llm.active_role if self._config else "default")
            custom_roles = self._config.llm.custom_roles if self._config else []
            overrides = self._config.llm.builtin_overrides if self._config else {}
            role = resolve_role(role_id, custom_roles, overrides)

            # 取得熱詞上下文（供 LLM 參考）
            hotword_ctx: list[str] = []
            if self._hotword:
                hotword_ctx = self._hotword.get_similar_context(text)

            self._invoke_gui("set_status", (str, "LLM 處理中..."))

            # 逾時保護：超過 polish_timeout 秒自動停止，貼出未潤色原文
            timeout_s = self._config.llm.polish_timeout if self._config else 0.0
            should_stop = None
            request_timeout = None
            if enforce_timeout and timeout_s and timeout_s > 0:
                deadline = _time.monotonic() + timeout_s
                should_stop = lambda: _time.monotonic() >= deadline  # noqa: E731
                request_timeout = timeout_s

            logger.info("LLM 潤色中...")
            result = processor.process(
                text=text,
                role=role,
                hotword_context=hotword_ctx,
                should_stop=should_stop,
                request_timeout=request_timeout,
            )

            # 逾時觸發 → 丟棄半截潤色，回傳原文
            if result.was_stopped:
                logger.warning("LLM 潤色逾時（>%.0fs），貼出未潤色原文", timeout_s)
                return LLMResultStatus(
                    success=False,
                    text=text,
                    was_processed=True,
                    error="潤色逾時",
                )

            # 有錯誤 → 顯示到主視窗讓用戶知道
            if result.error:
                error_msg = f"[LLM] {result.error}"
                logger.warning(error_msg)
                self._invoke_gui("append_result", (str, error_msg))
                return LLMResultStatus(
                    success=False,
                    text=text,
                    was_processed=True,
                    error=result.error
                )

            # 參數容錯警告 → 通知用戶
            if result.warnings:
                for w in result.warnings:
                    logger.warning(w)
                    self._invoke_gui("append_result", (str, f"[提示] {w}"))

            if result.text and result.text != text:
                logger.info("LLM 潤色: %s → %s", text, result.text)
                return LLMResultStatus(
                    success=True,
                    text=result.text,
                    was_processed=True,
                    error=""
                )

            # LLM 返回相同文本（無變化）
            return LLMResultStatus(
                success=True,
                text=result.text or text,
                was_processed=True,
                error=""
            )

        except Exception as err:
            logger.error("LLM 潤色失敗: %s", err, exc_info=True)
            error_msg = f"[LLM 異常] {err}"
            self._invoke_gui("append_result", (str, error_msg))
            return LLMResultStatus(
                success=False,
                text=text,
                was_processed=True,
                error=str(err)
            )

    def _polish_transcription_text(self, text: str, file_path: Path) -> str | None:
        """將轉錄文本送入 LLM 優化，保存為 _polished.txt。"""
        _CHUNK_SIZE = 1500

        self._invoke_gui("set_status", (str, "LLM 優化中..."))

        try:
            if len(text) <= _CHUNK_SIZE:
                result = self._try_llm_polish(text)
                # Bug 修復：只有成功時才保存
                if not result.success:
                    logger.info("LLM 優化失敗，跳過保存: %s", result.error or "unknown error")
                    return None
                polished = result.text
            else:
                polished = self._polish_chunked(text, _CHUNK_SIZE)

            if not polished:
                logger.info("LLM 優化無變化，跳過保存")
                self._invoke_gui("append_result", (str, "[LLM 優化] 無變化，跳過保存"))
                return None

            p = file_path if isinstance(file_path, Path) else Path(file_path)
            polished_path = p.with_name(f"{p.stem}_polished").with_suffix(".txt")
            polished_path.write_text(polished, encoding="utf-8")
            logger.info("LLM 優化結果已保存: %s", polished_path)
            return polished

        except Exception as err:
            logger.error("LLM 優化失敗: %s", err, exc_info=True)
            self._invoke_gui("append_result", (str, f"[LLM 優化失敗] {err}"))
            return None

    def _polish_chunked(self, text: str, chunk_size: int) -> str:
        """分段 LLM 優化長文本（帶跨段上下文）。

        將文本按 chunk_size 切片後，呼叫 transcribe.file_transcriber 中的
        polish_transcription_with_context()，使每段送 LLM 時都帶前 N 段的
        (原文, 潤色) 對照，緩解段間術語/人名不一致問題。

        該 helper 自動使用 enable_history=False 的 role 副本，
        不會污染主錄音的對話歷史。
        """
        from transcribe.file_transcriber import polish_transcription_with_context
        from transcribe.srt_writer import smart_split
        from llm.roles import resolve_role

        sentences = smart_split(text)
        if not sentences:
            return self._try_llm_polish(text).text

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for sentence in sentences:
            if current_len + len(sentence) > chunk_size and current:
                chunks.append("\n".join(current))
                current = [sentence]
                current_len = len(sentence)
            else:
                current.append(sentence)
                current_len += len(sentence)
        if current:
            chunks.append("\n".join(current))

        if self._llm is None or self._config is None:
            # 無 LLM：fallback 到逐段呼叫（保留既有錯誤路徑）
            return "\n".join(self._try_llm_polish(c).text for c in chunks)

        # 解析當前角色（一次性）
        role = resolve_role(
            self._config.llm.active_role,
            self._config.llm.custom_roles,
            self._config.llm.builtin_overrides,
        )

        def _on_chunk_start(idx: int, total: int) -> None:
            self._invoke_gui(
                "update_transcribe_progress",
                (float, 0.95 + 0.05 * (idx + 1) / total),
                (str, f"LLM 優化中 {idx + 1}/{total}..."),
            )

        polished_parts = polish_transcription_with_context(
            chunks=chunks,
            llm_processor=self._llm,
            role=role,
            on_chunk_start=_on_chunk_start,
        )
        return "\n".join(polished_parts)

    # ─── GUI 回調 ──────────────────────────────────────────

    def _on_gui_quit(self) -> None:
        """托盤退出。"""
        if self._main_window:
            self._main_window.force_close()
        if self._qt_app:
            self._qt_app.quit()

    def _on_copy_result(self) -> None:
        """複製最後一次識別結果。"""
        if self._last_result:
            from utils.clipboard import ClipboardManager
            if ClipboardManager.set_text(self._last_result):
                logger.info("已複製結果到剪貼板")
            else:
                logger.error("複製結果到剪貼板失敗")

    def _on_clear_memory(self) -> None:
        """清除 LLM 歷史。"""
        if self._llm:
            self._llm.clear_history()
            logger.info("LLM 記憶已清除")

    def _on_add_hotword(self) -> None:
        """快速添加熱詞（彈出輸入框）。"""
        try:
            from PySide6.QtWidgets import QInputDialog
            word, ok = QInputDialog.getText(
                self._main_window, "添加熱詞", "輸入熱詞："
            )
            if ok and word.strip():
                if self._hotword:
                    self._hotword.add_hotword(word.strip())
                    logger.info("已添加熱詞: %s", word.strip())
        except Exception as err:
            logger.error("添加熱詞失敗: %s", err)

    def _on_add_rectify(self) -> None:
        """快速添加糾錯記錄。"""
        try:
            from PySide6.QtWidgets import QInputDialog
            text, ok = QInputDialog.getText(
                self._main_window, "添加糾錯",
                "格式: 錯誤詞 → 正確詞\n例如: 語音識別 → 語音辨識"
            )
            if ok and "→" in text:
                parts = text.split("→", 1)
                wrong = parts[0].strip()
                right = parts[1].strip()
                if wrong and right and self._hotword:
                    self._hotword.add_rectify(wrong, right)
                    logger.info("已添加糾錯: %s → %s", wrong, right)
        except Exception as err:
            logger.error("添加糾錯失敗: %s", err)

    def _on_transcribe_requested(self) -> None:
        """托盤「文件轉錄」菜單：開啟文件選擇對話框。"""
        if self._main_window:
            self._main_window.open_file_dialog()

    def _on_files_dropped(self, paths: list, file_cfg=None) -> None:
        """處理拖放或選擇的媒體文件列表。

        Args:
            paths: 媒體文件路徑列表
            file_cfg: TranscribeTab 當前 UI 設定；None 時退化為 self._config.file
        """
        if not paths:
            return
        # iter 3 Bug E：atomic check-and-set，與 _is_processing 對稱修復。
        # 雖然目前呼叫路徑（Qt 信號）為主線程序列化，仍加 Lock 以防未來跨線程呼叫。
        with self._transcribing_lock:
            if self._is_transcribing:
                logger.warning("轉錄進行中，拒絕新的轉錄請求")
                if self._main_window:
                    self._main_window._tray.show_message("CC語音", "轉錄進行中，請等待完成")
                return
            if self._asr_process is None or not self._asr_process.is_running:
                logger.warning("ASR 未就緒，無法轉錄文件")
                if self._main_window:
                    self._main_window._tray.show_message("CC語音", "ASR 模型未就緒，無法轉錄")
                self._invoke_gui("on_transcribe_batch_finished")
                return
            self._is_transcribing = True
        cfg = file_cfg if file_cfg is not None else self._config.file
        # spawn 失敗（shutdown 中或 thread.start 例外）必須 reset _is_transcribing
        try:
            worker = self._spawn_worker(
                self._transcribe_files,
                args=(list(paths), cfg),
                name="transcribe-worker",
            )
            if worker is None:
                with self._transcribing_lock:
                    self._is_transcribing = False
        except BaseException:
            with self._transcribing_lock:
                self._is_transcribing = False
            raise

    def _transcribe_files(self, paths: list, file_cfg=None) -> None:
        """在後台線程中執行文件轉錄（逐個處理）。

        批次外層 try/finally 確保：
        - 整批結束後呼叫 on_transcribe_batch_finished 解除 UI 忙碌態
        - 無論成功 / 例外都統一收尾（隱藏進度條、重置狀態列）
        """
        from transcribe.file_transcriber import FileTranscriber
        transcriber = FileTranscriber(self._asr_process)
        file_cfg = file_cfg if file_cfg is not None else self._config.file

        any_success = False
        any_failure = False

        try:
            for file_path in paths:
                fp_str = str(file_path)
                logger.info("開始轉錄: %s", file_path)
                self._invoke_gui("show_progress")
                self._invoke_gui(
                    "update_file_status", (str, fp_str), (str, "轉錄中..."),
                )

                try:
                    result = transcriber.transcribe(
                        file_path=file_path,
                        on_progress=lambda r, m: self._invoke_gui(
                            "update_transcribe_progress",
                            (float, r), (str, m),
                        ),
                        save_srt=file_cfg.save_srt,
                        save_txt=file_cfg.save_txt,
                        save_json=file_cfg.save_json,
                    )
                except Exception as err:
                    logger.error("轉錄例外 %s: %s", file_path, err, exc_info=True)
                    result = None

                if result and result.text:
                    summary = (
                        f"轉錄完成: {file_path.name} | "
                        f"{result.segment_count} 段, "
                        f"{result.duration:.0f}s, "
                        f"耗時 {result.elapsed:.1f}s"
                    )
                    logger.info("轉錄完成: %s", summary)
                    self._invoke_gui("append_result", (str, summary))

                    if file_cfg.llm_polish and self._llm is not None:
                        self._invoke_gui(
                            "update_file_status",
                            (str, fp_str), (str, "LLM 優化中..."),
                        )
                        polished = self._polish_transcription_text(
                            result.text, file_path,
                        )
                        if polished:
                            self._invoke_gui("append_result", (str,
                                f"[LLM 優化] 已保存: {file_path.stem}_polished.txt",
                            ))

                    self._invoke_gui(
                        "update_file_status", (str, fp_str), (str, "完成"),
                    )
                    any_success = True
                else:
                    logger.warning("轉錄失敗或無內容: %s", file_path)
                    self._invoke_gui(
                        "update_file_status", (str, fp_str), (str, "失敗"),
                    )
                    any_failure = True
        finally:
            # iter 3 Bug E：用 lock reset，與 _on_files_dropped 對稱
            with self._transcribing_lock:
                self._is_transcribing = False
            # 批次收尾：解除按鈕忙碌狀態 + 單一可取消 timer 收起進度條
            self._invoke_gui("on_transcribe_batch_finished")
            if any_success:
                batch_status = "完成"
            elif any_failure:
                batch_status = "失敗"
            else:
                batch_status = "就緒"
            self._invoke_gui("set_status", (str, batch_status))

    def _apply_config(self, new_config: AppConfig) -> None:
        """應用新配置（從設置窗口）。

        操作順序：先嘗試不可逆的 ASR 重啟/重建，成功後才提交配置和更新子系統。
        這樣在 ASR 操作失敗時無需回滾任何狀態。
        """
        old_config = self._config

        # 1. 先嘗試不可逆操作：ASR 重啟或重建
        asr_changed = old_config.asr != new_config.asr
        audio_changed = old_config.audio != new_config.audio

        if asr_changed:
            # 模型切換：必須 stop + _init_asr() 重建（restart 不會換模型）
            if not self._apply_config_recreate_asr(new_config):
                return  # 重建失敗，配置未變更
        elif audio_changed:
            if self._asr_process and self._asr_process.is_running:
                logger.info("ASR 音頻配置變更，重啟 ASR 子進程...")
                self._invoke_gui("set_status", (str, "ASR 模型重啟中..."))
                try:
                    self._asr_process.restart()
                except Exception as exc:
                    logger.error("ASR 重啟失敗，配置未變更: %s", exc)
                    self._invoke_gui("set_status", (str, "ASR 重啟失敗，配置未變更"))
                    return  # 不 raise，不 commit，所有子系統保持原狀
                self._invoke_gui("set_status", (str, "就緒"))

        # 2. ASR 成功（或不需要重啟）→ 提交配置
        self._config = new_config
        ConfigManager.save(new_config)
        logger.info("配置已更新並保存")

        # 3. 更新快捷鍵（如果改變了）
        if old_config.shortcut != new_config.shortcut:
            if self._hotkey:
                sc = new_config.shortcut
                self._hotkey.update_config(
                    key=sc.key,
                    threshold=sc.threshold,
                    suppress=sc.suppress,
                )
                logger.info("快捷鍵已更新: key=%s, threshold=%.1f, suppress=%s",
                            sc.key, sc.threshold, sc.suppress)

            # 重新潤色鍵 / 模式 / 閾值 — 任一變更都重建 listener
            old_rp = (old_config.shortcut.repolish_key,
                      old_config.shortcut.repolish_instant,
                      old_config.shortcut.threshold)
            new_rp = (new_config.shortcut.repolish_key,
                      new_config.shortcut.repolish_instant,
                      new_config.shortcut.threshold)
            if old_rp != new_rp:
                self._stop_repolish_hotkey()
                if new_config.shortcut.repolish_key:
                    self._repolish_hotkey = self._make_repolish_hotkey(new_config.shortcut)
                    self._repolish_hotkey.start()
                    logger.info("重新潤色快捷鍵已更新: %s", new_config.shortcut.repolish_key)

        # 4. 更新 LLM 處理器（如果配置改變了）
        if old_config.llm != new_config.llm:
            if self._llm is not None:
                self._llm.update_config(new_config.llm)
                logger.info("LLM 配置已即時更新")
            elif new_config.llm.enabled:
                self._init_llm()
                logger.info("LLM 處理器已新建")

        # 5. 更新文字後處理（繁體轉換等）
        if old_config.output != new_config.output:
            from core.text_processor import TextProcessor
            self._text_processor = TextProcessor(new_config.output)
            logger.info("文字處理配置已更新")

        # 6. 更新熱詞管理器（如果配置改變了）
        if old_config.hotword != new_config.hotword:
            if self._hotword:
                self._hotword.reload(new_config.hotword)
                logger.info("熱詞管理器已重新載入")

        # 7. 更新托盤角色選單
        if old_config.llm.active_role != new_config.llm.active_role or \
                old_config.llm.custom_roles != new_config.llm.custom_roles or \
                old_config.llm.builtin_overrides != new_config.llm.builtin_overrides:
            self._refresh_tray_roles()
            logger.info("角色已切換: %s", new_config.llm.active_role)

    def _apply_config_recreate_asr(self, new_config: AppConfig) -> bool:
        """ASR 模型切換時，stop 舊進程 + _init_asr() 建新進程。

        Bug 6 修復：先停止舊進程再創建新進程，避免 RAM doubling。

        Returns:
            True 重建成功；False 失敗。
        """
        old_config = self._config
        old_asr_process = self._asr_process
        logger.info("ASR 模型配置變更，重建 ASR 子進程...")
        self._invoke_gui("set_status", (str, "ASR 模型重建中..."))

        # Bug 6 修復：先停止舊進程，釋放記憶體
        if old_asr_process and old_asr_process.is_running:
            try:
                # 先取消註冊舊進程的 shutdown callback
                if hasattr(self, "_lifecycle"):
                    self._lifecycle.unregister_shutdown(old_asr_process.stop)
                old_asr_process.stop()
                logger.info("舊 ASR 進程已停止")
            except Exception as exc:
                logger.warning("停止舊 ASR 進程時出錯（已忽略）: %s", exc)

        # 臨時設置 config 讓 _init_asr 讀到新模型（失敗時會恢復）
        self._config = new_config
        try:
            self._asr_process = None
            self._init_asr()
        except Exception as exc:
            # Rollback: 恢復舊 config
            logger.error("ASR 重建失敗，配置已回滾: %s", exc)
            self._config = old_config
            self._invoke_gui("set_status", (str, "ASR 重建失敗，配置未變更"))
            return False

        # 檢查新進程是否成功啟動
        if self._asr_process is None or not self._asr_process.is_running:
            # Rollback: 恢復舊 config
            logger.error("ASR 重建失敗（新進程未成功啟動），配置已回滾")
            self._config = old_config
            self._invoke_gui("set_status", (str, "ASR 重建失敗，配置未變更"))
            return False

        self._invoke_gui("set_status", (str, "就緒"))
        return True

    def _refresh_tray_roles(self) -> None:
        """刷新托盤角色切換子選單。"""
        if not self._main_window or not self._config:
            return
        try:
            from llm.roles import get_all_roles
            all_roles = get_all_roles(
                self._config.llm.custom_roles,
                self._config.llm.builtin_overrides,
            )
            role_items = [
                (rid, cfg.name or rid, is_builtin)
                for rid, cfg, is_builtin in all_roles
            ]
            tray = self._main_window._tray
            tray.update_roles(role_items, self._config.llm.active_role)
        except Exception as err:
            logger.warning("更新角色選單失敗: %s", err)

    def _on_role_switch(self, role_id: str) -> None:
        """托盤選單快速切換角色。"""
        if not self._config:
            return
        new_llm = replace(self._config.llm, active_role=role_id)
        new_config = replace(self._config, llm=new_llm)
        self._config = new_config
        ConfigManager.save(new_config)
        self._refresh_tray_roles()
        logger.info("角色已從托盤切換: %s", role_id)

        if self._main_window:
            tray = self._main_window._tray
            tray.show_message("CC語音", f"角色已切換：{role_id}")

    def _on_startup_toggle(self, enable: bool) -> None:
        """托盤選單切換開機自動啟動。"""
        from utils.startup import set_startup
        set_startup(enable)
        status = "已啟用" if enable else "已停用"
        logger.info("開機自動啟動: %s", status)

        if self._main_window:
            tray = self._main_window._tray
            tray.show_message("CC語音", f"開機自動啟動{status}")

    def _cleanup_gui(self) -> None:
        """清理 GUI 資源。"""
        if self._main_window:
            self._main_window.force_close()

    # ─── 等待循環 ──────────────────────────────────────────

    def _wait_loop(self) -> None:
        """等待退出信號（終端模式）。"""
        while not self._lifecycle.is_shutting_down:
            time.sleep(0.1)

    # ─── 清理回調 ──────────────────────────────────────────

    def _on_shutdown_save_config(self) -> None:
        """關閉時保存配置。"""
        if self._config is not None:
            ConfigManager.save(self._config)

    # ─── 啟動橫幅 ──────────────────────────────────────────

    def _print_banner(self) -> None:
        """顯示啟動資訊到日誌。"""
        sc = self._config.shortcut
        key_display = sc.key.replace("_", " ").title()

        rec_ok = self._recorder is not None and self._recorder.is_open
        asr_ok = self._asr_process is not None and self._asr_process.is_running
        hw_ok = self._hotword is not None
        llm_ok = self._llm is not None
        gui_ok = self._qt_app is not None

        logger.info(
            "CC語音 v%s 啟動完成 | 快捷鍵=%s 閾值=%.2fs | "
            "錄音=%s ASR=%s 熱詞=%s LLM=%s GUI=%s",
            self._config.version, key_display, sc.threshold,
            "OK" if rec_ok else "NO",
            "OK" if asr_ok else "NO",
            "OK" if hw_ok else "OFF",
            "OK" if llm_ok else "OFF",
            "OK" if gui_ok else "OFF",
        )


def _show_crash_message(error: str) -> None:
    """啟動失敗時用 Win32 MessageBox 通知用戶（Console 可能已禁用）。"""
    try:
        from utils.paths import LOG_DIR
        ctypes.windll.user32.MessageBoxW(
            None,
            f"CC語音啟動失敗：\n{error}\n\n詳見日誌：{LOG_DIR}",
            "CC語音 - 啟動錯誤",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass
