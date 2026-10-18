"""Persisted settings setup shared by remembered-server and connection tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _settings(tmp_path: Path, data: dict[str, Any] | None = None) -> Path:
    settings_file = tmp_path / "settings.json"
    if data is not None:
        settings_file.write_text(json.dumps(data), encoding="utf-8")
    return settings_file


def _stored(settings_file: Path) -> dict[str, Any]:
    stored: dict[str, Any] = json.loads(settings_file.read_text(encoding="utf-8"))
    return stored


CLOUD_ENTRY = {"host": "vbot.example.com", "port": 443, "scheme": "https"}
