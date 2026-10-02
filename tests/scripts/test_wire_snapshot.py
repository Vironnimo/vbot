"""Smoke test of the offline wire snapshot CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.providers._http_shared as http_shared
from scripts import wire_snapshot


def test_capture_is_deterministic_and_compare_reports_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    builder = http_shared.build_async_client
    first, second = tmp_path / "first", tmp_path / "second"

    for out in (first, second):
        arguments = ["capture", "--out", str(out), "--provider", "snapshot-custom"]
        assert wire_snapshot.main(arguments) == 0

    records = [
        json.loads(line)
        for line in (first / "snapshot-custom.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    render = next(
        record
        for record in records
        if record["key"] == "render|snapshot-custom:default|custom-chat|text|effort=high|mode=send"
    )
    assert render["outcome"] == "captured"
    assert render["requests"][-1]["headers"]["authorization"].startswith("Bearer <secret:")
    assert http_shared.build_async_client is builder
    capsys.readouterr()

    assert wire_snapshot.main(["compare", str(first), str(second)]) == 0
    assert "identical" in capsys.readouterr().out
