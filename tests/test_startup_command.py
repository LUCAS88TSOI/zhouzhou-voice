"""_startup_command 回歸測試。

防止再次寫入裸 python.exe（開機只彈空白 Python 視窗、不啟動本程式的 bug）。
顯式 patch IS_PACKAGED 覆蓋「原始碼 / 打包」兩種分支，避免回歸保護依賴執行環境。
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

_PROJ = Path(__file__).resolve().parent.parent
os.chdir(_PROJ)
sys.path.insert(0, str(_PROJ))

from utils.startup import _startup_command  # noqa: E402


def test_source_mode_has_main_py_arg() -> None:
    """原始碼模式：必須是『"直譯器" "main.py"』兩段，而非裸 python.exe。"""
    with patch("utils.startup.IS_PACKAGED", False):
        cmd = _startup_command()
    parts = shlex.split(cmd, posix=False)  # posix=False：保留引號、不解讀反斜線
    assert len(parts) == 2, cmd
    assert parts[1].strip('"').lower().endswith("main.py"), cmd
    assert Path(parts[0].strip('"')).exists(), parts[0]  # 直譯器確實存在


def test_packaged_mode_single_exe_no_script() -> None:
    """打包模式：只有單一 exe 路徑，不帶 main.py 參數。"""
    with patch("utils.startup.IS_PACKAGED", True):
        cmd = _startup_command()
    parts = shlex.split(cmd, posix=False)
    assert len(parts) == 1, cmd
    assert "main.py" not in cmd.lower()


if __name__ == "__main__":
    test_source_mode_has_main_py_arg()
    test_packaged_mode_single_exe_no_script()
    print("PASS: tests/test_startup_command.py")
