"""Computer use: elements behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.tools.availability import ToolAccess
from tests.resources.extensions.computer_use_helpers import (
    call,
    capture,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)
from tests.resources.extensions.computer_use_helpers import (
    lifecycle_connection as lifecycle_connection,
)


def test_sequence_infers_target_from_first_step_view_before_element_validation(computer):
    service, context, client, _ = computer
    data = capture(computer)["data"]
    result = service.handle(
        context,
        {
            "action": "sequence",
            "steps": [
                {"action": "click", "element": data["elements"][0]["element"]},
                {
                    "action": "drag",
                    "view_id": data["view_id"],
                    "coordinate": [20, 30],
                    "to_coordinate": [40, 50],
                },
            ],
        },
    )
    assert result["ok"] and client.inputs == 2


def test_foreign_view_and_mixed_target_sequence_fail_before_input(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]
    second = capture(computer, window_id=3)["data"]
    mismatch = call(computer, "click", window_id=3, view_id=first["view_id"], coordinate=[1, 1])
    assert mismatch["error"]["code"] == "invalid_arguments"
    result = service.handle(
        context,
        {
            "action": "sequence",
            "view_id": first["view_id"],
            "steps": [
                {"action": "click", "coordinate": [1, 1]},
                {"action": "click", "view_id": second["view_id"], "coordinate": [1, 1]},
            ],
        },
    )
    assert result["error"]["code"] == "invalid_arguments" and client.inputs == 0


def test_bad_coordinates_do_not_destroy_the_valid_observation(computer):
    data = capture(computer)["data"]
    bad = call(computer, "click", view_id=data["view_id"], coordinate=[9999, 10])
    assert bad["error"]["code"] == "invalid_coordinates"
    assert call(computer, "click", view_id=data["view_id"], coordinate=[10, 10])["ok"]
    assert computer[2].inputs == 1


def test_next_elements_can_be_requested_with_input_and_keep_control_state(computer):
    capture(computer)
    result = call(computer, "key", shortcut="tab", mode="som", query="Draft", limit=10)
    assert result["ok"]
    element = result["data"]["observation"]["elements"][0]
    assert element["role"] == "Edit" and element["label"] == "Draft"
    assert "element_token" not in element and "element_index" not in element
    assert call(computer, "set_value", element=element["element"], text="test-owned")["ok"]


def test_query_requests_elements_and_partial_target_stays_invalid(computer):
    service, context, client, _ = computer
    data = service.handle(
        context,
        {
            "action": "capture",
            "pid": 1,
            "window_id": 2,
            "query": "Draft",
        },
    )["data"]
    assert data["mode"] == "som" and data["elements"]
    result = service.handle(
        context,
        {
            "action": "zoom",
            "pid": 1,
            "view_id": data["view_id"],
            "coordinate": [1, 1],
            "to_coordinate": [50, 50],
        },
    )
    assert result["error"]["code"] == "invalid_arguments" and client.inputs == 0


def test_stale_sequence_root_view_cannot_fall_back_to_the_desktop(computer):
    service, context, client, _ = computer
    service.handle(context, {"action": "capture"})
    result = service.handle(
        context,
        {
            "action": "sequence",
            "view_id": "view_missing",
            "steps": [{"action": "type", "text": "must not type"}],
        },
    )
    assert result["error"]["code"] == "stale_view" and client.inputs == 0


@pytest.mark.parametrize(
    "action,fields",
    [
        ("capture", {"mode": "som"}),
        ("wait", {"duration_ms": 0}),
        ("verify", {"expect": [{"window": {"exists": True}}]}),
    ],
)
def test_observation_continuation_inherits_view_target_and_delivery(computer, action, fields):
    service, context, client, _ = computer
    first = capture(computer, foreground=True)["data"]
    result = service.handle(context, {"action": action, "view_id": first["view_id"], **fields})
    assert result["ok"]
    current = result["data"].get("observation", result["data"])
    assert current["target"] == first["target"] and current["foreground"] is True
    assert current["view_id"] != first["view_id"] and client.inputs == 0
    stale = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": first["view_id"],
            "coordinate": [0, 0],
            "to_coordinate": [20, 20],
        },
    )
    assert stale["error"]["code"] == "stale_view"
    assert stale["artifacts"][0]["observation"]["view_id"] == current["view_id"]


def test_target_delivery_persists_across_capture_and_skipped_observation(computer):
    first = capture(computer, foreground=True)["data"]
    assert call(computer, "key", shortcut="tab", view_id=first["view_id"], capture_after=False)[
        "ok"
    ]
    assert capture(computer)["data"]["foreground"] is True
    assert capture(computer, window_id=3)["data"]["foreground"] is False
    assert capture(computer, foreground=False)["data"]["foreground"] is False
    assert capture(computer)["data"]["foreground"] is False
    service, context, _, _ = computer
    other = replace(context, run_id="another-run")
    assert (
        service.handle(other, {"action": "capture", "pid": 1, "window_id": 2})["data"]["foreground"]
        is False
    )


@pytest.mark.parametrize("sequence", [False, True])
def test_element_ref_continues_target_and_delivery_after_zoom(computer, sequence):
    service, context, client, _ = computer
    first = capture(computer, foreground=True)["data"]
    assert service.handle(
        context,
        {
            "action": "zoom",
            "view_id": first["view_id"],
            "coordinate": [0, 0],
            "to_coordinate": [30, 30],
        },
    )["ok"]
    element = first["elements"][0]["element"]
    args = (
        {"action": "sequence", "steps": [{"action": "click", "element": element}]}
        if sequence
        else {"action": "click", "element": element}
    )
    result = service.handle(context, args)
    assert result["ok"] and result["data"]["observation"]["foreground"] is True
    sent = next(args for name, args in client.calls if name == "click")
    assert sent["pid"] == 1 and sent["window_id"] == 2 and sent["delivery_mode"] == "foreground"
    assert client.inputs == 1


def test_element_errors_distinguish_stale_from_invented_without_recapture(computer):
    service, context, client, _ = computer
    old = capture(computer)["data"]
    current = capture(computer)["data"]
    before = len(client.calls)
    stale = service.handle(context, {"action": "click", "element": old["elements"][0]["element"]})
    assert stale["error"]["code"] == "stale_element"
    assert stale["artifacts"][0]["observation"] == {
        key: value for key, value in current.items() if key != "action"
    }
    invented = call(
        computer, "click", element=current["elements"][0]["element"].split(":")[0] + ":999"
    )
    assert invented["error"]["code"] == "unknown_element"
    assert invented["artifacts"][0]["observation"] == stale["artifacts"][0]["observation"]
    assert len(client.calls) == before and client.inputs == 0
    assert service.handle(
        context, {"action": "click", "element": current["elements"][0]["element"]}
    )["ok"]


@pytest.mark.parametrize(
    "identity",
    [{"run_id": "other"}, {"agent_id": "other"}, {"session_id": "other"}, {"project_id": "other"}],
)
def test_element_refs_never_infer_another_owners_target(computer, identity):
    service, context, client, _ = computer
    element = capture(computer)["data"]["elements"][0]["element"]
    other = replace(context, **identity)
    result = service.handle(other, {"action": "click", "element": element})
    assert not result["ok"] and not result["artifacts"] and client.inputs == 0


def test_reference_recovery_does_not_expose_state_after_revocation(computer):
    service, context, _, agent = computer
    old = capture(computer)["data"]
    capture(computer)
    agent.tool_access = ToolAccess(mode="none")
    result = service.handle(context, {"action": "capture", "view_id": old["view_id"]})
    assert not result["ok"] and not result["artifacts"]


def test_literal_query_is_forwarded_without_regex_or_or_expansion(computer):
    client = computer[2]
    assert capture(computer, query="Color 1|Color 2")["ok"]
    request = next(args for name, args in reversed(client.calls) if name == "get_window_state")
    assert request["query"] == "Color 1|Color 2"


@pytest.mark.parametrize("field", ["pid", "window_id"])
@pytest.mark.parametrize("value", [[], {}, True, 1.0, None])
def test_element_resolution_rejects_malformed_target_before_lookup(computer, field, value):
    service, context, client, _ = computer
    current = capture(computer)["data"]
    before = len(client.calls)
    result = service.handle(
        context,
        {
            "action": "click",
            "pid": 1,
            "window_id": 2,
            "element": current["elements"][0]["element"],
            field: value,
        },
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert len(client.calls) == before


def test_numeric_element_can_use_an_owned_window_view(computer):
    service, context, client, _ = computer
    current = capture(computer, foreground=True)["data"]
    result = service.handle(
        context, {"action": "click", "view_id": current["view_id"], "element": "1"}
    )
    assert result["ok"] and client.inputs == 1
    assert result["data"]["observation"]["foreground"] is True


def test_ambiguous_element_ref_requires_an_explicit_window(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]
    # Model a Driver that reuses a token in another window's snapshot.
    client.snapshots = 0
    second = capture(computer, window_id=3)["data"]
    element = first["elements"][0]["element"]
    assert element == second["elements"][0]["element"]
    before = len(client.calls)
    ambiguous = service.handle(context, {"action": "click", "element": element})
    assert ambiguous["error"]["code"] == "invalid_arguments"
    assert len(client.calls) == before and client.inputs == 0
    chosen = service.handle(
        context, {"action": "click", "element": element, "view_id": second["view_id"]}
    )
    assert chosen["ok"] and client.inputs == 1
    sent = next(args for name, args in client.calls if name == "click")
    assert sent["window_id"] == 3
