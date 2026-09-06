"""Browser grants, lifecycle, scoped references, transport, and partial effects."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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
        if command[:2] == ["tab", "list"]:
            return {"tabs": self.tab_rows}
        if command[0] == "tab" and len(command) == 2 and len(command[1]) == 32:
            for row in self.tab_rows:
                row["active"] = row["targetId"] == command[1]
            return {"targetId": command[1]}
        if command[0] == "snapshot":
            return {
                "snapshot": self.tree,
                "origin": "https://example.com",
                "refs": {"e1": {}, "e2": {}, "e3": {}},
            }
        if command[:2] == ["get", "text"]:
            return {"text": "0123456789" * 3000}
        if command[0] == "screenshot":
            Path(command[1]).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
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


@pytest.mark.parametrize("action", ["close", "fill", "snapshot", "read", "downloads"])
def test_fresh_nonopening_actions_do_not_install_components(setup, monkeypatch, action):
    service, context, *_ = setup
    monkeypatch.setattr(service.runtime, "ensure", lambda *args: pytest.fail("unexpected setup"))
    arguments = {"action": action}
    if action == "fill":
        arguments["fields"] = [{"target": "stale", "text": "value"}]
    result = service.handle(context, arguments)
    assert result["ok"] is (action == "close")
    assert not service._sessions


def test_denied_agent_never_prepares_components(setup, monkeypatch):
    service, context, agent, *_ = setup
    agent.tool_access = ToolAccess(mode="all")
    monkeypatch.setattr(service.runtime, "ensure", lambda *args: pytest.fail("unexpected setup"))
    assert service.handle(context, {"action": "tabs"})["error"]["code"] == "browser_denied"


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
def test_mid_fill_failure_stops_remaining_fields_and_reports_partial(setup, failure):
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
        if command[:2] == ["fill", "@e2"] and failure == "failed":
            raise browser.BrowserError("failed")

    client.hook = hook
    client.calls.clear()
    result = service.handle(
        context,
        {
            "action": "fill",
            "fields": [{"target": ref, "text": str(index)} for index, ref in enumerate(refs)],
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


def test_failed_close_reply_is_reconciled_without_repeating_close(setup):
    service, context, session, client = opened(setup)
    target = "A" * 32

    def hook(command):
        if command[:2] == ["tab", "close"]:
            client.tab_rows = []
            raise browser.BrowserError("failed")

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


@pytest.mark.parametrize("name", list(_cases()))
def test_complete_model_case_matrix_runtime_results(setup, name):
    service, context, session, _ = opened(setup)
    args = json.loads(json.dumps(_cases()[name]))
    refs = list(session.refs)
    if "target" in args:
        args["target"] = refs[0]
    if "fields" in args:
        for index, item in enumerate(args["fields"]):
            item["target"] = refs[index]
    if "files" in args:
        path = context.workspace / "fixture.txt"
        path.write_text("fixture")
        args["files"] = [str(path)]
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
