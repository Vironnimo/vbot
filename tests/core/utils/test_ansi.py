"""Tests for ANSI escape-sequence stripping."""

from __future__ import annotations

import pytest

from core.utils.ansi import strip_ansi


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain text",
        "no escapes 123 | foo\nsecond line\n",
    ],
)
def test_clean_text_passes_through_unchanged(text: str) -> None:
    assert strip_ansi(text) == text


def test_returns_same_object_for_clean_text() -> None:
    # Fast path: clean text is returned as-is without a substitution pass.
    text = "untouched"
    assert strip_ansi(text) is text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\x1b[31mred\x1b[0m", "red"),  # CSI color
        ("\x1b[1;32mbold green\x1b[0m", "bold green"),  # multi-param CSI
        ("\x1b[?25lhidden\x1b[?25h", "hidden"),  # private-mode CSI
        ("a\x1b[2Kb", "ab"),  # erase-line CSI between characters
        ("\x1b]0;window title\x07text", "text"),  # OSC with BEL terminator
        ("\x1b]0;title\x1b\\text", "text"),  # OSC with ST terminator
        ("plain\nlines\n", "plain\nlines\n"),  # newlines are preserved
    ],
)
def test_strips_escape_sequences(text: str, expected: str) -> None:
    assert strip_ansi(text) == expected


def test_preserves_surrounding_visible_text() -> None:
    colored = "\x1b[32mbuild ok\x1b[0m\nnext line"
    assert strip_ansi(colored) == "build ok\nnext line"


def test_strips_8bit_c1_controls() -> None:
    assert strip_ansi("a\x9b31mb") == "ab"  # 8-bit CSI introducer
    assert strip_ansi("x\x84y") == "xy"  # stray C1 control
