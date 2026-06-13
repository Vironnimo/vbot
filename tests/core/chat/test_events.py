"""Tests for chat run-event projection helpers."""

from __future__ import annotations

from types import SimpleNamespace

from core.chat.events import (
    PARTIAL_THINKING_CAP,
    _maybe_persist_partial_thinking,
    _partial_thinking_note_content,
)
from core.sessions import PARTIAL_THINKING_NOTE_PREFIX


class TestPartialThinkingNoteContent:
    def test_prefix_and_label_present(self) -> None:
        content = _partial_thinking_note_content("a thought")

        assert content.startswith(PARTIAL_THINKING_NOTE_PREFIX)
        assert "Partial thinking before interruption:" in content
        assert content.endswith("a thought")

    def test_short_reasoning_is_not_truncated(self) -> None:
        content = _partial_thinking_note_content("short")

        assert "truncated" not in content

    def test_reasoning_over_cap_is_truncated_keeping_head(self) -> None:
        partial = "x" * (PARTIAL_THINKING_CAP + 500)

        content = _partial_thinking_note_content(partial)

        assert "[… partial thinking truncated]" in content
        # The retained head is exactly the cap; the overflow tail is dropped.
        assert content.count("x") == PARTIAL_THINKING_CAP


class TestMaybePersistPartialThinking:
    def test_routes_capped_content_to_note_hook(self) -> None:
        captured: list[str] = []
        accumulator = SimpleNamespace(partial_reasoning="y" * (PARTIAL_THINKING_CAP + 10))

        _maybe_persist_partial_thinking(accumulator, captured.append)

        assert len(captured) == 1
        assert captured[0].startswith(PARTIAL_THINKING_NOTE_PREFIX)
        assert captured[0].count("y") == PARTIAL_THINKING_CAP

    def test_no_note_when_no_partial_reasoning(self) -> None:
        captured: list[str] = []
        accumulator = SimpleNamespace(partial_reasoning=None)

        _maybe_persist_partial_thinking(accumulator, captured.append)

        assert captured == []

    def test_no_hook_is_a_noop(self) -> None:
        accumulator = SimpleNamespace(partial_reasoning="something")

        _maybe_persist_partial_thinking(accumulator, None)
