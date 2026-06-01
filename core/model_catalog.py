"""
model_catalog -- ASR 模型註冊表與元資料。

定義所有與 sherpa-onnx 相容的語音識別模型，
提供運行時掃描已安裝模型，以及預估資源用量。

用法：
    from core.model_catalog import get_installed_models, KNOWN_MODELS
    installed = get_installed_models(models_dir)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger("model_catalog")


# ─── 模型元資料 ───────────────────────────────────────────

@dataclass(frozen=True)
class ModelInfo:
    """單個 ASR 模型的不可變元資料。"""

    key: str                # 配置標識符，如 "sensevoice-small-int8"
    name: str               # 顯示名稱
    engine_type: str        # sherpa-onnx 工廠: "sense_voice" | "paraformer" | "whisper" | "zipformer" | "funasr_nano"
    description: str        # 中文描述
    languages: str          # 支援語言
    size_mb: int            # ONNX 文件大小（MB）
    memory_mb: int          # 預估載入後記憶體用量（MB）
    cpu_threads: int        # 預設 CPU 線程數
    accuracy: str           # 準確度: "高" | "中" | "低"
    speed: str              # 速度: "極快" | "快" | "中" | "慢"
    license: str            # 授權協議
    download_url: str       # 空字串表示已內建
    model_dir: str          # models/ 下的子目錄名
    model_file: str         # 目錄內的主 ONNX 文件名（encoder）
    tokens_file: str        # 目錄內的字典文件名
    decoder_file: str = ""  # Whisper/Zipformer decoder ONNX（空 = 無）
    joiner_file: str = ""   # Zipformer joiner ONNX（空 = 無）
    language: str = ""      # Whisper 語言: "" = 自動偵測, "zh"/"en"/"yue" = 強制指定
    download_files: tuple = ()  # ((url, filename), ...) 多檔案下載（非 tar.bz2）
    use_itn: bool = True        # SenseVoice 標點預測（微調版不支援）


# ─── 已知模型目錄 ─────────────────────────────────────────

KNOWN_MODELS: List[ModelInfo] = [
    # ── 英語專用（預設）─────────────────────────────────
    ModelInfo(
        key="nemo-parakeet-tdt-0.6b-v2-int8",
        name="Parakeet TDT 0.6B v2 (English)",
        engine_type="zipformer",
        description=(
            "NVIDIA Parakeet 英語專用模型，基於 600M 參數 Transducer。"
            "支援標點和大小寫，準確度高，適合專業英語語音輸入。"
        ),
        languages="英文（純英語）",
        size_mb=630,
        memory_mb=900,
        cpu_threads=4,
        accuracy="高",
        speed="快",
        license="CC BY-4.0",
        download_url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2"
        ),
        model_dir="nemo-parakeet-en",
        model_file="encoder.int8.onnx",
        tokens_file="tokens.txt",
        decoder_file="decoder.int8.onnx",
        joiner_file="joiner.int8.onnx",
    ),
    # ── 多語種 ───────────────────────────────────────────
    ModelInfo(
        key="sensevoice-small-int8",
        name="SenseVoice-Small (int8)",
        engine_type="sense_voice",
        description=(
            "阿里達摩院出品，五語種（中英日韓粵）自動偵測。"
            "int8 量化版，體積小、推理極快，適合日常語音輸入。"
        ),
        languages="中文、英文、日文、韓文、粵語",
        size_mb=229,
        memory_mb=350,
        cpu_threads=4,
        accuracy="高",
        speed="極快",
        license="Apache-2.0",
        download_url="",
        model_dir="sensevoice",
        model_file="model.onnx",
        tokens_file="tokens.txt",
    ),
    ModelInfo(
        key="sensevoice-yue-int8",
        name="SenseVoice 廣東話優化版 (int8)",
        engine_type="sense_voice",
        description=(
            "SenseVoice 21.8k 小時廣東話數據微調版。"
            "專為廣東話語音識別優化，中英夾雜表現良好。"
            "體積小、推理極快，適合日常使用。"
        ),
        languages="廣東話、中文、英文、日文、韓文",
        size_mb=226,
        memory_mb=350,
        cpu_threads=4,
        accuracy="高",
        speed="極快",
        license="Apache-2.0",
        download_url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2"
        ),
        model_dir="sensevoice-yue",
        model_file="model.int8.onnx",
        tokens_file="tokens.txt",
        language="yue",
        use_itn=False,
    ),
]

# 依 key 建立查找表
_MODELS_BY_KEY = {m.key: m for m in KNOWN_MODELS}


# ─── 公開工具函數 ──────────────────────────────────────────

def get_model_info(key: str) -> Optional[ModelInfo]:
    """依 *key* 返回模型元資料，未知則返回 ``None``。"""
    return _MODELS_BY_KEY.get(key)


def get_installed_models(models_dir: Path) -> List[ModelInfo]:
    """
    掃描 *models_dir*，返回磁碟上實際存在的模型列表。
    """
    installed: List[ModelInfo] = []
    for model in KNOWN_MODELS:
        onnx_path = models_dir / model.model_dir / model.model_file
        if onnx_path.exists():
            installed.append(model)
    return installed


def get_downloadable_models(models_dir: Path) -> List[ModelInfo]:
    """
    返回有下載連結但尚未安裝的模型列表。
    """
    downloadable: List[ModelInfo] = []
    for model in KNOWN_MODELS:
        if not model.download_url and not model.download_files:
            continue
        onnx_path = models_dir / model.model_dir / model.model_file
        if not onnx_path.exists():
            downloadable.append(model)
    return downloadable


def get_actual_size_mb(models_dir: Path, model: ModelInfo) -> Optional[float]:
    """
    返回 ONNX 文件的實際大小（MB），不存在則返回 ``None``。
    """
    onnx_path = models_dir / model.model_dir / model.model_file
    if not onnx_path.exists():
        return None
    return onnx_path.stat().st_size / (1024 * 1024)
