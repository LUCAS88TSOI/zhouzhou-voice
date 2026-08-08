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
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import get_logger
from utils.paths import APP_VERSION

logger = get_logger("config")


# ─── 數值防禦工具（frozen dataclass __post_init__ 共用）───────
#
# 供各 config dataclass 嘅 __post_init__ 呼叫，防止 config.json 傳入壞值
# （None / 非數字字串 / inf）導致啟動崩潰。int(float('inf')) 拋嘅係
# OverflowError（ArithmeticError 子類），唔喺 TypeError/ValueError 之列，
# 必須一齊 catch。

def _safe_int(val: object, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(val))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(val: object, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


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
    polish_selection_key: str = ""      # 潤色選取文字快捷鍵，空字串 = 停用（預設不綁定）
    polish_selection_instant: bool = True  # True = 速發（鬆開觸發），False = 長按觸發

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            object.__setattr__(self, "key", "caps_lock")
        if not isinstance(self.repolish_key, str):
            object.__setattr__(self, "repolish_key", "")
        if not isinstance(self.polish_selection_key, str):
            object.__setattr__(self, "polish_selection_key", "")


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
    min_polish_chars: int = 4           # 識別文字達此字數才送 LLM 潤飾（沿用舊硬編碼 _MIN_LLM_LENGTH 預設值）

    def __post_init__(self) -> None:
        # frozen=True 下必須用 object.__setattr__ 繞過 setattr 限制
        # 只做下限防禦（壞值/非數字/inf fallback 到 4，clamp 到 1），不做上限 clamp（用戶要求「無上限」）
        chars = _safe_int(self.min_polish_chars, 4, 1)
        object.__setattr__(self, "min_polish_chars", chars)


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
    # 標點移除模式：off=不移除 / trailing=只移除末尾 / all=移除全文所有 trash_punc 字元
    # 預設 trailing 以相容舊 config（舊版只做末尾移除）
    punc_strip_mode: str = "trailing"


@dataclass(frozen=True)
class FileConfig:
    """文件轉錄配置"""
    save_srt: bool = True
    save_txt: bool = True
    save_json: bool = False
    llm_polish: bool = False


@dataclass(frozen=True)
class UIConfig:
    """UI 偏好設定

    indicator_auto_center=True 時忽略 indicator_x/y，每次顯示浮窗都自動貼齊
    「游標所在螢幕」的底部中央。用布林旗標而非數值 sentinel（如 -1）表達模式，
    因為多螢幕虛擬桌面下負座標是完全合法的（副屏排在主屏左邊 / 上邊）。
    """
    indicator_x: int = 100
    indicator_y: int = 100
    indicator_auto_center: bool = True
    show_indicator: bool = True

    def __post_init__(self) -> None:
        # frozen=True 下必須用 object.__setattr__ 繞過 setattr 限制。
        # config.json 被手改壞（如 "indicator_x": "abc"）時，壞座標會令
        # RecordingIndicator 的 move() 拋 TypeError 被 except 吞掉 → 浮窗永久
        # 靜默停用，屬與「螢幕空隙座標」同類的無聲失效，故一併堵上。
        for name, default in (("indicator_x", 100), ("indicator_y", 100)):
            try:
                object.__setattr__(self, name, int(getattr(self, name)))
            except (TypeError, ValueError):
                object.__setattr__(self, name, default)
        for name in ("indicator_auto_center", "show_indicator"):
            val = getattr(self, name)
            if not isinstance(val, bool):
                object.__setattr__(self, name, True if val is None else bool(val))


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


# ─── 公開工具函數 ──────────────────────────────────────────

def merge_live_indicator_position(snapshot: AppConfig, live: AppConfig) -> AppConfig:
    """把 live 的浮窗座標與定位模式（僅 indicator_x/y/auto_center 三欄）蓋回設定頁快照。

    浮窗位置由 VoiceApp 擁有：拖動浮窗、按「重置到底部中央」都會即時寫入
    config。而 SettingsPanel 持有的是「開啟設定頁那一刻」的快照，若直接提交，
    「按重置按鈕 → 再按儲存」會令剛剛的重置被過期快照靜默覆蓋。
    設定頁仍然擁有 show_indicator 開關，故只蓋座標與模式三個欄位。

    唯一呼叫點是 `MainWindow._on_settings_save`（亦是 `settings_save_requested`
    的唯一 emit 點）。若日後新增其他提交路徑，必須同樣經過本函數。

    Args:
        snapshot: 設定頁產生的新配置（可能帶過期的 ui 座標）
        live: 目前生效的配置（座標的唯一真相來源）

    Returns:
        座標已校正的新 AppConfig
    """
    return replace(snapshot, ui=replace(
        snapshot.ui,
        indicator_x=live.ui.indicator_x,
        indicator_y=live.ui.indicator_y,
        indicator_auto_center=live.ui.indicator_auto_center,
    ))


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

    # 讀取層失敗的重試策略（防毒／同步軟體短暫鎖檔屬暫時性）
    READ_RETRIES = 3
    READ_RETRY_DELAY = 0.2

    # 只保留最近幾份損壞備份：每份都含明文 API Key，不能無限累積
    MAX_QUARANTINE_FILES = 3

    # load() 是否因損壞而回退到預設值（供 UI 提示用戶）
    _load_failed: bool = False
    _quarantine_path: str = ""
    # 檔案完全讀不到（非內容損壞）：禁止任何寫入，避免覆蓋掉完好的原檔
    _read_failed: bool = False
    # 磁碟上的 config.json 是否可信。載入時發現損壞就轉 False，
    # save() 據此跳過 .bak 滾動——否則會用壞檔蓋掉唯一還留著 API Key 的備份。
    _disk_trusted: bool = True

    @classmethod
    def load_failed(cls) -> bool:
        """上次 load() 是否因檔案損壞而回退到預設值（供啟動時提示用戶）。"""
        return cls._load_failed

    @classmethod
    def quarantine_path(cls) -> str:
        """上次隔離起來的損壞檔路徑（沒有則空字串）。"""
        return cls._quarantine_path

    @classmethod
    def _quarantine(cls, raw: str) -> None:
        """把損壞的 config 另存成帶時間戳的副本，之後的 save 不會蓋掉它。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = cls.CONFIG_DIR / f"config.corrupt.{stamp}.json"
        try:
            target.write_text(raw, encoding="utf-8")
            cls._quarantine_path = str(target)
            logger.error("配置檔損壞，原檔已備份至: %s", target)
        except OSError as err:
            logger.error("備份損壞的配置檔失敗: %s", err)
            return

        cls._prune_quarantine()

    @classmethod
    def _prune_quarantine(cls) -> None:
        """只保留最近 MAX_QUARANTINE_FILES 份隔離檔（每份都含明文 API Key）。"""
        try:
            files = sorted(
                cls.CONFIG_DIR.glob("config.corrupt.*.json"),
                key=lambda p: p.name,
                reverse=True,
            )
            for stale in files[cls.MAX_QUARANTINE_FILES:]:
                stale.unlink(missing_ok=True)
                logger.info("已清理舊的損壞備份: %s", stale.name)
        except OSError as err:
            logger.warning("清理舊的損壞備份失敗: %s", err)

    @classmethod
    def cleanup_temp_files(cls) -> None:
        """清掉上次被硬殺時殘留的 .config_*.tmp（內含明文 API Key）。"""
        try:
            for tmp in cls.CONFIG_DIR.glob(".config_*.tmp"):
                tmp.unlink(missing_ok=True)
                logger.info("已清理殘留的設定暫存檔: %s", tmp.name)
        except OSError as err:
            logger.warning("清理設定暫存檔失敗: %s", err)

    @classmethod
    def load(cls) -> AppConfig:
        """
        載入配置。文件不存在時返回預設配置並創建文件。

        檔案損壞時不再靜默重置：先把原檔隔離成 config.corrupt.<時間戳>.json，
        再嘗試從 .bak 還原；兩者都失敗才回預設值，並設定 load_failed 旗標
        讓 App 啟動後能提示用戶（否則 API Key 會無聲無息消失）。

        Returns:
            AppConfig 實例（不可變）
        """
        cls._load_failed = False
        cls._quarantine_path = ""
        cls._disk_trusted = True
        cls._read_failed = False

        if not cls.CONFIG_FILE.exists():
            logger.info("配置文件不存在，建立預設配置: %s", cls.CONFIG_FILE)
            config = AppConfig()
            cls.save(config)
            return config

        # 讀取層失敗（防毒即時掃描 / OneDrive / 備份軟體短暫鎖檔）多數係暫時性，
        # 重試幾次先當真。讀唔到 ≠ 內容壞咗，兩者必須分開處理（否則會用預設值
        # 覆蓋掉其實完好無損的 config.json，API Key 真係冇咗）。
        raw: str | None = None
        for attempt in range(cls.READ_RETRIES):
            try:
                raw = cls.CONFIG_FILE.read_text(encoding="utf-8")
                break
            except OSError as err:
                logger.warning(
                    "配置文件讀取失敗（第 %d/%d 次）: %s",
                    attempt + 1, cls.READ_RETRIES, err,
                )
                time.sleep(cls.READ_RETRY_DELAY)

        if raw is None:
            # 完全讀唔到 → 唔隔離（冇嘢可隔離）、唔重置、更加唔准寫回去，
            # 只以預設值撐住本次執行，等下次啟動再試。
            logger.error("配置文件無法讀取，本次執行以預設值運行且不會寫入設定檔")
            cls._load_failed = True
            cls._read_failed = True
            cls._disk_trusted = False
            return AppConfig()

        try:
            data = json.loads(raw)
            config = _dict_to_config(data)
            logger.info("配置載入成功: %s", cls.CONFIG_FILE)
            return config
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as err:
            logger.error("配置文件解析失敗: %s", err)

        cls._quarantine(raw)
        cls._disk_trusted = False

        recovered = cls._load_backup()
        if recovered is not None:
            logger.warning("已從 .bak 還原配置（原檔損壞）")
            return recovered

        # .bak 也救不回 → 回預設值，但標記失敗讓 UI 能提示
        cls._load_failed = True
        logger.error("備份檔亦無法還原，暫時使用預設配置")
        return AppConfig()

    @classmethod
    def _load_backup(cls) -> AppConfig | None:
        """嘗試從 config.json.bak 還原，失敗回 None。"""
        backup = Path(str(cls.CONFIG_FILE) + ".bak")
        if not backup.exists():
            return None
        try:
            return _dict_to_config(json.loads(backup.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError, OSError) as err:
            logger.error("備份配置解析失敗: %s", err)
            return None

    @classmethod
    def save(cls, config: AppConfig) -> None:
        """
        原子化保存配置到 JSON 文件。

        先寫臨時檔（同目錄）→ flush+fsync → os.replace 原子置換，確保進程
        中途被硬殺 / crash / 斷電時，要麼完整寫入、要麼原 config.json 不變，
        絕不留半截 JSON 導致下次載入失敗 fallback 預設、靜默遺失設定（含 API Key）。
        置換前另存一份 .bak 滾動備份作多一重保險。

        Args:
            config: 要保存的 AppConfig 實例
        """
        if cls._read_failed:
            # 讀不到原檔（暫時性鎖檔）→ 本次執行拿的是預設值，寫回去就會
            # 把完好的 config.json（含 API Key）永久覆蓋掉。
            logger.error("設定檔本次無法讀取，已跳過保存以免覆蓋原檔")
            return

        cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = _config_to_dict(config)
        payload = json.dumps(data, ensure_ascii=False, indent=2)

        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".config_", suffix=".tmp", dir=str(cls.CONFIG_DIR)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # 置換前保留上次良好副本（容錯：備份失敗不影響主流程，但記 log 以便排查）
            # 本次載入失敗時「不」滾動 .bak——那份可能是唯一還留著 API Key 的副本，
            # 一旦被預設值蓋掉就永久遺失。
            if cls.CONFIG_FILE.exists() and cls._disk_trusted:
                try:
                    shutil.copy2(str(cls.CONFIG_FILE), str(cls.CONFIG_FILE) + ".bak")
                except Exception as bak_err:
                    logger.warning("備份舊 config 失敗（不影響保存）: %s", bak_err)
            os.replace(tmp_path, str(cls.CONFIG_FILE))  # 同卷原子置換
        except Exception:
            # 失敗則清掉臨時檔；原 config.json 因尚未 replace 而保持不變
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception as cleanup_err:
                logger.warning("清理臨時 config 檔失敗: %s", cleanup_err)
            raise
        # 寫入成功 → 磁碟上已是完整可信的內容，之後的 save 可正常滾動 .bak
        cls._disk_trusted = True
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
