# ZhouZhou Voice / 州州語音

Windows 離線語音輸入工具 — 按住快捷鍵說話，鬆開即輸入文字。

基於本地 AI 模型（sherpa-onnx + SenseVoice），完全離線運行，無需網路，保護隱私。

---

## 功能特色

- **完全離線** — 語音識別在本機運行，不傳任何資料到網路
- **中英混合識別** — 自動識別中文和英文，無需切換語言
- **智能熱詞** — 自定義術語（人名、專有名詞），提升識別準確率
- **LLM 潤色**（可選） — 接入 OpenAI 兼容 API，將口語自動轉書面語
- **文件轉錄** — 拖放音視頻文件，輸出 SRT 字幕 / TXT / JSON
- **系統托盤** — 背景運行，不占畫面空間

---

## 快速開始（使用打包好的 exe）

1. 進入 `dist\main.dist\` 資料夾
2. 確認 `models\sensevoice\model.onnx` 和 `tokens.txt` 存在
3. 雙擊 `zhouzhou-voice.exe`（或桌面捷徑）
4. 程式會縮到系統托盤（螢幕右下角時鐘旁）
5. **長按 CapsLock 說話，放開即輸入文字**

### 使用方式

| 操作 | 效果 |
|------|------|
| 長按 CapsLock + 說話 + 放開 | 語音轉文字，自動貼上 |
| 短按 CapsLock | 正常切換大小寫（不觸發語音） |
| 托盤右鍵 | 選單：設置、複製結果、文件轉錄、退出 |
| 托盤雙擊 | 打開主視窗（查看識別結果） |
| 拖放文件到主視窗 | 轉錄音視頻文件為字幕 |

---

## 開發環境設置

### 系統需求

- Windows 10/11 (64-bit)
- Python 3.12+
- FFmpeg（文件轉錄功能需要）

### 安裝依賴

```bash
pip install -r requirements.txt
```

### 下載 ASR 模型

將 SenseVoice int8 模型放到 `models/sensevoice/`：
- `model.onnx` — SenseVoice int8 量化模型（約 229MB）
- `tokens.txt` — 字典文件

模型來源：[sherpa-onnx SenseVoice 模型](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)

### 開發模式啟動

```bash
python main.py
```

或使用啟動腳本：

```bash
start.bat
```

---

## 打包（Nuitka）

### 執行打包

```bash
build\build.bat
```

打包完成後輸出在 `dist\main.dist\`，需要手動複製模型文件（build.bat 會自動處理）。

### 打包結果

| 項目 | 數值 |
|------|------|
| exe 大小 | ~25 MB |
| dist 資料夾（含模型） | ~370 MB |
| dist 資料夾（不含模型） | ~140 MB |
| 編譯器 | MinGW64 gcc 14.2（Nuitka 自動下載） |

---

## 目錄結構

```
zhouzhou-voice/
├── main.py                 # 程式入口
├── start.bat               # 開發模式啟動腳本
├── requirements.txt        # Python 依賴清單
│
├── app/
│   ├── app.py              # 應用主類（串接所有模組）
│   └── lifecycle.py        # 生命週期管理
│
├── core/
│   ├── asr_engine.py       # sherpa-onnx SenseVoice 封裝
│   ├── asr_process.py      # ASR 子進程（multiprocessing）
│   ├── audio_recorder.py   # 麥克風錄音（sounddevice）
│   ├── model_catalog.py    # ASR 模型清單與元數據
│   ├── model_downloader.py # 模型下載工具
│   ├── recording_db.py     # 錄音歷史（SQLite）
│   └── text_processor.py   # 文字後處理（標點、空格、繁體）
│
├── hotword/
│   ├── defaults/           # 預設熱詞文件（首次安裝自動複製）
│   │   ├── hot.txt         # 廣東話預設熱詞（~130 個）
│   │   ├── hot-rule.txt    # 預設替換規則
│   │   └── hot-rectify.txt # 糾錯歷史範本
│   ├── phoneme.py          # 音素匹配（pypinyin）
│   ├── rules.py            # 規則替換（正則/等值）
│   ├── rectify.py          # 糾錯歷史
│   └── manager.py          # 熱詞管理器 + 文件監控
│
├── llm/
│   ├── builtin_key.py      # 內建金鑰佔位（免費額度已停用，需自行配置 API Key）
│   ├── provider.py         # LLM 服務商配置（9 家）
│   ├── client.py           # OpenAI 兼容 API 客戶端
│   ├── processor.py        # LLM 處理 + 串流輸出
│   └── roles/              # 角色定義
│       ├── default.py      # 預設潤色
│       ├── writing_mode.py # 書面語模式
│       ├── translator.py   # 翻譯模式
│       ├── assistant.py    # 助手模式
│       ├── instruction_mode.py  # 指令模式
│       └── dev_mode.py     # 開發模式
│
├── gui/
│   ├── main_window.py      # 主視窗（QStackedWidget）
│   ├── tray_icon.py        # 系統托盤
│   ├── settings_panel.py   # 設定面板（6 頁籤，可嵌入）
│   ├── mic_test_dialog.py  # 麥克風測試對話框
│   ├── recording_indicator.py  # 錄音狀態指示
│   ├── update_dialog.py    # 自動更新對話框
│   └── widgets/            # 自訂 UI 組件
│       ├── shortcut_input.py    # 快捷鍵輸入
│       ├── provider_combo.py    # 服務商下拉
│       ├── asr_tab.py           # ASR 設定頁籤
│       ├── role_tab.py          # 角色設定頁籤
│       ├── hotword_tab.py       # 熱詞設定頁籤
│       ├── history_tab.py       # 錄音歷史頁籤
│       └── audio_player.py      # 音頻播放器
│
├── transcribe/
│   ├── file_transcriber.py # FFmpeg + 分段 ASR
│   └── srt_writer.py       # SRT/TXT/JSON 輸出
│
├── utils/
│   ├── logger.py           # 日誌系統
│   ├── config.py           # 配置管理器（JSON + dataclass）
│   ├── paths.py            # 統一路徑（APP_ROOT / MODELS_DIR / APP_VERSION）
│   ├── hotkey.py           # pynput 快捷鍵監聽
│   ├── keyboard.py         # 鍵盤模擬
│   ├── clipboard.py        # Win32 剪貼板
│   ├── startup.py          # 開機自啟管理
│   └── updater.py          # 自動更新檢測與下載
│
├── models/sensevoice/      # ASR 模型（不進 git）
├── assets/                 # 圖標資源
├── build/                  # 打包腳本
│   ├── build.bat           # Nuitka 打包腳本（唯一入口）
│   └── verify_build.py     # 打包後驗證腳本
├── dist/                   # 打包輸出（不進 git）
└── docs/                   # 文檔
    └── ARCHITECTURE.md     # 系統架構設計
```

---

## 配置

配置文件位置：`%APPDATA%\zhouzhou-voice\config.json`

首次啟動自動生成預設配置。可在托盤右鍵 → 設置 中修改：

- **快捷鍵** — 預設 CapsLock，可改為其他鍵
- **LLM 服務商** — 預設關閉，可選 OpenAI / DeepSeek / SiliconFlow 等 9 家
- **輸出選項** — 繁體/簡體、標點保留、自動換行等

---

## 技術棧

| 模組 | 技術 | 授權 |
|------|------|------|
| 語音識別 | sherpa-onnx + SenseVoice | Apache 2.0 |
| GUI | PySide6 (Qt) | LGPL v3 |
| 快捷鍵 | pynput | LGPL v3 |
| 錄音 | sounddevice | MIT |
| 打包 | Nuitka | Apache 2.0 |

---

## 常見問題

### Windows 提示「無法辨識應用程式」或被防毒軟體阻擋怎麼辦？

本程式以 Nuitka 打包，沒有購買代碼簽名憑證，Windows SmartScreen 或 Defender 可能在首次執行時提示警告。這是所有未簽名開源程式的共同情況，程式本身不含任何惡意代碼。

**SmartScreen 藍色提示（「Windows 已保護您的電腦」）：**
1. 點擊「更多資訊」
2. 點擊「仍要執行」

**Windows Defender 隔離（exe 被自動移除）：**
1. 打開「Windows 安全性」→「病毒與威脅防護」→「保護歷程記錄」
2. 找到被隔離的 `zhouzhou-voice.exe`，點擊「動作」→「允許在裝置上執行」
3. 重新解壓縮 ZIP，再次啟動程式

**若仍有問題：**
在 Windows 安全性 → 病毒與威脅防護設定 → 排除項目，將 `zhouzhou-voice.exe` 加入排除清單。

---

## AI 助手開發

如果你使用 Claude Code 或其他 AI 助手開發此專案，請讓 AI 先閱讀根目錄的 `CLAUDE.md`。

該文件包含：
- 專案定位與技術棧
- 初始化順序與語音處理資料流
- 關鍵文件路徑與架構決策
- 已知問題與開發規則

AI 讀完 `CLAUDE.md` 後即可理解專案，再配合 `docs/ARCHITECTURE.md` 深入系統設計。

---

## 文檔索引

| 文檔 | 用途 |
|------|------|
| `README.md` | 你正在看的這個 — 快速上手 |
| `CHANGELOG.md` | 版本更新日誌 |
| `CONTRIBUTING.md` | 貢獻指南（開發設置、測試、規範） |
| `CLAUDE.md` | 專案總覽（架構、資料流、開發規則） |
| `docs/ARCHITECTURE.md` | 系統架構（模組設計、數據流） |
| `requirements.txt` | Python 依賴清單 |
| `build/build.bat` | Nuitka 打包腳本 |

---

## 授權 (License)

本專案以 **GNU AGPL-3.0-or-later** 授權，完整條款見 [`LICENSE`](LICENSE)。

AGPL-3.0 屬強 copyleft：若你修改本程式並對外提供服務（包含網路服務），須一併公開你的修改源碼。

> **開放核心（Open-Core）**：`llm/roles/` 內的 LLM 潤色提示詞為**開源 baseline**，可直接使用，也可在「設定 → 角色」中自行覆寫（覆寫內容存於 `%APPDATA%\zhouzhou-voice\config.json`，不進 repo）。專案維護者後續優化的提示詞版本可能以私有 overlay 形式發佈，不影響本開源版的完整可用性。

商業授權（雙許可）需求請聯絡專案維護者。

---

## 貢獻 (Contributing)

歡迎 Issue 與 Pull Request。送出前請：

1. 閱讀 `CLAUDE.md` 了解架構與開發規則
2. 確保 `pytest` 全部通過
3. 遵循專案風格：所有 GUI 更新經 `QMetaObject.invokeMethod()` 回主線程；config 用 `dataclasses.replace()`（frozen dataclass）；統一用 `logger` 而非 `print()`
4. commit message 採用 `<type>: <description>` 格式（feat / fix / refactor / docs / test / chore）
