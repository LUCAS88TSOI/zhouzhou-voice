"""
州州語音 - ASR 引擎封裝

封裝 sherpa-onnx 各類語音識別模型的載入和識別。
支援 SenseVoice、Paraformer、Whisper、Zipformer 四種引擎。
此模組在 ASR 子進程中運行，與主進程隔離。

用法（僅在子進程中）：
    from core.model_catalog import get_model_info
    engine = ASREngine(model_dir, model_info=get_model_info("whisper-tiny"))
    engine.load_model()
    result = engine.recognize(audio_bytes)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import numpy as np

if TYPE_CHECKING:
    from core.model_catalog import ModelInfo


# sherpa-onnx Whisper 支援的語言代碼（ISO 639-1，來自 OpenAI Whisper）
_WHISPER_LANGUAGES = frozenset({
    "", "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl",
    "ca", "nl", "ar", "sv", "it", "id", "hi", "fi", "vi", "he", "uk", "el",
    "ms", "cs", "ro", "da", "hu", "ta", "no", "th", "ur", "hr", "bg", "lt",
    "la", "mi", "ml", "cy", "sk", "te", "fa", "lv", "bn", "sr", "az", "sl",
    "kn", "et", "mk", "br", "eu", "is", "hy", "ne", "mn", "bs", "kk", "sq",
    "sw", "gl", "mr", "pa", "si", "km", "sn", "yo", "so", "af", "oc", "ka",
    "be", "tg", "sd", "gu", "am", "yi", "lo", "uz", "fo", "ht", "ps", "tk",
    "nn", "mt", "sa", "lb", "my", "bo", "tl", "mg", "as", "tt", "haw", "ln",
    "ha", "ba", "jw", "su", "yue",
})


# ─── 識別結果 ──────────────────────────────────────────────

@dataclass(frozen=True)
class RecognitionResult:
    """單段音頻的識別結果。"""
    text: str = ""
    tokens: List[str] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    duration: float = 0.0


# ─── ASR 引擎 ─────────────────────────────────────────────

class ASREngine:
    """
    多引擎語音識別封裝。

    根據 model_info.engine_type 自動選用對應的 sherpa-onnx 工廠：
    - sense_voice : SenseVoice（預設，向下相容）
    - paraformer  : Paraformer 中文
    - whisper     : Whisper 系列
    - zipformer   : Zipformer Transducer

    設計為在獨立子進程中運行。
    """

    SAMPLE_RATE = 16000
    NUM_THREADS = 4

    def __init__(
        self,
        model_dir: str | Path,
        model_info: Optional[ModelInfo] = None,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._model_info = model_info
        self._recognizer = None

    @property
    def is_loaded(self) -> bool:
        """模型是否已載入。"""
        return self._recognizer is not None

    def load_model(self) -> None:
        """
        根據 engine_type 載入對應的 sherpa-onnx 識別器。

        Raises:
            FileNotFoundError: 模型文件不存在
            ImportError: sherpa_onnx 未安裝
            ValueError: 未知的 engine_type
        """
        import sherpa_onnx

        info = self._model_info
        d = self._model_dir

        if info is None:
            # 向下相容：無 model_info 時走舊路徑（SenseVoice）
            self._load_sense_voice(
                sherpa_onnx, d / "model.onnx", d / "tokens.txt"
            )
            return

        model_path = d / info.model_file
        tokens_path = d / info.tokens_file

        self._check_file(model_path)
        self._check_file(tokens_path)

        match info.engine_type:
            case "sense_voice":
                lang = info.language if info.language else "auto"
                use_itn = info.use_itn if hasattr(info, "use_itn") else True
                self._load_sense_voice(
                    sherpa_onnx, model_path, tokens_path,
                    language=lang, use_itn=use_itn,
                )

            case "paraformer":
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    model=str(model_path),
                    tokens=str(tokens_path),
                    num_threads=self.NUM_THREADS,
                    provider="cpu",
                    debug=False,
                )

            case "whisper":
                if not info.decoder_file:
                    raise ValueError(f"Whisper 引擎需要 decoder_file，模型: {info.key!r}")
                decoder_path = d / info.decoder_file
                self._check_file(decoder_path)
                # None = 預設 zh；"" = 自動偵測；"zh"/"en" = 強制指定
                lang = info.language if info.language is not None else "zh"
                if lang and lang not in _WHISPER_LANGUAGES:
                    raise ValueError(
                        f"不支援的 Whisper 語言代碼: {lang!r}（模型: {info.key!r}）。"
                        f"僅接受 ISO 639-1 代碼如 'zh'、'en'、'ja'。"
                    )
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                    encoder=str(model_path),
                    decoder=str(decoder_path),
                    tokens=str(tokens_path),
                    language=lang,
                    task="transcribe",
                    num_threads=self.NUM_THREADS,
                    provider="cpu",
                    debug=False,
                )

            case "zipformer":
                if not info.decoder_file or not info.joiner_file:
                    raise ValueError(f"Zipformer 引擎需要 decoder_file 和 joiner_file，模型: {info.key!r}")
                decoder_path = d / info.decoder_file
                joiner_path = d / info.joiner_file
                self._check_file(decoder_path)
                self._check_file(joiner_path)
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(model_path),
                    decoder=str(decoder_path),
                    joiner=str(joiner_path),
                    tokens=str(tokens_path),
                    num_threads=self.NUM_THREADS,
                    provider="cpu",
                    debug=False,
                )

            case _:
                raise ValueError(f"未知的 engine_type: {info.engine_type!r}")

    def recognize(
        self, audio_data: bytes, sample_rate: int = 16000
    ) -> RecognitionResult:
        """
        識別一段音頻。

        Args:
            audio_data: float32 PCM 音頻位元組
            sample_rate: 取樣率（預設 16000）

        Returns:
            RecognitionResult 包含文字、token、時間戳

        Raises:
            RuntimeError: 模型未載入
        """
        if self._recognizer is None:
            raise RuntimeError("模型尚未載入，請先呼叫 load_model()")

        samples = np.frombuffer(audio_data, dtype=np.float32)
        duration = len(samples) / sample_rate

        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)

        result = stream.result
        text = result.text.strip()
        tokens = list(result.tokens) if result.tokens else []
        timestamps = list(result.timestamps) if result.timestamps else []

        # 若模型未返回時間戳，產生均勻的合成時間戳
        if text and not timestamps:
            chars = list(text)
            time_per_char = duration / len(chars) if chars else 0
            tokens = chars
            timestamps = [i * time_per_char for i in range(len(chars))]

        return RecognitionResult(
            text=text,
            tokens=tokens,
            timestamps=timestamps,
            duration=duration,
        )

    def close(self) -> None:
        """釋放模型資源。"""
        self._recognizer = None

    # ─── 內部工具 ──────────────────────────────────────────

    def _load_sense_voice(
        self, sherpa_onnx, model_path: Path, tokens_path: Path,
        language: str = "zh", use_itn: bool = True,
    ) -> None:
        self._check_file(model_path)
        self._check_file(tokens_path)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            use_itn=use_itn,
            language=language,
            num_threads=self.NUM_THREADS,
            provider="cpu",
            debug=False,
        )

    @staticmethod
    def _check_file(path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(
                f"模型文件不存在: {path}\n請確認模型已正確安裝到 {path.parent} 目錄"
            )
