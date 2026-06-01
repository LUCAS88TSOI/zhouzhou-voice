"""
州州語音 - 自動更新檢查

在應用啟動時於背景線程檢查是否有新版本可用。
透過 Qt Signal 安全地將結果回傳到主線程。

用法：
    from utils.updater import check_for_update

    def on_result(info):
        if info and info.available:
            tray.show_update_available(info.remote_version, info.download_url)

    check_for_update(on_result)
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import certifi
import urllib3

from PySide6.QtCore import QObject, Signal

from utils.logger import get_logger
from utils.paths import APP_VERSION

logger = get_logger("updater")

# 版本資料 URL
VERSION_CHECK_URL = "https://zhouzhou-voice.vercel.app/version.json"

# 允許的下載來源主機（防止版本伺服器被篡改後導向惡意 URL）
_ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
})


@dataclass(frozen=True)
class UpdateInfo:
    """版本檢查結果。"""

    available: bool              # 是否有比當前更新的版本
    remote_version: str          # 遠端版本號，如 "3.1"
    release_notes: tuple[str, ...]  # 更新說明（繁中，不可變）
    download_url: str            # 下載連結


def is_trusted_download_url(url: str) -> bool:
    """驗證下載 URL 是否來自可信主機。"""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc in _ALLOWED_DOWNLOAD_HOSTS


class _UpdateRelay(QObject):
    """橋接 daemon thread → Qt 主線程的中繼器。"""

    result_ready = Signal(object)  # UpdateInfo | None


# 保留 relay 物件引用，防止在 signal 發送前被 GC 回收（僅主線程操作）
_active_relays: list[_UpdateRelay] = []


def _is_newer(remote: str, local: str) -> bool:
    """
    判斷 remote 版本是否比 local 更新。

    優先使用 packaging.version，fallback 用 tuple 比較。
    異常時返回 False（保守策略，不觸發假更新提示）。
    """
    try:
        from packaging.version import Version  # type: ignore[import-untyped]
        return Version(remote) > Version(local)
    except Exception:
        pass

    # Fallback: 按 "." 分割後逐段比較整數
    def _to_tuple(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    try:
        return _to_tuple(remote) > _to_tuple(local)
    except Exception:
        return False


def _fetch_thread(relay: _UpdateRelay) -> None:
    """
    在 daemon thread 中執行 HTTP 請求，完成後透過 relay 通知主線程。

    任何異常都靜默處理（記 warning log），emit None 表示檢查失敗。
    """
    info: UpdateInfo | None = None
    try:
        http = urllib3.PoolManager(
            num_pools=1,
            maxsize=1,
            retries=False,
            timeout=urllib3.Timeout(connect=5, read=10),
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
        )
        resp = http.request("GET", VERSION_CHECK_URL, preload_content=True)

        if resp.status != 200:
            logger.warning("版本檢查返回 HTTP %d", resp.status)
            relay.result_ready.emit(None)
            return

        data = json.loads(resp.data.decode("utf-8"))
        remote = str(data["version"])
        notes = tuple(data.get("release_notes", {}).get("zh", []))
        dl_url: str = data.get(
            "download_url",
            "https://github.com/LUCAS88TSOI/zhouzhou-voice/releases/latest",
        )

        if not is_trusted_download_url(dl_url):
            logger.warning("下載 URL 不在允許清單: %s", dl_url)
            relay.result_ready.emit(None)
            return

        info = UpdateInfo(
            available=_is_newer(remote, APP_VERSION),
            remote_version=remote,
            release_notes=notes,
            download_url=dl_url,
        )
        logger.debug(
            "版本檢查完成: remote=%s local=%s available=%s",
            remote,
            APP_VERSION,
            info.available,
        )

    except Exception as err:
        logger.warning("版本檢查失敗（網路問題，已忽略）: %s", err)

    relay.result_ready.emit(info)


def check_for_update(
    callback: Callable[[UpdateInfo | None], None],
) -> None:
    """
    在背景線程中檢查更新，完成後在 Qt 主線程呼叫 callback。

    必須在 QApplication 建立後、Qt 事件循環啟動前呼叫。
    signal 會在事件循環啟動後自動在主線程發送。

    Args:
        callback: 接收 UpdateInfo | None 的函數
                  - UpdateInfo.available=True  → 有新版本
                  - UpdateInfo.available=False → 已是最新
                  - None                       → 網路錯誤（靜默忽略）
    """
    relay = _UpdateRelay()
    _active_relays.append(relay)

    def _on_result(info: UpdateInfo | None) -> None:
        try:
            callback(info)
        finally:
            # 完成後移除 relay 引用
            if relay in _active_relays:
                _active_relays.remove(relay)

    relay.result_ready.connect(_on_result)

    thread = threading.Thread(
        target=_fetch_thread,
        args=(relay,),
        daemon=True,
        name="update-checker",
    )
    thread.start()
    logger.debug("更新檢查線程已啟動（URL: %s）", VERSION_CHECK_URL)
