"""Secret references in the file edit Tools: values, reference text and its escape."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.secrets import KeyFileProtection, SecretStore
from core.secrets.agent_secrets import AgentSecrets
from tests.core.secrets.secrets_test_support import data_directory
from tests.core.tools.apply_patch_test_support import call, registry, text, update

VALUE = "sk-live-1234567890"


def secret_tools(tmp_path: Path):
    store = SecretStore(
        data_directory(tmp_path),
        protections=(KeyFileProtection(directories=(tmp_path / "keys",)),),
    )
    secrets = AgentSecrets(store, clock=lambda: datetime.now(UTC))
    secrets.create(
        name="api", kind="token", values={"value": VALUE}, actor="user", agents=["agent-test"]
    )
    tools = registry()
    tools.set_secrets(secrets)
    return tools


@pytest.mark.asyncio
async def test_old_text_with_a_reference_matches_the_value_or_the_reference_text(
    tmp_path: Path,
) -> None:
    """A read shows a value and the reference text alike; a copy of either line edits it."""
    tools = secret_tools(tmp_path)
    target = tmp_path / "file.txt"
    target.write_text(f"token = {VALUE}\ndoc: use {{{{secret:API}}}} here\n", encoding="utf-8")

    edited = await call(
        tmp_path,
        {
            "path": "file.txt",
            "edits": [
                {"old_string": "token = {{secret:API}}", "new_string": "token = {{secret:API}} #"},
                {
                    "old_string": "doc: use {{secret:API}} here",
                    "new_string": "doc: put {{secret:API}} here",
                },
            ],
        },
        tools=tools,
        name="edit",
    )

    assert edited["ok"] is True, text(edited)
    assert target.read_text(encoding="utf-8") == (
        f"token = {VALUE} #\ndoc: put {{{{secret:API}}}} here\n"
    )
    assert "{{secret:API}} as text" in text(edited)

    patched = await call(
        tmp_path,
        {"patch": update("-doc: put {{secret:API}} here\n+doc: set {{secret:API}} here")},
        tools=tools,
    )

    assert patched["ok"] is True, text(patched)
    assert target.read_text(encoding="utf-8") == (
        f"token = {VALUE} #\ndoc: set {{{{secret:API}}}} here\n"
    )


@pytest.mark.asyncio
async def test_an_escaped_reference_writes_the_reference_text(tmp_path: Path) -> None:
    tools = secret_tools(tmp_path)

    written = await call(
        tmp_path,
        {
            "path": "readme.md",
            "content": "Set {{!secret:API}} or {{!secret:MISSING.password}}; "
            "{{!!secret:API}} shows the escape.\n",
        },
        tools=tools,
        name="write",
    )
    refused = await call(
        tmp_path,
        {"path": "other.md", "content": "Set {{secret:MISSING}}.\n"},
        tools=tools,
        name="write",
    )

    assert written["ok"] is True, text(written)
    assert (tmp_path / "readme.md").read_text(encoding="utf-8") == (
        "Set {{secret:API}} or {{secret:MISSING.password}}; {{!secret:API}} shows the escape.\n"
    )
    # An unknown name refuses the call and names the escape for the reference text itself.
    assert refused["error"]["code"] == "secret_unavailable"
    assert "{{!secret:MISSING}}" in refused["error"]["message"]
    assert not (tmp_path / "other.md").exists()
