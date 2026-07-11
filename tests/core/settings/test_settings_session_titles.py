"""Automatic Session-title settings contracts."""

from __future__ import annotations

import json
from pathlib import Path

from core.settings import parse_settings_update, validate_settings_file


def test_public_update_normalizes_complete_session_title_section() -> None:
    assert parse_settings_update(
        {
            "session_titles": {
                "enabled": True,
                "model": " openai/gpt-4.1-mini::api-key ",
            }
        }
    ) == {
        "session_titles": {
            "enabled": True,
            "model": "openai/gpt-4.1-mini::api-key",
        }
    }


def test_raw_settings_accept_session_title_section(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "session_titles": {
                    "enabled": False,
                    "model": "",
                }
            }
        ),
        encoding="utf-8",
    )

    assert validate_settings_file(path).ok is True


def test_raw_settings_reject_invalid_session_title_fields(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "session_titles": {
                    "enabled": "yes",
                    "model": 7,
                    "extra": True,
                }
            }
        ),
        encoding="utf-8",
    )

    diagnostics = {
        (item.severity, item.path, item.message)
        for item in validate_settings_file(path).diagnostics
    }
    assert ("error", "$.session_titles.enabled", "must be a boolean") in diagnostics
    assert ("error", "$.session_titles.model", "must be a string") in diagnostics
    assert (
        "warning",
        "$.session_titles.extra",
        "unknown session_titles field: extra",
    ) in diagnostics
