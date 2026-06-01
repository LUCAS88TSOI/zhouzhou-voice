"""
州州語音 - 文件轉錄器

將音頻/視頻文件轉錄為文字。使用 FFmpeg 提取音頻，
按 60 秒分段送入 ASR 子進程識別，合併重疊段落，
最後輸出 SRT/TXT/JSON 文件。

支援的格式（取決於 FFmpeg）：
  音頻: mp3, wav, m4a, flac, ogg, aac
  視頻: mp4, mkv, avi, mov, wmv, webm

用法：
    transcriber = FileTranscriber(asr_process)
    transcriber.transcribe(
        file_path="video.mp4",
        on_progress=lambda p, m: print(f"{p:.0%} {m}"),
    )
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger("file_transcriber")


# ─── 常量 ──────────────────────────────────────────────────

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 4           # float32
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE  # 64000

# 分段參數
DEFAULT_SEG_DURATION = 60      # 每段 60 秒
DEFAULT_SEG_OVERLAP = 4        # 重疊 4 秒

# LLM 跨段潤色滑動窗口大小：每段送 LLM 時，前綴包含最近 N 段的
# (原始識別 / 潤色結果) 對照，讓術語、人名、語氣保持跨段一致。
_LLM_CONTEXT_WINDOW = 2

# 支援的媒體格式
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac",
    ".wma", ".opus", ".amr",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".flv", ".ts", ".m4v", ".3gp", ".3g2", ".mts",
})
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


# ─── 資料結構 ──────────────────────────────────────────────

@dataclass(frozen=True)
class SegmentResult:
    """單段識別結果。"""
    offset: float               # 在原始音頻中的起始時間（秒）
    text: str = ""
    tokens: List[str] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    duration: float = 0.0


@dataclass(frozen=True)
class TranscribeResult:
    """完整轉錄結果。"""
    text: str = ""
    tokens: List[str] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    duration: float = 0.0
    segment_count: int = 0
    elapsed: float = 0.0


# 進度回調類型：(progress_ratio: float, message: str) -> None
ProgressCallback = Callable[[float, str], None]


# ─── FFmpeg 工具 ───────────────────────────────────────────

# 解析快取：None 表示尚未查過；False 表示查過且無任何可用 ffmpeg
_ffmpeg_path_cache: object = None


def _reset_ffmpeg_cache() -> None:
    """重置 ffmpeg 路徑快取（僅測試用）。"""
    global _ffmpeg_path_cache
    _ffmpeg_path_cache = None


def _get_imageio_ffmpeg_exe() -> Optional[str]:
    """取得 imageio-ffmpeg 內建的 ffmpeg 執行檔路徑；套件未安裝或失敗回 None。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as err:  # pylint: disable=broad-except
        logger.debug("imageio-ffmpeg 無法使用: %s", err)
        return None


def resolve_ffmpeg_path() -> Optional[str]:
    """
    解析 ffmpeg 執行檔路徑（混合策略）：
    1. 優先使用系統 PATH 中的 ffmpeg（效能佳、codec 支援最全）
    2. 若未安裝則 fallback 至 imageio-ffmpeg 套件內建版
    3. 兩者都無則回 None

    結果會快取，避免重複查找。
    """
    global _ffmpeg_path_cache
    if _ffmpeg_path_cache is not None:
        return _ffmpeg_path_cache if _ffmpeg_path_cache else None

    system_path = shutil.which("ffmpeg")
    if system_path:
        logger.debug("使用系統 FFmpeg: %s", system_path)
        _ffmpeg_path_cache = system_path
        return system_path

    bundled = _get_imageio_ffmpeg_exe()
    if bundled:
        logger.info("系統未安裝 FFmpeg，使用 imageio-ffmpeg 內建版: %s", bundled)
        _ffmpeg_path_cache = bundled
        return bundled

    logger.warning("找不到任何可用的 FFmpeg（系統 PATH 與 imageio-ffmpeg 都不可用）")
    _ffmpeg_path_cache = False  # type: ignore[assignment]
    return None


def check_ffmpeg() -> bool:
    """檢查 FFmpeg 是否可用（含 fallback）。"""
    return resolve_ffmpeg_path() is not None


def check_ffprobe() -> bool:
    """檢查 ffprobe 是否可用（可選，用於進度條）。"""
    return shutil.which("ffprobe") is not None


def _parse_ffmpeg_duration(stderr: str) -> float:
    """從 ffmpeg stderr 輸出中解析 Duration: HH:MM:SS.xx。

    Args:
        stderr: ffmpeg 的 stderr 文字輸出

    Returns:
        時長（秒），解析失敗返回 0.0
    """
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mins, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return h * 3600 + mins * 60 + s
    return 0.0


def probe_duration(file_path: Path) -> float:
    """
    取得媒體文件時長（秒）。

    策略：
    1. 優先使用系統 ffprobe（最精確）
    2. ffprobe 不可用時 fallback 到 ffmpeg -i（解析 stderr Duration 行）
    3. 都不可用時返回 0.0

    Args:
        file_path: 媒體文件路徑

    Returns:
        時長（秒），失敗返回 0.0
    """
    # 策略 1: ffprobe
    if check_ffprobe():
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
                creationflags=_get_creation_flags(),
            )
            return float(result.stdout.strip())
        except (ValueError, subprocess.TimeoutExpired, OSError) as err:
            logger.debug("ffprobe 取得時長失敗: %s", err)

    # 策略 2: ffmpeg -i（適用於 imageio-ffmpeg fallback）
    ffmpeg_exe = resolve_ffmpeg_path()
    if ffmpeg_exe is not None:
        try:
            cmd = [
                ffmpeg_exe, "-i", str(file_path),
                "-hide_banner", "-f", "null", "-",
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                creationflags=_get_creation_flags(),
            )
            duration = _parse_ffmpeg_duration(result.stderr)
            if duration > 0:
                logger.debug("ffmpeg -i 取得時長: %.1fs", duration)
                return duration
        except (subprocess.TimeoutExpired, OSError) as err:
            logger.debug("ffmpeg -i 取得時長失敗: %s", err)

    return 0.0


def extract_audio_pcm(file_path: Path) -> Optional[subprocess.Popen]:
    """
    啟動 FFmpeg 子進程，將媒體文件轉為 16kHz mono float32 PCM。

    音頻資料透過 stdout pipe 串流輸出，不寫入中間文件。

    Args:
        file_path: 輸入媒體文件

    Returns:
        Popen 物件（stdout 可讀取 PCM 資料），失敗返回 None
    """
    ffmpeg_exe = resolve_ffmpeg_path()
    if ffmpeg_exe is None:
        logger.error("FFmpeg 不可用，無法提取音頻")
        return None

    cmd = [
        ffmpeg_exe,
        "-i", str(file_path),
        "-f", "f32le",          # raw float32 little-endian
        "-ac", "1",             # mono
        "-ar", str(SAMPLE_RATE),
        "-v", "error",          # 只輸出錯誤
        "-",                    # 輸出到 stdout
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_get_creation_flags(),
        )
        return process
    except OSError as err:
        logger.error("啟動 FFmpeg 失敗: %s", err)
        return None


def _get_creation_flags() -> int:
    """Windows 上隱藏 FFmpeg 控制台窗口。"""
    import sys
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


# ─── 分段器 ────────────────────────────────────────────────

def segment_audio(
    raw_pcm: bytes,
    seg_duration: float = DEFAULT_SEG_DURATION,
    seg_overlap: float = DEFAULT_SEG_OVERLAP,
) -> List[Tuple[float, bytes]]:
    """
    將 PCM 音頻切割為重疊分段。

    Args:
        raw_pcm: 完整的 float32 PCM 資料
        seg_duration: 每段基本時長（秒）
        seg_overlap: 重疊時長（秒）

    Returns:
        [(offset_seconds, segment_bytes), ...]
    """
    total_bytes = len(raw_pcm)
    if total_bytes == 0:
        return []

    seg_bytes = int(seg_duration * BYTES_PER_SECOND)
    overlap_bytes = int(seg_overlap * BYTES_PER_SECOND)
    stride_bytes = seg_bytes  # 步進 = 段長（重疊由擴展段實現）
    seg_with_overlap = seg_bytes + overlap_bytes  # 實際取出的段長

    # 短音頻：一段搞定
    if total_bytes <= seg_with_overlap:
        return [(0.0, raw_pcm)]

    segments = []
    offset_bytes = 0

    while offset_bytes < total_bytes:
        end_bytes = min(offset_bytes + seg_with_overlap, total_bytes)
        chunk = raw_pcm[offset_bytes:end_bytes]

        offset_seconds = offset_bytes / BYTES_PER_SECOND
        segments.append((offset_seconds, chunk))

        offset_bytes += stride_bytes

        # 最後不足一步的部分已在上面處理
        if end_bytes >= total_bytes:
            break

    return segments


def segment_audio_from_file(
    pcm_path: Path,
    seg_duration: float = DEFAULT_SEG_DURATION,
    seg_overlap: float = DEFAULT_SEG_OVERLAP,
) -> List[Tuple[float, bytes]]:
    """
    從 temp file 讀取 PCM 並切割為重疊分段（不一次載入整個文件）。
    """
    total_bytes = pcm_path.stat().st_size
    if total_bytes == 0:
        return []

    seg_bytes = int(seg_duration * BYTES_PER_SECOND)
    overlap_bytes = int(seg_overlap * BYTES_PER_SECOND)
    stride_bytes = seg_bytes
    seg_with_overlap = seg_bytes + overlap_bytes

    if total_bytes <= seg_with_overlap:
        return [(0.0, pcm_path.read_bytes())]

    segments = []
    with open(pcm_path, "rb") as f:
        offset_bytes = 0
        while offset_bytes < total_bytes:
            f.seek(offset_bytes)
            end_bytes = min(offset_bytes + seg_with_overlap, total_bytes)
            chunk = f.read(end_bytes - offset_bytes)
            segments.append((offset_bytes / BYTES_PER_SECOND, chunk))
            offset_bytes += stride_bytes
            if end_bytes >= total_bytes:
                break

    return segments


def _unique_path(path: Path) -> Path:
    """如果 path 已存在，附加 _1, _2, ... 序號直到不衝突。"""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(1, 10000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    return path  # fallback: 覆寫（極端情況）


# ─── Token 合併 ────────────────────────────────────────────

def merge_segment_tokens(
    segments: List[SegmentResult],
    overlap: float = DEFAULT_SEG_OVERLAP,
) -> Tuple[str, List[str], List[float]]:
    """
    合併多段識別結果，去除重疊區域的重複 token。

    使用 SequenceMatcher 進行重疊區 token 去重，
    確保時間戳連續且不重複。

    Args:
        segments: 排序好的分段結果列表
        overlap: 重疊秒數

    Returns:
        (merged_text, merged_tokens, merged_timestamps)
    """
    if not segments:
        return "", [], []

    if len(segments) == 1:
        seg = segments[0]
        # 轉為全域時間戳（local + offset）
        global_ts = [seg.offset + t for t in seg.timestamps]
        return seg.text, list(seg.tokens), global_ts

    # 初始化為第一段（轉為全域時間戳）
    first = segments[0]
    all_tokens: List[str] = list(first.tokens)
    all_timestamps: List[float] = [first.offset + t for t in first.timestamps]

    for i in range(1, len(segments)):
        prev_seg = segments[i - 1]
        curr_seg = segments[i]

        if not curr_seg.tokens:
            continue

        # 重疊區時間範圍
        overlap_start = curr_seg.offset
        overlap_end = curr_seg.offset + overlap

        # 找出 prev 中落在重疊區的 token 索引
        prev_overlap_tokens = []
        for j, ts in enumerate(prev_seg.timestamps):
            global_ts = prev_seg.offset + ts
            if global_ts >= overlap_start:
                prev_overlap_tokens.append(prev_seg.tokens[j])

        # 找出 curr 中落在重疊區的 token 索引
        curr_overlap_end_idx = 0
        for j, ts in enumerate(curr_seg.timestamps):
            global_ts = curr_seg.offset + ts
            if global_ts < overlap_end:
                curr_overlap_end_idx = j + 1
            else:
                break

        # 使用精確匹配找出重疊（參考 app._merge_text_overlap_parts）
        curr_start = 0
        partial_prefix = ""  # 部分重疊 token 的非重疊後綴
        if prev_overlap_tokens and curr_overlap_end_idx > 0:
            prev_text = "".join(prev_overlap_tokens)
            curr_text = "".join(curr_seg.tokens[:curr_overlap_end_idx])

            # 找出最長精確匹配
            max_overlap = 0
            max_check = min(len(prev_text), len(curr_text))  # 完整檢查，不限制長度
            for i in range(max_check, 0, -1):
                if prev_text[-i:] == curr_text[:i]:
                    max_overlap = i
                    break

            # 根據重疊字符數估算要跳過的 token 數量
            if max_overlap > 0:
                skipped_chars = 0
                for i, token in enumerate(curr_seg.tokens[:curr_overlap_end_idx]):
                    skipped_chars += len(token)
                    if skipped_chars >= max_overlap:
                        # 如果 token 只有部分在重疊區，保留非重疊後綴
                        overlap_in_token = max_overlap - (skipped_chars - len(token))
                        if overlap_in_token < len(token):
                            partial_prefix = token[overlap_in_token:]
                        curr_start = i + 1
                        break

        # 追加部分重疊 token 的非重疊後綴
        if partial_prefix:
            all_tokens.append(partial_prefix)
            # 使用下一個 token 的時間戳，若無則用重疊區結束時間
            if curr_start < len(curr_seg.timestamps):
                all_timestamps.append(curr_seg.offset + curr_seg.timestamps[curr_start])
            else:
                all_timestamps.append(curr_seg.offset + overlap)

        # 追加 curr 中非重疊的 token（帶全域時間戳）
        for j in range(curr_start, len(curr_seg.tokens)):
            all_tokens.append(curr_seg.tokens[j])
            all_timestamps.append(curr_seg.offset + curr_seg.timestamps[j])

    merged_text = "".join(all_tokens)
    return merged_text, all_tokens, all_timestamps


# ─── 跨段 LLM 潤色（滑動窗口上下文） ───────────────────────

def _build_context_prefix(
    history: List[Tuple[str, str]],
) -> str:
    """
    將最近 N 段的 (原始, 潤色) 對照拼成文字前綴，用於下一段請求。

    前綴形式：
        【前文上下文】以下是先前段落的對照，僅供你參考上下文、術語、人名、
        語氣的一致性；請勿重新輸出這些內容。
        -- 段 1 原文 --
        ...
        -- 段 1 潤色 --
        ...
        ...
        【本段原文】

    若 history 為空則回空字串（第一段直接送原文）。
    """
    if not history:
        return ""

    lines: list[str] = [
        "【前文上下文】以下是先前段落的對照，僅供你參考上下文、"
        "術語、人名、語氣的一致性；請勿重新輸出這些內容。",
    ]
    for idx, (raw, polished) in enumerate(history, start=1):
        lines.append(f"-- 段 {idx} 原文 --")
        lines.append(raw)
        lines.append(f"-- 段 {idx} 潤色 --")
        lines.append(polished)
    lines.append("【本段原文（請僅針對此段輸出潤色結果）】")
    return "\n".join(lines) + "\n"


def polish_transcription_with_context(
    chunks: List[str],
    llm_processor,
    role,
    on_chunk_start: Optional[Callable[[int, int], None]] = None,
) -> List[str]:
    """
    對多段轉錄文本做 LLM 潤色，段間以滑動窗口（N=2）注入上下文。

    設計（方案 B）：
    - 不修改 `LLMProcessor._conversation_history`（避免污染主錄音歷史）。
    - 每段呼叫 `llm_processor.process()`，將前 N 段 (原文, 潤色) 拼成
      context 前綴放入 user message 最前面。
    - 強制使用 `enable_history=False` 的 role 副本，確保本次潤色流程
      完全不讀寫 processor 的對話歷史狀態。
    - 任一段失敗 → 該段 fallback 回原始文本，不中斷其他段。

    Args:
        chunks:          已切分好的原始轉錄段落（順序即朗讀順序）
        llm_processor:   `llm.processor.LLMProcessor` 實例
        role:            `llm.processor.RoleConfig` 角色配置
        on_chunk_start:  每段開始時回調 (index, total)，用於進度條

    Returns:
        與 chunks 等長的潤色結果列表；失敗段保留原文。
    """
    if not chunks:
        return []

    # 強制關閉 history 讀寫，避免污染主錄音對話歷史
    safe_role = dataclasses.replace(role, enable_history=False) \
        if role is not None else None

    polished_out: List[str] = []
    # 本地滑動窗口：[(raw, polished), ...]，長度 ≤ _LLM_CONTEXT_WINDOW
    history: List[Tuple[str, str]] = []

    total = len(chunks)
    for idx, raw in enumerate(chunks):
        if on_chunk_start is not None:
            try:
                on_chunk_start(idx, total)
            except Exception as cb_err:
                logger.warning("on_chunk_start 回調異常: %s", cb_err)

        prefix = _build_context_prefix(history)
        user_text = f"{prefix}{raw}" if prefix else raw

        logger.debug(
            "LLM 潤色段 %d/%d: context=%d 段, 原文長=%d, 前綴長=%d",
            idx + 1, total, len(history), len(raw), len(prefix),
        )

        try:
            result = llm_processor.process(
                text=user_text,
                role=safe_role,
            )
            if result is None or not result.text or result.error:
                err_msg = (result.error if result else "unknown") if result else "no result"
                logger.warning(
                    "段 %d LLM 潤色失敗，fallback 原文: %s",
                    idx + 1, err_msg,
                )
                polished = raw
            else:
                polished = result.text
        except Exception as err:
            logger.error(
                "段 %d LLM 潤色異常，fallback 原文: %s",
                idx + 1, err, exc_info=True,
            )
            polished = raw

        polished_out.append(polished)

        # 更新滑動窗口（僅保留最近 N 段）
        history.append((raw, polished))
        if len(history) > _LLM_CONTEXT_WINDOW:
            history = history[-_LLM_CONTEXT_WINDOW:]

    return polished_out


# ─── 文件轉錄器 ────────────────────────────────────────────

class FileTranscriber:
    """
    文件轉錄器。

    將音頻/視頻文件透過 FFmpeg + ASR 轉錄為文字，
    並產生 SRT/TXT/JSON 輸出文件。

    Args:
        asr_process: 已啟動的 ASRProcess 實例
        seg_duration: 分段時長（秒），預設 60
        seg_overlap: 重疊時長（秒），預設 4
    """

    def __init__(
        self,
        asr_process,
        seg_duration: float = DEFAULT_SEG_DURATION,
        seg_overlap: float = DEFAULT_SEG_OVERLAP,
    ) -> None:
        self._asr = asr_process
        self._seg_duration = seg_duration
        self._seg_overlap = seg_overlap
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """取消正在進行的轉錄（線程安全）。"""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def transcribe(
        self,
        file_path: str | Path,
        on_progress: Optional[ProgressCallback] = None,
        save_srt: bool = True,
        save_txt: bool = True,
        save_json: bool = True,
    ) -> Optional[TranscribeResult]:
        """
        轉錄一個媒體文件。

        流程：
        1. 檢查 FFmpeg
        2. ffprobe 取得時長（進度條用）
        3. FFmpeg 提取 PCM → 記憶體
        4. 分段送入 ASR
        5. 合併段落
        6. 輸出 SRT/TXT/JSON

        Args:
            file_path: 媒體文件路徑
            on_progress: 進度回調 (ratio, message)
            save_srt: 是否保存 .srt
            save_txt: 是否保存 .txt
            save_json: 是否保存 .json

        Returns:
            TranscribeResult 或 None（失敗/取消時）
        """
        self._cancel_event.clear()
        file_path = Path(file_path)
        start_time = time.monotonic()

        # ── 1. 前置檢查 ──
        # 所有早退路徑都呼叫 on_progress(1.0, ...) 終結進度，避免 MainWindow
        # 進度條永遠卡在 < 100% 且不被 hide timer 觸發。
        if not file_path.exists():
            logger.error("文件不存在: %s", file_path)
            if on_progress:
                on_progress(1.0, f"失敗: 文件不存在 — {file_path.name}")
            return None

        if file_path.suffix.lower() not in MEDIA_EXTENSIONS:
            logger.error("不支援的格式: %s", file_path.suffix)
            if on_progress:
                on_progress(1.0, f"失敗: 不支援的格式 {file_path.suffix}")
            return None

        if not check_ffmpeg():
            logger.error("FFmpeg 不可用（系統 PATH 與 imageio-ffmpeg 皆無）")
            if on_progress:
                on_progress(1.0, "失敗: FFmpeg 不可用")
            return None

        if on_progress:
            on_progress(0.0, f"準備轉錄: {file_path.name}")

        # ── 2. 取得時長 ──
        total_duration = probe_duration(file_path)
        logger.info(
            "開始轉錄: %s (時長: %.1f 秒)",
            file_path.name, total_duration,
        )

        # ── 3. FFmpeg 提取 PCM（寫入 temp file，避免大文件佔滿 RAM）──
        if on_progress:
            on_progress(0.05, "提取音頻中...")

        pcm_path = self._extract_full_audio(file_path, on_progress)
        if pcm_path is None:
            if on_progress:
                on_progress(1.0, "失敗: 音頻提取失敗")
            return None

        try:
            if self.is_cancelled:
                logger.info("轉錄已取消")
                if on_progress:
                    on_progress(1.0, "已取消")
                return None

            actual_duration = pcm_path.stat().st_size / BYTES_PER_SECOND
            logger.info("音頻提取完成: %.1f 秒", actual_duration)

            # ── 4. 分段 + ASR 識別 ──
            if on_progress:
                on_progress(0.2, "分段識別中...")

            segment_results = self._recognize_segments(
                pcm_path, actual_duration, on_progress,
            )

            if self.is_cancelled:
                logger.info("轉錄已取消")
                if on_progress:
                    on_progress(1.0, "已取消")
                return None

            if not segment_results:
                logger.warning("未識別到任何內容")
                if on_progress:
                    on_progress(1.0, "未識別到內容")
                return TranscribeResult(
                    duration=actual_duration,
                    elapsed=time.monotonic() - start_time,
                )

            # ── 5. 合併段落 ──
            if on_progress:
                on_progress(0.9, "合併結果...")

            merged_text, merged_tokens, merged_timestamps = merge_segment_tokens(
                segment_results, self._seg_overlap,
            )

            elapsed = time.monotonic() - start_time
            logger.info(
                "轉錄完成: %d 段, %.1f 秒音頻, 耗時 %.1f 秒",
                len(segment_results), actual_duration, elapsed,
            )

            result = TranscribeResult(
                text=merged_text,
                tokens=merged_tokens,
                timestamps=merged_timestamps,
                duration=actual_duration,
                segment_count=len(segment_results),
                elapsed=elapsed,
            )

            # ── 6. 保存輸出文件 ──
            if on_progress:
                on_progress(0.95, "保存文件...")

            from transcribe.srt_writer import OutputWriter
            writer = OutputWriter(merged_tokens, merged_timestamps)

            base_path = file_path.with_suffix("")

            if save_txt:
                txt_path = _unique_path(base_path.with_suffix(".txt"))
                writer.save_txt(txt_path)
                logger.info("已保存: %s", txt_path.name)

            if save_json:
                json_path = _unique_path(base_path.with_suffix(".json"))
                writer.save_json(json_path)
                logger.info("已保存: %s", json_path.name)

            if save_srt:
                srt_path = _unique_path(base_path.with_suffix(".srt"))
                writer.save_srt(srt_path)
                logger.info("已保存: %s", srt_path.name)

            if on_progress:
                on_progress(1.0, f"轉錄完成 ({elapsed:.1f} 秒)")

            return result
        finally:
            # 清理 temp PCM file
            pcm_path.unlink(missing_ok=True)

    # ─── 內部方法 ──────────────────────────────────────────

    def _extract_full_audio(
        self,
        file_path: Path,
        on_progress: Optional[ProgressCallback],
    ) -> Optional[Path]:
        """
        用 FFmpeg 提取音頻到 temp file（避免大文件佔滿 RAM）。

        Args:
            file_path: 媒體文件
            on_progress: 進度回調

        Returns:
            temp file Path（呼叫方負責刪除），失敗返回 None
        """
        import os
        import tempfile

        ffmpeg_exe = resolve_ffmpeg_path()
        if ffmpeg_exe is None:
            logger.error("FFmpeg 不可用，無法提取音頻")
            return None

        fd, tmp_path = tempfile.mkstemp(suffix=".pcm")
        os.close(fd)

        cmd = [
            ffmpeg_exe,
            "-i", str(file_path),
            "-f", "f32le",
            "-ac", "1",
            "-ar", str(SAMPLE_RATE),
            "-v", "error",
            "-y", tmp_path,
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_get_creation_flags(),
            )
            _, stderr_data = process.communicate(timeout=3600)

            if process.returncode != 0:
                err_msg = stderr_data.decode("utf-8", errors="replace")
                logger.error("FFmpeg 失敗 (code %d): %s",
                             process.returncode, err_msg[:200])
                if on_progress:
                    on_progress(0.0, f"FFmpeg 錯誤: {err_msg[:50]}")
                Path(tmp_path).unlink(missing_ok=True)
                return None

            result_path = Path(tmp_path)
            if result_path.stat().st_size == 0:
                logger.error("FFmpeg 輸出為空")
                result_path.unlink(missing_ok=True)
                return None

            return result_path

        except subprocess.TimeoutExpired:
            process.kill()
            logger.error("FFmpeg 超時（1 小時限制）")
            Path(tmp_path).unlink(missing_ok=True)
            return None
        except Exception as err:
            logger.error("FFmpeg 異常: %s", err)
            Path(tmp_path).unlink(missing_ok=True)
            return None

    def _recognize_segments(
        self,
        pcm_source: bytes | Path,
        total_duration: float,
        on_progress: Optional[ProgressCallback],
    ) -> List[SegmentResult]:
        """
        分段識別音頻。

        Args:
            pcm_source: PCM bytes 或 temp file Path
            total_duration: 總時長
            on_progress: 進度回調

        Returns:
            分段識別結果列表
        """
        from core.asr_process import ASRRequest, new_task_id

        if isinstance(pcm_source, Path):
            segments = segment_audio_from_file(
                pcm_source, self._seg_duration, self._seg_overlap,
            )
        else:
            segments = segment_audio(
                pcm_source, self._seg_duration, self._seg_overlap,
            )
        logger.info("分為 %d 段", len(segments))

        results: List[SegmentResult] = []

        for idx, (offset, chunk) in enumerate(segments):
            if self.is_cancelled:
                break

            seg_duration = len(chunk) / BYTES_PER_SECOND
            logger.debug(
                "識別第 %d/%d 段 (offset=%.1fs, dur=%.1fs)",
                idx + 1, len(segments), offset, seg_duration,
            )

            # 進度：0.2 ~ 0.9 之間
            if on_progress and total_duration > 0:
                progress = 0.2 + 0.7 * (offset / total_duration)
                on_progress(
                    min(progress, 0.89),
                    f"識別中 {idx + 1}/{len(segments)} "
                    f"({offset:.0f}s/{total_duration:.0f}s)",
                )

            try:
                request = ASRRequest(
                    task_id=new_task_id(),
                    audio_data=chunk,
                    sample_rate=SAMPLE_RATE,
                    is_final=True,
                    seg_duration=self._seg_duration,
                    seg_overlap=self._seg_overlap,
                    offset=offset,
                )

                response = self._asr.send_and_wait(request, timeout=120.0)

                if response.error:
                    logger.warning(
                        "段 %d 識別錯誤: %s", idx + 1, response.error,
                    )
                    continue

                if response.text:
                    # 保持 LOCAL 時間戳，merge_segment_tokens 負責加 offset
                    seg_result = SegmentResult(
                        offset=offset,
                        text=response.text,
                        tokens=list(response.tokens),
                        timestamps=list(response.timestamps),
                        duration=seg_duration,
                    )
                    results.append(seg_result)
                    logger.debug(
                        "段 %d 結果: %s", idx + 1, response.text[:50],
                    )

            except TimeoutError:
                logger.warning("段 %d 識別超時", idx + 1)
            except Exception as err:
                logger.error(
                    "段 %d 識別異常: %s", idx + 1, err, exc_info=True,
                )

        return results
