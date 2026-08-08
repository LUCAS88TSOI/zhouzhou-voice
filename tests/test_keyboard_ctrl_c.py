"""utils/keyboard.py::KeyboardSimulator.press_ctrl_c() 單元測試。"""
import os
import sys
from unittest.mock import MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.keyboard import KeyboardSimulator


def test_press_ctrl_c_presses_and_releases_in_order():
    from pynput.keyboard import Key

    mock_ctrl = MagicMock()
    KeyboardSimulator._controller = mock_ctrl
    try:
        result = KeyboardSimulator.press_ctrl_c()
    finally:
        KeyboardSimulator._controller = None

    assert result is True
    assert mock_ctrl.press.call_args_list[0] == call(Key.ctrl_l)
    assert mock_ctrl.press.call_args_list[1] == call("c")
    assert mock_ctrl.release.call_args_list[0] == call("c")
    assert mock_ctrl.release.call_args_list[1] == call(Key.ctrl_l)


def test_press_ctrl_c_returns_false_on_exception():
    mock_ctrl = MagicMock()
    mock_ctrl.press.side_effect = RuntimeError("boom")
    KeyboardSimulator._controller = mock_ctrl
    try:
        result = KeyboardSimulator.press_ctrl_c()
    finally:
        KeyboardSimulator._controller = None

    assert result is False
