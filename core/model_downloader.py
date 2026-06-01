"""
州州語音 - ASR 模型下載與解壓工具

從遠端 URL 下載 .tar.bz2 模型包並解壓到 models/ 目錄。
也支援從 HuggingFace 等來源逐一下載多個散裝檔案。
設計為在背景線程呼叫（阻塞式），透過回調函數回報進度。

用法：
    import threading
    from core.model_downloader import download_and_extract, download_multi_files

    # tar.bz2 模式
    threading.Thread(
        target=download_and_extract,
        args=(url, models_dir, "paraformer-zh", on_progress, on_done),
        daemon=True,
    ).start()

    # 多檔案模式（如 HuggingFace 散裝 ONNX）
    files = [("https://hf.co/.../encoder.onnx", "encoder.onnx"), ...]
    threading.Thread(
        target=download_multi_files,
        args=(files, models_dir, "whisper-large-v3", on_progress, on_done),
        daemon=True,
    ).start()
"""

from __future__ import annotations

import shutil
import tarfile
import urllib.request
from pathlib import Path
from typing import Callable, Sequence, Tuple

from utils.logger import get_logger

logger = get_logger("model_downloader")


def download_and_extract(
    url: str,
    models_dir: Path,
    target_dir_name: str,
    on_progress: Callable[[int], None],
    on_done: Callable[[bool, str], None],
) -> None:
    """
    阻塞式：下載 .tar.bz2 模型包並解壓到 models/<target_dir_name>/。

    應在背景線程呼叫，不可在主線程直接呼叫。

    Args:
        url: 下載 URL（.tar.bz2）
        models_dir: models/ 目錄路徑
        target_dir_name: 解壓後重命名為此目錄名（如 "paraformer-zh"）
        on_progress: 進度回調，參數為 0-100 整數
        on_done: 完成回調，參數為 (success, error_message)
    """
    archive = models_dir / "_download_tmp.tar.bz2"
    models_dir.mkdir(parents=True, exist_ok=True)

    try:
        logger.info("開始下載模型: %s", url)

        def _report(count: int, block_size: int, total_size: int) -> None:
            if total_size > 0:
                pct = min(int(count * block_size * 100 / total_size), 99)
                on_progress(pct)

        urllib.request.urlretrieve(url, archive, reporthook=_report)
        logger.info("下載完成，開始解壓")

        with tarfile.open(archive, "r:bz2") as tar:
            members = tar.getmembers()
            if not members:
                raise ValueError("壓縮包是空的")
            # 找出所有頂層目錄名（取 set 確保唯一）
            top_dirs = {m.name.split("/")[0] for m in members}
            if len(top_dirs) != 1:
                raise ValueError(
                    f"壓縮包不是單一頂層目錄結構（找到: {top_dirs}）"
                )
            top_dir = next(iter(top_dirs))
            # filter="data" 防止路徑遍歷（Python 3.12+）
            tar.extractall(models_dir, filter="data")

        # 重命名到目標目錄
        extracted = models_dir / top_dir
        target = models_dir / target_dir_name
        if target.exists():
            shutil.rmtree(target)
        extracted.rename(target)
        logger.info("解壓完成: %s", target)

        on_progress(100)
        on_done(True, "")

    except Exception as exc:
        logger.error("模型下載失敗: %s", exc, exc_info=True)
        on_done(False, str(exc))

    finally:
        if archive.exists():
            try:
                archive.unlink()
            except OSError:
                pass


def download_multi_files(
    files: Sequence[Tuple[str, str]],
    models_dir: Path,
    target_dir_name: str,
    on_progress: Callable[[int], None],
    on_done: Callable[[bool, str], None],
) -> None:
    """
    阻塞式：逐一下載多個檔案到 models/<target_dir_name>/。

    適用於 HuggingFace 等以散裝檔案提供的模型（非 tar.bz2）。
    應在背景線程呼叫，不可在主線程直接呼叫。

    Args:
        files: ((url, filename), ...) 下載清單
        models_dir: models/ 目錄路徑
        target_dir_name: 目標子目錄名（如 "whisper-large-v3"）
        on_progress: 進度回調，參數為 0-100 整數
        on_done: 完成回調，參數為 (success, error_message)
    """
    target = models_dir / target_dir_name
    target.mkdir(parents=True, exist_ok=True)

    n = len(files)

    try:
        for i, (url, filename) in enumerate(files):
            dest = target / filename
            logger.info("下載檔案 [%d/%d]: %s", i + 1, n, filename)

            # 進度：每個檔案平均分配（避免 HEAD 請求不一致問題）
            def _report(
                count: int,
                block_size: int,
                total_size: int,
                _idx: int = i,
            ) -> None:
                file_pct = (
                    min(count * block_size * 100 / total_size, 100)
                    if total_size > 0 else 0
                )
                overall = min(int((_idx * 100 + file_pct) / n), 99)
                on_progress(overall)

            urllib.request.urlretrieve(url, dest, reporthook=_report)
            logger.info("完成: %s", filename)

        on_progress(100)
        on_done(True, "")
        logger.info("多檔案下載完成: %s（共 %d 個）", target, n)

    except Exception as exc:
        logger.error("多檔案下載失敗: %s", exc, exc_info=True)
        # 移除整個目標目錄，避免半成品被誤判為已安裝
        try:
            if target.exists():
                shutil.rmtree(target)
        except OSError:
            pass
        on_done(False, str(exc))
