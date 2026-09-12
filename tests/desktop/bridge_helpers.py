"""Shared fixtures and fakes for bridge behavior tests."""

from __future__ import annotations

import json
from pathlib import Path


def _write_settings(path: Path, wakeword_config: dict | None = None) -> None:
    data = {"host": "127.0.0.1", "port": 8420}
    if wakeword_config is not None:
        data["wakeword"] = wakeword_config
    path.write_text(json.dumps(data), encoding="utf-8")


class FakeWorker:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.stop_recording_calls = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.started = False

    def stop_recording(self) -> None:
        self.stop_recording_calls += 1

    def is_running(self) -> bool:
        return self.started
