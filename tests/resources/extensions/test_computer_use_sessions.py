"""Computer use: sessions behavior."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use.driver import ComputerUseError, unpack
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


def test_owned_session_cleanup_and_retirement(computer):
    service, context, client, _ = computer
    capture(computer)
    service.handle(replace(context, run_id="r2"), {"action": "apps"})
    owned = {session.name for session in service._sessions.values()}
    service.run_end(SimpleNamespace(run_id="r"))
    assert len(service._sessions) == 1
    service.close()
    assert not service._sessions
    assert {args["session"] for name, args in client.calls if name == "end_session"} == owned
    count = len(client.calls)
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert len(client.calls) == count


@pytest.mark.parametrize("finish", ["run_end", "cancel_between_tools", "close"])
def test_next_run_gets_fresh_connection_after_last_session_ends(
    computer, lifecycle_connection, finish
):
    service, context, _, _ = computer
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    assert service.handle(context, {"action": "apps"})["ok"]
    previous = lifecycle_connection[0]
    # Simulate Cua's five-minute idle expiry without a wait or desktop access.
    previous.ended.add("implicit")
    if finish == "close":
        assert service.handle(context, {"action": "close"})["ok"]
    else:
        if finish == "cancel_between_tools":
            callbacks[0]()
        service.run_end(context)
    assert previous.closed and service._driver is None and not service._sessions

    following = replace(context, agent_id="b", session_id="s2", run_id="r2")
    assert service.handle(following, {"action": "apps"})["ok"]
    assert service.handle(following, {"action": "windows"})["ok"]
    assert len(lifecycle_connection) == 2


def test_implicit_session_expiry_and_run_cleanup_preserve_other_run(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    other = replace(context, agent_id="b", session_id="s2", run_id="r2")
    assert service.handle(other, {"action": "apps"})["ok"]
    service.run_end(context)
    assert not connection.closed and len(service._sessions) == 1

    connection.ended.add("implicit")
    before = len(connection.calls)
    assert service.handle(other, {"action": "windows"})["ok"]
    assert connection.calls[before:] == [("start_session", {}), ("list_windows", {})]
    service.run_end(context)  # A late completion must not close the surviving Run.
    assert not connection.closed and len(lifecycle_connection) == 1
    service.run_end(other)
    assert connection.closed and service._driver is None


def test_failed_implicit_start_prevents_dispatch(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    connection.fail_start = True
    before = len(connection.calls)
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert connection.calls[before:] == [("start_session", {})]
    connection.fail_start = False
    assert service.handle(context, {"action": "windows"})["ok"]
    assert len(lifecycle_connection) == 1


def test_failed_session_start_is_not_cached(computer):
    service, context, client, _ = computer
    client.fail = "start_session"
    assert not service.handle(context, {"action": "apps"})["ok"]
    assert not service._sessions
    client.fail = None
    assert service.handle(context, {"action": "apps"})["ok"]
    assert [name for name, _ in client.calls].count("start_session") == 2


def test_broken_worker_replaces_cached_session_before_dispatch(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    previous = next(iter(service._sessions.values())).name
    service._driver.broken = True
    assert service.handle(context, {"action": "windows"})["ok"]
    current = next(iter(service._sessions.values())).name
    assert current != previous
    assert lifecycle_connection[0].closed and len(lifecycle_connection) == 2
    assert ("start_session", {"session": current}) in lifecycle_connection[1].calls


def test_cleanup_transport_loss_does_not_connect_to_close_remaining_sessions(
    computer, lifecycle_connection
):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    other = replace(context, session_id="s2", run_id="r2")
    assert service.handle(other, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    connection.fail_transport_on = "end_session"
    service._close_sessions()
    assert connection.closed and service._driver is None and not service._sessions
    assert len(lifecycle_connection) == 1


def test_failed_capture_retires_old_view(computer):
    capture(computer)
    computer[2].fail = "get_window_state"
    assert not capture(computer)["ok"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


def test_session_expiry_is_actionable_without_exposing_a_driver_only_action():

    for result in [
        {"isError": True, "structuredContent": {"code": "session_ended"}},
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        "this session has ended; call start_session explicitly to reuse its label"
                    ),
                }
            ],
        },
    ]:
        with pytest.raises(ComputerUseError) as caught:
            unpack(result)
        assert caught.value.code == "computer_session_expired"
        assert "start_session" not in str(caught.value)


@pytest.mark.parametrize("action", ["get_window_state", "click"])
def test_named_session_expiry_is_recovered_without_replaying_input(
    computer, lifecycle_connection, action
):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    name = next(iter(service._sessions.values())).name
    connection.ended.add(name)
    before = len(connection.calls)
    args = {"session": name, "pid": 1, "window_id": 2}
    if action == "get_window_state":
        service._driver.call(action, {**args, "include_screenshot": False})
        assert [n for n, _ in connection.calls[before:]] == ["start_session", action]
    else:
        with pytest.raises(ComputerUseError) as caught:
            service._driver.call(
                action, {**args, "element_token": "s00000001:1", "delivery_mode": "background"}
            )
        assert caught.value.code == "computer_session_expired"
        assert connection.calls[before:] == [("start_session", {"session": name})]
    assert name not in connection.ended and len(lifecycle_connection) == 1


def test_failed_named_renewal_never_dispatches_input(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    name = next(iter(service._sessions.values())).name
    connection.fail_start = True
    before = len(connection.calls)
    with pytest.raises(ComputerUseError):
        service._driver.call(
            "click", {"session": name, "pid": 1, "window_id": 2, "delivery_mode": "background"}
        )
    assert connection.calls[before:] == [("start_session", {"session": name})]
