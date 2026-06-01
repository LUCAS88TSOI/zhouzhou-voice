# 州州語音 系統架構設計

**版本**: 3.6.2
**最後更新**: 2026-04-14

---

## 一、整體架構

```
┌─────────────────────────────────────────────────────────────────┐
│                      州州語音.exe                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                    主進程                                │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │   │
│  │  │  GUI 線程   │  │ 事件循環    │  │ 錄音線程    │     │   │
│  │  │ (PySide6)   │  │ (asyncio)   │  │(sounddevice)│     │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘     │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              │                                  │
│                     內部佇列（Queue）                            │
│                              │                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                 ASR 子進程（隔離）                        │   │
│  │  ┌─────────────────────────────────────────────────┐    │   │
│  │  │  sherpa-onnx + SenseVoice 模型                   │    │   │
│  │  └─────────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 設計原則

- **單一入口**：用戶只需啟動一個 exe
- **進程隔離**：ASR 模型運行在獨立子進程，崩潰不影響主程式
- **模組化**：各模組職責單一，可獨立測試和替換
- **配置驅動**：所有可選項通過 JSON 配置管理，為 GUI 預留接口

---

## 二、目錄結構

```
zhouzhou-voice/
├── main.py                      # 單一入口（Win32 mutex 單實例）
│
├── app/
│   ├── app.py                   # 應用主類（協調所有模組）
│   └── lifecycle.py             # 生命週期管理（啟動、關閉、信號處理）
│
├── core/
│   ├── asr_engine.py            # ASR 引擎封裝（sherpa-onnx）
│   ├── asr_process.py           # ASR 子進程管理（Queue + task_id IPC）
│   ├── audio_recorder.py        # 錄音管理（sounddevice，FIR 降採樣）
│   ├── model_catalog.py         # ASR 模型清單與元數據
│   ├── model_downloader.py      # 模型下載工具
│   ├── recording_db.py          # 錄音歷史（SQLite）
│   └── text_processor.py        # 文字後處理（ITN、標點、空格、繁體）
│
├── hotword/
│   ├── defaults/                # 預設熱詞文件（首次安裝自動複製到 %APPDATA%）
│   │   ├── hot.txt              # 廣東話預設熱詞（~130 個）
│   │   ├── hot-rule.txt         # 預設替換規則
│   │   └── hot-rectify.txt      # 糾錯歷史範本
│   ├── manager.py               # 熱詞管理器（加載、監控、替換）
│   ├── phoneme.py               # 音素 RAG 匹配算法（pypinyin）
│   ├── rules.py                 # 正則/等號規則替換
│   └── rectify.py               # 糾錯歷史 RAG
│
├── llm/
│   ├── provider.py              # LLM 服務商配置與預設列表（9 家）
│   ├── client.py                # API 客戶端（OpenAI 兼容格式，urllib3 連線池，僅 HTTPS）
│   ├── processor.py             # LLM 處理邏輯（流式輸出、停止控制）
│   └── roles/                   # 角色配置（6 個內建角色）
│       ├── default.py           # 預設角色（潤色）
│       ├── writing_mode.py      # 書面語模式
│       ├── translator.py        # 翻譯模式
│       ├── assistant.py         # 助手模式
│       ├── instruction_mode.py  # 指令模式
│       └── dev_mode.py          # 開發模式（口語轉指令）
│
├── gui/
│   ├── main_window.py           # 主窗口（QStackedWidget 雙頁）
│   ├── tray_icon.py             # 托盤圖標和右鍵菜單
│   ├── settings_panel.py        # 設定面板（6 頁籤，可嵌入）
│   ├── mic_test_dialog.py       # 麥克風測試對話框（首次啟動）
│   ├── recording_indicator.py   # 錄音狀態指示浮窗
│   ├── update_dialog.py         # 自動更新對話框
│   └── widgets/                 # 可複用 UI 組件
│       ├── shortcut_input.py    # 快捷鍵輸入控件
│       ├── provider_combo.py    # 服務商下拉選擇
│       ├── asr_tab.py           # ASR 設定頁籤
│       ├── role_tab.py          # 角色設定頁籤
│       ├── hotword_tab.py       # 熱詞設定頁籤
│       ├── history_tab.py       # 錄音歷史頁籤（右鍵複製、重新處理）
│       └── audio_player.py      # 音頻播放器（依賴 QtMultimedia）
│
├── transcribe/
│   ├── file_transcriber.py      # 文件轉錄主邏輯（FFmpeg 讀取）
│   └── srt_writer.py            # 字幕文件生成（SRT/TXT/JSON）
│
├── utils/
│   ├── config.py                # 配置管理器（frozen dataclass + JSON 深度合併）
│   ├── paths.py                 # 統一路徑（APP_ROOT / MODELS_DIR / APP_VERSION）
│   ├── clipboard.py             # 剪貼板操作（複製、粘貼、恢復）
│   ├── hotkey.py                # 快捷鍵監聽（pynput 封裝）
│   ├── keyboard.py              # 鍵盤模擬（按鍵補發、Ctrl+V）
│   ├── logger.py                # 日誌系統
│   ├── startup.py               # 開機自啟管理
│   └── updater.py               # 自動更新檢測與下載
│
├── models/                      # ASR 模型（不進 git）
│   └── sensevoice/
│       ├── model.onnx
│       └── tokens.txt
│
├── assets/                      # 資源文件
│   ├── icon.ico
│   └── icon.png
│
├── docs/                        # 設計文檔
│   ├── PRD.md
│   └── ARCHITECTURE.md
│
└── build/
    ├── build.bat                # Nuitka 打包腳本（唯一入口）
    └── verify_build.py          # 打包後驗證腳本
```

---

## 三、核心模組設計

各模組職責簡述（詳細實現請參考源碼）：

| 模組 | 文件 | 職責 |
|------|------|------|
| **主入口** | `main.py` | Win32 mutex 單實例 + freeze_support |
| **應用主類** | `app/app.py` | 協調所有模組：配置、ASR、錄音、熱詞、LLM、GUI；含 `_merge_text_overlap_parts()` 分段去重函數 |
| **ASR 引擎** | `core/asr_engine.py` | sherpa-onnx 封裝，支援 sense_voice/paraformer/whisper/zipformer |
| **ASR 進程** | `core/asr_process.py` | multiprocessing.Process + Queue IPC，task_id 驗證，_drain_stale 清理 |
| **錄音管理** | `core/audio_recorder.py` | sounddevice 錄音，48kHz→16kHz FIR 濾波降採樣，MAX_DURATION=120s |
| **文字處理** | `core/text_processor.py` | ITN、標點、空格、繁體轉換（opencc） |
| **錄音歷史** | `core/recording_db.py` | SQLite 儲存識別結果，支援重新潤色 |
| **模型目錄** | `core/model_catalog.py` | ASR 模型清單與下載元數據 |
| **熱詞系統** | `hotword/` | 音素匹配（pypinyin）+ 規則替換 + 糾錯歷史；`defaults/` 提供首次安裝預設 |
| **LLM 客戶端** | `llm/client.py` | urllib3 連線池，OpenAI 兼容 API，SSE 串流 |
| **LLM 處理器** | `llm/processor.py` | 角色系統 + 熱詞注入 + 多輪歷史；repolish 獨立 daemon thread |
| **內建 Key** | `llm/builtin_key.py` | BigModel 免費 API Key（XOR 混淆），無需用戶設定 |
| **快捷鍵** | `utils/hotkey.py` | pynput 封裝，長按偵測，短按穿透 |
| **配置管理** | `utils/config.py` | frozen dataclass + JSON + 深度合併 |
| **路徑管理** | `utils/paths.py` | APP_ROOT / MODELS_DIR / LOG_DIR / APP_VERSION（讀 VERSION 文件） |
| **自動更新** | `utils/updater.py` | 啟動時檢測新版本，下載並執行更新腳本 |
| **開機自啟** | `utils/startup.py` | Windows 登錄檔管理開機自啟 |

---

## 四、進程通信協議

### 4.1 請求/響應格式

```python
from dataclasses import dataclass, field
from typing import List

@dataclass
class ASRRequest:
    """主進程 → ASR 子進程"""
    task_id: str
    audio_data: bytes       # float32, 16kHz, mono
    sample_rate: int = 16000
    is_final: bool = True
    seg_duration: float = 5.0
    seg_overlap: float = 1.0

@dataclass
class ASRResponse:
    """ASR 子進程 → 主進程"""
    task_id: str
    text: str               # 簡單拼接結果
    text_accu: str = ""     # 精確拼接結果（字幕用）
    tokens: List[str] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    duration: float = 0.0
    is_final: bool = True
```

### 4.2 通信流程

```
主進程                          ASR 子進程
  │                                │
  │── ASRRequest (audio_data) ──→ │
  │                                │── 識別音頻
  │                                │
  │←── ASRResponse (text) ────── │
  │                                │
```

---

## 五、GUI 設計

GUI 模組職責簡述（詳細實現請參考源碼）：

| 模組 | 文件 | 職責 |
|------|------|------|
| **主視窗** | `gui/main_window.py` | QStackedWidget 雙頁（語音頁/設定頁），齒輪按鈕切換，最小化到托盤；`_sync_indicator_state()` 處理浮窗狀態 |
| **設定面板** | `gui/settings_panel.py` | 6 頁籤（快捷鍵/ASR/LLM/角色/熱詞/關於），QWidget 可嵌入 |
| **系統托盤** | `gui/tray_icon.py` | 右鍵選單，角色切換子選單，多個 Signal |
| **麥克風測試** | `gui/mic_test_dialog.py` | 首次啟動彈出，驗證錄音鏈路 |
| **更新對話框** | `gui/update_dialog.py` | 自動更新提示與下載進度 |
| **文件轉錄頁籤** | `gui/widgets/transcribe_tab.py` | 拖放匯入、文件列表、轉錄配置；`transcribe_requested` 信號攜帶 `FileConfig` |
| **自訂組件** | `gui/widgets/` | ShortcutInput、ProviderCombo、ASRTab、RoleTab、HotwordTab、HistoryTab、AudioPlayer |

---

## 六、數據流

### 6.1 錄音識別流程

```
[用戶按住快捷鍵]
       │
       ▼
┌──────────────────┐
│ HotkeyListener   │ ──→ 檢測按下，開始錄音
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ AudioRecorder    │ ──→ 持續錄音，每 5 秒分段
└──────────────────┘
       │
       ▼
[用戶鬆開快捷鍵]
       │
       ▼
┌──────────────────┐
│ ASRProcess       │ ──→ 發送到子進程識別
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ TextProcessor    │ ──→ ITN + 標點 + 空格 + 繁體
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ HotwordManager   │ ──→ 音素匹配 + 規則替換
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ LLMProcessor     │ ──→ LLM 潤色（可選）
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ Clipboard        │ ──→ 寫入剪貼板 → Ctrl+V
└──────────────────┘
```

### 6.2 文件轉錄流程

```
[用戶選擇/拖放文件]
       │
       ▼
┌──────────────────┐
│ FileTranscriber  │ ──→ 讀取音頻，轉 16kHz mono
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ 分段處理          │ ──→ 每 60 秒一段，4 秒重疊
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ ASRProcess       │ ──→ 逐段識別
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ 結果合併          │ ──→ 時間戳去重拼接
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ SRTWriter        │ ──→ 生成 srt / txt / json
└──────────────────┘
```

---

## 七、技術決策記錄

| 決策 | 選擇 | 原因 |
|------|------|------|
| ASR 引擎 | sherpa-onnx | 中文準確率最高，支援 SenseVoice |
| GUI 框架 | PySide6 | 原生桌面 UI，LGPL 可商用 |
| 快捷鍵 | pynput | 同時支援鍵盤和滑鼠，統一庫 |
| 錄音 | sounddevice | MIT 授權，穩定可靠 |
| 進程通信 | multiprocessing.Queue | 簡單可靠，無需 WebSocket |
| 配置格式 | JSON | 可讀性好，GUI 易操作 |
| 打包工具 | Nuitka | 編譯為機器碼，防反編譯 |
| ASR 模型 | SenseVoice | Apache 2.0 可商用，速度快 |

---

## 八、最近改進（v3.6.2）

### 長錄音分段去重（P1）

**問題**：長錄音被分段識別後，各段首尾有重疊區域。直接拼接時重疊文字會出現兩次。

**解決方案**：新增純函數 `_merge_text_overlap_parts(parts: list[str], max_check: int = 50) -> str`（`app/app.py`）：
- 逐對比較相鄰段的尾端與下一段首端
- 從最長可能匹配開始，向下搜索到第一個完整匹配
- 移除重疊部分後拼接結果

**使用**：`_recognize_long_audio()` 現在使用此函數替代 `" ".join(parts)`。

### Transcribe 頁籤使用當前 UI 設定（P2）

**問題**：`TranscribeTab` 信號 `transcribe_requested` 原本只發送文件列表，導致後端使用過時的持久化配置，忽視用戶在 UI 中的臨時修改。

**解決方案**：
- `transcribe_requested` 信號改為 `Signal(list, object)` 攜帶 `FileConfig` 第二參數
- `_on_start()` 現在發送 `emit(paths, self.get_config())`，確保 UI 當前狀態即時反映
- `_on_files_dropped()` 及 `_transcribe_files()` 接收並使用傳入的 `FileConfig`

**影響**：用戶在轉錄頁籤修改的選項（SRT/TXT/JSON 格式、LLM 優化）現在即使未按 Save 也能生效。

### 錄音上限浮窗不消失（P3）

**問題**：當錄音達到上限時，浮窗狀態指示器被隱藏，用戶無法看到「已達錄音上限」狀態。

**解決方案**：`MainWindow._sync_indicator_state()` 中添加 `or status == "已達錄音上限"` 到錄音狀態條件：

```python
if (
    status == STATUS_RECORDING
    or status.startswith("錄音")
    or status == "已達錄音上限"
):
    return "recording"
```

**效果**：浮窗現在在用戶達到錄音上限時保持可見，明確提示狀態。
