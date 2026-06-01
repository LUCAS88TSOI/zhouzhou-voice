"""
州州語音 - ASR 子進程管理

管理語音識別模型的獨立子進程，透過 Queue 進行通信。
模型崩潰不影響主程式（進程隔離設計）。

通信模式：
    queue_in:  主進程 → ASR（識別請求）
    queue_out: ASR → 主進程（識別結果）

用法：
    asr = ASRProcess(model_dir)
    asr.start()                        # 啟動子進程，等待模型載入
    asr.send(ASRRequest(...))          # 發送識別請求
    response = asr.receive(timeout=5)  # 接收識別結果
    asr.stop()                         # 停止子進程
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from multiprocessing import Event, Process, Queue
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger("asr_process")


# ─── IPC 資料結構 ──────────────────────────────────────────

@dataclass
class ASRRequest:
    """主進程 → ASR 子進程 的識別請求。"""
    task_id: str
    audio_data: bytes       # float32, 16kHz, mono
    sample_rate: int = 16000
    is_final: bool = True
    seg_duration: float = 5.0
    seg_overlap: float = 1.0
    offset: float = 0.0


@dataclass
class ASRResponse:
    """ASR 子進程 → 主進程 的識別結果。"""
    task_id: str
    text: str = ""
    tokens: List[str] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    duration: float = 0.0
    is_final: bool = True
    error: str = ""


def new_task_id() -> str:
    """產生唯一任務 ID。"""
    return uuid.uuid4().hex[:12]


# ─── ASR 子進程管理器 ──────────────────────────────────────

class ASRProcess:
    """
    ASR 子進程管理器。

    負責：
    - 啟動/停止 ASR 子進程
    - 透過 Queue 發送請求和接收結果
    - 監測子進程健康狀態
    - 支援崩潰後重啟
    """

    MODEL_LOAD_TIMEOUT = 120  # 模型載入超時（秒）

    def __init__(
        self,
        model_dir: str | Path,
        model_info=None,  # Optional[ModelInfo]，避免頂層 import
    ) -> None:
        self._model_dir = Path(model_dir)
        self._model_info = model_info
        self._process: Optional[Process] = None
        self._queue_in: Queue = Queue()
        self._queue_out: Queue = Queue()
        self._ready_event: Event = Event()
        # 序列化 send_and_wait — 避免 live 錄音與文件轉錄 worker 同時
        # 進入共享通道時，_drain_stale() 把對方等待的回應清掉。
        self._call_lock: threading.Lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """子進程是否在運行。"""
        return self._process is not None and self._process.is_alive()

    def start(self) -> None:
        """
        啟動 ASR 子進程並等待模型載入完成。

        Raises:
            RuntimeError: 模型載入超時或子進程異常退出
        """
        if self.is_running:
            logger.warning("ASR 子進程已在運行")
            return

        logger.info("正在啟動 ASR 子進程，模型: %s", self._model_dir)
        self._ready_event.clear()

        self._process = Process(
            target=_worker_main,
            args=(
                self._model_dir,
                self._model_info,
                self._queue_in,
                self._queue_out,
                self._ready_event,
            ),
            daemon=True,
            name="asr-worker",
        )
        self._process.start()
        logger.info("ASR 子進程已啟動 (PID: %d)", self._process.pid)

        # 等待模型載入完成
        if not self._ready_event.wait(timeout=self.MODEL_LOAD_TIMEOUT):
            self.stop()
            raise RuntimeError(
                f"ASR 模型載入超時（{self.MODEL_LOAD_TIMEOUT} 秒）"
            )

        if not self._process.is_alive():
            error_msg = "ASR 子進程在模型載入期間異常退出"
            try:
                resp = self._queue_out.get_nowait()
                if resp and resp.error:
                    error_msg = f"ASR 模型載入失敗: {resp.error}"
            except Exception as exc:
                logger.debug("無法從 queue 讀取載入錯誤: %s", exc)
            raise RuntimeError(error_msg)

        logger.info("ASR 模型載入完成")

    def send(self, request: ASRRequest) -> None:
        """
        發送識別請求。

        Args:
            request: ASR 識別請求

        Raises:
            RuntimeError: 子進程未運行
        """
        if not self.is_running:
            raise RuntimeError("ASR 子進程未運行")
        self._queue_in.put(request)

    def receive(self, timeout: Optional[float] = None) -> Optional[ASRResponse]:
        """
        接收識別結果。

        Args:
            timeout: 等待超時（秒），None 表示無限等待

        Returns:
            ASRResponse 或 None（超時）
        """
        try:
            return self._queue_out.get(timeout=timeout)
        except queue.Empty:
            return None

    def send_and_wait(
        self, request: ASRRequest, timeout: float = 30.0
    ) -> ASRResponse:
        """
        發送請求並等待結果（同步操作）。

        發送前清空過期響應，接收後驗證 task_id，
        防止超時後殘留的舊結果污染下一次識別。

        Args:
            request: 識別請求
            timeout: 等待超時（秒）

        Returns:
            ASRResponse

        Raises:
            TimeoutError: 等待超時
        """
        deadline = time.monotonic() + timeout
        with self._call_lock:
            self._drain_stale()
            self.send(request)

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"ASR 回應超時（{timeout} 秒）")
                response = self.receive(timeout=remaining)
                if response is None:
                    raise TimeoutError(f"ASR 回應超時（{timeout} 秒）")
                if response.task_id == request.task_id:
                    return response
                logger.warning(
                    "丟棄過期 ASR 回應: task_id=%s", response.task_id,
                )

    def _drain_stale(self) -> None:
        """清空 queue_out 中所有過期響應。"""
        drained = 0
        while True:
            try:
                self._queue_out.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.warning("清空 %d 個過期 ASR 回應", drained)

    def stop(self) -> None:
        """
        停止子進程。

        優雅關閉 → 終止 → 強制殺死（三級遞進）。
        """
        if not self.is_running:
            return

        logger.info("正在停止 ASR 子進程...")

        # 1. 發送哨兵值（優雅關閉）
        try:
            self._queue_in.put(None)
        except Exception:
            pass

        # 2. 等待優雅退出
        self._process.join(timeout=5)

        # 3. 終止
        if self._process.is_alive():
            logger.warning("ASR 子進程未回應，正在終止")
            self._process.terminate()
            self._process.join(timeout=3)

        # 4. 強制殺死
        if self._process.is_alive():
            logger.error("ASR 子進程仍然存活，強制殺死")
            self._process.kill()

        self._process = None
        logger.info("ASR 子進程已停止")

    def restart(self) -> None:
        """重啟子進程（用於崩潰恢復）。"""
        self.stop()
        self.start()


# ─── 子進程工作函數 ────────────────────────────────────────

def _worker_main(
    model_dir: Path,
    model_info,  # Optional[ModelInfo]
    queue_in: Queue,
    queue_out: Queue,
    ready_event: Event,
) -> None:
    """
    ASR 工作循環（在子進程中運行）。

    流程：
    1. 載入模型
    2. 通知主進程「準備就緒」
    3. 循環處理請求，直到收到 None 哨兵值
    """
    from utils.logger import setup_logging
    setup_logging()  # 子進程也需要文件日誌

    from core.asr_engine import ASREngine

    engine = ASREngine(model_dir, model_info)

    try:
        engine.load_model()
    except Exception as err:
        logger.error("ASR 模型載入失敗: %s", err)
        queue_out.put(ASRResponse(
            task_id="__load_error__",
            error=str(err),
        ))
        ready_event.set()
        return

    ready_event.set()

    # 主處理循環
    while True:
        try:
            request = queue_in.get(timeout=1.0)
        except queue.Empty:
            continue

        # 哨兵值 = 停止
        if request is None:
            break

        try:
            result = engine.recognize(
                request.audio_data, request.sample_rate
            )
            response = ASRResponse(
                task_id=request.task_id,
                text=result.text,
                tokens=list(result.tokens),
                timestamps=list(result.timestamps),
                duration=result.duration,
                is_final=request.is_final,
            )
        except Exception as err:
            response = ASRResponse(
                task_id=request.task_id,
                text="",
                is_final=request.is_final,
                error=str(err),
            )

        queue_out.put(response)

    engine.close()
