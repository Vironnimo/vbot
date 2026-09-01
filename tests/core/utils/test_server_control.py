"""Tests for the authenticated local server control record."""

from __future__ import annotations

import json
from pathlib import Path

from core.utils.server_control import (
    control_record_path,
    create_server_control,
    is_authorized_control_token,
    read_server_control,
    remove_server_control,
)


def test_control_record_round_trips_and_is_removed_by_exact_owner(tmp_path: Path) -> None:
    record = create_server_control(
        tmp_path,
        8420,
        pid=1234,
        process_create_time=1000.25,
        token="secret-token",
    )

    assert read_server_control(tmp_path, 8420) == record
    assert record.path == control_record_path(tmp_path, 8420)

    remove_server_control(record)

    assert not record.path.exists()


def test_stale_owner_cannot_remove_replacement_control_record(tmp_path: Path) -> None:
    stale = create_server_control(
        tmp_path,
        8420,
        pid=1234,
        process_create_time=1000.25,
        token="stale-token",
    )
    current = create_server_control(
        tmp_path,
        8420,
        pid=5678,
        process_create_time=1001.5,
        token="current-token",
    )

    remove_server_control(stale)

    assert read_server_control(tmp_path, 8420) == current


def test_invalid_or_oversized_control_record_is_ignored(tmp_path: Path) -> None:
    path = control_record_path(tmp_path, 8420)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 0,
                "process_create_time": 1000.25,
                "port": 8420,
                "token": "x",
            }
        )
    )

    assert read_server_control(tmp_path, 8420) is None

    path.write_bytes(b"x" * 20_000)
    assert read_server_control(tmp_path, 8420) is None


def test_control_token_authorization_requires_exact_nonempty_secret() -> None:
    assert is_authorized_control_token("secret", "secret") is True
    assert is_authorized_control_token("wrong", "secret") is False
    assert is_authorized_control_token(None, "secret") is False
    assert is_authorized_control_token("secret", None) is False
