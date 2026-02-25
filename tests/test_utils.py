"""Tests for shared utility helpers."""

from __future__ import annotations

import pathlib
import sys

import pytest

from drago.utils import run_cmd
from drago.utils import clip_text


def test_run_cmd_returns_stdout():
    out = run_cmd([sys.executable, "-c", "print('ok')"])
    assert out == "ok"


def test_run_cmd_raises_runtime_error_on_timeout():
    with pytest.raises(RuntimeError, match="Command timed out after"):
        run_cmd([sys.executable, "-c", "import time; time.sleep(1)"], timeout_sec=0.05)


def test_run_cmd_raises_runtime_error_on_missing_command():
    with pytest.raises(RuntimeError, match="Command not found"):
        run_cmd(["__drago_nonexistent_cmd__"])


def test_run_cmd_handles_pathlike_items_in_error_message():
    with pytest.raises(RuntimeError, match="Command failed:"):
        run_cmd([pathlib.Path(sys.executable), "-c", "import sys; sys.exit(3)"])


def test_clip_text_respects_max_chars_for_small_limits():
    text = "x" * 500

    clipped = clip_text(text, 50)
    assert len(clipped) <= 50
    assert "...(truncated)..." in clipped

    tiny = clip_text(text, 5)
    assert len(tiny) == 5
