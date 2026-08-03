"""Append one Codex lifecycle hook event to its owning Terminal Session stream."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

TERMINAL_EVENT_FILE_ENV = "VBOT_TERMINAL_EVENT_FILE"
TERMINAL_EVENT_NONCE_ENV = "VBOT_TERMINAL_EVENT_NONCE"
TERMINAL_HOOK_INPUT_CAP_BYTES = 256 * 1024
TERMINAL_HOOK_EVENT_VERSION = 1


def main() -> int:
    """Persist one bounded hook record and always leave Codex unblocked."""
    try:
        event_file = _event_file()
        nonce = os.environ.get(TERMINAL_EVENT_NONCE_ENV, "")
        raw = sys.stdin.buffer.read(TERMINAL_HOOK_INPUT_CAP_BYTES + 1)
        if not nonce or len(raw) > TERMINAL_HOOK_INPUT_CAP_BYTES:
            return _success()
        event = json.loads(raw.decode("utf-8"))
        if not isinstance(event, dict):
            return _success()
        record: dict[str, Any] = {
            "version": TERMINAL_HOOK_EVENT_VERSION,
            "nonce": nonce,
            "event": event,
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor = os.open(event_file, os.O_APPEND | os.O_WRONLY)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
    except Exception:
        # Hooks are an attention side channel. A sink failure must not change or
        # block Codex's own approval, question, or completion behavior.
        pass
    return _success()


def _event_file() -> Path:
    value = os.environ.get(TERMINAL_EVENT_FILE_ENV, "")
    if not value:
        raise ValueError("Terminal event file is unavailable")
    return Path(value)


def _success() -> int:
    sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "TERMINAL_EVENT_FILE_ENV",
    "TERMINAL_EVENT_NONCE_ENV",
    "TERMINAL_HOOK_EVENT_VERSION",
    "TERMINAL_HOOK_INPUT_CAP_BYTES",
    "main",
]
