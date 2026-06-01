"""
州州語音 - 自動更新對話框

啟動時發現新版本時彈出，顯示更新內容，支援一鍵下載安裝。
打包模式：下載 zip → 寫 bat 腳本 → 啟動腳本 → 退出 app → 腳本替換文件 → 重啟。
開發模式：直接開瀏覽器下載。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import certifi
import urllib3

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from utils.logger import get_logger
from utils.paths import APP_DATA_DIR, APP_ROOT, APP_VERSION, IS_PACKAGED
from utils.updater import UpdateInfo, is_trusted_download_url

logger = get_logger("update_dialog")


class _DownloadRelay(QObject):
    """背景線程 → Qt 主線程的信號橋接。"""

    progress = Signal(int)        # 0-100
    finished = Signal(bool, str)  # (success, error_or_path)


class UpdateDialog(QDialog):
    """自動更新對話框：顯示版本資訊 + 下載進度 + 一鍵更新。"""

    def __init__(self, info: UpdateInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = info
        self._cancel = threading.Event()
        self._downloading = False

        self.setWindowTitle("軟體更新")
        self.setFixedSize(460, 360)
        self.setWindowFlags(
            self.windowFlags()
            & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        # Signal relay
        self._relay = _DownloadRelay()
        self._relay.progress.connect(self._on_progress)
        self._relay.finished.connect(self._on_finished)

        self._build_ui()

    # ── UI ──

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        title = QLabel("新版本可用！")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        ver = QLabel(f"v{APP_VERSION}  →  v{self._info.remote_version}")
        ver.setStyleSheet("font-size: 14px; color: #666;")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(ver)

        lay.addSpacing(4)

        notes_label = QLabel("更新內容:")
        notes_label.setStyleSheet("font-weight: bold;")
        lay.addWidget(notes_label)

        self._notes = QTextEdit()
        self._notes.setReadOnly(True)
        self._notes.setMaximumHeight(120)
        bullets = "\n".join(f"• {n}" for n in self._info.release_notes)
        self._notes.setPlainText(bullets or "（無更新說明）")
        lay.addWidget(self._notes)

        # Progress bar (hidden initially)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFixedHeight(20)
        self._progress.hide()
        lay.addWidget(self._progress)

        # Status label (hidden initially)
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.hide()
        lay.addWidget(self._status)

        lay.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_later = QPushButton("稍後提醒")
        self._btn_later.clicked.connect(self.reject)
        self._btn_update = QPushButton("立即更新")
        self._btn_update.clicked.connect(self._on_update_clicked)
        self._btn_update.setStyleSheet(
            "QPushButton { background: #0078d4; color: white; "
            "padding: 6px 20px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background: #106ebe; }"
        )
        btn_row.addWidget(self._btn_later)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_update)
        lay.addLayout(btn_row)

    # ── Actions ──

    def _on_update_clicked(self) -> None:
        if self._downloading:
            # Cancel
            self._cancel.set()
            return

        if not is_trusted_download_url(self._info.download_url):
            logger.warning("下載 URL 不可信: %s", self._info.download_url)
            return

        if not IS_PACKAGED:
            webbrowser.open(self._info.download_url)
            self.accept()
            return

        self._start_download()

    def _start_download(self) -> None:
        self._downloading = True
        self._cancel.clear()
        self._btn_update.setText("取消")
        self._btn_later.setEnabled(False)
        self._progress.setValue(0)
        self._progress.show()
        self._status.setText("下載中...")
        self._status.show()

        threading.Thread(
            target=self._download_worker,
            args=(self._info.download_url, self._relay, self._cancel),
            daemon=True,
            name="update-download",
        ).start()

    @staticmethod
    def _download_worker(
        url: str, relay: _DownloadRelay, cancel: threading.Event,
    ) -> None:
        """在 daemon thread 中下載 zip，透過 relay 回報進度。"""
        dest = APP_DATA_DIR / "_update_tmp.zip"
        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            http = urllib3.PoolManager(
                num_pools=1, maxsize=1, retries=False,
                timeout=urllib3.Timeout(connect=10, read=30),
                cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
            )

            # 手動跟蹤 redirect（GitHub releases URL 會返回 302）
            _MAX_REDIRECTS = 5
            current_url = url
            for i in range(_MAX_REDIRECTS):
                # Bug 2 修復：每次 redirect 後重新驗證 URL
                if i > 0 and not is_trusted_download_url(current_url):
                    relay.finished.emit(False, f"Redirect URL 不可信: {current_url}")
                    return

                resp = http.request("GET", current_url, preload_content=False, redirect=False)
                if resp.status == 200:
                    # Bug 1 修復：驗證 Content-Length 避免空文件或明顯錯誤的檔案
                    content_length = int(resp.headers.get("Content-Length", 0))
                    if content_length < 1024:  # 小於 1KB 視為可疑
                        relay.finished.emit(False, f"檔案大小異常: {content_length} bytes")
                        return
                    break
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location")
                    if not location:
                        relay.finished.emit(False, f"HTTP {resp.status} 無 Location")
                        return
                    current_url = location
                    continue
                # 非 2xx / 非 3xx → 視為失敗
                relay.finished.emit(False, f"HTTP {resp.status}")
                return
            else:
                relay.finished.emit(False, "下載重導向次數過多")
                return

            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                for chunk in resp.stream(65536):
                    if cancel.is_set():
                        resp.release_conn()
                        dest.unlink(missing_ok=True)
                        relay.finished.emit(False, "已取消")
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        relay.progress.emit(int(downloaded * 100 / total))
            resp.release_conn()
            relay.finished.emit(True, str(dest))

        except Exception as err:
            dest.unlink(missing_ok=True)
            relay.finished.emit(False, str(err))

    # ── Callbacks (main thread) ──

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)
        self._status.setText(f"下載中... {pct}%")

    def _on_finished(self, ok: bool, msg: str) -> None:
        self._downloading = False
        if not ok:
            if msg == "已取消":
                self._reset_ui()
                return
            self._status.setText(f"下載失敗: {msg}")
            self._btn_update.setText("重試")
            self._btn_later.setEnabled(True)
            logger.warning("更新下載失敗: %s", msg)
            return

        logger.info("更新下載完成: %s", msg)
        self._status.setText("正在準備更新...")
        self._progress.setRange(0, 0)  # indeterminate
        self._btn_update.setEnabled(False)

        try:
            self._apply_update(Path(msg))
        except Exception as err:
            logger.error("套用更新失敗: %s", err)
            self._status.setText(f"更新失敗: {err}")
            self._progress.setRange(0, 100)
            self._btn_update.setText("重試")
            self._btn_update.setEnabled(True)
            self._btn_later.setEnabled(True)

    def _reset_ui(self) -> None:
        self._progress.hide()
        self._status.hide()
        self._btn_update.setText("立即更新")
        self._btn_later.setEnabled(True)

    # ── Apply update ──

    def _apply_update(self, zip_path: Path) -> None:
        """寫 bat 腳本 → 啟動 → 退出 app。"""
        zip_resolved = zip_path.resolve()
        if not zip_resolved.is_relative_to(APP_DATA_DIR.resolve()):
            raise ValueError(f"zip path outside expected dir: {zip_resolved}")

        exe_name = Path(sys.executable).name
        pid = os.getpid()
        extract_dir = APP_DATA_DIR / "_update_tmp"

        # 使用雙引號包裹所有路徑，防止特殊字元注入
        ps_zip = str(zip_resolved).replace('"', '`"')
        ps_extract = str(extract_dir.resolve()).replace('"', '`"')

        script = APP_DATA_DIR / "_update.bat"
        script.write_text(
            f'@echo off\r\n'
            f'echo Waiting for application to close...\r\n'
            f':wait\r\n'
            f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL\r\n'
            f'if not errorlevel 1 (\r\n'
            f'    timeout /t 1 /nobreak >NUL\r\n'
            f'    goto wait\r\n'
            f')\r\n'
            f'echo Extracting update...\r\n'
            f'if exist "{extract_dir}" rmdir /s /q "{extract_dir}"\r\n'
            f'powershell -NoProfile -Command "Expand-Archive'
            f' -Path \\\"{ps_zip}\\\"'
            f' -DestinationPath \\\"{ps_extract}\\\"'
            f' -Force"\r\n'
            f'if errorlevel 1 (\r\n'
            f'    echo Extraction failed!\r\n'
            f'    pause\r\n'
            f'    exit /b 1\r\n'
            f')\r\n'
            f'echo Copying files...\r\n'
            f'for /d %%D in ("{extract_dir}\\*") do (\r\n'
            f'    xcopy /E /Y /I "%%D\\*" "{APP_ROOT}\\" >NUL 2>&1\r\n'
            f')\r\n'
            f'echo Cleaning up...\r\n'
            f'del /q "{zip_resolved}" 2>NUL\r\n'
            f'rmdir /s /q "{extract_dir}" 2>NUL\r\n'
            f'echo Starting application...\r\n'
            f'start "" "{APP_ROOT / exe_name}"\r\n'
            f'del "%~f0"\r\n',
            encoding="utf-8",
        )

        logger.info("啟動更新腳本: %s", script)
        # DETACHED_PROCESS so the bat survives app exit
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )

        self._status.setText("即將重啟...")
        QApplication.instance().quit()

    # ── Close guard ──

    def closeEvent(self, event) -> None:
        if self._downloading:
            self._cancel.set()
        super().closeEvent(event)
