"""
州州語音 - 熱詞管理器

協調所有熱詞子系統（音素匹配、規則替換、糾錯歷史），
監控文件變更並自動重新載入。

職責：
- 統一管理熱詞文件的讀取和寫入
- 協調三個子系統的執行順序
- 定時檢查文件變更，自動重新載入
- 與生命週期管理器整合

設計原則：
- 不可變性：配置使用 frozen dataclass
- 最小依賴：文件監控使用 threading.Timer，不引入 watchdog
- 安全降級：任何子系統失敗不影響其他子系統
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from utils.config import HotwordConfig
from utils.logger import get_logger
from utils.paths import APP_ROOT

from hotword.phoneme import PhonemeIndex
from hotword.rectify import RectifyStore
from hotword.rules import RuleEngine

logger = get_logger("hotword.manager")


# ─── 資料結構 ──────────────────────────────────────────────

@dataclass(frozen=True)
class FileTimestamps:
    """文件修改時間快照。"""
    hotword: float = 0.0
    rule: float = 0.0
    rectify: float = 0.0


# ─── 常數 ──────────────────────────────────────────────────

_CONFIG_DIR = Path.home() / "AppData" / "Roaming" / "zhouzhou-voice"
_HOTWORD_FILE = "hot.txt"
_RULE_FILE = "hot-rule.txt"
_RECTIFY_FILE = "hot-rectify.txt"

_DEFAULTS_DIR = APP_ROOT / "hotword" / "defaults"

_WATCH_INTERVAL_SECONDS = 5.0


# ─── 內部工具 ──────────────────────────────────────────────

def _ensure_file(path: Path) -> None:
    """確保文件存在，不存在則從 defaults/ 複製預設內容（若無預設則建空文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        default = _DEFAULTS_DIR / path.name
        if default.exists():
            path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("初始化熱詞文件（預設內容）: %s", path)
        else:
            path.write_text("", encoding="utf-8")
            logger.info("建立熱詞文件: %s", path)


def _get_mtime(path: Path) -> float:
    """安全地取得文件修改時間。"""
    try:
        return path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        return 0.0


def _read_lines(path: Path) -> list[str]:
    """
    從文件讀取有效行列表（通用版本）。

    過濾空行和 # 註解行。

    Args:
        path: 文件路徑

    Returns:
        有效行列表
    """
    if not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as err:
        logger.error("讀取文件失敗: %s — %s", path, err)
        return []

    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            lines.append(line)

    return lines


def _remove_line(path: Path, target: str) -> bool:
    """
    從文件中移除指定行。

    讀取所有行，移除匹配項，重寫文件。

    Args:
        path:   文件路徑
        target: 要移除的行內容（strip 後比較）

    Returns:
        是否有行被移除
    """
    if not path.exists():
        return False

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as err:
        logger.error("讀取文件失敗: %s — %s", path, err)
        return False

    original_lines = content.splitlines()
    filtered = [ln for ln in original_lines if ln.strip() != target]

    if len(filtered) == len(original_lines):
        return False

    try:
        path.write_text("\n".join(filtered), encoding="utf-8")
        return True
    except OSError as err:
        logger.error("寫入文件失敗: %s — %s", path, err)
        return False


def _read_hotword_list(path: Path) -> list[str]:
    """
    從熱詞文件讀取熱詞列表。

    文件格式：每行一個熱詞，# 開頭為註解。

    Args:
        path: 熱詞文件路徑

    Returns:
        熱詞列表
    """
    if not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as err:
        logger.error("讀取熱詞文件失敗: %s — %s", path, err)
        return []

    words: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            words.append(line)

    return words


# ─── 熱詞管理器 ────────────────────────────────────────────

class HotwordManager:
    """
    熱詞管理器 — 協調所有熱詞子系統。

    子系統：
    - PhonemeIndex: 音素模糊匹配
    - RuleEngine: 規則替換
    - RectifyStore: 糾錯歷史

    文件位置（%APPDATA%/zhouzhou-voice/）：
    - hot.txt: 熱詞列表（每行一個）
    - hot-rule.txt: 替換規則
    - hot-rectify.txt: 糾錯歷史

    用法：
        manager = HotwordManager(config.hotword)
        manager.load_all()
        corrected = manager.correct("今天用愛皮愛寫代碼")
        manager.start_watcher()
        # ... 應用運行 ...
        manager.stop_watcher()
    """

    def __init__(self, config: HotwordConfig) -> None:
        self._config = config

        # 文件路徑
        self._hotword_path = _CONFIG_DIR / _HOTWORD_FILE
        self._rule_path = _CONFIG_DIR / _RULE_FILE
        self._rectify_path = _CONFIG_DIR / _RECTIFY_FILE

        # 子系統
        self._phoneme_index = PhonemeIndex()
        self._rule_engine = RuleEngine()
        self._rectify_store = RectifyStore()

        # 文件監控
        self._timestamps = FileTimestamps()
        self._watcher_timer: threading.Timer | None = None
        self._watcher_running = False
        self._lock = threading.Lock()

    # ─── 公開屬性 ──────────────────────────────────────────

    @property
    def config(self) -> HotwordConfig:
        """熱詞配置（唯讀）。"""
        return self._config

    @property
    def hotword_count(self) -> int:
        """已載入的熱詞數量。"""
        return self._phoneme_index.size

    @property
    def rule_count(self) -> int:
        """已載入的規則數量。"""
        return self._rule_engine.rule_count

    @property
    def rectify_count(self) -> int:
        """已載入的糾錯記錄數量。"""
        return self._rectify_store.pair_count

    # ─── 載入 ──────────────────────────────────────────────

    def load_all(self) -> None:
        """
        載入所有熱詞文件。

        確保文件存在後依次載入：熱詞列表、替換規則、糾錯歷史。
        任一文件載入失敗不影響其他文件。
        """
        _ensure_file(self._hotword_path)
        _ensure_file(self._rule_path)
        _ensure_file(self._rectify_path)

        self._load_hotwords()
        self._load_rules()
        self._load_rectify()
        self._update_timestamps()

        logger.info(
            "熱詞系統載入完成: %d 個熱詞, %d 條規則, %d 條糾錯",
            self.hotword_count, self.rule_count, self.rectify_count,
        )

    def _load_hotwords(self) -> None:
        """載入熱詞列表並建立音素索引。"""
        try:
            words = _read_hotword_list(self._hotword_path)
            self._phoneme_index.build(words)
        except Exception as err:
            logger.error("熱詞列表載入失敗: %s", err)

    def _load_rules(self) -> None:
        """載入替換規則。"""
        try:
            self._rule_engine.load(self._rule_path)
        except Exception as err:
            logger.error("替換規則載入失敗: %s", err)

    def _load_rectify(self) -> None:
        """載入糾錯歷史。"""
        try:
            self._rectify_store.load(self._rectify_path)
        except Exception as err:
            logger.error("糾錯歷史載入失敗: %s", err)

    # ─── 核心功能 ──────────────────────────────────────────

    def correct(self, text: str) -> str:
        """
        對文字進行熱詞校正。

        執行順序：
        1. 音素匹配替換（修正同音字錯誤）
        2. 規則替換（套用使用者定義的規則）

        若熱詞功能未啟用，直接返回原文。

        Args:
            text: ASR 輸出文字

        Returns:
            校正後的文字
        """
        if not self._config.enabled or not text:
            return text

        result = text

        # 第一步：音素匹配
        result = self._phoneme_index.match(
            result, threshold=self._config.threshold,
        )

        # 第二步：規則替換
        result = self._rule_engine.apply(result)

        # 第三步：糾錯庫套用（精確替換）
        result = self._rectify_store.apply(result)

        if result != text:
            logger.debug("熱詞校正: '%s' → '%s'", text, result)

        return result

    def reload(self, new_config: HotwordConfig) -> None:
        """
        重新載入熱詞配置（hotword/rectify 規則）。

        用於配置變更後更新熱詞管理器，無需重建實例。

        Args:
            new_config: 新的熱詞配置
        """
        self._config = new_config
        self._load_hotwords()
        self._load_rectify()
        logger.info("熱詞管理器已重新載入")

    def get_similar_context(self, text: str) -> list[str]:
        """
        取得輸入文字相關的上下文提示（供 LLM 使用）。

        合併來源：
        1. 音素相似匹配（可能拼錯的詞）
        2. 糾錯歷史（過去的修正記錄）

        Args:
            text: 輸入文字

        Returns:
            上下文提示列表
        """
        if not self._config.enabled or not text:
            return []

        context: list[str] = []

        # 音素相似提示
        similar = self._phoneme_index.find_similar(
            text, threshold=self._config.similar_threshold,
        )
        for match in similar:
            hint = (
                f"「{match.original}」可能是「{match.matched}」"
                f"（相似度 {match.similarity:.0%}）"
            )
            context.append(hint)

        # 糾錯歷史提示
        rectify_hints = self._rectify_store.get_context(
            text, threshold=self._config.similar_threshold,
        )
        context.extend(rectify_hints)

        return context

    # ─── 新增操作 ──────────────────────────────────────────

    def add_hotword(self, word: str) -> None:
        """
        新增一個熱詞到 hot.txt。

        追加到文件末尾並重新載入音素索引。

        Args:
            word: 要新增的熱詞
        """
        cleaned = word.strip()
        if not cleaned:
            logger.warning("嘗試新增空熱詞，已忽略")
            return

        _ensure_file(self._hotword_path)

        try:
            with self._hotword_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{cleaned}")
            logger.info("新增熱詞: %s", cleaned)
            self._load_hotwords()
            self._update_timestamps()
        except OSError as err:
            logger.error("新增熱詞失敗: %s — %s", cleaned, err)

    def add_rectify(self, wrong: str, right: str) -> None:
        """
        新增一條糾錯記錄到 hot-rectify.txt。

        格式：wrong → right

        Args:
            wrong: 錯誤文字
            right: 正確文字
        """
        wrong_clean = wrong.strip()
        right_clean = right.strip()

        if not wrong_clean or not right_clean:
            logger.warning("嘗試新增空糾錯記錄，已忽略")
            return

        _ensure_file(self._rectify_path)

        try:
            with self._rectify_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{wrong_clean} → {right_clean}")
            logger.info("新增糾錯: '%s' → '%s'", wrong_clean, right_clean)
            self._load_rectify()
            self._update_timestamps()
        except OSError as err:
            logger.error(
                "新增糾錯失敗: '%s' → '%s' — %s",
                wrong_clean, right_clean, err,
            )

    # ─── 列表讀取 ──────────────────────────────────────────

    def get_hotwords(self) -> list[str]:
        """
        讀取 hot.txt 並返回熱詞列表。

        Returns:
            熱詞列表（每行一個，忽略空行和 # 註解）
        """
        return _read_hotword_list(self._hotword_path)

    def get_rules(self) -> list[str]:
        """
        讀取 hot-rule.txt 並返回規則行列表。

        Returns:
            規則行列表（保留原始格式，忽略空行和 # 註解）
        """
        return _read_lines(self._rule_path)

    def get_rectify_pairs(self) -> list[str]:
        """
        讀取 hot-rectify.txt 並返回糾錯行列表。

        Returns:
            糾錯行列表（保留原始格式，忽略空行和 # 註解）
        """
        return _read_lines(self._rectify_path)

    # ─── 刪除操作 ──────────────────────────────────────────

    def remove_hotword(self, word: str) -> bool:
        """
        從 hot.txt 移除一個熱詞。

        Args:
            word: 要移除的熱詞

        Returns:
            是否成功移除
        """
        removed = _remove_line(self._hotword_path, word.strip())
        if removed:
            logger.info("移除熱詞: %s", word.strip())
            self._load_hotwords()
            self._update_timestamps()
        return removed

    def remove_rule(self, line: str) -> bool:
        """
        從 hot-rule.txt 移除一條規則。

        Args:
            line: 要移除的規則行（完整原文）

        Returns:
            是否成功移除
        """
        removed = _remove_line(self._rule_path, line.strip())
        if removed:
            logger.info("移除規則: %s", line.strip())
            self._load_rules()
            self._update_timestamps()
        return removed

    def remove_rectify(self, line: str) -> bool:
        """
        從 hot-rectify.txt 移除一條糾錯記錄。

        Args:
            line: 要移除的糾錯行（完整原文）

        Returns:
            是否成功移除
        """
        removed = _remove_line(self._rectify_path, line.strip())
        if removed:
            logger.info("移除糾錯: %s", line.strip())
            self._load_rectify()
            self._update_timestamps()
        return removed

    # ─── 新增規則 ──────────────────────────────────────────

    def add_rule(self, pattern: str, replacement: str) -> None:
        """
        新增一條替換規則到 hot-rule.txt。

        格式：pattern = replacement

        Args:
            pattern:     匹配模式
            replacement: 替換文字
        """
        pattern_clean = pattern.strip()
        replacement_clean = replacement.strip()

        if not pattern_clean or not replacement_clean:
            logger.warning("嘗試新增空規則，已忽略")
            return

        _ensure_file(self._rule_path)

        try:
            with self._rule_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{pattern_clean} = {replacement_clean}")
            logger.info("新增規則: %s = %s", pattern_clean, replacement_clean)
            self._load_rules()
            self._update_timestamps()
        except OSError as err:
            logger.error(
                "新增規則失敗: %s = %s — %s",
                pattern_clean, replacement_clean, err,
            )

    # ─── 文件監控 ──────────────────────────────────────────

    def start_watcher(self) -> None:
        """
        啟動文件變更監控。

        定時檢查熱詞文件的修改時間，有變更時自動重新載入。
        使用 threading.Timer 實現，不依賴 watchdog。
        """
        if self._watcher_running:
            logger.debug("文件監控已在運行中")
            return

        self._watcher_running = True
        self._schedule_next_check()
        logger.info(
            "文件監控已啟動（間隔 %.0f 秒）", _WATCH_INTERVAL_SECONDS,
        )

    def stop_watcher(self) -> None:
        """停止文件變更監控。"""
        self._watcher_running = False

        with self._lock:
            timer = self._watcher_timer
            self._watcher_timer = None

        if timer is not None:
            timer.cancel()

        logger.info("文件監控已停止")

    def _schedule_next_check(self) -> None:
        """排程下一次文件檢查。"""
        if not self._watcher_running:
            return

        timer = threading.Timer(
            _WATCH_INTERVAL_SECONDS, self._check_file_changes,
        )
        timer.daemon = True

        with self._lock:
            self._watcher_timer = timer

        timer.start()

    def _check_file_changes(self) -> None:
        """
        檢查文件是否有變更，有則重新載入。

        逐一比較各文件的修改時間，只重新載入有變更的文件。
        """
        if not self._watcher_running:
            return

        try:
            current_hw = _get_mtime(self._hotword_path)
            current_rule = _get_mtime(self._rule_path)
            current_rect = _get_mtime(self._rectify_path)

            reloaded: list[str] = []

            if current_hw != self._timestamps.hotword:
                self._load_hotwords()
                reloaded.append("熱詞列表")

            if current_rule != self._timestamps.rule:
                self._load_rules()
                reloaded.append("替換規則")

            if current_rect != self._timestamps.rectify:
                self._load_rectify()
                reloaded.append("糾錯歷史")

            if reloaded:
                self._update_timestamps()
                logger.info("檢測到文件變更，已重新載入: %s", "、".join(reloaded))

        except Exception as err:
            logger.error("文件變更檢查異常: %s", err)

        # 排程下一次
        self._schedule_next_check()

    def _update_timestamps(self) -> None:
        """更新文件修改時間快照。"""
        self._timestamps = FileTimestamps(
            hotword=_get_mtime(self._hotword_path),
            rule=_get_mtime(self._rule_path),
            rectify=_get_mtime(self._rectify_path),
        )
