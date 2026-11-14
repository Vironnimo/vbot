from __future__ import annotations

import json
import subprocess
from typing import override

import pytest

from cli.application import _service_prerequisites as prerequisites
from cli.application._service_prerequisites import Commands, ContainerRuntime


class RuntimeCommands(Commands):
    def __init__(self, endpoint="unix:///var/run/docker.sock"):
        self.endpoint = endpoint
        self.calls = []

    @override
    def run(self, args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(args)
        output = "{}"
        if args[1:3] == ["context", "inspect"]:
            output = json.dumps(
                [{"Name": "local", "Endpoints": {"docker": {"Host": self.endpoint}}}]
            )
        elif args[1:3] == ["machine", "list"]:
            output = "[]"
        return subprocess.CompletedProcess(args, 0, output, "")


@pytest.mark.parametrize(
    "endpoint,accepted",
    [
        ("unix:///var/run/docker.sock", True),
        ("npipe:////./pipe/docker_engine", True),
        (r"npipe://\\.\pipe\docker_engine", True),
        ("npipe:////another-host/pipe/docker_engine", False),
        (r"npipe://\\another-host\pipe\docker_engine", False),
        ("npipe://another-host/pipe/docker_engine", False),
        ("npipe:////localhost/pipe/docker_engine", False),
        ("npipe:////./pipe/docker_engine%2fremote", False),
        ("unix://another-host/var/run/docker.sock", False),
        ("ssh://another.example", False),
        ("tcp://127.0.0.1:2375", False),
    ],
)
def test_runtime_discovery_only_uses_local_container_endpoints(monkeypatch, endpoint, accepted):
    monkeypatch.setattr(
        prerequisites, "executable", lambda name: name if name == "docker" else None
    )
    commands = RuntimeCommands(endpoint)
    found = prerequisites.discover_runtime(commands)
    assert (found is not None) is accepted
    assert any("info" in call for call in commands.calls) is accepted


def test_windows_machine_setup_preserves_default_connection(monkeypatch):
    monkeypatch.setattr(prerequisites.sys, "platform", "win32")
    monkeypatch.setattr(prerequisites, "executable", lambda name: name)
    commands = RuntimeCommands()
    runtime = prerequisites.prepare_runtime(
        commands, ContainerRuntime("podman", "podman"), machine="vbot-test"
    )
    assert runtime.options == ("--connection", "vbot-test")
    init = next(call for call in commands.calls if call[1:3] == ["machine", "init"])
    start = next(call for call in commands.calls if call[1:3] == ["machine", "start"])
    assert "--update-connection=false" in init
    assert "--update-connection=false" in start
    assert all(call[-1] == "vbot-test" for call in [init, start])


def test_existing_windows_docker_is_started_with_a_bounded_desktop_command(monkeypatch):
    monkeypatch.setattr(prerequisites.sys, "platform", "win32")
    monkeypatch.setattr(
        prerequisites,
        "discover_runtime",
        lambda *args, **kwargs: ContainerRuntime("docker", "docker", ready=True),
    )
    commands = RuntimeCommands()
    runtime = prerequisites.prepare_runtime(
        commands, ContainerRuntime("docker", "docker"), machine="vbot-test"
    )
    assert runtime.ready
    assert commands.calls == [["docker", "desktop", "start", "--timeout", "120"]]
