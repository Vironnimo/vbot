"""Data-directory credential RPCs: names, sources and states, never a value."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from core.secrets import CREDENTIALS
from tests.server.rpc_test_support import (
    StubAdapter,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

_SECRET = "sk-value-that-never-leaves"
_CHANGED = [{"kind": "data_store"}, {"kind": "providers"}]


def _damage(state: Any, name: str, *, like: str) -> None:
    """Give ``name`` a valid ciphertext of the store bound to another name."""
    secrets = state.runtime.storage.secrets
    document = json.loads(secrets.path.read_text(encoding="utf-8"))
    document[CREDENTIALS][name] = document[CREDENTIALS][like]
    secrets.path.write_text(json.dumps(document), encoding="utf-8")
    secrets.reload()


@pytest.mark.asyncio
async def test_credentials_are_listed_set_and_unset_by_name_without_their_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("VBOT_TEST_TOKEN", "VBOT_TEST_DAMAGED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VBOT_TEST_PROCESS", "from-process")
    state = make_state(tmp_path, StubAdapter())

    assert await rpc_result(state, "secrets.list") == {
        "state": "absent",
        "reason": None,
        "credentials": [],
    }

    saved = await rpc_result(state, "secrets.set", name="VBOT_TEST_TOKEN", value=_SECRET)
    repeated = await rpc_result(state, "secrets.set", name="VBOT_TEST_TOKEN", value=_SECRET)

    assert saved == {
        "name": "VBOT_TEST_TOKEN",
        "configured": True,
        "source": "data_dir",
        "changed": True,
    }
    assert repeated["changed"] is False
    assert resource_changes(state) == _CHANGED
    assert state.runtime.resolve_environment_credential("VBOT_TEST_TOKEN") == _SECRET

    # The process environment keeps precedence over a stored value.
    shadowed = await rpc_result(state, "secrets.set", name="VBOT_TEST_PROCESS", value="stored")
    assert shadowed["source"] == "process_environment"
    await rpc_result(state, "secrets.set", name="VBOT_TEST_DAMAGED", value="damaged-value")
    _damage(state, "VBOT_TEST_DAMAGED", like="VBOT_TEST_TOKEN")

    listed = await rpc_result(state, "secrets.list")

    assert listed == {
        "state": "available",
        "reason": None,
        "credentials": [
            {"name": "VBOT_TEST_DAMAGED", "configured": False, "source": None, "damaged": True},
            {
                "name": "VBOT_TEST_PROCESS",
                "configured": True,
                "source": "process_environment",
                "damaged": False,
            },
            {"name": "VBOT_TEST_TOKEN", "configured": True, "source": "data_dir", "damaged": False},
        ],
    }

    removed = await rpc_result(state, "secrets.unset", name="VBOT_TEST_DAMAGED")
    absent = await rpc_result(state, "secrets.unset", name="VBOT_TEST_DAMAGED")
    process = await rpc_result(state, "secrets.unset", name="VBOT_TEST_PROCESS")

    assert removed == {
        "name": "VBOT_TEST_DAMAGED",
        "configured": False,
        "source": None,
        "removed": True,
    }
    assert absent["removed"] is False
    # Unsetting never touches the process environment.
    assert process == {
        "name": "VBOT_TEST_PROCESS",
        "configured": True,
        "source": "process_environment",
        "removed": True,
    }
    assert state.runtime.storage.secrets.entry_names(CREDENTIALS) == ("VBOT_TEST_TOKEN",)
    assert "Data-directory credential saved (name=VBOT_TEST_TOKEN" in caplog.text
    for value in (_SECRET, "damaged-value", "stored"):
        assert value not in caplog.text
        assert value not in json.dumps(listed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "message"),
    [
        ("secrets.set", {"name": "sk-pasted-secret", "value": "x"}, "environment variable name"),
        ("secrets.set", {"name": "1BAD", "value": "x"}, "environment variable name"),
        ("secrets.set", {"name": "A" * 201, "value": "x"}, "environment variable name"),
        ("secrets.unset", {"name": 7}, "environment variable name"),
        ("secrets.set", {"name": "VBOT_TEST_TOKEN", "value": ""}, "non-empty string"),
        ("secrets.set", {"name": "VBOT_TEST_TOKEN", "value": "sk-a\nsk-b"}, "single line"),
        ("secrets.set", {"name": "VBOT_TEST_TOKEN"}, "non-empty string"),
        ("secrets.set", {"name": "VBOT_TEST_TOKEN", "value": "x", "echo": "y"}, "unsupported"),
        ("secrets.list", {"name": "VBOT_TEST_TOKEN"}, "unsupported"),
    ],
)
async def test_invalid_credential_requests_change_nothing_and_never_repeat_their_input(
    tmp_path: Path, method: str, params: dict[str, Any], message: str
) -> None:
    state = make_state(tmp_path, StubAdapter())

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert message in error["message"]
    assert "sk-" not in error["message"]
    assert not state.runtime.storage.secrets.path.exists()
    assert resource_changes(state) == []
