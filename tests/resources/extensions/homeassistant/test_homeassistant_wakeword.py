"""Configured Home Assistant calls, through the shipped Extension's public operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from core.extensions.operations import ExtensionHost, ExtensionOperationError
from core.model_tasks.decision_types import DecisionError, validate_questions
from tests.resources.extensions.homeassistant.homeassistant_test_support import (
    EXTENSION_NAME,
    TOKEN,
    LiveSettings,
    load,
)

COMMAND: dict[str, Any] = {
    "phrase": "Licht an",
    "aliases": ["Wohnzimmerlampe einschalten"],
    "tool": "ha_call_service",
    "arguments": {"domain": "light", "service": "turn_on", "entity_id": "light.wohnzimmer"},
}
MISS = {"matched": False}
FULL_MATCH = {
    "command": {"type": "choice", "choice": "c1"},
    "scope": {"type": "choice", "choice": "nothing"},
}


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def setup(tmp_path: Path):
    settings = LiveSettings(token=TOKEN)
    registry, _ = load(settings)
    judge = AsyncMock()
    host = ExtensionHost(
        data_dir=tmp_path,
        state_dir=tmp_path / "extension-data" / EXTENSION_NAME,
        sample=AsyncMock(),
        resolve_agent=lambda *_: None,
        resolve_tool_agent=lambda *_: None,
        store_attachment=lambda *_: None,
        resolve_credential=settings.resolve_credential,
        set_credential=lambda *_: None,
        decide=judge,
    )
    registry.bind_host(host)
    await registry.fire_startup()
    try:
        yield registry.management(EXTENSION_NAME).invoke, settings, judge
    finally:
        await registry.fire_shutdown()


async def test_saved_command_exact_semantic_and_no_match_routes(setup) -> None:
    invoke, settings, judge = setup

    async def match(text: str):
        candidate = await invoke("match_wakeword", {"text": text})
        if not candidate["matched"]:
            return candidate
        return await invoke("confirm_wakeword", {"text": text, "selection": candidate["selection"]})

    assert await match("Licht an") == MISS
    await invoke("set_wakeword_command", {"previous": None, "command": COMMAND})
    selected = {
        "matched": True,
        "tool": COMMAND["tool"],
        "arguments": COMMAND["arguments"],
        "notify_agent": False,
    }
    assert await match("  LICHT   an! ") == selected
    settings.config["wakeword_use_decision"] = False
    settings.config["wakeword_notify_agent"] = True
    assert await match("wohnzimmerlampe einschalten.") == {**selected, "notify_agent": True}
    assert await match("Licht an?") == MISS
    assert await match("mach bitte das Licht an") == MISS
    judge.assert_not_awaited()

    settings.config["wakeword_use_decision"] = True
    judge.return_value = {"answers": FULL_MATCH}
    assert await match("mach bitte das Licht an") == {**selected, "notify_agent": True}
    context, request = judge.await_args.args
    assert context is None
    assert request["state"] == {
        "transcript": "mach bitte das Licht an",
        "configured_phrases": [[COMMAND["phrase"], *COMMAND["aliases"]]],
    }
    question, coverage = validate_questions(request["questions"])
    assert coverage["id"] == "scope"
    assert set(question["criteria"]) == {"none", "c1"}
    assert question["criteria"]["c1"] == COMMAND["phrase"]
    assert COMMAND["arguments"]["entity_id"] not in json.dumps(request)
    assert await match("Licht an?") == MISS  # Even a Model which selected the on switch.
    query = {
        "phrase": "Licht an?",
        "aliases": [],
        "tool": "ha_get_state",
        "arguments": {"entity_id": COMMAND["arguments"]["entity_id"]},
    }
    await invoke("set_wakeword_command", {"previous": None, "command": query})
    assert (await match("Licht an?"))["tool"] == "ha_get_state"

    for answer in (
        {"type": "choice", "choice": "none"},
        {"type": "refusal"},
        {"type": "choice", "choice": "c3"},
    ):
        judge.return_value = {"answers": {**FULL_MATCH, "command": answer}}
        assert await match("Ist das Licht an?") == MISS
    for scope in ({"type": "choice", "choice": "detail"}, {"type": "refusal"}, {}):
        judge.return_value = {"answers": {**FULL_MATCH, "scope": scope}}
        assert await match("Mach das Licht in der Küche an") == MISS
    judge.side_effect = DecisionError("offline", code="not_configured")
    assert await match("freie Formulierung") == MISS
    judge.reset_mock()
    assert (await match("Licht an"))["matched"] is True
    del settings.credentials["HASS_TOKEN"]
    assert await match("Licht an") == MISS
    judge.assert_not_awaited()


async def test_command_edits_preserve_configuration_and_reject_stale_or_ambiguous_changes(
    setup, tmp_path: Path
) -> None:
    invoke, _, _ = setup
    path = tmp_path / "extension-data" / EXTENSION_NAME / "wakeword-commands.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"format_version": 1, "commands": [{**COMMAND, "future": 4}], "future": 3}),
        encoding="utf-8",
    )
    initial = await invoke("wakeword_commands", {})
    assert initial == {"commands": [COMMAND], "decision_command_limit": 254}
    edited = {**COMMAND, "phrase": "Schreibtischlicht an"}
    assert await invoke("set_wakeword_command", {"previous": COMMAND, "command": edited}) == {
        "commands": [edited],
        "decision_command_limit": 254,
    }
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["future"] == 3 and stored["commands"][0]["future"] == 4
    with pytest.raises(ExtensionOperationError) as exc:
        await invoke("set_wakeword_command", {"previous": COMMAND, "command": None})
    assert exc.value.code == "wakeword_command_conflict"
    duplicate = {**COMMAND, "aliases": ["  SCHREIBTISCHLICHT  an! "]}
    with pytest.raises(ExtensionOperationError) as exc:
        await invoke("set_wakeword_command", {"previous": None, "command": duplicate})
    assert exc.value.code == "wakeword_command_conflict"
    with pytest.raises(ValueError):
        await invoke(
            "set_wakeword_command", {"previous": None, "command": {**COMMAND, "tool": "shell"}}
        )
    with pytest.raises(ValueError):
        await invoke(
            "set_wakeword_command", {"previous": None, "command": {**COMMAND, "arguments": {}}}
        )
    assert await invoke("set_wakeword_command", {"previous": edited, "command": None}) == {
        "commands": [],
        "decision_command_limit": 254,
    }


async def test_inflight_choice_cannot_authorize_a_changed_command(setup) -> None:
    invoke, settings, judge = setup
    await invoke("set_wakeword_command", {"previous": None, "command": COMMAND})
    candidate = await invoke("match_wakeword", {"text": "Licht an"})
    exact = {"text": "Licht an", "selection": candidate["selection"]}
    assert await invoke("confirm_wakeword", {**exact, "text": "Licht aus"}) == MISS
    settings.config.update(wakeword_notify_agent=True, wakeword_use_decision=False)
    assert (await invoke("confirm_wakeword", exact))["notify_agent"]
    settings.config["wakeword_use_decision"] = True
    choosing, resume = asyncio.Event(), asyncio.Event()

    async def decide(*_args: Any) -> dict[str, Any]:
        choosing.set()
        await resume.wait()
        return {"answers": FULL_MATCH}

    judge.side_effect = decide
    pending = asyncio.create_task(invoke("match_wakeword", {"text": "Mach bitte Licht"}))
    await choosing.wait()
    await invoke("set_wakeword_command", {"previous": COMMAND, "command": None})
    resume.set()
    proposed = await pending
    assert await invoke("confirm_wakeword", exact) == MISS
    assert (
        await invoke(
            "confirm_wakeword", {"text": "Mach bitte Licht", "selection": proposed["selection"]}
        )
        == MISS
    )
