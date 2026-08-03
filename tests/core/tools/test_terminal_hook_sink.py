"""Tests for the non-blocking Codex hook side-channel sink."""

from __future__ import annotations

import io
import json
from pathlib import Path

from core.tools import terminal_hook_sink


def test_hook_sink_appends_authenticated_record(tmp_path: Path, monkeypatch, capsys) -> None:
    event_file = tmp_path / "events.jsonl"
    event_file.touch()
    monkeypatch.setenv(terminal_hook_sink.TERMINAL_EVENT_FILE_ENV, str(event_file))
    monkeypatch.setenv(terminal_hook_sink.TERMINAL_EVENT_NONCE_ENV, "nonce-a")
    monkeypatch.setattr(
        terminal_hook_sink.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps({"hook_event_name": "Stop", "turn_id": "turn-a"}).encode("utf-8")
            ),
            encoding="utf-8",
        ),
    )

    assert terminal_hook_sink.main() == 0
    assert capsys.readouterr().out == "{}"
    record = json.loads(event_file.read_text(encoding="utf-8"))
    assert record == {
        "version": terminal_hook_sink.TERMINAL_HOOK_EVENT_VERSION,
        "nonce": "nonce-a",
        "event": {"hook_event_name": "Stop", "turn_id": "turn-a"},
    }


def test_hook_sink_never_blocks_codex_on_invalid_input(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(terminal_hook_sink.TERMINAL_EVENT_FILE_ENV, str(tmp_path / "events.jsonl"))
    monkeypatch.setenv(terminal_hook_sink.TERMINAL_EVENT_NONCE_ENV, "nonce-a")
    monkeypatch.setattr(
        terminal_hook_sink.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b"not-json"), encoding="utf-8"),
    )

    assert terminal_hook_sink.main() == 0
    assert capsys.readouterr().out == "{}"
    assert not (tmp_path / "events.jsonl").exists()
