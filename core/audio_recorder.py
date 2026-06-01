"""
州州語音 - 錄音管理

使用 sounddevice 管理麥克風錄音。
自動從麥克風硬體取樣率（48kHz）降採樣到 ASR 模型要求的 16kHz。

用法：
    recorder = AudioRecorder()
    recorder.open()                   # 開啟音頻流
    recorder.start_recording()        # 開始錄音
    audio_bytes = recorder.stop_recording()  # 停止並取得音頻
    recorder.close()                  # 關閉音頻流
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

from utils.logger import get_logger

logger = get_logger("recorder")


# ─── 抗鋸齒濾波器設計 ────────────────────────────────────

def _design_lowpass_fir(
    cutoff_hz: float,
    sample_rate: float,
    num_taps: int = 31,
) -> np.ndarray:
    """設計 FIR 低通濾波器（窗口 sinc 法）。

    在降採樣前過濾高於 Nyquist 頻率的成分，防止鋸齒失真。

    Args:
        cutoff_hz:   截止頻率（Hz），應設為目標取樣率的一半
        sample_rate: 原始取樣率（Hz）
        num_taps:    濾波器階數（奇數，越大越精確但延遲越高）

    Returns:
        float32 的 FIR 濾波器係數陣列
    """
    fc = cutoff_hz / sample_rate          # 歸一化截止頻率
    n = np.arange(num_taps) - (num_taps - 1) / 2

    # sinc 低通核心 + Hamming 窗
    with np.errstate(invalid="ignore"):
        h = 2 * fc * np.sinc(2 * fc * n)
    h *= np.hamming(num_taps)
    h /= np.sum(h)                        # 歸一化增益為 1

    return h.astype(np.float32)


class AudioRecorder:
    """
    麥克風錄音器。

    特性：
    - 硬體以 48kHz 錄音，回調中自動降採樣到 16kHz
    - 立體聲自動轉單聲道
    - 線程安全的緩衝區管理
    - 支援分段提取（用於長語音串流識別）
    """

    CAPTURE_RATE = 48000    # 麥克風取樣率
    TARGET_RATE = 16000     # ASR 模型取樣率
    DOWNSAMPLE_FACTOR = 3   # 48000 / 16000
    BLOCK_DURATION = 0.05   # 每個音頻塊 50ms
    DTYPE = "float32"

    # 抗鋸齒濾波器：截止 7600Hz（略低於 Nyquist 8000Hz，留過渡帶）
    _FILTER_CUTOFF = 7600
    _FILTER_TAPS = 31

    def __init__(self, max_duration: float = 1800.0) -> None:
        """初始化錄音器。

        Args:
            max_duration: 最大錄音時長（秒）。預設 1800 秒（30 分鐘）作為安全上限。
                達到此上限時會自動停止追加音頻並觸發 ``limit_reached`` 回調。
        """
        self._stream = None
        self._is_recording = False
        self._buffer: List[np.ndarray] = []
        self._start_time: float = 0.0
        self._stop_time: float = 0.0
        self._lock = threading.Lock()
        self._device_channels: int = 1

        # 即時音量監測（VU meter 用）
        self._current_rms: float = 0.0

        # 錄音上限相關
        self._max_duration: float = float(max_duration)
        self._limit_reached: bool = False  # 去重標記，避免多次觸發回調
        self._on_limit_reached: Optional[Callable[[], None]] = None

        # 預計算濾波器係數（只算一次，之後每次回調重複使用）
        self._filter_coeffs: np.ndarray = _design_lowpass_fir(
            cutoff_hz=self._FILTER_CUTOFF,
            sample_rate=self.CAPTURE_RATE,
            num_taps=self._FILTER_TAPS,
        )
        logger.debug(
            "抗鋸齒濾波器已初始化: cutoff=%dHz, taps=%d, max_duration=%.1fs",
            self._FILTER_CUTOFF,
            self._FILTER_TAPS,
            self._max_duration,
        )

    # ─── 上限回調設置 ──────────────────────────────────────

    def set_limit_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """設置達到錄音上限時的回調。

        回調將在音頻線程中觸發，實作方須自行保證線程安全
        （例如透過 ``QMetaObject.invokeMethod`` 切回主線程）。

        Args:
            callback: 無參數無返回值的可呼叫物件，或 ``None`` 以清除回調。
        """
        self._on_limit_reached = callback

    def set_max_duration(self, max_duration: float) -> None:
        """動態更新最大錄音時長（秒）。下次 ``start_recording()`` 生效。"""
        self._max_duration = float(max_duration)

    # ─── 屬性 ──────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        """是否正在錄音。"""
        return self._is_recording

    @property
    def is_open(self) -> bool:
        """音頻流是否已開啟。"""
        return self._stream is not None

    @property
    def recording_duration(self) -> float:
        """當前錄音時長（秒）。

        錄音中：返回從開始到現在的經過時間。
        已停止但 ``_start_time`` 仍有效：返回從開始到停止的時間
        （方便達到上限後仍能取得最終時長）。
        完全未錄過音：返回 0。
        """
        if self._is_recording:
            return time.monotonic() - self._start_time
        if self._start_time > 0.0 and self._stop_time >= self._start_time:
            return self._stop_time - self._start_time
        return 0.0

    @property
    def level_db(self) -> float:
        """當前音量（dB）。靜音時返回 -100。"""
        import math
        rms = self._current_rms
        return 20 * math.log10(rms) if rms > 1e-10 else -100.0

    # ─── 音頻流管理 ────────────────────────────────────────

    def open(self) -> None:
        """
        開啟音頻流（不開始錄音）。

        自動偵測預設輸入裝置的聲道數。
        """
        import sounddevice as sd

        device_info = sd.query_devices(kind="input")
        max_channels = int(device_info.get("max_input_channels", 1))
        self._device_channels = min(2, max(1, max_channels))

        block_size = int(self.BLOCK_DURATION * self.CAPTURE_RATE)

        self._stream = sd.InputStream(
            samplerate=self.CAPTURE_RATE,
            channels=self._device_channels,
            dtype=self.DTYPE,
            blocksize=block_size,
            callback=self._audio_callback,
        )
        self._stream.start()

        logger.info(
            "音頻流已開啟: %dHz, %d 聲道, block=%d",
            self.CAPTURE_RATE,
            self._device_channels,
            block_size,
        )

    def close(self) -> None:
        """關閉音頻流並釋放資源。"""
        if self._stream is not None:
            self._is_recording = False
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logger.info("音頻流已關閉")

    # ─── 錄音控制 ──────────────────────────────────────────

    def start_recording(self) -> None:
        """
        開始錄音。

        若音頻流未開啟會自動開啟。
        清空緩衝區，重新計時，並重置上限觸發狀態。
        """
        if self._stream is None:
            self.open()
        with self._lock:
            self._buffer = []
        self._start_time = time.monotonic()
        self._stop_time = 0.0
        self._limit_reached = False
        self._is_recording = True
        logger.debug("錄音開始 (max_duration=%.1fs)", self._max_duration)

    def stop_recording(self) -> bytes:
        """
        停止錄音並返回音頻資料。

        Returns:
            float32 PCM 位元組，16kHz 單聲道。
            無資料時返回空位元組。
        """
        was_recording = self._is_recording
        self._is_recording = False
        if was_recording:
            self._stop_time = time.monotonic()
        duration = self._stop_time - self._start_time if self._start_time > 0.0 else 0.0

        with self._lock:
            if not self._buffer:
                logger.debug("錄音停止: 無音頻資料")
                return b""
            audio = np.concatenate(self._buffer)
            self._buffer = []

        logger.debug(
            "錄音停止: %.2f 秒, %d 個採樣 (16kHz)",
            duration,
            len(audio),
        )
        return audio.astype(np.float32).tobytes()

    # ─── 分段提取（串流識別用） ────────────────────────────

    def get_buffer_duration(self) -> float:
        """取得當前緩衝區時長（秒）。"""
        with self._lock:
            total_samples = sum(len(chunk) for chunk in self._buffer)
        return total_samples / self.TARGET_RATE

    def get_segments(
        self, seg_duration: float = 5.0, seg_overlap: float = 1.0
    ) -> List[Tuple[bytes, float]]:
        """
        從緩衝區提取已完成的分段。

        每段長度 = seg_duration + seg_overlap，
        步進 = seg_duration（段間重疊 seg_overlap 秒）。

        用於長語音的串流識別：錄音中每累積足夠的音頻，
        就提取一段送去識別，不用等錄音結束。

        Args:
            seg_duration: 分段長度（秒）
            seg_overlap: 重疊長度（秒）

        Returns:
            [(audio_bytes, offset), ...] 列表。
            audio_bytes 為 float32 位元組，offset 為時間偏移。
        """
        with self._lock:
            if not self._buffer:
                return []
            full_audio = np.concatenate(self._buffer)

        seg_samples = int(seg_duration * self.TARGET_RATE)
        overlap_samples = int(seg_overlap * self.TARGET_RATE)
        segment_size = seg_samples + overlap_samples
        stride = seg_samples

        segments: List[Tuple[bytes, float]] = []
        pos = 0

        while pos + segment_size <= len(full_audio):
            chunk = full_audio[pos : pos + segment_size]
            offset = pos / self.TARGET_RATE
            segments.append((chunk.astype(np.float32).tobytes(), offset))
            pos += stride

        # 清理已提取的部分，保留剩餘音頻
        if segments:
            with self._lock:
                if pos < len(full_audio):
                    self._buffer = [full_audio[pos:]]
                else:
                    self._buffer = []

        return segments

    # ─── 音頻回調 ──────────────────────────────────────────

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        _time_info: object,
        status: object,
    ) -> None:
        """
        sounddevice 音頻回調（在音頻線程中執行）。

        處理步驟：
        1. 立體聲 → 單聲道（取平均）
        2. 抗鋸齒低通濾波（去除 >8kHz 高頻）
        3. 48kHz → 16kHz 降採樣
        4. 存入線程安全緩衝區
        """
        # 立體聲 → 單聲道（即時音量監測 + 錄音都需要）
        if indata.ndim > 1 and indata.shape[1] > 1:
            mono = np.mean(indata, axis=1)
        else:
            mono = indata.ravel()

        # 即時 RMS（VU meter 用，無論是否錄音都計算）
        self._current_rms = float(np.sqrt(np.mean(mono ** 2)))

        if not self._is_recording:
            return

        # 超過最大錄音時長：自動停止錄音並觸發一次上限回調
        if time.monotonic() - self._start_time > self._max_duration:
            self._is_recording = False
            self._stop_time = time.monotonic()
            if not self._limit_reached:
                self._limit_reached = True
                logger.warning(
                    "錄音達到上限 %.1f 秒，已自動停止追加音頻",
                    self._max_duration,
                )
                callback = self._on_limit_reached
                if callback is not None:
                    try:
                        callback()
                    except Exception as exc:  # noqa: BLE001 — 音頻線程必須永遠不拋
                        logger.exception("上限回調拋出異常: %s", exc)
            return

        if status:
            logger.warning("音頻回調警告: %s", status)

        # 抗鋸齒低通濾波：去除 >8kHz 的高頻成分，防止降採樣鋸齒失真
        filtered = np.convolve(mono, self._filter_coeffs, mode="same")

        # 降採樣：48kHz → 16kHz
        downsampled = filtered[:: self.DOWNSAMPLE_FACTOR]

        # .copy() 是必須的，sounddevice 會重用 indata 緩衝區
        with self._lock:
            self._buffer.append(downsampled.copy())
