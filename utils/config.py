"""
州州語音 - 配置管理器

使用 frozen dataclass 定義配置結構，JSON 文件持久化。
配置存放於 %APPDATA%/zhouzhou-voice/config.json

設計原則：
- 不可變性：所有配置 dataclass 使用 frozen=True
- 修改配置時使用 dataclasses.replace() 建立新物件
- 向前兼容：載入時深度合併，新增的配置項自動獲得預設值
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import get_logger
from utils.paths import APP_VERSION

logger = get_logger("config")


# ─── 預設 LLM 服務商 ──────────────────────────────────────

# ─── 服務商參數支援矩陣（容錯重試用）────────────────────────

PROVIDER_PARAM_SUPPORT: Dict[str, frozenset] = {
    "openai":      frozenset({"temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"}),
    "deepseek":    frozenset({"temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"}),
    "anthropic":   frozenset({"temperature", "max_tokens", "top_p"}),
    "google":      frozenset({"temperature", "max_tokens", "top_p"}),
    "zhipu":       frozenset({"temperature", "max_tokens", "top_p", "do_sample"}),
    "bigmodel":    frozenset({"temperature", "max_tokens", "top_p", "do_sample"}),
    "moonshot":    frozenset({"temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"}),
    "siliconflow": frozenset({"temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"}),
    "groq":        frozenset({"temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"}),
    "custom":      frozenset({"temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty", "do_sample"}),
}


DEFAULT_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "api_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "enabled": True,
    },
    "deepseek": {
        "name": "DeepSeek",
        "api_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "enabled": True,
    },
    "anthropic": {
        "name": "Anthropic",
        "api_url": "https://api.anthropic.com/v1",
        "api_key": "",
        "model": "claude-3-haiku-20240307",
        "enabled": True,
    },
    "google": {
        "name": "Google",
        "api_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "",
        "model": "gemini-1.5-flash",
        "enabled": True,
    },
    "zhipu": {
        "name": "智譜",
        "api_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash-250414",
        "enabled": True,
    },
    "bigmodel": {
        "name": "BigModel",
        "api_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash-250414",
        "enabled": True,
    },
    "moonshot": {
        "name": "月之暗面",
        "api_url": "https://api.moonshot.cn/v1",
        "api_key": "",
        "model": "moonshot-v1-8k",
        "enabled": True,
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "api_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "deepseek-ai/DeepSeek-V2.5",
        "enabled": True,
    },
    "groq": {
        "name": "Groq",
        "api_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "model": "llama-3.1-8b-instant",
        "enabled": True,
    },
    "custom": {
        "name": "自定義",
        "api_url": "",
        "api_key": "",
        "model": "",
        "enabled": True,  # 預設啟用，填寫 API Key 後即可使用
    },
}


# ─── 配置 Dataclass ───────────────────────────────────────

@dataclass(frozen=True)
class ShortcutConfig:
    """快捷鍵配置"""
    key: str = "caps_lock"
    threshold: float = 0.3
    suppress: bool = False
    repolish_key: str = "f2"          # 重新潤色快捷鍵，空字串 = 停用
    repolish_instant: bool = True       # True = 速發（鬆開觸發），False = 長按觸發

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            object.__setattr__(self, "key", "caps_lock")
        if not isinstance(self.repolish_key, str):
            object.__setattr__(self, "repolish_key", "")


@dataclass(frozen=True)
class ASRConfig:
    """語音識別配置"""
    model: str = "nemo-parakeet-tdt-0.6b-v2-int8"
    language: str = "auto"


@dataclass(frozen=True)
class LLMConfig:
    """LLM 潤色配置"""
    enabled: bool = True
    active_provider: str = "bigmodel"
    active_role: str = "default"
    stop_key: str = "esc"
    temperature: float = 0.3
    max_tokens: int = 1024
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    do_sample: bool = True
    providers: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()}
    )
    custom_roles: list[Dict[str, Any]] = field(default_factory=list)
    builtin_overrides: Dict[str, str] = field(default_factory=dict)
    repolish_provider: str = ""         # 重新潤色專用服務商，空字串 = 使用 active_provider
    repolish_model: str = ""            # 重新潤色專用模型，空字串 = 使用服務商預設模型
    repolish_role: str = ""             # 重新潤色專用角色，空字串 = 使用 active_role
    polish_timeout: float = 10.0        # 語音潤色逾時上限（秒），超時直接貼原文；0 = 不限制


@dataclass(frozen=True)
class HotwordConfig:
    """熱詞配置"""
    enabled: bool = True
    threshold: float = 0.85
    similar_threshold: float = 0.6


@dataclass(frozen=True)
class OutputConfig:
    """輸出配置"""
    paste_mode: bool = True
    # 預設不還原：識別結果留喺剪貼板，避免「貼上後 0.15s 還原」的時序競爭
    # （慢應用未貼完就被還原 → 貼出舊內容）。想還原者可喺設定頁勾選。
    restore_clip: bool = False
    traditional_convert: bool = True
    traditional_locale: str = "zh-hk"
    format_num: bool = True
    format_spell: bool = True
    trash_punc: str = "，。,."


@dataclass(frozen=True)
class FileConfig:
    """文件轉錄配置"""
    save_srt: bool = True
    save_txt: bool = True
    save_json: bool = False
    llm_polish: bool = False


@dataclass(frozen=True)
class UIConfig:
    """UI 偏好設定"""
    indicator_x: int = 100
    indicator_y: int = 100
    show_indicator: bool = True


@dataclass(frozen=True)
class AudioConfig:
    """錄音與長音頻分段配置"""
    max_recording_seconds: int = 1800       # 安全上限：30 分鐘，達上限自動停止 + 通知
    long_audio_threshold: float = 60.0      # 觸發分段識別的閾值（秒）
    segment_seconds: float = 60.0           # 長音頻分段長度（秒）
    segment_overlap: float = 1.0            # 段間重疊（秒），緩解切點處字詞截斷

    def __post_init__(self) -> None:
        # frozen=True 下必須用 object.__setattr__ 繞過 setattr 限制
        # 對所有數值做邊界 clamp，避免 JSON 傳入壞值導致極端切段 / 無限迴圈
        def _safe_int(val: object, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(val))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        def _safe_float(val: object, default: float, minimum: float = 0.0) -> float:
            try:
                return max(minimum, float(val))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        max_rec = _safe_int(self.max_recording_seconds, 1800, 1)
        threshold = _safe_float(self.long_audio_threshold, 60.0, 1.0)
        seg = _safe_float(self.segment_seconds, 60.0, 1.0)
        # overlap 必須 >= 0 且嚴格小於 segment_seconds（否則 stride 會退化到 1）
        overlap = _safe_float(self.segment_overlap, 1.0, 0.0)
        if overlap >= seg:
            overlap = max(0.0, seg - 0.1)
        object.__setattr__(self, "max_recording_seconds", max_rec)
        object.__setattr__(self, "long_audio_threshold", threshold)
        object.__setattr__(self, "segment_seconds", seg)
        object.__setattr__(self, "segment_overlap", overlap)


@dataclass(frozen=True)
class HistoryConfig:
    """錄音歷史配置"""
    enabled: bool = True              # 是否啟用錄音歷史
    min_duration: float = 0.5         # 最短錄音長度閾值（秒），低於此不儲存
    max_records: int = 1000           # 最大保留記錄數
    auto_cleanup_days: int = 30       # 自動清理超過 N 天的記錄（0 = 停用）


@dataclass(frozen=True)
class AppConfig:
    """應用總配置（不可變）"""
    version: str = APP_VERSION
    setup_complete: bool = False
    shortcut: ShortcutConfig = field(default_factory=ShortcutConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    hotword: HotwordConfig = field(default_factory=HotwordConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    file: FileConfig = field(default_factory=FileConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)


# ─── 內部工具函數 ──────────────────────────────────────────

def _deep_merge(base: Dict, override: Dict) -> Dict:
    """
    深度合併字典。override 的值覆蓋 base 的值。
    兩邊都是 dict 時遞歸合併，否則 override 直接覆蓋。
    """
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _pick_fields(cls: type, data: Dict[str, Any]) -> Dict[str, Any]:
    """只保留 dataclass 已定義的欄位，忽略未知 key。"""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__}


def _dict_to_config(data: Dict[str, Any]) -> AppConfig:
    """
    將 JSON 字典轉換為 AppConfig。
    安全解析：忽略未知欄位，缺失欄位用預設值。
    """
    def _safe_section(key: str) -> Dict[str, Any]:
        """取得區段資料，非 dict 型別退回空 dict。"""
        val = data.get(key, {})
        return val if isinstance(val, dict) else {}

    shortcut_data = _safe_section("shortcut")
    asr_data = _safe_section("asr")
    llm_data = _safe_section("llm")
    hotword_data = _safe_section("hotword")
    output_data = _safe_section("output")
    file_data = _safe_section("file")
    ui_data = _safe_section("ui")
    history_data = _safe_section("history")
    audio_data = _safe_section("audio")

    # LLM providers: 先用預設值，再覆蓋用戶設定
    user_providers = llm_data.get("providers", {})
    if not isinstance(user_providers, dict):
        user_providers = {}
    merged_providers = _deep_merge(
        {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()},
        user_providers,
    )

    # 過濾 LLM 欄位（providers 單獨處理）
    llm_fields = {
        k: v for k, v in llm_data.items()
        if k in LLMConfig.__dataclass_fields__ and k != "providers"
    }

    return AppConfig(
        version=APP_VERSION,
        setup_complete=data.get("setup_complete", False),
        shortcut=ShortcutConfig(**_pick_fields(ShortcutConfig, shortcut_data)),
        asr=ASRConfig(**_pick_fields(ASRConfig, asr_data)),
        llm=LLMConfig(**llm_fields, providers=merged_providers),
        hotword=HotwordConfig(**_pick_fields(HotwordConfig, hotword_data)),
        output=OutputConfig(**_pick_fields(OutputConfig, output_data)),
        file=FileConfig(**_pick_fields(FileConfig, file_data)),
        ui=UIConfig(**_pick_fields(UIConfig, ui_data)),
        history=HistoryConfig(**_pick_fields(HistoryConfig, history_data)),
        audio=AudioConfig(**_pick_fields(AudioConfig, audio_data)),
    )


def _config_to_dict(config: AppConfig) -> Dict[str, Any]:
    """將 AppConfig 轉換為可 JSON 序列化的字典。"""
    return asdict(config)


# ─── 配置管理器 ────────────────────────────────────────────

class ConfigManager:
    """
    配置管理器 — 負責 JSON 配置的讀取、保存和重置。

    配置文件位置: %APPDATA%/zhouzhou-voice/config.json

    所有方法都是類方法（classmethod），無需實例化。
    修改配置時返回新的 AppConfig（不可變設計）。
    """

    CONFIG_DIR = Path.home() / "AppData" / "Roaming" / "zhouzhou-voice"
    CONFIG_FILE = CONFIG_DIR / "config.json"

    @classmethod
    def load(cls) -> AppConfig:
        """
        載入配置。文件不存在時返回預設配置並創建文件。

        Returns:
            AppConfig 實例（不可變）
        """
        if not cls.CONFIG_FILE.exists():
            logger.info("配置文件不存在，建立預設配置: %s", cls.CONFIG_FILE)
            config = AppConfig()
            cls.save(config)
            return config

        try:
            raw = cls.CONFIG_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            config = _dict_to_config(data)
            logger.info("配置載入成功: %s", cls.CONFIG_FILE)
            return config
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as err:
            logger.error("配置文件解析失敗，使用預設配置: %s", err)
            return AppConfig()

    @classmethod
    def save(cls, config: AppConfig) -> None:
        """
        保存配置到 JSON 文件。

        Args:
            config: 要保存的 AppConfig 實例
        """
        cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = _config_to_dict(config)
        cls.CONFIG_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("配置已保存: %s", cls.CONFIG_FILE)

    @classmethod
    def reset(cls) -> AppConfig:
        """
        重置為預設配置並保存。

        Returns:
            全新的預設 AppConfig
        """
        config = AppConfig()
        cls.save(config)
        logger.info("配置已重置為預設值")
        return config

    @classmethod
    def get_provider(
        cls, config: AppConfig, name: str
    ) -> Optional[Dict[str, Any]]:
        """
        獲取指定 LLM 服務商配置。

        Args:
            config: 當前配置
            name: 服務商 key（如 "openai"、"deepseek"）

        Returns:
            服務商配置字典，不存在則返回 None
        """
        return config.llm.providers.get(name)

    @classmethod
    def set_provider_key(
        cls, config: AppConfig, provider: str, key: str
    ) -> AppConfig:
        """
        設置服務商 API Key，返回新配置（不可變）。

        Args:
            config: 當前配置
            provider: 服務商 key
            key: API Key

        Returns:
            包含更新後 API Key 的新 AppConfig
        """
        providers = {k: dict(v) for k, v in config.llm.providers.items()}
        if provider not in providers:
            logger.warning("未知服務商: %s", provider)
            return config
        providers[provider] = {**providers[provider], "api_key": key}
        new_llm = replace(config.llm, providers=providers)
        return replace(config, llm=new_llm)
