"""
ASR 模型比較測試工具

獨立的 CLI 工具，用來比較不同 ASR 模型的識別結果。
按空白鍵錄音，自動用所有已安裝模型識別，並排顯示結果。

用法：
    cd zhouzhou-voice
    python tools/asr_benchmark.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event

# 加入專案根目錄到 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.asr_engine import ASREngine
from core.audio_recorder import AudioRecorder
from core.model_catalog import get_installed_models


def clear_line() -> None:
    """清除當前行。"""
    print("\r\033[K", end="", flush=True)


def print_header() -> None:
    """印標題。"""
    print("\n=== ASR 模型比較測試 ===\n")


def wait_for_space(prompt: str) -> None:
    """等待使用者按空白鍵。"""
    print(prompt, end="", flush=True)
    while True:
        ch = sys.stdin.read(1)
        if ch == " ":
            break


def record_audio(recorder: AudioRecorder) -> bytes:
    """
    錄音並返回音頻資料。
    使用 threading.Event 來處理空白鍵停止。
    """
    import msvcrt  # Windows only

    print("按 [空白鍵] 開始錄音...", end="", flush=True)

    # 等待開始
    while True:
        if msvcrt.kbhit() and msvcrt.getch() == b" ":
            break
        time.sleep(0.01)

    recorder.start_recording()
    clear_line()
    print("● 錄音中 0.0s (再按空白鍵停止)", end="", flush=True)

    # 顯示錄音時間，等待停止
    start = time.monotonic()
    while recorder.is_recording:
        duration = time.monotonic() - start
        print(f"\r● 錄音中 {duration:.1f}s (再按空白鍵停止)", end="", flush=True)

        # 檢查是否按下空白鍵
        if msvcrt.kbhit() and msvcrt.getch() == b" ":
            break
        time.sleep(0.05)

    audio = recorder.stop_recording()
    duration = time.monotonic() - start
    clear_line()
    print(f"● 錄音完成 {duration:.1f}s\n")

    return audio


def run_benchmark(models_dir: Path) -> None:
    """執行基準測試。"""
    print_header()

    # 掃描已安裝模型
    installed = get_installed_models(models_dir)

    if not installed:
        print("錯誤：找不到已安裝的模型")
        print(f"請確認 {models_dir} 目錄下有模型檔案")
        return

    print(f"已安裝 {len(installed)} 個模型，全部選中\n")

    # 列出模型
    for i, m in enumerate(installed, 1):
        langs = m.languages.split("、")[0] if "、" in m.languages else m.languages
        print(f"  [{i}] {m.name} ({m.engine_type}, {langs})")
    print()

    # 初始化錄音器
    recorder = AudioRecorder()
    recorder.open()

    try:
        while True:
            # 錄音
            audio = record_audio(recorder)

            if not audio:
                print("錯誤：沒有錄到音頻\n")
                continue

            # 依序用每個模型識別
            results: list[tuple[str, float, str]] = []  # (name, time, text)

            for model in installed:
                model_dir = models_dir / model.model_dir
                engine = ASREngine(model_dir, model_info=model)

                # 載入模型
                load_start = time.monotonic()
                try:
                    engine.load_model()
                    load_time = time.monotonic() - load_start
                    print(f"載入 {model.key}... OK ({load_time:.1f}s)")
                except Exception as e:
                    print(f"載入 {model.key}... 失敗: {e}")
                    results.append((model.name, 0, f"[錯誤: {e}]"))
                    continue

                # 識別
                try:
                    recog_start = time.monotonic()
                    result = engine.recognize(audio)
                    recog_time = time.monotonic() - recog_start
                    results.append((model.name, recog_time, result.text or "(無結果)"))
                except Exception as e:
                    results.append((model.name, 0, f"[識別錯誤: {e}]"))
                finally:
                    engine.close()

            # 顯示結果表格
            print(f"\n─── 識別結果 ───")
            print(f"{'模型':<24} {'耗時':>6}  {'識別結果'}")
            print("-" * 60)

            for name, t, text in results:
                # 顯示完整文字（不截斷）
                print(f"{name:<24} {t:>5.2f}s")
                print(f"{'':24} {'':>7}{text}")
                print()

            # 詢問下一步
            print()
            while True:
                print("[R] 重新錄音  [Q] 退出", end=" ", flush=True)
                ch = sys.stdin.read(1).lower()
                print()
                if ch == "r":
                    print()
                    break
                elif ch == "q":
                    return

    finally:
        recorder.close()


def main() -> None:
    models_dir = ROOT / "models"
    run_benchmark(models_dir)


if __name__ == "__main__":
    main()
