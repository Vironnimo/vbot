"""Installation-owned SearXNG lifecycle and its owned search selection."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import time
from typing import Any

from cli.application._service_prerequisites import (
    ActionRequiredError,
    Commands,
    ContainerRuntime,
    discover_runtime,
    local_package_command,
    needs_local_authorization,
    package_command,
    prepare_runtime,
)
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    exclusive,
    read_json,
    write_json,
)
from core.utils.atomic import atomic_write_text
from core.utils.file_status import exists_strict
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
_LOGGER = get_logger("application.services")
IMAGE = "docker.io/searxng/searxng:latest"
OWNER_LABEL = "io.vbot.installation"
SERVICE_LABEL = "io.vbot.service"
ACTIONS = frozenset({"install", "start", "stop", "update", "remove"})


class ManagedService:
    """Own only the container whose labels match this exact installation."""

    def __init__(self, install: Installation, *, commands: Commands | None = None) -> None:
        self.install = install
        self.commands = commands or Commands()
        self.directory = contained(install.root, "services/searxng")
        self.record_path = contained(install.root, "services/searxng/service.json")
        self.owner = hashlib.sha256(
            os.path.normcase(str(install.root.resolve())).encode()
        ).hexdigest()[:20]
        self.name = f"vbot-searxng-{self.owner}"
        self.machine = f"vbot-{self.owner}"

    def read(self) -> JsonObject:
        if not exists_strict(self.record_path):
            return {"schema": 1, "state": "idle", "installed": False}
        record = read_json(self.record_path)
        if record.get("schema") != 1 or record.get("state") not in {
            "idle",
            "running",
            "action_required",
            "failed",
            "completed",
        }:
            raise ApplicationError("The managed SearXNG record is invalid; it was not changed")
        if record.get("owner", self.owner) != self.owner:
            raise ApplicationError("The managed SearXNG record belongs to another installation")
        for field in ("installed", "running", "desired_running"):
            if field in record and not isinstance(record[field], bool):
                raise ApplicationError(f"The managed SearXNG {field} field is invalid")
        if "port" in record and (
            type(record["port"]) is not int or not 1024 <= record["port"] <= 65535
        ):
            raise ApplicationError("The managed SearXNG port is invalid")
        if "runtime" in record and record["runtime"] not in {"docker", "podman"}:
            raise ApplicationError("The managed SearXNG runtime is invalid")
        return record

    def save(self, record: JsonObject, **fields: Any) -> None:
        # Preserve fields a later version wrote, as all application-owned records do.
        record.update(fields)
        record.update(schema=1, owner=self.owner)
        self.directory.mkdir(parents=True, exist_ok=True)
        write_json(self.record_path, record)

    def _runtime(self, record: JsonObject) -> ContainerRuntime | None:
        saved = record.get("runtime_options")
        if saved is not None and not (
            isinstance(saved, list)
            and all(isinstance(item, str) for item in saved)
            and (
                saved in ([], ["--remote=false"])
                or len(saved) == 2
                and saved[0] in {"--context", "--connection"}
            )
        ):
            raise ApplicationError("The managed service runtime target is invalid")
        runtime = discover_runtime(
            self.commands,
            preferred=record.get("runtime", ""),
            saved_options=tuple(saved) if saved else None,
        )
        if (
            runtime is not None
            and runtime.kind == "podman"
            and record.get("machine") == self.machine
        ):
            runtime = ContainerRuntime("podman", runtime.executable, ("--connection", self.machine))
            ready = self.commands.run(runtime.command("info", "--format", "json")).returncode == 0
            return ContainerRuntime(runtime.kind, runtime.executable, runtime.options, ready)
        return runtime

    def status(self, *, active: bool = False) -> JsonObject:
        record = self.read()
        runtime = self._runtime(record)
        prerequisites = runtime is None or not runtime.ready
        steps = []
        if runtime is None:
            steps.append(
                {"id": "runtime", "label": "Install Podman using the system package manager"}
            )
        if prerequisites:
            steps.append(
                {
                    "id": "runtime_start",
                    "label": (
                        "Prepare the local container runtime; Windows can request WSL "
                        "2 and a restart"
                    ),
                }
            )
        steps.extend(
            [
                {
                    "id": "container",
                    "label": (
                        "Download SearXNG and create a vBot-owned container bound "
                        "only to this computer"
                    ),
                },
                {
                    "id": "configure",
                    "label": (
                        "Enable JSON search, run a test search, and select SearXNG "
                        "for vBot web search"
                    ),
                },
            ]
        )
        fingerprint = json.dumps(
            {
                "runtime": runtime.kind if runtime else None,
                "executable": runtime.executable if runtime else None,
                "options": runtime.options if runtime else None,
                "ready": runtime.ready if runtime else False,
                "installed": record.get("installed", False),
                "image": IMAGE,
                "package": package_command() if runtime is None else None,
            },
            sort_keys=True,
        )
        phase = record.get("phase", "")
        state = record["state"]
        message = record.get("message", "")
        if state == "running" and not active:
            state = "action_required"
            message = (
                "The previous operation was interrupted. Resume it to reconcile the "
                "existing service."
            )
        container = (
            self._container(runtime)
            if runtime is not None and runtime.ready and not active
            else None
        )
        running = bool(container and container.get("State", {}).get("Running"))
        health = container.get("State", {}).get("Health", {}).get("Status") if container else None
        return {
            "id": "searxng",
            "name": "SearXNG",
            "available": True,
            "installed": record.get("installed", False),
            "state": state,
            "phase": phase,
            "message": message,
            "error": record.get("error"),
            "url": self.url(record) if "port" in record else None,
            "runtime": runtime.kind if runtime else None,
            "running": running if not active else record.get("running", False),
            "health": health,
            "plan": {
                "id": hashlib.sha256(fingerprint.encode()).hexdigest(),
                "steps": steps,
                "requires_prerequisites": prerequisites,
                "can_install": runtime is not None or package_command() is not None,
                "local_authorization": runtime is None and needs_local_authorization(),
                "prerequisite_command": local_package_command() if runtime is None else None,
            },
            "actions": (
                ["start", "stop", "update", "remove"] if record.get("installed") else ["install"]
            ),
        }

    @staticmethod
    def url(record: JsonObject) -> str:
        return f"http://127.0.0.1:{record['port']}"

    def admit(self, action: str, *, plan_id: str | None, allow_prerequisites: bool) -> JsonObject:
        if action not in ACTIONS:
            raise ApplicationError("Unknown managed service action")
        with exclusive(self.install.root, "operation", timeout=5):
            status = self.status()
            plan = status["plan"]
            if action in {"install", "update"}:
                if plan_id != plan["id"]:
                    raise ApplicationError(
                        "The installation plan changed. Review the current plan before continuing"
                    )
                if plan["requires_prerequisites"] and not allow_prerequisites:
                    raise ActionRequiredError(
                        "Approve the displayed container prerequisites, or keep the "
                        "current web search settings"
                    )
                if not plan["can_install"]:
                    raise ActionRequiredError(
                        "Install Docker or Podman on this computer, then resume setup"
                    )
            record = self.read()
            self.save(
                record,
                state="running",
                phase="queued",
                action=action,
                error=None,
                message="Waiting to start the selected service operation",
            )
            return record

    def transition(self, record: JsonObject, phase: str, message: str) -> None:
        self.save(record, phase=phase, message=message)
        _LOGGER.info(
            "Managed service operation advanced (service=searxng action=%s phase=%s)",
            record.get("action"),
            phase,
        )

    def execute(self, action: str, *, allow_prerequisites: bool) -> str | None:
        """Reconcile one admitted operation; return the tested URL for Settings coordination."""
        with exclusive(self.install.root, "operation", timeout=5):
            record = self.read()
            try:
                runtime = self._runtime(record)
                if (
                    action == "start"
                    and runtime is not None
                    and not runtime.ready
                    and record.get("machine") == self.machine
                ):
                    # Starting an already-created owned VM installs no prerequisites.
                    self.commands.checked(
                        [
                            runtime.executable,
                            "machine",
                            "start",
                            "--update-connection=false",
                            self.machine,
                        ],
                        timeout=300,
                    )
                    runtime = self._runtime(record)
                if action in {"install", "update"} and (runtime is None or not runtime.ready):
                    if not allow_prerequisites:
                        raise ActionRequiredError(
                            "The container runtime changed. Review and approve its "
                            "prerequisites before resuming"
                        )
                    self.transition(
                        record,
                        "prerequisites",
                        "Preparing the container runtime; approve any system authorization dialog",
                    )
                    runtime = prepare_runtime(self.commands, runtime, machine=self.machine)
                    self.save(
                        record,
                        runtime=runtime.kind,
                        machine=self.machine
                        if runtime.options == ("--connection", self.machine)
                        else "",
                    )
                if runtime is None or not runtime.ready:
                    raise ActionRequiredError(
                        "Start the container runtime, then resume the selected operation"
                    )
                self.save(record, runtime=runtime.kind, runtime_options=list(runtime.options))
                if action == "remove":
                    self._remove(runtime)
                    self.save(record, installed=False, running=False, desired_running=False)
                    return None
                if action == "stop":
                    if self._container(runtime) is not None:
                        self.commands.checked(runtime.command("stop", self.name), timeout=60)
                    self.save(record, running=False, desired_running=False)
                    return None
                if action == "start":
                    if self._container(runtime) is None:
                        raise ActionRequiredError(
                            "The managed container is missing. Install SearXNG again"
                        )
                    self.commands.checked(runtime.command("start", self.name), timeout=60)
                    self._probe(self.url(record))
                    self.save(record, running=True, desired_running=True)
                    return self.url(record)
                return self._install(runtime, record, update=action == "update")
            except ActionRequiredError as exc:
                self.save(record, state="action_required", error=None, message=str(exc))
                _LOGGER.warning(
                    "Managed service requires operator action (service=searxng action=%s)", action
                )
                raise
            except Exception:
                self.save(
                    record,
                    state="failed",
                    error="The service operation failed. Inspect the vBot logs and resume.",
                    message="The service operation failed",
                )
                _LOGGER.exception(
                    "Managed service operation failed (service=searxng action=%s)", action
                )
                raise

    def finish(self, *, error: str | None = None) -> None:
        record = self.read()
        self.save(
            record,
            state="failed" if error else "completed",
            phase="complete",
            error=error,
            message=error or "The service operation completed",
        )

    def _container(self, runtime: ContainerRuntime, name: str | None = None) -> JsonObject | None:
        name = name or self.name
        names = self.commands.checked(
            runtime.command("container", "ls", "--all", "--format", "{{.Names}}")
        )
        if name not in names.splitlines():
            return None
        result = json.loads(self.commands.checked(runtime.command("container", "inspect", name)))
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise ApplicationError("The container runtime returned an invalid inspection")
        container = result[0]
        labels = container.get("Config", {}).get("Labels", {})
        if labels.get(OWNER_LABEL) != self.owner or labels.get(SERVICE_LABEL) != "searxng":
            raise ApplicationError(
                "A container with this name is not owned by this vBot installation; it was "
                "not changed"
            )
        return container

    def _remove(self, runtime: ContainerRuntime) -> None:
        owned = [
            name
            for name in (self.name, self.name + "-previous")
            if self._container(runtime, name) is not None
        ]
        for name in owned:
            self.commands.checked(runtime.command("rm", "--force", name), timeout=60)

    def _install(self, runtime: ContainerRuntime, record: JsonObject, *, update: bool) -> str:
        previous_name = self.name + "-previous"
        current = self._container(runtime)
        previous = self._container(runtime, previous_name)
        # Reconcile a crash between rename/create/probe without deleting the working baseline.
        if previous is not None:
            if current is not None:
                self.commands.checked(runtime.command("rm", "--force", self.name), timeout=60)
            self.commands.checked(runtime.command("rename", previous_name, self.name))
            self.commands.checked(runtime.command("start", self.name), timeout=60)
            current = self._container(runtime)
        if current is not None and not update:
            self.commands.checked(runtime.command("start", self.name), timeout=60)
            self._probe(self.url(record))
            self.save(record, installed=True, running=True, desired_running=True)
            return self.url(record)
        self.transition(record, "download", "Downloading the SearXNG container image")
        self.commands.checked(runtime.command("pull", IMAGE), timeout=900)
        images = json.loads(self.commands.checked(runtime.command("image", "inspect", IMAGE)))
        image = images[0].get("Id") or images[0].get("ID")
        if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
            raise ApplicationError(
                "The container runtime returned no immutable SearXNG image identity"
            )
        if "port" not in record:
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                self.save(record, port=candidate.getsockname()[1])
        config = contained(self.install.root, "services/searxng/settings.yml")
        if not exists_strict(config):
            atomic_write_text(
                config,
                "use_default_settings: true\nsearch:\n  formats: [html, json]\n"
                "server:\n  secret_key: "
                + secrets.token_hex(32)
                + "\n  limiter: false\n  image_proxy: false\n",
                mode=0o644,
            )
        self.transition(record, "container", "Configuring the managed SearXNG container")
        if current is not None:
            self.commands.checked(runtime.command("stop", self.name), timeout=60)
            self.commands.checked(runtime.command("rename", self.name, previous_name))
        try:
            self.commands.checked(
                runtime.command(
                    "create",
                    "--name",
                    self.name,
                    "--label",
                    f"{OWNER_LABEL}={self.owner}",
                    "--label",
                    f"{SERVICE_LABEL}=searxng",
                    "--restart",
                    "unless-stopped",
                    "--publish",
                    f"127.0.0.1:{record['port']}:8080",
                    "--env",
                    f"SEARXNG_BASE_URL={self.url(record)}/",
                    image,
                ),
                timeout=60,
            )
            # Copy works with Docker Desktop and Podman's local VM too; host bind paths do not.
            self.commands.checked(
                runtime.command("cp", str(config), f"{self.name}:/etc/searxng/settings.yml")
            )
            self.commands.checked(runtime.command("start", self.name), timeout=60)
            self.transition(record, "verify", "Checking the JSON search API with a test search")
            self._probe(self.url(record))
        except Exception:
            if self._container(runtime) is not None:
                self.commands.checked(runtime.command("rm", "--force", self.name), timeout=60)
            if current is not None:
                self.commands.checked(runtime.command("rename", previous_name, self.name))
                self.commands.checked(runtime.command("start", self.name), timeout=60)
            raise
        if current is not None:
            self.commands.checked(runtime.command("rm", previous_name), timeout=60)
        self.save(record, installed=True, running=True, desired_running=True, image=image)
        return self.url(record)

    @staticmethod
    def _probe(url: str) -> None:
        import httpx

        from core.utils.tls import ssl_context_for

        deadline = time.monotonic() + 60
        with httpx.Client(trust_env=False, timeout=15, verify=ssl_context_for(url)) as client:
            while True:
                try:
                    response = client.get(
                        url + "/search", params={"q": "SearXNG", "format": "json"}
                    )
                    response.raise_for_status()
                    value = response.json()
                    if isinstance(value, dict) and isinstance(value.get("results"), list):
                        if value["results"] or not value.get("unresponsive_engines"):
                            return
                        raise ActionRequiredError(
                            "SearXNG started, but its search engines did not answer. Check "
                            "engine availability, then resume setup"
                        )
                    raise ActionRequiredError(
                        "SearXNG did not return JSON search results. Check its "
                        "configuration, then resume setup"
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    if time.monotonic() >= deadline:
                        raise ActionRequiredError(
                            "SearXNG did not pass the search check. Check its container "
                            "logs, then resume setup"
                        ) from exc
                    time.sleep(1)


def remove_for_uninstall(install: Installation) -> None:
    """Called under the application's operation lock before deleting its record/config."""
    service = ManagedService(install)
    record = service.read()
    if not exists_strict(service.record_path):
        return
    # The port is persisted before the first container mutation and never cleared.
    # A retry resets the phase, so phase alone cannot prove no container was created.
    container_possible = record.get("installed") or "port" in record
    already_removed = (
        not record.get("installed")
        and record.get("action") == "remove"
        and record.get("state") == "completed"
    )
    if container_possible and not already_removed:
        runtime = service._runtime(record)
        if runtime is None or not runtime.ready:
            raise ApplicationError(
                "Start the container runtime so vBot can remove its managed SearXNG container "
                "before uninstalling"
            )
        service._remove(runtime)
    # The server has stopped. Retained data must not point at the removed service,
    # including a retry after container removal but before Settings restoration.
    service.save(
        record,
        state="completed",
        installed=False,
        running=False,
        desired_running=False,
        action="remove",
    )
    _restore_search_offline(service, record)
    service.save(record)


def search_restore_operations(record: JsonObject, raw: JsonObject) -> list[JsonObject]:
    """Restore only the selection that still belongs to this managed service."""
    if "port" not in record:
        return []
    search = raw.get("web_search", {})
    if search.get("provider") != "searxng" or search.get("searxng", {}).get(
        "base_url"
    ) != ManagedService.url(record):
        return []
    before = record.get("search_before", {})
    return [
        {"op": "unset", "path": path}
        if before.get(field) is None
        else {"op": "set", "path": path, "value": before[field]}
        for field, path in (
            ("provider", "web_search.provider"),
            ("base_url", "web_search.searxng.base_url"),
        )
    ]


def _restore_search_offline(service: ManagedService, record: JsonObject) -> None:
    from core.settings import load_validated_settings_json
    from core.settings.paths import parse_patch_operations
    from core.storage import StorageManager
    from core.utils.errors import StorageError

    if "port" not in record or not service.install.server_data_directory:
        return
    storage = StorageManager(service.install.server_data_directory)
    if not exists_strict(storage.settings_path):
        return

    class SelectionChangedError(Exception):
        pass

    def still_owned(previous: JsonObject, candidate: JsonObject) -> None:
        del candidate
        if not search_restore_operations(record, previous):
            raise SelectionChangedError

    try:
        operations = search_restore_operations(
            record, load_validated_settings_json(storage.settings_path)
        )
        if not operations:
            return
        storage.patch_settings(parse_patch_operations(operations), validate_candidate=still_owned)
    except SelectionChangedError:
        return  # A later user choice wins, checked within the Settings transaction.
    except (StorageError, ValueError, OSError) as exc:
        raise ApplicationError(
            "Could not restore web search Settings before uninstalling; repair Settings and retry"
        ) from exc
    record.pop("search_before", None)
