"""Computer use: recovery behavior."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from core.tools.availability import ToolAccess
from resources.extensions.computer_use import observations
from resources.extensions.computer_use.driver import ComputerUseError
from tests.resources.extensions.computer_use_helpers import (
    call,
    capture,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def test_failure_invalidates_capture_and_never_retries_input(computer):
    view = capture(computer)["data"]["view_id"]
    computer[2].fail = "type_text"
    result = call(computer, "type", text="draft", apply=True)
    assert not result["ok"]
    assert result["artifacts"][0]["observation"]["view_id"] != view
    assert call(computer, "type", view_id=view, text="draft")["error"]["code"] == "stale_view"
    assert sum(name == "type_text" for name, _ in computer[2].calls) == 1


def test_post_action_capture_failure_preserves_applied_outcome(computer):
    capture(computer)
    computer[2].fail_capture_after_input = True
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation_error"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("capture_after", [False, True])
@pytest.mark.parametrize("mode", ["vision", "som", "ax"])
def test_missing_observation_returns_executable_recovery_without_input_replay(
    computer, foreground, capture_after, mode
):
    service, context, client, _ = computer
    first = capture(computer, foreground=foreground, resolution="original")["data"]
    client.fail_capture_after_input = capture_after
    options = {"mode": mode}
    if mode != "vision":
        options.update(query="Draft", limit=12)
    result = service.handle(
        context,
        {
            "action": "type",
            "view_id": first["view_id"],
            "text": "test-owned-private-text",
            "capture_after": capture_after,
            **options,
        },
    )
    data = json.loads(json.dumps(result))["data"]
    assert result["ok"] and data["applied"] and client.inputs == 1
    assert data["target"] == {"pid": 1, "window_id": 2}
    assert data["foreground"] is foreground
    recovery = data["recovery"]
    assert recovery["action"] == "capture" and "view_id" not in recovery
    assert recovery["resolution"] == "original"
    assert "test-owned-private-text" not in json.dumps(result)
    stale = service.handle(
        context, {"action": "click", "view_id": first["view_id"], "coordinate": [10, 10]}
    )
    assert stale["error"]["code"] == "stale_view" and client.inputs == 1
    client.fail_capture_after_input = False
    observed = service.handle(context, recovery)
    assert observed["ok"] and observed["data"]["mode"] == mode
    assert observed["data"]["foreground"] is foreground and client.inputs == 1
    if mode != "vision":
        assert recovery["query"] == "Draft" and recovery["limit"] == 12


@pytest.mark.parametrize("target", [{}, {"monitor": 2}, {"pid": 1, "window_id": 2}])
def test_failed_read_keeps_target_without_using_stale_pixels(computer, target):
    service, context, client, _ = computer
    first = service.handle(context, {"action": "capture", **target})["data"]
    client.fail = "resolve_window" if "window_id" in target else "get_desktop_state"
    failed = service.handle(context, {"action": "capture", "view_id": first["view_id"]})
    assert not failed["ok"]
    recovery = failed["artifacts"][0]
    assert recovery["target"] == target and "observation" not in recovery
    stale = service.handle(
        context, {"action": "wait", "view_id": first["view_id"], "duration_ms": 0}
    )
    assert stale["error"]["code"] == "stale_view"
    assert stale["artifacts"][0]["recovery"] == recovery["recovery"]
    client.fail = None
    observed = service.handle(context, recovery["recovery"])
    assert observed["ok"] and observed["data"]["target"] == target
    assert client.inputs == 0


@pytest.mark.parametrize(
    "code,action",
    [
        ("stale_window", "windows"),
        ("target_not_foreground", "capture"),
        ("focus_refused", "capture"),
        ("window_not_visible", "capture"),
    ],
)
def test_recovery_routes_closed_or_inactive_window_to_read_only_discovery(computer, code, action):
    service, context, client, _ = computer
    first = capture(computer, foreground=True)["data"]

    def fail_capture(name):
        if name == "capture_pixels":
            raise ComputerUseError("test-owned observation error", code)

    client.hook = fail_capture
    result = service.handle(
        context, {"action": "key", "view_id": first["view_id"], "shortcut": "enter"}
    )
    assert result["ok"] and result["data"]["applied"] and client.inputs == 1
    data = result["data"]
    assert data["observation_error"]["code"] == code
    assert data["target"] == {"pid": 1, "window_id": 2} and data["foreground"] is True
    assert data["recovery"] == {"action": action}
    client.hook = None
    # Discovery in the fixture must remain read-only too.
    if action == "windows":
        original = client.call
        client.call = lambda name, args: (
            {"windows": []} if name == "list_windows" else original(name, args)
        )
    assert service.handle(context, data["recovery"])["ok"] and client.inputs == 1


def test_partial_sequence_and_failed_observation_keep_completed_steps(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]
    client.fail_capture_after_input = True
    client.fail = "press_key"
    result = service.handle(
        context,
        {
            "action": "sequence",
            "view_id": first["view_id"],
            "steps": [
                {"action": "click", "coordinate": [10, 10]},
                {"action": "key", "shortcut": "enter"},
                {"action": "type", "text": "must not be sent"},
            ],
        },
    )
    data = result["data"]
    assert result["ok"] and data["partial"] and data["completed_steps"] == 1
    assert data["stopped_step"] == 2 and data["total_steps"] == 3 and client.inputs == 1
    client.fail_capture_after_input = False
    assert service.handle(context, data["recovery"])["ok"] and client.inputs == 1


def test_zero_completion_failure_carries_recovery_in_artifacts(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]

    def fail(name):
        if name in {"click", "capture_pixels"}:
            raise ComputerUseError("test-owned failure")

    client.hook = fail
    result = service.handle(
        context, {"action": "click", "view_id": first["view_id"], "coordinate": [10, 10]}
    )
    assert not result["ok"] and client.inputs == 0
    state = result["artifacts"][0]
    assert state["applied"] is False and "recovery" in state
    client.hook = None
    assert service.handle(context, state["recovery"])["ok"] and client.inputs == 0


def test_verified_closed_window_preserves_verification_and_discovery(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]

    def fail(name):
        if name == "resolve_window":
            raise ComputerUseError("test-owned closed window", "stale_window")

    client.hook = fail
    result = service.handle(
        context,
        {
            "action": "verify",
            "view_id": first["view_id"],
            "expect": [{"window": {"exists": False}}],
        },
    )
    assert result["ok"] and result["data"]["verification"]["verified"]
    assert result["data"]["recovery"] == {"action": "windows"} and client.inputs == 0


def test_retired_reference_recovery_preserves_explicit_delivery_and_owner(computer):
    service, context, client, _ = computer
    first = capture(computer, foreground=False)["data"]
    service.handle(
        context,
        {"action": "type", "view_id": first["view_id"], "text": "draft", "capture_after": False},
    )
    args = {
        "action": "capture",
        "view_id": first["view_id"],
        "foreground": True,
        "resolution": "original",
    }
    result = service.handle(context, args)
    assert not result["ok"] and result["artifacts"][0]["recovery"]["foreground"] is True
    assert result["artifacts"][0]["recovery"]["resolution"] == "original"
    other = replace(context, run_id="other")
    assert not service.handle(other, args)["artifacts"]
    assert not service.handle(context, {**args, "pid": 1, "window_id": 3})["artifacts"]
    assert client.inputs == 1


def test_failed_observation_does_not_suggest_recovery_after_revocation(computer):
    service, context, client, agent = computer
    first = capture(computer)["data"]

    def revoke(name):
        if name == "type_text":
            agent.tool_access = ToolAccess(mode="none")

    client.hook = revoke
    result = service.handle(
        context, {"action": "type", "view_id": first["view_id"], "text": "draft"}
    )
    assert result["data"]["applied"] and "recovery" not in result["data"]
    assert client.inputs == 1


def test_unexpected_capture_error_keeps_read_only_recovery(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]

    def fail(name):
        if name == "resolve_window":
            raise RuntimeError("test-owned unexpected failure")

    client.hook = fail
    result = service.handle(context, {"action": "capture", "view_id": first["view_id"]})
    assert result["error"]["code"] == "computer_use_failed"
    recovery = result["artifacts"][0]["recovery"]
    client.hook = None
    assert service.handle(context, recovery)["ok"] and client.inputs == 0


def test_retired_monitor_reference_does_not_redirect_recovery(computer):
    service, context, client, _ = computer
    first = service.handle(context, {"action": "capture", "monitor": 2})["data"]
    service.handle(
        context,
        {
            "action": "key",
            "view_id": first["view_id"],
            "shortcut": "tab",
            "capture_after": False,
        },
    )
    result = service.handle(
        context, {"action": "capture", "view_id": first["view_id"], "monitor": 3}
    )
    assert result["error"]["code"] == "stale_view" and not result["artifacts"]
    assert client.inputs == 1


def test_observation_disk_failure_keeps_dispatched_outcome(computer, monkeypatch):
    capture(computer)

    def fail(*args, **kwargs):
        raise OSError("test-owned disk failure")

    monkeypatch.setattr(observations, "capture", fail)
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation_error"]["code"] == "observation_failed"


def test_transport_loss_during_recapture_retires_all_sessions(computer):
    service, context, client, _ = computer
    capture(computer)
    service._driver = client
    original_call = client.call

    def response(name, args):
        if name in {"get_window_state", "capture_pixels"}:
            client.broken = True
            raise ComputerUseError("test-owned transport failure")
        return original_call(name, args)

    client.call = response
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert not service._sessions


def test_failed_session_cleanup_is_retained_for_shutdown_retry(computer):
    service, context, client, _ = computer
    capture(computer)
    client.fail = "end_session"
    service.run_end(context)
    assert len(service._sessions) == 1
    client.fail = None
    service.close()
    assert not service._sessions


def test_cleanup_after_stop_does_not_start_a_replacement_worker(computer):
    service, _, client, _ = computer
    capture(computer)
    service._driver = client
    client.broken = True
    previous = list(client.calls)
    service._close_sessions()
    assert not service._sessions
    assert client.calls == previous


def test_verified_window_disappearance_survives_capture_failure(computer):
    computer[2].fail = "capture_pixels"
    result = call(computer, "verify", expect=[{"window": {"exists": False}}], timeout_ms=0)
    assert result["ok"] and result["data"]["verification"]["verified"]
    assert result["data"]["observation_error"]


@pytest.mark.parametrize("effect", ["suspected_noop", "partial"])
def test_uncertain_effect_stops_later_steps_and_preserves_verification(computer, effect):
    capture(computer)
    client = computer[2]
    original = client.call

    def respond(name, args):
        payload = original(name, args)
        if name == "click":
            payload.update(effect=effect, verified=False, escalation={"rung": "px"})
        return payload

    client.call = respond
    result = call(
        computer,
        "sequence",
        steps=[
            {"action": "click", "element": "1"},
            {"action": "type", "text": "must not type"},
        ],
    )
    data = result["data"]
    assert data["partial"] and data["completed_steps"] == 1 and data["stopped_step"] == 1
    assert data["step_results"][0]["verified"] is False
    assert data["step_results"][0]["escalation"] == {"rung": "px"}
    assert data["error"]["code"] == "effect_uncertain" and client.inputs == 1


@pytest.mark.parametrize("sequence", [False, True])
def test_blocked_input_returns_a_usable_dialog_observation_without_extra_capture(
    computer, sequence
):
    service, context, client, _ = computer
    capture(computer)
    original = client.call
    blocked = [True]

    def respond(name, args):
        if name == "type_text" and args["window_id"] == 2:
            raise ComputerUseError("test-owned blocked window", "target_blocked")
        if name == "resolve_window" and blocked[0]:
            return {"pid": 1, "window_id": 3}
        return original(name, args)

    client.call = respond
    result = (
        call(computer, "sequence", steps=[{"action": "type", "text": "draft"}])
        if sequence
        else call(computer, "type", text="draft")
    )
    assert not result["ok"] and result["error"]["code"] == "target_blocked"
    recovery = result["artifacts"][0]
    assert recovery["applied"] is False and client.inputs == 0
    observed = recovery["observation"]
    assert observed["target"] == {"pid": 1, "window_id": 3}
    assert observed["requested_target"] == {"pid": 1, "window_id": 2}
    blocked[0] = False
    assert service.handle(
        context,
        {
            "action": "click",
            "view_id": observed["view_id"],
            "coordinate": [10, 10],
        },
    )["ok"]
    assert client.inputs == 1
