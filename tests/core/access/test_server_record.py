"""The data directory's server access record: round trip and fail-closed reading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.access.config import AccessConfigError, access_config
from core.access.server_record import (
    RecordedServerAccess,
    read_server_access,
    server_access_path,
    validate_server_access_file,
    write_server_access,
)


def test_the_record_round_trips_and_refuses_what_the_server_must_not_serve(
    tmp_path: Path,
) -> None:
    assert read_server_access(tmp_path) is None
    recorded = RecordedServerAccess(access_config("lan", allow_hosts=["vbot.home.arpa"]), "0.0.0.0")

    write_server_access(tmp_path, recorded)

    path = server_access_path(tmp_path)
    assert read_server_access(tmp_path) == recorded
    assert validate_server_access_file(path).ok
    # A record that names a mode its bind address contradicts never loads.
    document = json.loads(path.read_text(encoding="utf-8"))
    document["mode"] = "local"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AccessConfigError, match="records cannot be read"):
        read_server_access(tmp_path)
    assert not validate_server_access_file(path).ok
    # The server decided: writing replaces even an unreadable record.
    write_server_access(tmp_path, recorded)
    assert read_server_access(tmp_path) == recorded
