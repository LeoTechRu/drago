"""Tests for shared utility helpers."""

from __future__ import annotations

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


def test_clip_text_respects_max_chars_for_small_limits():
    text = "x" * 500

    clipped = clip_text(text, 50)
    assert len(clipped) <= 50
    assert "...(truncated)..." in clipped

    tiny = clip_text(text, 5)
    assert len(tiny) == 5
