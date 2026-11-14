"""Setup entry points preserve the server-owned lifecycle and explicit interaction."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from cli import _dispatch_service, _dispatch_setup
from cli import main as cli_main
from cli._server_target import CommandResult, RpcFailure
from cli.application import _service_prerequisites, processes
from cli.rpc_client import RpcPayload
from tests.cli.cli_test_support import make_instance


@pytest.mark.parametrize("interactive", [False, True])
def test_setup_terminal_requires_a_terminal_and_reuses_the_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interactive: bool,
) -> None:
    instance = make_instance(tmp_path)
    opened = []

    async def open_setup(**kwargs: Any) -> int:
        opened.append(kwargs)
        return 0

    monkeypatch.setitem(sys.modules, "cli.tui.setup", SimpleNamespace(run_setup=open_setup))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: interactive)
    starter = Mock(return_value=True)
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", starter)
    code = cli_main.run(["setup"], resolve=lambda **_kwargs: instance)
    output = capsys.readouterr()
    assert code == (0 if interactive else 2)
    assert opened == ([{"target": instance}] if interactive else [])
    assert starter.call_count == int(interactive)
    if interactive:
        assert not output.err


@pytest.mark.parametrize("command,method", [("status", "setup.get"), ("finish", "setup.finish")])
@pytest.mark.parametrize("succeeds", [False, True])
def test_setup_scripted_commands_use_one_rpc_and_json_without_opening_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    method: str,
    succeeds: bool,
) -> None:
    instance = make_instance(tmp_path)
    data = {"status": "skipped", "step": "provider", "agent_id": "main", "auto_show": False}
    request = Mock(
        return_value=RpcPayload(
            ok=succeeds,
            instance=instance,
            data=data,
            message="test-owned refusal",
            failure=None if succeeds else RpcFailure(method, "responded", "invalid_request"),
        )
    )
    monkeypatch.setattr(_dispatch_setup, "rpc_call", request)
    starter = Mock(side_effect=AssertionError("scripted setup never starts a server"))
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", starter)
    code = cli_main.run(["setup", command, "--json"], resolve=lambda **_kwargs: instance)
    value = json.loads(capsys.readouterr().out)
    assert code == (0 if succeeds else 1)
    assert value["ok"] is succeeds
    if succeeds:
        assert value["result"] == data
    else:
        assert value["error"]["code"] == "invalid_request"
    request.assert_called_once_with(instance, method, {})
    starter.assert_not_called()


@pytest.mark.parametrize("authenticated", [False, True])
def test_setup_browser_signs_in_without_exposing_the_ticket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    authenticated: bool,
) -> None:
    instance = make_instance(tmp_path)
    path = "/auth/ticket/test_ticket_abcdefghijklmnop"
    request = Mock(
        return_value=RpcPayload(
            ok=authenticated,
            instance=instance,
            data={"path": path},
            failure=None
            if authenticated
            else RpcFailure("auth.login_ticket", "responded", "auth_disabled"),
        )
    )
    opened = Mock(return_value=True)
    monkeypatch.setattr(_dispatch_setup, "rpc_call", request)
    monkeypatch.setattr(_dispatch_setup.webbrowser, "open", opened)
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", lambda _instance: True)
    monkeypatch.setattr(processes, "owning_installation", lambda _instance: None)
    code = cli_main.run(["setup", "--browser"], resolve=lambda **_kwargs: instance)
    output = capsys.readouterr()
    assert code == 0
    opened.assert_called_once_with(instance.url + (path if authenticated else "/?setup=1"))
    request.assert_called_once_with(
        instance, "auth.login_ticket", {"name": "Browser", "next": "/?setup=1"}
    )
    assert path not in output.out + output.err


@pytest.mark.parametrize("owned", [False, True])
def test_setup_browser_uses_the_owned_public_origin_only_for_the_exact_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], owned: bool
) -> None:
    instance = make_instance(tmp_path)
    selected = instance if owned else replace(instance, url="https://remote.invalid")
    installation = SimpleNamespace(
        access=SimpleNamespace(mode="authenticated", public_url="https://owned.invalid")
    )
    monkeypatch.setattr(processes, "owning_installation", lambda _instance: installation)
    monkeypatch.setattr(processes, "target", lambda _install: instance)
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", lambda _instance: True)
    path = "/auth/ticket/test_ticket_abcdefghijklmnop"
    monkeypatch.setattr(
        _dispatch_setup,
        "rpc_call",
        lambda *_args: RpcPayload(ok=True, instance=selected, data={"path": path}),
    )
    opened = Mock(return_value=True)
    monkeypatch.setattr(_dispatch_setup.webbrowser, "open", opened)
    assert cli_main.run(["setup", "--browser"], resolve=lambda **_kwargs: selected) == 0
    opened.assert_called_once_with(("https://owned.invalid" if owned else selected.url) + path)
    assert path not in capsys.readouterr().out


@pytest.mark.parametrize("if_needed", [False, True])
def test_setup_browser_refuses_a_run_before_starting_or_issuing_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    if_needed: bool,
) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "agent")
    starter = Mock(side_effect=AssertionError("no side effect before refusal"))
    request = Mock(side_effect=AssertionError("no sign-in ticket in a Run"))
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", starter)
    monkeypatch.setattr(_dispatch_setup, "rpc_call", request)
    code = cli_main.run(
        ["setup", "--browser", *(["--if-needed"] if if_needed else [])],
        resolve=lambda **_kwargs: make_instance(tmp_path),
    )
    assert code == 2
    starter.assert_not_called()
    request.assert_not_called()
    capsys.readouterr()


@pytest.mark.parametrize(
    "path", ["https://other.invalid/ticket", "/auth/ticket/short", "/?secret=value"]
)
def test_setup_browser_refuses_untrusted_sign_in_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", lambda _instance: True)
    monkeypatch.setattr(
        _dispatch_setup,
        "rpc_call",
        lambda *_args: RpcPayload(ok=True, instance=instance, data={"path": path}),
    )
    opened = Mock()
    monkeypatch.setattr(_dispatch_setup.webbrowser, "open", opened)
    assert cli_main.run(["setup", "--browser"], resolve=lambda **_kwargs: instance) == 1
    opened.assert_not_called()
    assert path not in capsys.readouterr().out


@pytest.mark.parametrize("owned", [False, True])
def test_setup_starts_only_the_exact_installed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], owned: bool
) -> None:
    instance = make_instance(tmp_path)
    installation = object()
    monkeypatch.setattr(processes, "owning_installation", lambda _instance: installation)
    monkeypatch.setattr(processes, "target", lambda _install: instance)
    starter = Mock(
        return_value=CommandResult(ok=True, instance=instance, message="already running")
    )
    monkeypatch.setattr(processes, "start_or_restart", starter)
    selected = (
        instance
        if owned
        else replace(instance, url="https://another.invalid", host="another.invalid")
    )
    assert _dispatch_setup._start_owned_server(selected)
    assert starter.call_count == int(owned)
    capsys.readouterr()


@pytest.mark.parametrize(
    "arguments",
    [
        ["status", "--browser"],
        ["finish", "--browser"],
        ["status", "--if-needed"],
        ["finish", "--if-needed"],
        ["--json"],
    ],
)
def test_setup_rejects_conflicting_interaction_modes(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli_main.run(["setup", *arguments])
    assert error.value.code == 2


@pytest.mark.parametrize("status", ["pending", "completed", "skipped"])
@pytest.mark.parametrize("browser", [False, True])
def test_setup_if_needed_opens_only_pending_setup_and_skips_without_a_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    browser: bool,
) -> None:
    instance = make_instance(tmp_path)
    opened = []

    async def open_setup(**kwargs: Any) -> int:
        opened.append(kwargs)
        return 0

    monkeypatch.setitem(sys.modules, "cli.tui.setup", SimpleNamespace(run_setup=open_setup))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: status == "pending")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: status == "pending")
    starter = Mock(return_value=True)
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", starter)
    browser_open = Mock(return_value=0)
    monkeypatch.setattr(_dispatch_setup, "_open_browser", browser_open)
    request = Mock(
        return_value=RpcPayload(
            ok=True,
            instance=instance,
            data={"status": status, "auto_show": status == "pending"},
        )
    )
    monkeypatch.setattr(_dispatch_setup, "rpc_call", request)
    argv = ["setup", "--if-needed", *(["--browser"] if browser else [])]
    assert cli_main.run(argv, resolve=lambda **_kwargs: instance) == 0
    starter.assert_called_once_with(instance)
    request.assert_called_once_with(instance, "setup.get", {})
    assert opened == ([{"target": instance}] if status == "pending" and not browser else [])
    if status == "pending" and browser:
        browser_open.assert_called_once_with(instance, start_server=False)
    else:
        browser_open.assert_not_called()
    output = capsys.readouterr()
    if status != "pending":
        assert status in output.out
        assert instance.url in output.out


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"status": "pending", "auto_show": False},
        {"status": "completed", "auto_show": True},
        {"status": "skipped", "auto_show": "false"},
        {"status": "unknown", "auto_show": False},
        None,
    ],
)
def test_setup_if_needed_refuses_missing_or_inconsistent_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: dict[str, Any] | None,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(_dispatch_setup, "_start_owned_server", lambda _instance: True)
    monkeypatch.setattr(
        _dispatch_setup,
        "rpc_call",
        lambda *_args: RpcPayload(
            ok=data is not None,
            instance=instance,
            data=data or {},
            message="test-owned RPC failure",
        ),
    )
    browser_open = Mock(return_value=0)
    monkeypatch.setattr(_dispatch_setup, "_open_browser", browser_open)
    assert (
        cli_main.run(["setup", "--if-needed", "--browser"], resolve=lambda **_kwargs: instance) == 1
    )
    browser_open.assert_not_called()
    assert "already" not in capsys.readouterr().out


@pytest.mark.parametrize("allow", [False, True])
def test_service_install_uses_exact_plan_and_explicit_prerequisite_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], allow: bool
) -> None:
    instance = make_instance(tmp_path)
    request = Mock(
        side_effect=[
            RpcPayload(ok=True, instance=instance, data={"plan": {"id": "plan-one"}}),
            RpcPayload(ok=True, instance=instance, data={"id": "searxng", "state": "running"}),
        ]
    )
    monkeypatch.setattr(_dispatch_service, "rpc_call", request)
    argv = ["--output", "plain", "service", "install", "searxng", "--json"]
    if allow:
        argv.append("--install-prerequisites")
    assert cli_main.run(argv, resolve=lambda **_kwargs: instance) == 0
    assert request.call_args_list[0].args == (instance, "managed_service.get", {"id": "searxng"})
    assert request.call_args_list[1].args == (
        instance,
        "managed_service.apply",
        {"id": "searxng", "action": "install", "plan_id": "plan-one", "allow_prerequisites": allow},
    )
    assert json.loads(capsys.readouterr().out)["result"]["state"] == "running"


@pytest.mark.parametrize(
    "restriction", ["no_consent", "no_terminal", "remote", "unowned", "agent_run", "json"]
)
def test_service_local_authorization_needs_explicit_local_human_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restriction: str,
) -> None:
    instance = make_instance(tmp_path)
    selected = (
        instance if restriction != "remote" else replace(instance, url="https://remote.invalid")
    )
    installation = None if restriction == "unowned" else object()
    monkeypatch.setattr(processes, "owning_installation", lambda _instance: installation)
    monkeypatch.setattr(processes, "target", lambda _install: instance)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: restriction != "no_terminal")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: restriction != "no_terminal")
    if restriction == "agent_run":
        monkeypatch.setenv("VBOT_RUN_AGENT_ID", "agent")
    install = Mock()
    monkeypatch.setattr(_service_prerequisites, "install_local_prerequisites", install)
    request = Mock(
        return_value=RpcPayload(
            ok=True,
            instance=selected,
            data={
                "plan": {
                    "id": "plan-one",
                    "requires_prerequisites": True,
                    "local_authorization": True,
                    "prerequisite_command": ["sudo", "/usr/bin/apt-get", "install", "-y", "podman"],
                }
            },
        )
    )
    monkeypatch.setattr(_dispatch_service, "rpc_call", request)
    argv = ["service", "install", "searxng"]
    if restriction != "no_consent":
        argv.append("--install-prerequisites")
    if restriction == "json":
        argv.append("--json")
    assert cli_main.run(argv, resolve=lambda **_kwargs: selected) == 1
    install.assert_not_called()
    request.assert_called_once_with(selected, "managed_service.get", {"id": "searxng"})
    output = capsys.readouterr()
    assert "sudo /usr/bin/apt-get install -y podman" in output.out
    assert "action_required" in output.out
    if restriction == "json":
        assert json.loads(output.out)["result"]["state"] == "action_required"


def test_service_local_authorization_refreshes_plan_after_fixed_host_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(processes, "owning_installation", lambda _instance: object())
    monkeypatch.setattr(processes, "target", lambda _install: instance)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    install = Mock()
    monkeypatch.setattr(_service_prerequisites, "install_local_prerequisites", install)
    request = Mock(
        side_effect=[
            RpcPayload(
                ok=True,
                instance=instance,
                data={
                    "plan": {
                        "id": "old-plan",
                        "requires_prerequisites": True,
                        "local_authorization": True,
                        "prerequisite_command": ["untrusted-server-command"],
                    }
                },
            ),
            RpcPayload(ok=True, instance=instance, data={"plan": {"id": "fresh-plan"}}),
            RpcPayload(ok=True, instance=instance, data={"state": "running"}),
        ]
    )
    monkeypatch.setattr(_dispatch_service, "rpc_call", request)
    assert (
        cli_main.run(
            ["service", "install", "searxng", "--install-prerequisites"],
            resolve=lambda **_kwargs: instance,
        )
        == 0
    )
    install.assert_called_once_with()
    assert request.call_args_list[1].args == (instance, "managed_service.get", {"id": "searxng"})
    assert request.call_args_list[2].args == (
        instance,
        "managed_service.apply",
        {
            "id": "searxng",
            "action": "install",
            "plan_id": "fresh-plan",
            "allow_prerequisites": True,
        },
    )
    output = capsys.readouterr()
    assert "running" in output.out
    assert "service status searxng" in output.out
    assert not output.err
