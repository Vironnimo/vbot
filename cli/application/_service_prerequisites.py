"""Fixed, reviewable local container prerequisites for managed services."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cli.application.state import ApplicationError
from core.utils.processes import subprocess_creation_flags


class ActionRequiredError(ApplicationError):
    """The operator can resume after the described host action."""


class Commands:
    """No shell, no inherited remote container target, and bounded command waits."""

    def run(self, args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "CONTAINER_HOST", "CONTAINER_CONNECTION"):
            env.pop(name, None)
        try:
            return subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                creationflags=subprocess_creation_flags(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ActionRequiredError(
                "The host command timed out. A system installer can still be running. "
                "Wait for it and any system authorization dialog to finish, "
                "then check the container runtime and resume setup."
            ) from exc
        except OSError as exc:
            raise ActionRequiredError(
                "The container command could not start. Resume after repairing the runtime."
            ) from exc

    def checked(self, args: list[str], *, timeout: float = 30) -> str:
        result = self.run(args, timeout=timeout)
        if result.returncode:
            if Path(args[0]).stem in {"sudo", "pkexec"}:
                command = ["sudo", *(arg for arg in args[1:] if arg != "-n")]
                raise ActionRequiredError(
                    "System authorization is required on the vBot server. Run "
                    f"{shlex.join(command)} in its terminal, then resume setup."
                )
            raise ActionRequiredError(
                f"{Path(args[0]).stem} could not complete the requested operation "
                f"(exit {result.returncode}). Check its installation and system authorization, "
                "then resume setup."
            )
        return result.stdout


@dataclass(frozen=True)
class ContainerRuntime:
    kind: str
    executable: str
    options: tuple[str, ...] = ()
    ready: bool = False

    def command(self, *args: str) -> list[str]:
        return [self.executable, *self.options, *args]


def executable(name: str) -> str | None:
    found = shutil.which(name)
    if found or sys.platform != "win32" or name != "podman":
        return found
    # Installers update PATH for future processes; this server predates that update.
    for relative in ("Podman/podman.exe", "RedHat/Podman/podman.exe"):
        candidate = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / relative
        if candidate.is_file():
            return str(candidate)
    return None


def _json(commands: Commands, args: list[str]) -> object:
    value = commands.run(args, timeout=5)
    if value.returncode:
        return None
    try:
        return json.loads(value.stdout)
    except ValueError:
        return None


def discover_runtime(
    commands: Commands, *, preferred: str = "", saved_options: tuple[str, ...] | None = None
) -> ContainerRuntime | None:
    """Select a local runtime, never a remote Docker/Podman context."""
    names = [preferred] if preferred in {"docker", "podman"} else ["docker", "podman"]
    pending = None
    for name in names:
        path = executable(name)
        if path is None:
            continue
        options: tuple[str, ...] = ()
        if name == "docker":
            selected_context = list(saved_options[1:]) if saved_options else []
            contexts = _json(commands, [path, "context", "inspect", *selected_context])
            if not isinstance(contexts, list) or not contexts or not isinstance(contexts[0], dict):
                continue
            context = contexts[0]
            endpoint = context.get("Endpoints", {}).get("docker", {}).get("Host", "")
            if not _local_docker_endpoint(endpoint):
                continue
            context_name = context.get("Name")
            if not isinstance(context_name, str):
                continue
            options = ("--context", context_name)
        elif sys.platform == "win32":
            connections = _json(
                commands, [path, "system", "connection", "list", "--format", "json"]
            )
            default = (
                next(
                    (
                        item
                        for item in connections or []
                        if isinstance(item, dict)
                        and (
                            item.get("Name") == saved_options[1]
                            if saved_options
                            else item.get("Default")
                        )
                    ),
                    None,
                )
                if isinstance(connections, list)
                else None
            )
            if default is not None:
                uri = urlsplit(str(default.get("URI", "")))
                if uri.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    continue
                options = ("--connection", str(default["Name"]))
        else:
            options = ("--remote=false",)
        runtime = ContainerRuntime(name, path, options)
        ready = commands.run(runtime.command("info", "--format", "json"), timeout=5).returncode == 0
        runtime = ContainerRuntime(name, path, options, ready)
        if ready:
            return runtime
        pending = pending or runtime
    return pending


def _local_docker_endpoint(endpoint: object) -> bool:
    if not isinstance(endpoint, str):
        return False
    try:
        uri = urlsplit(endpoint.replace("\\", "/"))
    except ValueError:
        return False
    if uri.netloc or uri.query or uri.fragment:
        return False
    if uri.scheme == "unix":
        return uri.path.startswith("/") and not uri.path.startswith("//")
    # Named pipes can address another machine over SMB. Only the explicit local
    # \\.\pipe\ namespace is local; neither a hostname nor a URL authority is.
    prefix = "//./pipe/"
    if uri.scheme != "npipe" or not uri.path.lower().startswith(prefix):
        return False
    name = uri.path[len(prefix) :]
    return bool(name) and "/" not in name and "%" not in name


def package_command() -> list[str] | None:
    if sys.platform == "win32":
        winget = executable("winget")
        return (
            [
                winget,
                "install",
                "--id",
                "Podman.CLI",
                "--exact",
                "--source",
                "winget",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ]
            if winget
            else None
        )
    if not sys.platform.startswith("linux"):
        return None
    for name, args in (
        ("apt-get", ["install", "-y", "podman"]),
        ("dnf", ["install", "-y", "podman"]),
        ("zypper", ["--non-interactive", "install", "podman"]),
        ("pacman", ["-S", "--noconfirm", "--needed", "podman"]),
    ):
        path = executable(name)
        if path:
            if os.geteuid() == 0:
                return [path, *args]
            broker = executable("pkexec")
            if broker and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return [broker, path, *args]
            sudo = executable("sudo")
            return [sudo, "-n", path, *args] if sudo else None
    return None


def local_package_command() -> list[str] | None:
    """A fixed command a local human terminal can authenticate without RPC passwords."""
    if sys.platform == "win32" or not sys.platform.startswith("linux"):
        return None
    command = package_command()
    if command is None:
        return None
    if Path(command[0]).stem in {"sudo", "pkexec"}:
        return [executable("sudo") or "sudo", *(part for part in command[1:] if part != "-n")]
    return command


def needs_local_authorization() -> bool:
    if sys.platform == "win32" or not sys.platform.startswith("linux"):
        return False
    return os.geteuid() != 0 and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def install_local_prerequisites() -> None:
    """Run after the caller proved a local installed target and explicit user consent."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ActionRequiredError(
            "Installing system prerequisites needs a local interactive terminal"
        )
    command = local_package_command()
    if command is None:
        raise ActionRequiredError("Install Docker or Podman on this computer, then resume setup")
    try:
        result = subprocess.run(command, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionRequiredError(
            "The package installation did not finish. "
            "Check the system package manager before resuming"
        ) from exc
    if result.returncode:
        raise ActionRequiredError(
            "The package installation did not complete. "
            "Resolve the reported error, then resume setup"
        )


def prepare_runtime(
    commands: Commands, runtime: ContainerRuntime | None, *, machine: str
) -> ContainerRuntime:
    """Execute only after the accessor explicitly approved the inspected prerequisites."""
    if runtime is None:
        package = package_command()
        if package is None:
            raise ActionRequiredError(
                "Install Docker or Podman on this computer, then resume setup."
            )
        commands.checked(package, timeout=900)
        runtime = discover_runtime(commands, preferred="podman")
        if runtime is None:
            raise ActionRequiredError(
                "Podman installation finished. Restart vBot, then resume setup."
            )
    if runtime.ready:
        return runtime
    if runtime.kind == "docker" and sys.platform == "win32":
        commands.checked([runtime.executable, "desktop", "start", "--timeout", "120"], timeout=150)
        ready = discover_runtime(commands, preferred="docker", saved_options=runtime.options)
        if ready is not None and ready.ready:
            return ready
        raise ActionRequiredError(
            "Docker Desktop started but its Linux engine is not ready. "
            "Check Docker Desktop, then resume setup"
        )
    if runtime.kind != "podman" or sys.platform != "win32":
        raise ActionRequiredError("Start the selected container runtime, then resume setup.")
    wsl = executable("wsl")
    if wsl is None or commands.run([wsl, "--status"]).returncode:
        powershell = executable("powershell")
        if powershell is None:
            raise ActionRequiredError(
                "Enable WSL 2 in Windows, restart this computer, then resume setup."
            )
        # Fixed system command; no user text or filesystem path enters PowerShell code.
        script = (
            "$p = Start-Process -FilePath wsl.exe -ArgumentList "
            "'--install','--no-distribution' -Verb RunAs -WindowStyle Hidden "
            "-Wait -PassThru; exit $p.ExitCode"
        )
        commands.checked(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script], timeout=900
        )
        raise ActionRequiredError(
            "WSL setup was requested. Restart this computer if Windows asks, then resume setup."
        )
    machines = _json(commands, [runtime.executable, "machine", "list", "--format", "json"])
    if not isinstance(machines, list):
        raise ActionRequiredError(
            "Podman could not list its machines. Repair Podman, then resume setup."
        )
    owned = next(
        (item for item in machines if isinstance(item, dict) and item.get("Name") == machine), None
    )
    if owned is None:
        commands.checked(
            [runtime.executable, "machine", "init", "--update-connection=false", machine],
            timeout=900,
        )
    if owned is None or owned.get("Running") is not True:
        commands.checked(
            [runtime.executable, "machine", "start", "--update-connection=false", machine],
            timeout=300,
        )
    result = ContainerRuntime("podman", runtime.executable, ("--connection", machine), True)
    commands.checked(result.command("info", "--format", "json"))
    return result
