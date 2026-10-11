"""Effort updates: a thinking effort change at its place in the request history.

A Model whose wire switches the effort mid-conversation keeps the effort its
history started from as the request effort and gets each change where it
happened; every other route asks for the effort now in effect. The planning is
deterministic, so every later request of a conversation renders the same prefix.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
import respx

from core.providers.effort_updates import (
    EFFORT_BASE_KWARG,
    effort_update_message,
    plan_effort_updates,
)

from . import anthropic_test_support as anthropic
from . import wire_paths_test_support as paths

_LEVELS = {"low": "low", "medium": "medium", "high": "high"}


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


@pytest.mark.parametrize(
    ("base", "history", "request_effort", "planned"),
    [
        pytest.param(
            "low",
            [_user("a"), effort_update_message("high"), _user("b")],
            "low",
            [_user("a"), effort_update_message("high"), _user("b")],
            id="update-in-place",
        ),
        pytest.param(
            "low",
            [_user("a"), effort_update_message("low"), _user("b")],
            "low",
            [_user("a"), _user("b")],
            id="no-change-dropped",
        ),
        pytest.param(
            "low",
            [_user("a"), effort_update_message("medium"), effort_update_message("high")],
            "low",
            [_user("a"), effort_update_message("high")],
            id="adjacent-keeps-the-second",
        ),
        pytest.param(
            "",
            [_user("a"), effort_update_message("high"), _user("b")],
            "high",
            [_user("a"), _user("b")],
            id="from-unexpressed-starts-later",
        ),
        pytest.param(
            "low",
            [
                _user("a"),
                effort_update_message("none"),
                _user("b"),
                effort_update_message("medium"),
                _user("c"),
                effort_update_message("high"),
            ],
            "medium",
            [_user("a"), _user("b"), _user("c"), effort_update_message("high")],
            id="through-unexpressed-starts-after-it",
        ),
    ],
)
def test_a_plan_keeps_each_expressed_update_and_starts_after_the_last_unexpressed(
    base: str,
    history: list[dict[str, Any]],
    request_effort: str,
    planned: list[dict[str, Any]],
) -> None:
    plan = plan_effort_updates(history, base, _LEVELS.get)

    assert (plan.request_effort, plan.messages) == (request_effort, planned)


_HISTORY = [
    {"role": "user", "content": "One"},
    {"role": "assistant", "content": "Done."},
    effort_update_message("high"),
    {"role": "user", "content": "Two"},
]


def _wire_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The conversation as the wire carries it, without prompt-cache markers."""

    messages = (
        anthropic.strip_cache_control(payload)["messages"]
        if "messages" in payload
        else payload["input"]
    )
    return cast(list[dict[str, Any]], messages)


@pytest.mark.parametrize(
    ("path", "update", "request_effort", "beta"),
    [
        pytest.param(
            replace(paths.MESSAGES, model_id="claude-opus-5-5"),
            {"role": "system", "content": [], "output_config": {"effort": "high"}},
            ("output_config", "effort"),
            "mid-conversation-output-config-2026-07-01",
            id="messages",
        ),
        pytest.param(
            paths.PLATFORM,
            {"type": "configuration_update", "reasoning": {"effort": "high"}},
            ("reasoning", "effort"),
            None,
            id="responses",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_model_that_takes_updates_keeps_the_base_effort_and_switches_in_place(
    path: paths.RequestPath,
    update: dict[str, Any],
    request_effort: tuple[str, str],
    beta: str | None,
) -> None:
    adapter = path.adapter()
    assert adapter.effort_updates(path.model_id)
    with respx.mock:
        route = respx.post(path.url).mock(return_value=path.success())
        await paths.request(
            adapter, path, {"thinking_effort": "high", EFFORT_BASE_KWARG: "low"}, _HISTORY
        )
    request = route.calls.last.request
    payload = json.loads(request.content)

    field, key = request_effort
    assert payload[field][key] == "low"
    assert update in _wire_messages(payload)
    assert _wire_messages(payload).index(update) == len(_wire_messages(payload)) - 2
    if beta is not None:
        assert beta in request.headers["anthropic-beta"].split(",")


@pytest.mark.asyncio
async def test_a_model_without_updates_asks_for_the_effort_now_in_effect() -> None:
    adapter = anthropic.make_adapter()
    assert not adapter.effort_updates(anthropic.MODEL_ID)

    request = await anthropic.send_request(
        adapter, _HISTORY, thinking_effort="high", **{EFFORT_BASE_KWARG: "low"}
    )
    payload = json.loads(request.content)

    assert payload["output_config"] == {"effort": "high"}
    assert all("output_config" not in message for message in payload["messages"])
    assert "anthropic-beta" not in request.headers


@pytest.mark.parametrize(
    ("path", "rejection"),
    [
        pytest.param(
            replace(paths.MESSAGES, model_id="claude-opus-5-5"),
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "output_config.effort requires a model that supports "
                    "per-turn effort; this model does not",
                }
            },
            id="messages",
        ),
        pytest.param(
            paths.PLATFORM,
            {"error": {"message": "Invalid input item type: 'configuration_update'"}},
            id="responses",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_rejected_update_turns_updates_off_for_the_model(
    path: paths.RequestPath, rejection: dict[str, Any]
) -> None:
    adapter = path.adapter()
    with respx.mock:
        route = respx.post(path.url).mock(
            side_effect=[httpx.Response(400, json=rejection), path.success(), path.success()]
        )
        for _ in range(2):
            await paths.request(
                adapter, path, {"thinking_effort": "high", EFFORT_BASE_KWARG: "low"}, _HISTORY
            )

    sent = [json.loads(call.request.content) for call in route.calls]
    carried = [
        any(
            "output_config" in item or item.get("type") == "configuration_update"
            for item in _wire_messages(payload)
        )
        for payload in sent
    ]
    assert carried == [True, False, False]
    assert not adapter.effort_updates(path.model_id)
