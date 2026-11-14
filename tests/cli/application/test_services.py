from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import override

import pytest

from cli.application import services
from cli.application._service_prerequisites import ActionRequiredError, Commands, ContainerRuntime
from cli.application.services import ManagedService
from cli.application.state import ApplicationError, Installation
from core.storage import StorageManager


class ContainerCommands(Commands):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict] = {}
        self.image = "sha256:" + "a" * 64

    @override
    def run(self, args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(args)
        command = args[1:]
        output = ""
        if command[:2] == ["container", "ls"]:
            output = "\n".join(self.containers)
        elif command[:2] == ["container", "inspect"]:
            output = json.dumps([self.containers[command[2]]])
        elif command[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": self.image}])
        elif command[0] == "create":
            name = command[command.index("--name") + 1]
            labels = dict(
                command[i + 1].split("=", 1) for i, part in enumerate(command) if part == "--label"
            )
            self.containers[name] = {
                "Config": {"Labels": labels},
                "State": {"Running": False},
                "Image": command[-1],
            }
        elif command[0] == "rename":
            self.containers[command[2]] = self.containers.pop(command[1])
        elif command[0] in {"start", "stop"}:
            self.containers[command[1]]["State"]["Running"] = command[0] == "start"
        elif command[0] == "rm":
            self.containers.pop(command[-1])
        return subprocess.CompletedProcess(args, 0, output, "")


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install = Installation(
        tmp_path / "install", "server", "127.0.0.1", 22332, str(tmp_path / "data")
    )
    install.root.mkdir()
    commands = ContainerCommands()
    service = ManagedService(install, commands=commands)
    runtime = ContainerRuntime("docker", "docker", ready=True)
    monkeypatch.setattr(services, "discover_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(
        services, "package_command", lambda: ["package-manager", "install", "podman"]
    )
    monkeypatch.setattr(ManagedService, "_probe", staticmethod(lambda url: None))
    return service, commands


def install_service(service: ManagedService) -> str:
    plan = service.status()["plan"]
    service.admit("install", plan_id=plan["id"], allow_prerequisites=False)
    url = service.execute("install", allow_prerequisites=False)
    service.finish()
    assert url is not None
    return url


def test_service_install_is_local_owned_repeatable_and_status_observes_container(managed):
    service, commands = managed
    url = install_service(service)
    creation = next(call for call in commands.calls if call[1] == "create")
    assert creation[creation.index("--publish") + 1] == f"127.0.0.1:{url.rsplit(':', 1)[1]}:8080"
    assert creation[-1] == commands.image
    assert service.status()["running"] is True
    commands.containers[service.name]["State"]["Running"] = False
    assert service.status()["running"] is False
    install_service(service)
    assert len([call for call in commands.calls if call[1] == "create"]) == 1
    configuration = (service.directory / "settings.yml").read_text()
    assert "formats: [html, json]" in configuration
    assert "limiter: false" in configuration


def test_service_requires_reviewed_plan_and_explicit_prerequisite_consent(managed, monkeypatch):
    service, commands = managed
    plan_id = service.status()["plan"]["id"]
    monkeypatch.setattr(services, "discover_runtime", lambda *args, **kwargs: None)
    with pytest.raises(ApplicationError):
        service.admit("install", plan_id=plan_id, allow_prerequisites=True)
    current = service.status()["plan"]
    assert current["requires_prerequisites"]
    with pytest.raises(ActionRequiredError):
        service.admit("install", plan_id=current["id"], allow_prerequisites=False)
    assert not service.record_path.exists()
    assert all(call[1] == "container" for call in commands.calls)


@pytest.mark.parametrize("action", ["install", "start", "stop", "update", "remove"])
def test_service_never_mutates_foreign_container(managed, action):
    service, commands = managed
    commands.containers[service.name] = {"Config": {"Labels": {}}, "State": {"Running": True}}
    with pytest.raises(ApplicationError):
        service.execute(action, allow_prerequisites=False)
    assert commands.containers[service.name]["State"]["Running"] is True
    assert all(call[1] == "container" for call in commands.calls)


def test_service_failed_update_restores_working_container(managed, monkeypatch):
    service, commands = managed
    install_service(service)
    old_image = commands.image
    commands.image = "sha256:" + "b" * 64

    def refuse(url):
        raise ActionRequiredError("Test search unavailable")

    monkeypatch.setattr(ManagedService, "_probe", staticmethod(refuse))
    service.admit("update", plan_id=service.status()["plan"]["id"], allow_prerequisites=False)
    with pytest.raises(ActionRequiredError):
        service.execute("update", allow_prerequisites=False)
    assert commands.containers[service.name]["Image"] == old_image
    assert commands.containers[service.name]["State"]["Running"] is True
    assert service.name + "-previous" not in commands.containers
    assert service.status()["state"] == "action_required"


def test_service_resume_reconciles_interrupted_replacement_and_keeps_unknown_fields(managed):
    service, commands = managed
    install_service(service)
    commands.containers[service.name + "-previous"] = commands.containers.pop(service.name)
    record = service.read()
    service.save(record, state="running", phase="container", future={"retained": True})
    assert service.status()["state"] == "action_required"
    service.execute("install", allow_prerequisites=False)
    service.finish()
    assert service.read()["future"] == {"retained": True}
    assert list(commands.containers) == [service.name]


def test_service_remove_only_removes_owned_containers_and_keeps_runtime(managed):
    service, commands = managed
    install_service(service)
    commands.containers["another-service"] = {"Config": {"Labels": {}}, "State": {"Running": True}}
    service.execute("remove", allow_prerequisites=False)
    service.finish()
    assert list(commands.containers) == ["another-service"]
    assert service.read()["installed"] is False
    assert service.read()["desired_running"] is False
    assert all(
        not (call[1:3] == ["image", "rm"] or call[1] == "machine") for call in commands.calls
    )
    assert (service.directory / "settings.yml").is_file()


@pytest.mark.parametrize(
    "phase,container_possible",
    [
        ("queued", False),
        ("prerequisites", False),
        ("download", False),
        ("container", True),
        ("installed", True),
        ("removed", False),
    ],
)
def test_service_uninstall_requires_runtime_only_for_possible_owned_containers(
    managed, monkeypatch, phase, container_possible
):
    service, _commands = managed
    if phase in {"installed", "removed"}:
        install_service(service)
        if phase == "removed":
            service.admit("remove", plan_id=None, allow_prerequisites=False)
            service.execute("remove", allow_prerequisites=False)
            service.finish()
    else:
        record = service.read()
        service.save(record, action="install", state="failed", phase=phase)
        if phase == "container":
            service.save(record, port=18888)
        # Admission resets the phase even if a prior attempt may have created a container.
        service.admit("install", plan_id=service.status()["plan"]["id"], allow_prerequisites=False)
    monkeypatch.setattr(services, "discover_runtime", lambda *args, **kwargs: None)
    if container_possible:
        with pytest.raises(ApplicationError):
            services.remove_for_uninstall(service.install)
    else:
        services.remove_for_uninstall(service.install)
        assert service.read()["installed"] is False
    assert service.record_path.exists()


@pytest.mark.parametrize(
    "selection",
    [
        "owned",
        "defaults",
        "provider_changed",
        "url_changed",
        "changed_during_patch",
        "already_removed",
        "failed_restore",
    ],
)
def test_service_uninstall_restores_only_the_owned_search_selection(
    managed, monkeypatch, selection
):
    service, commands = managed
    url = install_service(service)
    monkeypatch.setattr(services, "Commands", lambda: commands)
    record = service.read()
    before = (
        {}
        if selection == "defaults"
        else {"provider": "brave", "base_url": "https://prior.example"}
    )
    service.save(record, search_before=before)
    storage = StorageManager(service.install.server_data_directory)
    storage.ensure_directories()
    raw = {
        "web_search": {
            "provider": "searxng",
            "searxng": {"base_url": url, "future_option": "kept"},
        },
        "future_setting": True,
    }
    if selection == "provider_changed":
        raw["web_search"]["provider"] = "tavily"
    elif selection == "url_changed":
        raw["web_search"]["searxng"]["base_url"] = "https://user.example"
    storage.save_settings(raw)
    if selection == "changed_during_patch":
        patch = StorageManager.patch_settings

        def change_first(self, operations, **kwargs):
            raw["web_search"]["provider"] = "tavily"
            storage.save_settings(raw)
            return patch(self, operations, **kwargs)

        monkeypatch.setattr(StorageManager, "patch_settings", change_first)
    elif selection == "already_removed":
        service.admit("remove", plan_id=None, allow_prerequisites=False)
        service.execute("remove", allow_prerequisites=False)
        service.finish()
        monkeypatch.setattr(services, "discover_runtime", lambda *args, **kwargs: None)
    elif selection == "failed_restore":
        original = storage.settings_path.read_text()
        storage.settings_path.write_text("{")
        with pytest.raises(ApplicationError, match="restore web search"):
            services.remove_for_uninstall(service.install)
        assert not commands.containers
        assert service.read()["search_before"] == before
        assert storage.settings_path.read_text() == "{"
        storage.settings_path.write_text(original)
        monkeypatch.setattr(services, "discover_runtime", lambda *args, **kwargs: None)
    services.remove_for_uninstall(service.install)
    actual = json.loads(storage.settings_path.read_text())
    search = actual["web_search"]
    if selection in {"owned", "defaults", "already_removed", "failed_restore"}:
        assert search.get("provider") == before.get("provider")
        assert search["searxng"].get("base_url") == before.get("base_url")
        assert "search_before" not in service.read()
    else:
        assert search == raw["web_search"]
    assert search["searxng"]["future_option"] == "kept"
    assert actual["future_setting"] is True
    assert not commands.containers
