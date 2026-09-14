"""Tests for the authenticated local server control record."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from core.utils.processes import kill_process_tree, subprocess_creation_flags
from core.utils.server_control import (
    control_record_path,
    create_server_control,
    is_authorized_control_token,
    read_server_control,
    remove_server_control,
    server_control_claim,
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


def test_control_claim_prevents_replacing_current_authority(tmp_path: Path) -> None:
    with server_control_claim(tmp_path, 8420):
        original = create_server_control(tmp_path, 8420, token="original")
        with (
            pytest.raises(RuntimeError, match="control authority"),
            server_control_claim(tmp_path, 8420),
        ):
            raise AssertionError("unreachable")
        assert read_server_control(tmp_path, 8420) == original


def test_process_exit_releases_claim_without_replacing_record(tmp_path: Path) -> None:
    script = (
        "import sys, time; from core.utils.server_control import server_control_claim, "
        "create_server_control; root=sys.argv[1]; "
        "claim=server_control_claim(root, 8420); claim.__enter__(); "
        "create_server_control(root, 8420, token='child'); "
        "print('ready', flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[3],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        creationflags=subprocess_creation_flags(),
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        original = read_server_control(tmp_path, 8420)
        assert original is not None and original.token == "child"
        with (
            pytest.raises(RuntimeError, match="control authority"),
            server_control_claim(tmp_path, 8420),
        ):
            raise AssertionError("unreachable")
        assert read_server_control(tmp_path, 8420) == original
    finally:
        # A Windows venv interpreter may be a launcher with a Python child.
        kill_process_tree(child)
        child.wait(timeout=10)
    with server_control_claim(tmp_path, 8420):
        replacement = create_server_control(tmp_path, 8420, token="replacement")
    assert read_server_control(tmp_path, 8420) == replacement


def test_claim_refuses_live_legacy_control_owner(tmp_path: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        creationflags=subprocess_creation_flags(),
    )
    try:
        create_server_control(
            tmp_path,
            8420,
            pid=child.pid,
            process_create_time=psutil.Process(child.pid).create_time(),
            token="legacy",
        )
        with (
            pytest.raises(RuntimeError, match="live server"),
            server_control_claim(tmp_path, 8420),
        ):
            raise AssertionError("unreachable")
        assert read_server_control(tmp_path, 8420).token == "legacy"  # type: ignore[union-attr]
    finally:
        kill_process_tree(child)
        child.wait(timeout=10)
