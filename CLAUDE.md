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

## 測試與驗證

- `tools/asr_benchmark.py`：CLI 工具，比較所有已安裝 ASR 模型的識別速度與準確率

## 打包（Nuitka）

```bash
# 使用 build.bat，禁止手動拼命令（曾踩坑 GUI 不顯示）
build\build.bat
```

輸出：`dist\main.dist\zhouzhou-voice.exe`（25MB exe + ~370MB 含模型）。需時 10-30 分鐘。

## 高階架構

> 完整系統設計（初始化順序、語音資料流、各模組職責）見 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

### 執行緒模型

| 執行緒 | 內容 |
|--------|------|
| 主執行緒 | Qt 事件循環 + 托盤 |
| pynput 執行緒 | 全局快捷鍵監聽 |
| voice-worker daemon | ASR → 熱詞 → LLM → 輸出 |
| repolish-worker daemon | 重新潤色（threading.Lock 防雙次觸發） |
| ASR 子進程 | sherpa-onnx 模型隔離（crash 不影響主進程） |

### 關鍵設計決策

- **ASR 子進程隔離**：模型崩潰不影響主進程，可重啟
- **Frozen dataclass**：所有 config 物件 `frozen=True`，防意外修改
- **Queue + task_id**：IPC 通信，每次請求前呼叫 `_drain_stale()` 清過期響應
- **LLM 連線池**：urllib3.PoolManager 全局共享，同 host 復用 TCP+TLS
- **GUI 線程安全**：所有主線程更新通過 `_invoke_gui(method, *args)` 進行（v3.6.0 統一介面）
- **長錄音分段去重**：`_merge_text_overlap_parts()` 逐對比較相鄰段邊界，找最長精確匹配後拼接，避免重疊文字重複

## 已知坑

- pynput 1.8：`win32_event_filter` 返回 False 會跳過回調，已在 `utils/hotkey.py` 修復
- Nuitka 打包必須用 `build.bat`，禁止手動拼命令
- ASR 動態超時：`max(30, 錄音長度×1.5)` 秒
- 不可排除 `PySide6.QtMultimedia`（`gui/widgets/audio_player.py` 依賴）
- dist 目錄偶有 Windows 保留名 `nul` 文件，無法刪除，忽略即可
- 日誌在 `%APPDATA%\zhouzhou-voice\logs\app.log`
