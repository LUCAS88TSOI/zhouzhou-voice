# 貢獻指南 (Contributing)

感謝你有興趣為 **州州語音 (ZhouZhou Voice)** 出一分力！本文說明如何設置開發環境、提交變更與遵循專案規範。

## 開發環境設置

需求：Windows 10/11 (64-bit)、Python 3.10+、FFmpeg（文件轉錄功能需要）。

```bash
# 1. 安裝依賴
pip install -r requirements.txt

# 2. 下載 ASR 模型（model.onnx 不入 repo，太大）
#    放到 models/sensevoice/：model.onnx + tokens.txt
#    來源見 README「下載 ASR 模型」

# 3. 開發啟動
python main.py
```

## 執行測試

本專案使用 **pytest**。GUI 測試需以 headless 模式跑：

```bash
# Windows PowerShell
$env:QT_QPA_PLATFORM = "offscreen"; pytest

# bash
QT_QPA_PLATFORM=offscreen pytest
```

送 PR 前請確保 `pytest` 全部通過。新功能與 bug 修復請附上對應測試（先寫失敗測試，再實作 — TDD）。

## 程式碼規範

- 遵循 **PEP 8**，函數簽名加 **type hints**
- 統一用 `logger`（`utils/logger.py`），**禁止 `print()` 調試**
- 所有 GUI 更新必須經 `QMetaObject.invokeMethod()` 回主線程（worker thread 不可直接碰 Qt widget）
- 配置物件為 frozen dataclass，修改用 `dataclasses.replace()`，禁止直接賦值（否則 `FrozenInstanceError`）
- 錯誤要顯式處理，不可靜默吞錯（`except: pass`）
- 檔案保持聚焦：單檔 ≤ 800 行為目標，函數 ≤ 50 行
- 修改代碼後請在 `DEVLOG.md`（如有）或 PR 描述記錄變更與根因

架構與資料流請先讀根目錄 `CLAUDE.md`，深入設計見 `docs/ARCHITECTURE.md`。

## Commit 與 PR

- Commit message 採 **Conventional Commits**：`<type>: <description>`
  （type：`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`）
- 一個 PR 聚焦一件事；描述清楚動機、做法與測試方式
- 送 PR 前先 `git pull` 對齊 `main`，解決衝突

## 關於 LLM 提示詞（開放核心）

`llm/roles/*.py` 內的潤色提示詞為**開源 baseline**，歡迎改進。終端使用者亦可在「設定 → 角色」自行覆寫（存於 `%APPDATA%\zhouzhou-voice\config.json`，不入 repo）。

請注意：專案維護者後續持續調優的提示詞版本可能以**私有 overlay** 形式發佈，不會進入本開源 repo——這不影響開源版的完整可用性，PR 仍以改進 baseline 為主。

## 回報問題

開 Issue 時請附上：重現步驟、預期 vs 實際行為、`%APPDATA%\zhouzhou-voice\logs\app.log` 相關片段（**記得先移除任何 API Key 等敏感資料**）、OS 與 Python 版本。

感謝你的貢獻！🎙️
