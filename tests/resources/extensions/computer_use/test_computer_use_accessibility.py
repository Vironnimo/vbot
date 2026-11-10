"""Window and element targeting through production dispatch, locally and remotely."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest

from tests.resources.extensions.computer_use.computer_use_test_support import (
    NOTEPAD,
    Harness,
    images,
    model_text,
)
from tests.resources.extensions.computer_use.test_computer_use_remote import (
    NativeLink,
    remote_context,
)

pytestmark = pytest.mark.asyncio


def field() -> dict[str, Any]:
    return {
        "runtime_id": [1, 10],
        "generation": "native-test-observation",
        "depth": 1,
        "name": "Subject",
        "role": "edit",
        "control_type": 50004,
        "automation_id": "subject",
        "password": False,
        "focused": True,
        "enabled": True,
        "offscreen": False,
        "bounds": [120, 140, 320, 170],
        "actions": ["focus", "set_value"],
        "value": "old",
        "selected_text": "old",
    }


def identifier(result: dict[str, Any], prefix: str) -> str:
    match = re.search(rf"\b{prefix}_[a-z0-9]+\b", model_text(result))
    assert match is not None, result
    return match.group()


@pytest.mark.parametrize("remote", [False, True])
async def test_inspection_and_element_actions_keep_their_computer_and_session(
    computer: Harness,
    remote: bool,
) -> None:
    link = NativeLink(computer.computers) if remote else None
    if link:
        await link.connect()
    target = link.target if link else computer.target
    target.accessibility[1] = [field()]
    context = remote_context(computer) if remote else computer.context_for("computer")

    async def call(**arguments):
        context.result_media.clear()
        return await computer.call("computer", arguments, context)

    try:
        shot = await call(action="screenshot")
        assert shot["ok"] and images(context)
        assert target.inspections == []  # Accessibility is never an automatic screenshot cost.
        win = identifier(shot, "win")
        state = await call(action="inspect", window_id=win)
        assert state["ok"] and not images(context)
        assert target.inspections == [1] and "Subject" in model_text(state)
        element = identifier(state, "elm")
        for text in ("new subject", ""):
            result = await call(action="set_value", element_id=element, text=text)
            assert result["ok"] and images(context)
            assert target.accessibility[1][0]["value"] == text
        empty = await call(action="inspect", window_id=win)
        assert '"value": ""' in model_text(empty)
        assert (await call(action="focus", element_id=element))["ok"]
        assert (await call(action="left_click", element_id=element))["ok"]
        assert target.inputs[-1] == ("click", 220, 155, "left", 1, [])
        before = list(target.inputs)
        unsupported = await call(action="invoke", element_id=element)
        assert unsupported["error"]["code"] == "element_not_supported"
        wrong_session = await computer.call(
            "computer",
            {"action": "set_value", "element_id": element, "text": "wrong"},
            replace(context, session_id="another-session", result_media=[]),
        )
        assert wrong_session["error"]["code"] == "element_unavailable"
        if remote:
            wrong_computer = await computer.computer(action="focus", element_id=element)
            assert wrong_computer["error"]["code"] == "element_unavailable"
        target.accessibility[1][0]["name"] = "Different field"
        stale = await call(action="set_value", element_id=element, text="wrong")
        assert stale["error"]["code"] == "element_unavailable"
        assert target.inputs == before
    finally:
        if link:
            await link.close(computer)


async def test_exact_window_target_distinguishes_two_windows_of_one_app(computer: Harness) -> None:
    first = computer.target.windows_[0]
    second = replace(first, handle=12, title="Other document")
    computer.target.windows_.insert(1, second)
    shot = await computer.computer(action="screenshot")
    first_id = identifier(shot, "win")
    listing = await computer.call("computer_apps", {"action": "windows", "query": "Other document"})
    second_id = identifier(listing, "win")
    assert first_id != second_id
    computer.target.activate(second)
    for action in (
        {"action": "type", "text": "wrong"},
        {"action": "left_click", "coordinate": [30, 30]},
    ):
        refused = await computer.computer(**action)
        assert refused["error"]["code"] in {"focus_changed", "screen_changed"}
        assert "another window" in model_text(refused)
    assert computer.target.inputs == []
    result = await computer.computer(action="type", text="correct", window_id=first_id)
    assert result["ok"] and computer.target.foreground().handle == first.handle
    result = await computer.call("computer_apps", {"action": "open", "window_id": second_id})
    assert result["ok"] and computer.target.foreground().handle == second.handle
    computer.target.windows_[0] = replace(second, process_id=500)
    stale = await computer.computer(action="type", text="wrong", window_id=second_id)
    assert stale["error"]["code"] == "window_unavailable"
    assert computer.target.inputs == [("type", "correct")]


async def test_batch_validates_all_explicit_targets_before_input(computer: Harness) -> None:
    shot = await computer.computer(action="screenshot")
    first_id = identifier(shot, "win")
    other = await computer.call("computer_apps", {"action": "windows", "query": "Chrome"})
    other_id = identifier(other, "win")
    for last in (
        {"action": "focus", "element_id": "elm_unknown"},
        {"action": "type", "window_id": "win_unknown", "text": "wrong"},
        {"action": "left_click", "window_id": other_id, "coordinate": [20, 20]},
    ):
        result = await computer.call(
            "computer_batch",
            {
                "actions": [
                    {"action": "type", "window_id": first_id, "text": "must not run"},
                    last,
                ]
            },
        )
        assert not result["ok"]
        assert computer.target.inputs == []


async def test_inspection_obeys_app_access_and_never_exposes_unapproved_titles(
    computer: Harness,
) -> None:
    target = computer.target
    target.accessibility[1] = [field()]
    shot = await computer.computer(action="screenshot")
    win = identifier(shot, "win")
    computer.ask_per_app()
    listing = await computer.call("computer_apps", {"action": "windows"})
    assert "Notepad window" not in model_text(listing)
    refused = await computer.computer(action="inspect", window_id=win)
    assert refused["error"]["code"] == "access_required"
    assert target.inspections == []
    await computer.grant(NOTEPAD.name)
    assert (await computer.computer(action="inspect", window_id=win))["ok"]


async def test_element_target_conflicts_are_refused_before_effects(computer: Harness) -> None:
    computer.target.accessibility[1] = [field()]
    state = await computer.computer(action="inspect")
    element = identifier(state, "elm")
    listing = await computer.call("computer_apps", {"action": "windows", "query": "Chrome"})
    other = identifier(listing, "win")
    for arguments in (
        {"action": "set_value", "element_id": element, "window_id": other, "text": "wrong"},
        {"action": "left_click", "element_id": element, "coordinate": [2, 3]},
        {"action": "type", "element_id": element, "text": "wrong"},
    ):
        result = await computer.computer(**arguments)
        assert not result["ok"]
    ignored_target = await computer.call("computer_apps", {"action": "list", "windowId": other})
    assert not ignored_target["ok"]
    assert computer.target.inputs == []


async def test_accessibility_batch_preserves_completed_effects_on_later_failure(
    computer: Harness,
) -> None:
    computer.target.accessibility[1] = [field()]
    inspected = await computer.computer(action="inspect")
    element = identifier(inspected, "elm")

    def change_after_input(_):
        computer.target.accessibility[1][0]["name"] = "Changed after first action"

    computer.target.on_input = change_after_input
    context = computer.context_for("computer_batch")
    result = await computer.call(
        "computer_batch",
        {
            "actions": [
                {"action": "set_value", "element_id": element, "text": "applied"},
                {"action": "focus", "element_id": element},
            ]
        },
        context,
    )
    assert result["error"]["code"] == "element_unavailable"
    assert computer.target.accessibility[1][0]["value"] == "applied"
    assert len(computer.target.inputs) == 1 and images(context)
