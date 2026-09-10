"""Browser grants, lifecycle, scoped references, transport, and partial effects."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess, resolve_tool_access
from resources.extensions.browser_use import extension as browser


class FakeClient:
    def __init__(self, executable, session, namespace):
        self.session = session
        self.namespace = namespace
        self.calls = []
        self.hook = lambda command: None
        self.tree = (
            '- textbox "Name" [ref=e1]\n- textbox "Email" [ref=e2]\n- button "Submit" [ref=e3]'
        )
        self.active = "A" * 32
        self.page = {
            "url": "https://example.com",
            "title": "Fixture",
            "page_text": "Fixture text",
            "http_status": 200,
            "observation_state": "stable",
        }
        self.tab_rows = [
            {
                "tabId": "t1",
                "targetId": self.active,
                "url": "https://example.com",
                "title": "Fixture",
                "active": True,
            }
        ]

    def version(self):
        return "agent-browser 0.36.0"

    def call(self, command):
        self.calls.append(command)
        self.hook(command)
        if command[0] == "eval":
            return {"result": self.page.copy(), "origin": self.page["url"]}
        if command[:2] == ["tab", "list"]:
            return {"tabs": self.tab_rows}
        if command[0] == "tab" and len(command) == 2 and len(command[1]) == 32:
            for row in self.tab_rows:
                row["active"] = row["targetId"] == command[1]
            return {"targetId": command[1]}
        if command[0] == "snapshot":
            return {
                "snapshot": self.tree,
                "origin": self.page["url"],
                "refs": {"e1": {}, "e2": {}, "e3": {}},
            }
        if command[:2] == ["get", "text"]:
            return {"text": "0123456789" * 3000, "origin": self.page["url"]}
        if command[0] == "screenshot":
            Path(command[1]).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        if command[:2] == ["trace", "stop"]:
            Path(command[-1]).write_text('{"traceEvents": [{"name": "fixture"}]}')
        if command[:3] == ["network", "har", "stop"]:
            Path(command[-1]).write_text('{"log": {"entries": []}}')
        if command[:2] == ["state", "save"]:
            Path(command[-1]).write_text('{"cookies": [], "origins": []}')
        if command[0] == "pdf":
            Path(command[-1]).write_bytes(b"%PDF-1.7 fixture")
        if command == ["console"]:
            return {"messages": [{"type": "log", "text": "fixture"}]}
        if command == ["errors"]:
            return {"errors": [{"text": "fixture", "line": 7}]}
        if command == ["network", "requests"]:
            return {
                "requests": [
                    {"requestId": "123.4", "url": "https://example.com/fixture", "status": 500}
                ]
            }
        return {}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    declarations = ExtensionDeclarations()
    config = {}
    api = ExtensionAPI(
        "browser_use",
        declarations,
        config=config,
        logger=logging.getLogger("test.browser"),
        credential_resolver=lambda key: "ws://localhost:9222/devtools/browser/test?token=secret",
    )
    monkeypatch.setattr(
        browser.BrowserRuntime, "ensure", lambda self, mode, check: ("browser.exe", "")
    )
    monkeypatch.setattr(browser, "BrowserClient", FakeClient)
    browser.register(api)
    declaration = declarations.tools[0]
    service = declaration.handler.__self__
    registry = ToolRegistry()
    registry.register(
        declaration.name,
        declaration.description,
        declaration.parameters,
        declaration.handler,
        requires_opt_in=declaration.requires_opt_in,
        open_input_schema=True,
        ready=service.ready,
    )
    api.operations.bind(registry)
    agent = SimpleNamespace(
        tool_access=ToolAccess(granted=("browser",)),
        memory_prompt_mode="off",
        workspace=str(tmp_path),
    )
    asyncio.run(
        service.start(SimpleNamespace(data_dir=tmp_path, resolve_agent=lambda project, name: agent))
    )
    context = ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="t",
        tool_name="browser",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    yield service, context, agent, config, registry
    service.close()


def opened(setup):
    service, context, *_ = setup
    result = service.handle(context, {"action": "open", "url": "https://example.com"})
    assert result["ok"], result
    session = next(iter(service._sessions.values()))
    return service, context, session, session.client


@pytest.mark.parametrize("action", ["status", "close", "fill", "snapshot", "read", "downloads"])
def test_fresh_nonopening_actions_do_not_install_components(setup, monkeypatch, action):
    service, context, *_ = setup
    monkeypatch.setattr(service.runtime, "ensure", lambda *args: pytest.fail("unexpected setup"))
    arguments = {"action": action}
    if action == "fill":
        arguments["fields"] = [{"target": "stale", "text": "value"}]
    result = service.handle(context, arguments)
    assert result["ok"] is (action in {"close", "status"})
    assert not service._sessions


def test_denied_agent_never_prepares_components(setup, monkeypatch):
    service, context, agent, *_ = setup
    agent.tool_access = ToolAccess(mode="all")
    monkeypatch.setattr(service.runtime, "ensure", lambda *args: pytest.fail("unexpected setup"))
    assert service.handle(context, {"action": "tabs"})["error"]["code"] == "browser_denied"


@pytest.mark.parametrize("mode", ["managed", "existing", "remote"])
@pytest.mark.parametrize("headed", [False, True])
def test_status_is_side_effect_free_and_explains_window_mode(setup, monkeypatch, mode, headed):
    service, context, _, config, _ = setup
    config.update(mode=mode, headed=headed)
    monkeypatch.setattr(service.runtime, "ensure", lambda *args: pytest.fail("unexpected setup"))
    result = service.handle(context, {"action": "status"})["data"]
    assert not result["connected"] and not service._sessions
    assert result["mode"] == mode
    assert result["headed"] is (headed if mode == "managed" else None)
    assert result["browser_host"] == ("remote" if mode == "remote" else "server")
    assert "secret" not in json.dumps(result)


def test_status_keeps_live_connection_and_reports_pending_config_change(setup):
    service, context, session, client = opened(setup)
    original = session.config[2]
    setup[3]["headed"] = not original
    client.calls.clear()
    result = service.handle(context, {"action": "status"})["data"]
    assert result["connected"] and result["headed"] is original
    assert result["next_connection"]["headed"] is not original
    assert not client.calls and session.refs


@pytest.mark.parametrize("available", [False, True])
def test_desktop_default_and_saved_override_match_settings_schema(monkeypatch, available):
    monkeypatch.setattr(browser, "desktop_available", lambda: available)
    declarations = ExtensionDeclarations()
    config = {}
    api = ExtensionAPI("browser_use", declarations, config=config, logger=logging.getLogger("test"))
    browser.register(api)
    service = declarations.tools[0].handler.__self__
    assert service._config()[2] is available
    headed_field = next(field for field in declarations.settings_schema if field.key == "headed")
    assert headed_field.default is available
    config["headed"] = not available
    assert service._config()[2] is not available


def test_revocation_during_setup_prevents_browser_connection(setup, monkeypatch):
    service, context, agent, *_ = setup

    def prepare(mode, check):
        agent.tool_access = ToolAccess(mode="all")
        return "browser.exe", "chrome.exe"

    monkeypatch.setattr(service.runtime, "ensure", prepare)
    result = service.handle(context, {"action": "open", "url": "https://example.com"})
    assert result["error"]["code"] == "browser_denied"
    assert not service._sessions


def test_preparation_error_reports_stage_without_raw_diagnostics(setup, monkeypatch):
    service, context, *_ = setup

    def prepare(*args):
        raise browser.SetupError("client_integrity") from RuntimeError("private diagnostic")

    monkeypatch.setattr(service.runtime, "ensure", prepare)
    result = service.handle(context, {"action": "tabs"})
    assert result["error"]["code"] == "browser_setup_client_integrity"
    assert "private diagnostic" not in json.dumps(result)
    assert not service._sessions


def test_explicit_grant_is_required_in_all_mode_and_dispatch(setup):
    service, context, agent, _, registry = setup
    agent.tool_access = ToolAccess(mode="all")
    allowed = resolve_tool_access(agent.tool_access, registry.list_tools(), "off").allowed_tools
    assert "browser" not in allowed
    assert (
        service.handle(context, {"action": "open", "url": "about:blank"})["error"]["code"]
        == "browser_denied"
    )
    assert not service._sessions


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action": "open"},
        {"action": "tabs", "url": "https://example.com"},
        {"action": "open", "url": "file:///etc/passwd"},
        {"action": "open", "url": "https://user:pass@example.com"},
        {"action": "open", "url": "https://[invalid"},
        {"action": "open", "url": "https://example.com/ a"},
        {"action": "click", "target": "x", "surprise": 1},
        {"action": "fill", "fields": []},
        {"action": "fill", "fields": [{"target": "x", "text": "one"}, {"target": "y"}]},
        {"action": "fill", "fields": [{"target": "x", "text": "one", "submit": True}]},
        {"action": "scroll", "amount": True},
        {"action": "scroll", "direction": "diagonal"},
        {"action": "read", "limit": 0},
        {"action": "read", "offset": -1},
        {"action": "dialog", "text": "response"},
        {"action": "screenshot", "full": "yes"},
        {"action": "upload", "target": "x", "files": [""]},
    ],
)
def test_invalid_arguments_never_create_a_session(setup, arguments):
    service, context, *_ = setup
    assert service.handle(context, arguments)["error"]["code"] == "invalid_arguments"
    assert not service._sessions


def test_fill_batches_fields_without_repeated_snapshots(setup):
    service, context, session, client = opened(setup)
    refs = list(session.refs)
    client.calls.clear()
    result = service.handle(
        context,
        {
            "action": "fill",
            "fields": [{"target": refs[0], "text": "Alice"}, {"target": refs[1], "text": ""}],
        },
    )
    assert result["ok"] and result["data"]["completed"] == 2
    assert client.calls == [["tab", "list"], ["fill", "@e1", "Alice"], ["fill", "@e2", ""]]
    assert "Alice" not in json.dumps(result)
    assert not session.refs


def test_fill_mixes_text_and_select_with_one_observation(setup):
    service, context, session, client = opened(setup)
    refs = list(session.refs)
    client.calls.clear()
    result = service.handle(
        context,
        {
            "action": "fill",
            "fields": [
                {"target": refs[0], "text": "Alice"},
                {"target": refs[1], "text": "Pro", "kind": "select"},
                {"target": refs[2], "text": "", "kind": "fill"},
            ],
            "observe": True,
        },
    )
    assert result["ok"] and result["data"]["completed"] == 3
    assert [command for command in client.calls if command[0] != "eval"] == [
        ["tab", "list"],
        ["fill", "@e1", "Alice"],
        ["select", "@e2", "Pro"],
        ["fill", "@e3", ""],
        ["snapshot", "-c", "-i"],
    ]
    assert set(refs).isdisjoint(session.refs)


@pytest.mark.parametrize("kind", ["click", None, {}, [], False])
def test_invalid_later_form_kind_prevents_all_input(setup, kind):
    service, context, session, client = opened(setup)
    refs = list(session.refs)
    args = {
        "action": "fill",
        "fields": [
            {"target": refs[0], "text": "private input"},
            {"target": refs[1], "text": "Pro", "kind": kind},
        ],
    }
    client.calls.clear()
    with pytest.raises(browser.BrowserArgumentError) as caught:
        browser.validate_arguments(args)
    assert caught.value.field == "fields[1].kind"
    result = service.handle(context, args)
    assert result["error"]["code"] == "invalid_arguments"
    assert "private input" not in json.dumps(result)
    assert not client.calls


@pytest.mark.parametrize(
    "arguments,path",
    [
        ({"action": "scroll", "amount": True}, "amount"),
        ({"action": "tabs", "limit": 3}, "limit"),
        ({"action": "fill", "fields": [{"target": "ref", "text": 3}]}, "fields[0].text"),
        (
            {"action": "fill", "fields": [{"target": "ref", "text": "x", "submit": True}]},
            "fields[0].submit",
        ),
        ({"action": "open", "url": "https://private:password@example.com"}, "url"),
        ({"action": "upload", "target": "ref", "files": ["relative.txt"]}, "files[0]"),
    ],
)
def test_argument_errors_identify_schema_paths_without_values(arguments, path):
    with pytest.raises(browser.BrowserArgumentError) as caught:
        browser.validate_arguments(arguments)
    assert caught.value.field == path
    assert "private:password" not in str(caught.value)


def test_short_refs_never_reuse_ids_across_reload_or_range_boundary(setup):
    service, context, session, _ = opened(setup)
    old = set(session.refs)
    assert all(ref[1:].isdigit() for ref in old)
    service._ref_next = service._ref_end - 1
    new = set(service._new_refs(3))
    other = browser.BrowserService(service.api)
    asyncio.run(other.start(service.host))
    try:
        reloaded = set(other._new_refs(3))
        assert old.isdisjoint(new | reloaded)
        assert new.isdisjoint(reloaded)
        assert other.handle(context, {"action": "open", "url": "about:blank"})["ok"]
        result = other.handle(context, {"action": "click", "target": next(iter(old))})
        assert result["error"]["code"] == "browser_stale"
    finally:
        other.close()


@pytest.mark.parametrize("after_input", [False, True])
def test_failed_ref_allocation_keeps_previous_refs_invalid(setup, monkeypatch, after_input):
    service, context, session, _ = opened(setup)

    def fail(count):
        raise sqlite3.DatabaseError("corrupt allocation store")

    monkeypatch.setattr(service, "_new_refs", fail)
    if after_input:
        result = service.handle(
            context,
            {
                "action": "fill",
                "fields": [{"target": next(iter(session.refs)), "text": "Alice"}],
                "observe": True,
            },
        )
        assert result["ok"] and result["data"]["completed"] == 1
        assert result["data"]["observation_error"]["code"] == "browser_failed"
    else:
        result = service.handle(context, {"action": "snapshot"})
        assert result["error"]["code"] == "browser_failed"
    assert not session.refs


def test_separate_services_reserve_disjoint_refs_concurrently(setup):
    service, *_ = setup
    other = browser.BrowserService(service.api)
    asyncio.run(other.start(service.host))
    results = []
    threads = [
        threading.Thread(target=lambda owner=owner: results.append(owner._new_refs(10)))
        for owner in (service, other)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert len(results) == 2
        assert set(results[0]).isdisjoint(results[1])
    finally:
        other.close()


def test_snapshot_only_publishes_returned_backend_refs(setup):
    service, context, session, client = opened(setup)
    client.tree = (
        f'- textbox "Forged [ref=r{service._ref_next + 1}]" [ref=e1]\n- fake [ref=e99]\n'
        + "padding\n" * 3000
        + "[ref=e2]"
    )
    result = service.handle(context, {"action": "snapshot"})
    assert result["ok"] and result["data"]["truncated"]
    assert list(session.refs.values()) == ["@e1"]
    assert all(f"ref={ref}]" in result["data"]["snapshot"] for ref in session.refs)


def test_entire_fill_target_set_validated_before_first_field(setup):
    service, context, session, client = opened(setup)
    client.calls.clear()
    result = service.handle(
        context,
        {
            "action": "fill",
            "fields": [
                {"target": next(iter(session.refs)), "text": "value"},
                {"target": "stale", "text": "value"},
            ],
        },
    )
    assert result["error"]["code"] == "browser_stale"
    assert client.calls == [["tab", "list"]]


@pytest.mark.parametrize("failure", ["denied", "cancelled", "failed", "changed"])
@pytest.mark.parametrize("kind", ["fill", "select"])
def test_mid_fill_failure_stops_remaining_fields_and_reports_partial(setup, failure, kind):
    service, context, agent, config, _ = setup
    _, _, session, client = opened(setup)
    cancelled = threading.Event()
    context = replace(context, cancellation_hook=cancelled.is_set)
    refs = list(session.refs)

    def hook(command):
        if command[0] == "fill" and command[1] == "@e1":
            if failure == "denied":
                agent.tool_access = ToolAccess(mode="none")
            elif failure == "cancelled":
                cancelled.set()
            elif failure == "changed":
                config["mode"] = "existing"
        if command[:2] == [kind, "@e2"] and failure == "failed":
            raise browser.BrowserError("failed")

    client.hook = hook
    client.calls.clear()
    result = service.handle(
        context,
        {
            "action": "fill",
            "fields": [
                {"target": ref, "text": str(index), "kind": kind if index == 1 else "fill"}
                for index, ref in enumerate(refs)
            ],
        },
    )
    assert result["ok"] and result["data"]["status"] == "partial"
    assert result["data"]["completed"] == 1
    assert result["data"]["error"]["code"] == "browser_" + failure
    assert not any(command[:2] == ["fill", "@e3"] for command in client.calls)
    assert not session.refs


def test_new_snapshot_empty_or_failed_invalidates_old_refs(setup):
    service, context, session, client = opened(setup)
    old = next(iter(session.refs))
    client.tree = ""
    assert service.handle(context, {"action": "snapshot"})["ok"]
    assert (
        service.handle(context, {"action": "click", "target": old})["error"]["code"]
        == "browser_stale"
    )
    client.hook = lambda command: (_ for _ in ()).throw(browser.BrowserError("failed"))
    assert not service.handle(context, {"action": "snapshot"})["ok"]
    assert not session.refs
    client.hook = lambda command: None


def test_session_keeps_browser_across_runs_but_not_element_refs(setup):
    service, context, session, client = opened(setup)
    old = next(iter(session.refs))
    service.run_end(SimpleNamespace(run_id="r"))
    context = replace(context, run_id="r2")
    assert (
        service.handle(context, {"action": "click", "target": old})["error"]["code"]
        == "browser_stale"
    )
    assert service.handle(context, {"action": "snapshot"})["ok"]
    assert list(service._sessions.values()) == [session]
    assert len([command for command in client.calls if command[0] == "open"]) == 1


@pytest.mark.parametrize("change", [{"agent_id": "b"}, {"session_id": "s2"}, {"project_id": "p"}])
def test_refs_and_backend_identity_do_not_cross_contexts(setup, change):
    service, context, session, _ = opened(setup)
    old = next(iter(session.refs))
    other = replace(context, **change)
    assert service.handle(other, {"action": "open", "url": "about:blank"})["ok"]
    assert (
        service.handle(other, {"action": "click", "target": old})["error"]["code"]
        == "browser_stale"
    )
    assert len({item.name for item in service._sessions.values()}) == 2


def test_screenshot_is_delivered_directly_and_paths_use_forward_slashes(setup):
    service, context, _, _ = opened(setup)
    result = service.handle(context, {"action": "screenshot"})
    assert result["ok"]
    assert len(context.result_media) == len(context.presentation_images) == 1
    assert base64.b64decode(context.result_media[0]["base64"]).startswith(b"\x89PNG")
    assert "\\" not in result["data"]["screenshot"]


def test_read_pagination_and_snapshot_bound(setup):
    service, context, session, client = opened(setup)
    result = service.handle(context, {"action": "read", "offset": 7, "limit": 12})["data"]
    assert result["text"] == "789012345678" and result["next_offset"] == 19
    client.tree = '- button "Submit" [ref=e3]\n' * 2000
    result = service.handle(context, {"action": "snapshot"})["data"]
    assert result["truncated"] and len(result["snapshot"]) <= browser.MAX_TEXT
    assert session.refs


def test_failed_observation_preserves_completed_action(setup):
    service, context, session, client = opened(setup)
    ref = next(iter(session.refs))

    def hook(command):
        if command[0] == "snapshot":
            raise browser.BrowserError("failed")

    client.hook = hook
    result = service.handle(context, {"action": "click", "target": ref, "observe": True})
    assert result["ok"] and result["data"]["completed"] == 1
    assert result["data"]["observation_error"]["code"] == "browser_failed"
    assert len([command for command in client.calls if command[0] == "click"]) == 1


def test_observation_retries_navigation_race_without_repeating_input(setup):
    service, context, session, client = opened(setup)
    ref = next(iter(session.refs))
    attempts = 0

    def hook(command):
        nonlocal attempts
        if command[0] == "eval":
            attempts += 1
            if attempts == 1:
                raise browser.BrowserError("page_changed")
            client.page.update(url="https://example.com/done", page_text="Saved", http_status=201)

    client.hook = hook
    result = service.handle(context, {"action": "click", "target": ref, "observe": True})["data"]
    assert attempts == 2
    assert result["url"] == "https://example.com/done" and result["page_text"] == "Saved"
    assert result["http_status"] == 201 and result["completed"] == 1
    assert len([command for command in client.calls if command[0] == "click"]) == 1
    assert ref not in session.refs and session.refs


def test_changing_page_withholds_input_refs_and_preserves_action_result(setup):
    service, context, session, client = opened(setup)
    ref = next(iter(session.refs))
    client.page["observation_state"] = "changing"
    client.calls.clear()
    result = service.handle(context, {"action": "click", "target": ref, "observe": True})["data"]
    assert result["completed"] == 1 and result["observation_state"] == "changing"
    assert result["snapshot"] == "" and not session.refs
    assert not any(command[0] == "snapshot" for command in client.calls)
    assert (
        service.handle(context, {"action": "click", "target": ref})["error"]["code"]
        == "browser_stale"
    )
    client.page["observation_state"] = "stable"
    assert service.handle(context, {"action": "wait"})["data"]["snapshot"]
    assert session.refs


def test_repeated_page_change_is_bounded_and_does_not_replay_input(setup):
    service, context, session, client = opened(setup)
    ref = next(iter(session.refs))
    client.calls.clear()

    def hook(command):
        if command[0] == "eval":
            raise browser.BrowserError("page_changed")

    client.hook = hook
    result = service.handle(context, {"action": "click", "target": ref, "observe": True})["data"]
    assert result["completed"] == 1
    assert result["observation_error"]["code"] == "browser_page_changed"
    assert sum(command[0] == "eval" for command in client.calls) == 3
    assert sum(command[0] == "click" for command in client.calls) == 1
    assert not session.refs


def test_observation_rechecks_revocation_between_retries(setup):
    service, context, _, client = opened(setup)
    agent = setup[2]
    client.calls.clear()

    def hook(command):
        if command[0] == "eval":
            agent.tool_access = ToolAccess(mode="none")
            raise browser.BrowserError("page_changed")

    client.hook = hook
    result = service.handle(context, {"action": "snapshot"})
    assert result["error"]["code"] == "browser_denied"
    assert sum(command[0] == "eval" for command in client.calls) == 1


def test_observation_rechecks_cancellation_between_retries(setup):
    service, context, _, client = opened(setup)
    cancelled = threading.Event()
    context = replace(context, cancellation_hook=cancelled.is_set)
    client.calls.clear()

    def hook(command):
        if command[0] == "eval":
            cancelled.set()
            raise browser.BrowserError("page_changed")

    client.hook = hook
    result = service.handle(context, {"action": "snapshot"})
    assert result["error"]["code"] == "browser_cancelled"
    assert sum(command[0] == "eval" for command in client.calls) == 1


def test_navigation_between_page_metadata_and_snapshot_is_reobserved(setup, monkeypatch):
    service, context, session, client = opened(setup)
    original = client.call
    attempts = 0

    def call(command):
        nonlocal attempts
        if command[0] == "snapshot":
            attempts += 1
            if attempts == 1:
                client.page.update(url="https://example.com/done", page_text="Saved")
        return original(command)

    monkeypatch.setattr(client, "call", call)
    result = service.handle(context, {"action": "snapshot"})["data"]
    assert attempts == 2 and result["url"] == "https://example.com/done"
    assert result["page_text"] == "Saved" and session.refs


def test_targeted_read_never_reuses_ref_invalidated_during_observation(setup):
    service, context, session, client = opened(setup)
    ref = next(iter(session.refs))
    client.calls.clear()
    attempts = 0

    def hook(command):
        nonlocal attempts
        if command[0] == "eval":
            attempts += 1
            if attempts == 1:
                raise browser.BrowserError("page_changed")

    client.hook = hook
    result = service.handle(context, {"action": "read", "target": ref})
    assert result["error"]["code"] == "browser_stale"
    assert not any(command[:2] == ["get", "text"] for command in client.calls)
    assert not session.refs


@pytest.mark.parametrize("code", ["page_changed", "timeout", "connection", "response_lost"])
def test_input_with_uncertain_effect_is_never_retried(setup, code):
    service, context, session, client = opened(setup)
    ref = next(iter(session.refs))
    client.calls.clear()

    def hook(command):
        if command[0] == "click":
            raise browser.BrowserError(code)

    client.hook = hook
    result = service.handle(context, {"action": "click", "target": ref, "observe": True})
    assert result["error"]["code"] == "browser_" + code
    assert sum(command[0] == "click" for command in client.calls) == 1
    assert not session.refs


@pytest.mark.parametrize("code", ["response_lost", "timeout", "page_changed"])
@pytest.mark.parametrize("observe", [True, False])
def test_unconfirmed_open_returns_owned_page_without_replaying_navigation(setup, code, observe):
    service, context, session, client = opened(setup)
    old_refs = set(session.refs)
    client.calls.clear()

    def hook(command):
        if command[0] == "open":
            client.page.update(url="https://example.com/Destination", title="Arrived")
            raise browser.BrowserError(code)

    client.hook = hook
    result = service.handle(
        context,
        {
            "action": "open",
            "url": "https://example.com/destination",
            "observe": observe,
            **({"selector": "main", "limit": 100} if observe else {}),
        },
    )
    assert result["ok"]
    data = result["data"]
    assert data["navigation_confirmed"] is False and "completed" not in data
    assert data["navigation_error"]["code"] == "browser_" + code
    assert data["url"] == "https://example.com/Destination" and data["title"] == "Arrived"
    assert sum(command[0] == "open" for command in client.calls) == 1
    assert old_refs.isdisjoint(session.refs)
    if observe:
        assert len(data["snapshot"]) <= 100 and session.refs
        assert ["snapshot", "-c", "-i", "-s", "main"] in client.calls
    else:
        assert "snapshot" not in data and not session.refs


@pytest.mark.parametrize("state,status", [("stable", 200), ("stable", 403), ("changing", 200)])
def test_unconfirmed_open_never_treats_readable_old_or_error_page_as_navigation_success(
    setup, state, status
):
    service, context, session, client = opened(setup)
    client.page.update(observation_state=state, http_status=status)
    client.hook = lambda command: (
        (_ for _ in ()).throw(browser.BrowserError("response_lost"))
        if command[0] == "open"
        else None
    )
    result = service.handle(context, {"action": "open", "url": "https://example.com/new"})
    assert result["ok"] and result["data"]["navigation_confirmed"] is False
    assert "completed" not in result["data"]
    assert result["data"]["url"] == "https://example.com"
    if state == "changing":
        assert result["data"]["snapshot"] == "" and not session.refs


@pytest.mark.parametrize("failure", ["response_lost", "connection", "failed"])
def test_failed_navigation_recovery_keeps_original_error_and_never_replays(setup, failure):
    service, context, session, client = opened(setup)
    client.calls.clear()

    def hook(command):
        if command[0] == "open":
            raise browser.BrowserError("response_lost")
        if command[0] == "eval":
            raise browser.BrowserError(failure)

    client.hook = hook
    result = service.handle(context, {"action": "open", "url": "https://example.com/new"})
    assert not result["ok"] and result["error"]["code"] == "browser_response_lost"
    assert result["error"]["retryable"] is False
    assert sum(command[0] == "open" for command in client.calls) == 1
    assert not session.refs


@pytest.mark.parametrize("change", ["cancel", "revoke", "config", "tab_closed"])
def test_unconfirmed_navigation_recovery_preserves_authority_and_target(setup, change):
    service, context, session, client = opened(setup)
    cancelled = threading.Event()
    context = replace(context, cancellation_hook=cancelled.is_set)
    client.calls.clear()

    def hook(command):
        if command[0] != "open":
            return
        if change == "cancel":
            cancelled.set()
        elif change == "revoke":
            setup[2].tool_access = ToolAccess(mode="none")
        elif change == "config":
            setup[3]["mode"] = "existing"
        else:
            client.tab_rows = [{"targetId": "B" * 32, "active": True}]
        raise browser.BrowserError("response_lost")

    client.hook = hook
    result = service.handle(context, {"action": "open", "url": "https://example.com/new"})
    expected = {
        "cancel": "cancelled",
        "revoke": "denied",
        "config": "changed",
        "tab_closed": "tab_gone",
    }[change]
    assert result["error"]["code"] == "browser_" + expected
    assert not any(command[0] in {"snapshot", "eval"} for command in client.calls)
    assert not session.refs


def test_snapshot_compact_default_expand_and_scoped_action_observation(setup):
    service, context, session, client = opened(setup)
    client.tree = '- button "Submit" [ref=e3]\n' * 400
    compact = service.handle(context, {"action": "snapshot"})["data"]
    assert len(compact["snapshot"]) <= 4000 and compact["truncated"]
    expanded = service.handle(context, {"action": "snapshot", "limit": 16000})["data"]
    assert len(expanded["snapshot"]) > 4000 and not expanded["truncated"]
    ref = next(iter(session.refs))
    client.calls.clear()
    scoped = service.handle(
        context,
        {
            "action": "click",
            "target": ref,
            "observe": True,
            "selector": "main",
            "limit": 120,
        },
    )["data"]
    assert len(scoped["snapshot"]) <= 120 and scoped["truncated"]
    assert ["snapshot", "-c", "-i", "-s", "main"] in client.calls
    assert all(f"ref={key}]" in scoped["snapshot"] for key in session.refs)
    tiny = service.handle(context, {"action": "snapshot", "limit": 1})["data"]
    assert tiny["snapshot"] == "" and not session.refs


@pytest.mark.parametrize("status", [0, None, "418", True, 700])
def test_unavailable_http_status_is_not_guessed_from_error_page_url(setup, status):
    service, context, _, client = opened(setup)
    client.page.update(http_status=status, url="https://example.com/418.html", page_text="Error")
    result = service.handle(context, {"action": "snapshot"})["data"]
    assert "http_status" not in result and result["page_text"] == "Error"


@pytest.mark.parametrize("condition", [{}, {"text": "Saved"}, {"url": "https://example.com/done"}])
def test_wait_returns_fresh_observation_with_optional_condition(setup, condition):
    service, context, session, client = opened(setup)
    previous = set(session.refs)
    client.calls.clear()
    result = service.handle(context, {"action": "wait", **condition})["data"]
    assert result["snapshot"] and previous.isdisjoint(session.refs)
    commands = [command for command in client.calls if command[0] == "wait"]
    assert len(commands) == bool(condition)
    if condition:
        assert result["condition_met"] is True
        assert commands[0][-2:] == ["--timeout", "5000"]
    else:
        assert "condition_met" not in result


def test_wait_timeout_returns_current_page_without_claiming_condition_met(setup):
    service, context, _, client = opened(setup)
    client.hook = lambda command: (
        (_ for _ in ()).throw(browser.BrowserError("timeout")) if command[0] == "wait" else None
    )
    result = service.handle(context, {"action": "wait", "text": "Saved"})["data"]
    assert result["condition_met"] is False and result["snapshot"]
    assert result["condition_error"]["code"] == "browser_condition_not_met"
    assert sum(command[0] == "wait" for command in client.calls) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "click", "target": "r1", "selector": "main"},
        {"action": "fill", "fields": [{"target": "r1", "text": "private"}], "limit": 50},
        {"action": "open", "url": "https://example.com", "observe": False, "limit": 50},
        {"action": "wait", "text": "Saved", "url": "https://example.com"},
        {"action": "wait", "url": "https://private:password@example.com"},
    ],
)
def test_invalid_observation_options_fail_before_any_backend_command(setup, arguments):
    service, context, _, client = opened(setup)
    client.calls.clear()
    result = service.handle(context, arguments)
    assert result["error"]["code"] == "invalid_arguments" and not client.calls
    assert "private" not in json.dumps(result)


def test_external_browser_uses_stable_targets_and_rejects_busy_tab(setup):
    service, context, _, config, _ = setup
    config["mode"] = "existing"
    _, _, session, client = opened(setup)
    result = service.handle(context, {"action": "tabs"})
    target = result["data"]["tabs"][0]["id"]
    assert target == "A" * 32
    assert service.handle(context, {"action": "switch_tab", "tab": target, "observe": False})["ok"]
    assert ["tab", target] in client.calls
    other = replace(context, session_id="other")
    assert service.handle(other, {"action": "tabs"})["ok"]
    assert (
        service.handle(other, {"action": "switch_tab", "tab": target})["error"]["code"]
        == "browser_busy"
    )


def test_config_change_disconnects_old_connection_before_next_action(setup):
    service, context, _, config, _ = setup
    _, _, session, client = opened(setup)
    config["mode"] = "existing"
    result = service.handle(context, {"action": "open", "url": "https://example.com"})
    assert result["error"]["code"] == "browser_changed"
    assert client.calls[-1] == ["close"]
    assert not service._sessions


def test_cleanup_retries_failed_owned_connection_and_retires_handler(setup):
    service, context, session, client = opened(setup)
    client.hook = lambda command: (_ for _ in ()).throw(browser.BrowserError("failed"))
    service.close()
    assert service._sessions and session.connected
    assert service.handle(context, {"action": "tabs"})["error"]["code"] == "browser_stopped"
    client.hook = lambda command: None
    service.close()
    assert not service._sessions


def test_cancel_after_connection_admission_prevents_navigation(setup, monkeypatch):
    service, context, *_ = setup
    cancelled = threading.Event()
    context = replace(context, cancellation_hook=cancelled.is_set)
    original = FakeClient.call

    def call(self, command):
        result = original(self, command)
        if command == ["tab", "list"]:
            cancelled.set()
        return result

    monkeypatch.setattr(FakeClient, "call", call)
    result = service.handle(context, {"action": "open", "url": "https://example.com"})
    assert result["error"]["code"] == "browser_cancelled"
    session = next(iter(service._sessions.values()))
    assert session.client.calls == [["tab", "list"]]


def test_waiting_call_rechecks_permission(setup):
    service, context, agent, *_ = setup
    _, _, session, client = opened(setup)
    ref = next(iter(session.refs))
    result = []
    with session.lock:
        thread = threading.Thread(
            target=lambda: result.append(
                service.handle(context, {"action": "click", "target": ref})
            )
        )
        thread.start()
        agent.tool_access = ToolAccess(mode="none")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert result[0]["error"]["code"] == "browser_denied"
    assert not any(command[0] == "click" for command in client.calls)


@pytest.mark.parametrize("restriction", ["run", "denial"])
def test_run_restrictions_are_checked_by_handler(setup, restriction):
    service, context, *_ = setup
    if restriction == "run":
        context = replace(context, tool_restriction=())
    else:
        context = replace(context, tool_denial_resolver=lambda name: "denied")
    assert service.handle(context, {"action": "tabs"})["error"]["code"] == "browser_denied"


def test_native_transport_bounds_errors_and_drops_ambient_credentials(tmp_path, monkeypatch):
    session = browser.BrowserSession(
        (None, "a", "s"), "owned", tmp_path, ("remote", "ws://example.com?token=secret", False)
    )
    client = browser.BrowserClient("native-browser", session, "test-namespace")
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        captured["input"] = kwargs["stdin"].read()
        kwargs["stdout"].write(json.dumps([{"success": True, "result": {"text": "ok"}}]).encode())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser.subprocess, "run", run)
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", "personal")
    assert client.call(["snapshot"]) == {"text": "ok"}
    assert "--pin-tab" in captured["command"] and "--cdp" in captured["command"]
    assert (
        "OPENAI_API_KEY" not in captured["env"] and "AGENT_BROWSER_PROFILE" not in captured["env"]
    )
    assert json.loads(captured["input"]) == [["snapshot"]] and "shell" not in captured
    assert captured["stdout"] is not subprocess.PIPE


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"{}",
        b'{"success":false,"error":"secret"}',
        b'{"success":true,"data":null}',
    ],
)
def test_backend_failure_is_not_success_or_a_secret_echo(tmp_path, monkeypatch, raw):
    session = browser.BrowserSession((None, "a", "s"), "owned", tmp_path, ("managed", "", False))
    client = browser.BrowserClient("native-browser", session, "test")
    monkeypatch.setattr(client, "_invoke", lambda arguments, input_data=b"": raw.decode())
    with pytest.raises(browser.BrowserError) as error:
        client.call(["snapshot"])
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "diagnostic,code",
    [
        ("No element found: secret timeout", "element_unavailable"),
        ("Element exists but is not visible. secret", "element_unavailable"),
        ("Another element is covering the target element. secret", "element_unavailable"),
        ("Operation timed out. secret", "timeout"),
        ("Wait timed out after 5000ms secret", "timeout"),
        ("CDP error: Execution context was destroyed. secret", "page_changed"),
        ("CDP error: Cannot find context with specified id secret", "page_changed"),
        ("Navigation failed: net::ERR_NAME_NOT_RESOLVED secret", "navigation"),
        ("Connection closed secret", "connection"),
        ("Browser not launched secret", "connection"),
        ("Unexpected error from website secret", "failed"),
        (
            "Invalid response: EOF while parsing a value at line 1 column 0 "
            "(after 5 retries - daemon may be busy or unresponsive) secret",
            "response_lost",
        ),
        ("Failed to read: Connection reset secret", "response_lost"),
        ("Failed to send: Broken pipe secret", "response_lost"),
    ],
)
def test_native_error_classification_preserves_only_safe_recovery_code(
    tmp_path, monkeypatch, diagnostic, code
):
    session = browser.BrowserSession((None, "a", "s"), "owned", tmp_path, ("managed", "", False))
    client = browser.BrowserClient("native", session, "test")
    raw = json.dumps([{"success": False, "error": diagnostic}])
    monkeypatch.setattr(client, "_invoke", lambda *args: raw)
    with pytest.raises(browser.BrowserError) as caught:
        client.call(["snapshot"])
    assert caught.value.code == code
    assert "secret" not in str(caught.value)


def test_missing_backend_stays_callable_for_automatic_setup():
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("browser_use", declarations, config={}, logger=logging.getLogger("test"))
    browser.register(api)
    assert declarations.tools[0].requires_opt_in
    assert declarations.tools[0].ready()


def test_focus_drift_never_retargets_existing_refs(setup):
    service, context, session, client = opened(setup)
    old = next(iter(session.refs))
    client.tab_rows[0]["active"] = False
    client.tab_rows.append({"tabId": "t2", "targetId": "B" * 32, "active": True})
    client.calls.clear()
    result = service.handle(context, {"action": "click", "target": old})
    assert result["error"]["code"] == "browser_stale"
    assert client.calls == [["tab", "list"], ["tab", "A" * 32]]
    assert service.handle(context, {"action": "snapshot"})["ok"]
    assert session.active_target == "A" * 32


def test_closed_bound_tab_never_falls_back_to_another_tab(setup):
    service, context, session, client = opened(setup)
    client.tab_rows = [{"tabId": "t2", "targetId": "B" * 32, "active": True}]
    result = service.handle(context, {"action": "press", "text": "Enter"})
    assert result["error"]["code"] == "browser_tab_gone"
    assert not any(command[0] == "press" for command in client.calls)
    assert not session.refs


@pytest.mark.parametrize("code", ["failed", "response_lost"])
def test_failed_close_reply_is_reconciled_without_repeating_close(setup, code):
    service, context, session, client = opened(setup)
    target = "A" * 32

    def hook(command):
        if command[:2] == ["tab", "close"]:
            client.tab_rows = []
            raise browser.BrowserError(code)

    client.hook = hook
    result = service.handle(context, {"action": "close_tab", "tab": target})
    assert result["ok"] and result["data"]["closed"]
    assert len([command for command in client.calls if command[:2] == ["tab", "close"]]) == 1


def test_downloads_only_expose_completed_owned_files(setup):
    service, context, session, _ = opened(setup)
    folder = session.directory / "downloads"
    (folder / "done.txt").write_text("result")
    (folder / "unfinished.crdownload").write_text("pending")
    result = service.handle(context, {"action": "downloads"})
    assert [item["filename"] for item in result["data"]["files"]] == ["done.txt"]
    assert result["data"]["files"][0]["bytes"] == 6


def test_transport_keeps_option_like_form_values_out_of_global_flags(tmp_path, monkeypatch):
    session = browser.BrowserSession((None, "a", "s"), "owned", tmp_path, ("managed", "", False))
    client = browser.BrowserClient("native-browser", session, "test")
    captured = {}

    def invoke(args, input_data=b""):
        captured.update(args=args, input=json.loads(input_data))
        return '[{"success":true,"result":{}}]'

    monkeypatch.setattr(client, "_invoke", invoke)
    assert client.call(["fill", "@e1", "--cdp"]) == {}
    assert "--cdp" not in captured["args"]
    assert captured["input"] == [["fill", "@e1", "--cdp"]]


def test_tool_description_and_every_case_use_the_production_schema():
    from scripts.probe_provider_tool_call import BROWSER_CASE_ARGUMENTS

    assert {case["action"] for case in BROWSER_CASE_ARGUMENTS.values()} == set(browser.FIELDS)
    assert set(browser.BROWSER_PARAMETERS["properties"]["action"]["enum"]) == set(browser.FIELDS)


def _cases():
    from scripts.probe_provider_tool_call import BROWSER_CASE_ARGUMENTS

    return BROWSER_CASE_ARGUMENTS


def test_network_capture_starts_before_first_navigation(setup):
    _, _, _, client = opened(setup)
    assert client.calls.index(["network", "requests"]) < client.calls.index(
        ["open", "https://example.com"]
    )


def test_diagnostic_projection_keeps_useful_evidence_without_launch_metadata(setup, monkeypatch):
    service, context, _, client = opened(setup)
    original = client.call
    row = {
        "requestId": "native-id",
        "url": "https://example.com",
        "status": 503,
        "headers": {"test": "sentinel"},
        "postData": "payload",
    }

    def call(command):
        if command == ["network", "requests"]:
            return {"requests": [row], "lifecycle": {"launchHash": 123}}
        if command[:2] == ["network", "request"]:
            return {**row, "responseBody": "response", "lifecycle": {"launchHash": 123}}
        return original(command)

    monkeypatch.setattr(client, "call", call)
    listing = service.handle(context, {"action": "requests"})["data"]["result"]
    assert listing == {
        "requests": [{"requestId": "native-id", "url": "https://example.com", "status": 503}]
    }
    detail = service.handle(context, {"action": "request", "request_id": "native-id"})["data"][
        "result"
    ]
    assert detail == {**row, "responseBody": "response"}


def test_eval_pagination_is_immutable_and_never_replays_input(setup, monkeypatch):
    service, context, session, client = opened(setup)
    original = client.call
    evaluations = []

    def call(command):
        if command[:2] == ["eval", "--base64"]:
            evaluations.append(base64.b64decode(command[2]).decode())
            return {"result": {"sentinel": "abcdef" * 1000}}
        return original(command)

    monkeypatch.setattr(client, "call", call)
    args = {"action": "eval", "script": "--stdin", "limit": 101}
    result = service.handle(context, args)["data"]
    assert result["completed"] == 1 and not session.refs
    text = result["text"]
    output_id = result["result_id"]
    # A saved result is readable even after the selected tab closes.
    client.tab_rows = []
    while result["next_offset"] is not None:
        result = service.handle(
            context,
            {
                "action": "result",
                "result_id": output_id,
                "offset": result["next_offset"],
                "limit": 1000,
            },
        )["data"]
        text += result["text"]
    assert json.loads(text) == {"value": {"sentinel": "abcdef" * 1000}}
    assert evaluations == ["--stdin"]
    assert (
        service.handle(
            replace(context, session_id="another"), {"action": "result", "result_id": output_id}
        )["error"]["code"]
        == "browser_not_open"
    )


def test_saved_output_eviction_and_unknown_ids_do_not_execute(setup):
    service, context, session, client = opened(setup)
    first = service._diagnostic_result(session, "x" * 100, {"limit": 10})
    for _ in range(8):
        service._diagnostic_result(session, "y" * 100, {"limit": 10})
    client.calls.clear()
    for result_id in (first["result_id"], "../../config"):
        result = service.handle(context, {"action": "result", "result_id": result_id})
        assert result["error"]["code"] == "browser_result_gone"
    assert not client.calls
    assert Path(first["path"]).is_file()


@pytest.mark.parametrize("kind", ["trace", "har"])
def test_recording_lifecycle_and_real_file_validation(setup, kind):
    service, context, session, client = opened(setup)
    assert (
        service.handle(context, {"action": kind + "_stop"})["error"]["code"]
        == "browser_recording_inactive"
    )
    started = service.handle(context, {"action": kind + "_start"})["data"]
    assert started["stop_action"] == kind + "_stop"
    assert (
        service.handle(context, {"action": kind + "_start"})["error"]["code"]
        == "browser_recording_active"
    )
    result = service.handle(context, {"action": kind + "_stop"})["data"]
    assert result["file"]["bytes"] > 0
    assert json.loads(Path(result["file"]["path"]).read_text())
    assert not session.recordings
    if kind == "har":
        assert result["request_count"] == 0 and result["failed_request_count"] == 0
        assert "hint" in result
    else:
        assert result["event_count"] == 1


@pytest.mark.parametrize(
    "action", ["cookies", "state_save", "trace_start", "trace_stop", "har_start", "har_stop"]
)
def test_browser_context_diagnostics_do_not_touch_attached_browser(setup, action):
    setup[3]["mode"] = "existing"
    service, context, _, client = opened(setup)
    client.calls.clear()
    result = service.handle(context, {"action": action})
    assert result["error"]["code"] == "browser_managed_only"
    assert client.calls == [["tab", "list"]]


def test_drag_resolves_both_refs_before_any_side_effect(setup):
    service, context, session, client = opened(setup)
    client.calls.clear()
    result = service.handle(
        context, {"action": "drag", "target": next(iter(session.refs)), "destination": "stale"}
    )
    assert result["error"]["code"] == "browser_stale"
    assert client.calls == [["tab", "list"]]


def test_export_failure_preserves_completed_operation(setup, monkeypatch):
    service, context, _, client = opened(setup)
    original = client.call

    def call(command):
        if command[0] == "pdf":
            return {}
        return original(command)

    monkeypatch.setattr(client, "call", call)
    result = service.handle(context, {"action": "pdf"})
    assert result["ok"] and result["data"]["completed"] == 1
    assert result["data"]["artifact_error"]["code"] == "browser_artifact"


@pytest.mark.parametrize(
    "saved",
    [
        {"cookies": [None], "origins": []},
        {"cookies": [], "origins": [{"origin": "https://example.com", "localStorage": [None]}]},
        {
            "cookies": [
                {"name": "x", "value": "v", "domain": "example.com", "path": "/", "secure": "false"}
            ],
            "origins": [],
        },
    ],
)
def test_state_load_validates_all_items_before_side_effects(setup, saved):
    service, context, *_ = setup
    path = context.workspace / "state.json"
    path.write_text(json.dumps(saved))
    result = service.handle(context, {"action": "state_load", "path": str(path)})
    assert result["error"]["code"] == "invalid_arguments"
    assert not service._sessions


def test_state_load_uses_the_validated_snapshot(setup):
    service, context, _, client = opened(setup)
    path = context.workspace / "state.json"
    saved = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://example.com",
                "localStorage": [{"name": "fixture", "value": "safe"}],
            }
        ],
    }
    path.write_text(json.dumps(saved))
    client.hook = lambda _: path.write_text("invalid after validation")
    result = service.handle(context, {"action": "state_load", "path": str(path)})
    assert result["ok"]
    command = next(command for command in client.calls if command[:2] == ["state", "load"])
    assert json.loads(Path(command[2]).read_text()) == saved


@pytest.mark.parametrize(
    "args",
    [
        {"action": "route", "pattern": "--abort", "body": "{}"},
        {"action": "route", "pattern": "**/*", "body": "NaN"},
        {"action": "eval", "script": 123},
        {"action": "resize", "width": True, "height": 720},
        {"action": "console", "offset": 1},
        {"action": "state_load", "path": "relative.json"},
    ],
)
def test_invalid_debug_arguments_never_start_browser(setup, args):
    service, context, *_ = setup
    result = service.handle(context, args)
    assert result["error"]["code"] == "invalid_arguments"
    assert not service._sessions


@pytest.mark.parametrize("name", list(_cases()))
def test_complete_model_case_matrix_runtime_results(setup, name):
    service, context, session, _ = opened(setup)
    args = json.loads(json.dumps(_cases()[name]))
    refs = list(session.refs)
    if "target" in args:
        args["target"] = refs[0]
    if "destination" in args:
        args["destination"] = refs[1]
    if "fields" in args:
        for index, item in enumerate(args["fields"]):
            item["target"] = refs[index]
    if "files" in args:
        path = context.workspace / "fixture.txt"
        path.write_text("fixture")
        args["files"] = [str(path)]
    if args["action"] == "result":
        saved = service._diagnostic_result(session, {"value": "x" * 10000}, {"limit": 10})
        args["result_id"] = saved["result_id"]
    if args["action"] in {"trace_stop", "har_stop"}:
        service.handle(context, {"action": args["action"].replace("_stop", "_start")})
    if args["action"] == "state_load":
        saved = service.handle(context, {"action": "state_save"})["data"]
        args["path"] = saved["file"]["path"]
    result = service.handle(context, args)
    if name.startswith("invalid_"):
        assert not result["ok"] and result["error"]["code"] == "invalid_arguments"
    else:
        assert result["ok"], result


@pytest.mark.parametrize("version", ["agent-browser 0.33.2", "garbage"])
def test_unsupported_backend_never_opens_browser(setup, monkeypatch, version):
    service, context, *_ = setup
    monkeypatch.setattr(FakeClient, "version", lambda self: version)
    result = service.handle(context, {"action": "open", "url": "about:blank"})
    assert result["error"]["code"] == "browser_unavailable"
    assert not next(iter(service._sessions.values())).client.calls


def test_idle_cleanup_only_closes_expired_owned_connection(setup):
    service, context, session, client = opened(setup)
    other = service._get_session(replace(context, session_id="other"))
    session.last_used -= 1000
    service._prune(other)
    assert client.calls[-1] == ["close"]
    assert session.key not in service._sessions
    assert other.key in service._sessions


def test_idle_cleanup_rechecks_activity_after_lock_admission(setup):
    service, context, session, client = opened(setup)
    other = service._get_session(replace(context, session_id="other"))
    recent = session.last_used
    session.last_used -= 1000
    original = session.lock

    class RefreshingLock:
        def acquire(self, **kwargs):
            session.last_used = recent
            return original.acquire(**kwargs)

        def release(self):
            original.release()

    session.lock = RefreshingLock()
    try:
        service._prune(other)
        assert session.key in service._sessions
        assert ["close"] not in client.calls
    finally:
        session.lock = original


@pytest.mark.parametrize("mode", ["existing", "remote"])
def test_connected_browser_downloads_are_not_misrepresented_as_server_files(setup, mode):
    service, context, _, config, _ = setup
    config["mode"] = mode
    assert service.handle(context, {"action": "open", "url": "about:blank"})["ok"]
    result = service.handle(context, {"action": "downloads"})
    assert result["error"]["code"] == "browser_local_download"
