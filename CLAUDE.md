# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 建立於：2026-03-29 | 更新：2026-04-14

## 專案說明

**ZhouZhou Voice（州州語音）** — Windows 離線語音輸入工具。長按快捷鍵（預設 CapsLock）錄音 → sherpa-onnx ASR 識別 → 可選 LLM 潤色 → 自動貼上到當前應用。完全離線，支援自訂熱詞、批次檔案轉譯、系統托盤常駐。

## 開發規則

- 所有溝通使用繁體中文
- 修改代碼後必須追加 DEVLOG.md 記錄
- Bug 修復前必須排查根因
- 禁止 print 調試，統一用 logger
- 所有 GUI 更新必須透過 `QMetaObject.invokeMethod()` 回主線程
- 配置修改用 `dataclasses.replace()`，禁止直接修改 frozen dataclass（否則拋 `FrozenInstanceError`）
  ```python
  # 正確
  new_cfg = dataclasses.replace(cfg, llm=dataclasses.replace(cfg.llm, enabled=True))
  ```
- 代碼精簡：能用一行不用三行，能刪冗餘方法就刪

## 啟動與開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 開發啟動
python main.py
# 或
start.bat
```

## 測試與驗證

- `tests/test_fixes.py`：10 個回歸測試，涵蓋長錄音分段去重、transcribe tab 信號、錄音上限浮窗
- `tools/asr_benchmark.py`：CLI 工具，比較所有已安裝 ASR 模型的識別速度與準確率
- 首次啟動會彈出麥克風測試對話框（`gui/mic_test_dialog.py`），驗證錄音鏈路

## 打包（Nuitka）

```bash
# 使用 build.bat，禁止手動拼命令（曾踩坑 GUI 不顯示）
build\build.bat
```

輸出：`dist\main.dist\zhouzhou-voice.exe`（25MB exe + ~370MB 含模型）。需時 10-30 分鐘。

## 高階架構

### 初始化順序（7 階段）

`main.py` → 單實例 mutex → `VoiceApp.__init__()` → `_initialize()`：

1. Config + Logging（`utils/config.py`、`utils/logger.py`）
2. ASR 系統（`core/audio_recorder.py`、`core/asr_process.py` 子進程）
3. 快捷鍵監聽（`utils/hotkey.py`，pynput）
4. 熱詞管理（`hotword/manager.py`，含檔案監控）
5. LLM 潤色（`llm/processor.py`，可選）
6. 錄音歷史（`core/recording_db.py`，SQLite）— **已移前至 Phase 6**，供 SettingsPanel 共享 DB 實例
7. GUI（`gui/main_window.py`、`gui/tray_icon.py`，PySide6）

### 語音處理資料流

```
CapsLock 長按
  → AudioRecorder (sounddevice, 16kHz mono)
  → ASRProcess 子進程 (sherpa-onnx, multiprocessing.Queue + task_id)
  → TextProcessor (標點/繁簡/空格)
  → HotwordManager (音素 RAG 匹配)
  → 雜訊過濾（純標點/空字串丟棄）
  → LLMProcessor (OpenAI 兼容 API, SSE 串流, 可選)
  → Win32 剪貼板貼上
  → SQLite 記錄
  → GUI 更新（QMetaObject.invokeMethod）
```

### 執行緒模型

| 執行緒 | 內容 |
|--------|------|
| 主執行緒 | Qt 事件循環 + 托盤 |
| pynput 執行緒 | 全局快捷鍵監聽 |
| voice-worker daemon | ASR → 熱詞 → LLM → 輸出 |
| repolish-worker daemon | 重新潤色（threading.Lock 防雙次觸發） |
| ASR 子進程 | sherpa-onnx 模型隔離（crash 不影響主進程） |

### 熱詞系統（hotword）

三個子系統按順序執行：

| 子系統 | 類別 | 資料來源 | 功能 |
|--------|------|----------|------|
| 音素匹配 | `PhonemeIndex` | `hot.txt` | pypinyin 模糊匹配，糾正同音字 |
| 規則替換 | `RuleEngine` | `hot-rule.txt` | 正則替換規則 |
| 糾錯對 | `RectifyStore` | `hot-rectify.txt` | 歷史糾錯對記憶 |

用戶資料目錄：`%APPDATA%\zhouzhou-voice\`（含以上三個 txt 檔案）。首次安裝時，`_ensure_file()` 會從 `hotword/defaults/` 複製預設內容（廣東話熱詞表等），若無預設則建空文件。每 5 秒輪詢 mtime，原子時間戳快照確保線程安全；任一子系統失敗不影響其他。

### LLM 子系統

- **9 個服務商**：OpenAI、Deepseek、Anthropic、Google、Zhipu、BigModel、Moonshot、SiliconFlow、Groq + Custom（`llm/provider.py`）
- **6 個內建角色**：`default`、`writing_mode`、`translator`、`assistant`、`instruction_mode`、`dev_mode`（`llm/roles/`）
- **跳過門檻**：識別結果 <4 字不送 LLM
- **Repolish**：可對上次結果重新潤色，獨立 `repolish-worker` daemon thread + Lock 防雙次觸發

### 檔案轉錄管線（transcribe）

`transcribe/file_transcriber.py`（FFmpeg 讀取媒體檔案）→ ASR → `transcribe/srt_writer.py`（SRT 字幕輸出）。依賴系統 FFmpeg 在 PATH 中。

**v3.6.2 改進**：TranscribeTab 信號現在攜帶 `FileConfig` 第二參數（而非使用過期持久化設定），確保用戶在 UI 中修改的設定（即使未按 Save）也能生效。

### 信號架構（Tray Signals）

從 v3.6.0 起，托盤信號統一在 `VoiceApp._init_gui()` 中連接：

| 信號 | 發送來源 | 處理方法 | 備註 |
|------|--------|--------|------|
| `copy_result_requested` | `TrayIcon` | `VoiceApp._on_copy_result()` | 複製最近識別結果 |
| `clear_memory_requested` | `TrayIcon` | `VoiceApp._on_clear_memory()` | 清空 LLM 多輪歷史 |
| `add_hotword_requested` | `TrayIcon` | `VoiceApp._on_add_hotword()` | 新增熱詞到 hot.txt |
| `add_rectify_requested` | `TrayIcon` | `VoiceApp._on_add_rectify()` | 新增糾錯對到 hot-rectify.txt |
| `role_switch_requested` | `TrayIcon` | `VoiceApp._on_role_switch()` | 切換 LLM 角色 |

**重要**：v3.5.9 曾有信號雙重連接 bug（`MainWindow._connect_tray_signals()` 中重複連接），v3.6.0 已修復。

### 關鍵設計決策

- **ASR 子進程隔離**：模型崩潰不影響主進程，可重啟
- **Frozen dataclass**：所有 config 物件 `frozen=True`，防意外修改
- **Queue + task_id**：IPC 通信，每次請求前呼叫 `_drain_stale()` 清過期響應
- **LLM 連線池**：urllib3.PoolManager 全局共享，同 host 復用 TCP+TLS
- **GUI 線程安全**：所有主線程更新通過 `_invoke_gui(method, *args)` 進行（v3.6.0 統一介面）
- **長錄音分段去重**：`_merge_text_overlap_parts()` 逐對比較相鄰段邊界，找最長精確匹配後拼接，避免重疊文字重複

## 關鍵文件

| 文件 | 用途 |
|------|------|
| `main.py` | 入口：Win32 mutex 單實例 + freeze_support |
| `app/app.py` | VoiceApp：整合所有模組的主協調者；含 `_merge_text_overlap_parts()` 分段去重函數 |
| `utils/config.py` | ConfigManager：frozen dataclass + JSON 深度合併 |
| `utils/paths.py` | 統一路徑（frozen 偵測，APP_ROOT/MODELS_DIR/LOG_DIR）|
| `core/asr_process.py` | ASR 子進程 IPC，task_id 驗證 |
| `core/audio_recorder.py` | 錄音，MAX_DURATION=120s，FIR 濾波降採樣 |
| `gui/main_window.py` | 主視窗，QStackedWidget（頁0=語音，頁1=設定）；`_sync_indicator_state()` 處理「已達錄音上限」狀態 |
| `gui/widgets/transcribe_tab.py` | 文件轉錄頁籤；`transcribe_requested` 信號攜帶 `FileConfig` |
| `llm/provider.py` | 9 個 LLM 服務商預設 |
| `hotword/manager.py` | 熱詞管理，含音素匹配、預設複製與檔案熱重載 |
| `hotword/defaults/` | 廣東話預設熱詞表，首次安裝自動複製到 %APPDATA% |
| `utils/updater.py` | 自動更新：啟動時檢測版本，下載並執行更新腳本 |
| `VERSION` | 版本號單一來源（build.bat 讀取）|
| `tests/test_fixes.py` | 10 個回歸測試 |
| `docs/ARCHITECTURE.md` | 詳細系統設計文件 |
| `DEVLOG.md` | 開發更動記錄（每次修改必須追加）|

## 配置

存放路徑：`%APPDATA%\zhouzhou-voice\config.json`

區段：`asr`、`shortcut`、`output`、`llm`（含 9 個服務商）、`hotword`、`history`、`ui`

## 已知坑

- pynput 1.8：`win32_event_filter` 返回 False 會跳過回調，已在 `utils/hotkey.py` 修復
- Nuitka 打包必須用 `build.bat`，禁止手動拼命令
- ASR 動態超時：`max(30, 錄音長度×1.5)` 秒
- 不可排除 `PySide6.QtMultimedia`（`gui/widgets/audio_player.py` 依賴）
- dist 目錄偶有 Windows 保留名 `nul` 文件，無法刪除，忽略即可
- 日誌在 `%APPDATA%\zhouzhou-voice\logs\app.log`
