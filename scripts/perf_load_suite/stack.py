"""Process orchestration: the fake Provider and one disposable vBot server.

Both processes run from this checkout, on free loopback ports, with output
captured to log files. Every process is started in its own process group so a
console Ctrl+C reaches only the harness, which then stops each process tree
itself. The harness additionally activates vBot's process containment, so the
operating system reaps every child even if the harness is killed outright.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psutil  # type: ignore[import-untyped]

from cli._server_target import ServerInstance, find_listening_process
from cli.server_management import resolve_instance, stop_server
from core.storage.layout import initialize_data_directory
from core.utils.processes import guarded_process_launch, subprocess_creation_flags
from scripts.perf_load_suite.fake_provider import MODEL_ID, SERVICE_NAME
from scripts.perf_load_suite.rpc import RpcClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"
FAKE_PROVIDER_ID = "perf"
AGENT_MODEL = f"{FAKE_PROVIDER_ID}/{MODEL_ID}::default"
MODEL_CONTEXT_WINDOW = 1_000_000
MODEL_MAX_OUTPUT_TOKENS = 65_536
SERVER_STARTUP_TIMEOUT_SECONDS = 120.0
PROVIDER_STARTUP_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 10.0
# Only what processes need to run; Provider credentials and VBOT_* variables
# from the invoking shell never reach the disposable server.
_SERVER_ENVIRONMENT_KEYS = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMPUTERNAME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LOCALAPPDATA",
        "LOGNAME",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PSMODULEPATH",
        "PUBLIC",
        "SHELL",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)


class StackError(RuntimeError):
    """A harness-owned process could not be started or verified."""


def free_port(host: str = LOOPBACK) -> int:
    """Return a currently free TCP port on ``host``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlisted environment for harness-owned child processes."""
    environment = {
        key: value
        for key, value in (os.environ if source is None else source).items()
        if key.upper() in _SERVER_ENVIRONMENT_KEYS or key.startswith("LC_")
    }
    environment["PYTHONUTF8"] = "1"
    environment["VBOT_LOG_STDIO"] = "0"
    return environment


def provider_settings(provider_base_url: str) -> dict[str, Any]:
    """``settings.json`` registering the fake Provider as the default Agent Model."""
    return {
        "format_version": 1,
        "defaults": {"agent": {"model": AGENT_MODEL}},
        "reflection": {"enabled": False},
        "providers": {
            "connections": {f"{FAKE_PROVIDER_ID}:default": True},
            "custom": {
                FAKE_PROVIDER_ID: {
                    "name": "Perf Fake Provider",
                    "adapter": "openai_compatible",
                    "base_url": provider_base_url,
                    "auth": "none",
                    "models_endpoint": "/models",
                    "defaults": {"temperature": 0},
                    "models": {
                        MODEL_ID: {
                            "name": "Perf Model",
                            "context_window": MODEL_CONTEXT_WINDOW,
                            "max_output_tokens": MODEL_MAX_OUTPUT_TOKENS,
                            "capabilities": {
                                "input_modalities": ["text"],
                                "json_mode": False,
                                "output_modalities": ["text"],
                                "reasoning": False,
                                "supported_parameters": [],
                                "supported_voices": [],
                                "task_types": ["chat", "text_output"],
                                "tools": True,
                                "vision": False,
                            },
                        }
                    },
                }
            },
        },
    }


def spawn(
    argv: list[str], *, log_path: Path, environment: dict[str, str]
) -> subprocess.Popen[bytes]:
    """Start a windowless child in its own process group, logging to ``log_path``."""
    launch = guarded_process_launch(argv)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            list(launch.argv),
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            pass_fds=launch.pass_fds,
            creationflags=subprocess_creation_flags(new_process_group=True),
            start_new_session=os.name != "nt",
        )


def stop_process_tree(pid: int, *, timeout_seconds: float = PROCESS_STOP_TIMEOUT_SECONDS) -> None:
    """Terminate a process and every descendant, killing what does not exit."""
    try:
        root = psutil.Process(pid)
        processes = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            continue
    _gone, alive = psutil.wait_procs(processes, timeout=timeout_seconds)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(alive, timeout=timeout_seconds)


def log_tail(path: Path, *, lines: int = 30) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


class FakeProvider:
    """The scripted fake Provider process (one per harness invocation)."""

    def __init__(self, *, log_path: Path) -> None:
        self.port = free_port()
        self.base_url = f"http://{LOOPBACK}:{self.port}"
        self._log_path = log_path
        self._process: subprocess.Popen[bytes] | None = None
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0, trust_env=False)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def api_base_url(self) -> str:
        return f"{self.base_url}/v1"

    def start(self) -> None:
        argv = [
            sys.executable,
            "-m",
            "scripts.perf_load_suite.fake_provider",
            "--host",
            LOOPBACK,
            "--port",
            str(self.port),
        ]
        self._process = spawn(argv, log_path=self._log_path, environment=child_environment())
        deadline = time.monotonic() + PROVIDER_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise StackError(
                    f"fake Provider exited during startup:\n{log_tail(self._log_path)}"
                )
            try:
                response = self._client.get("/health")
                ready = (
                    response.status_code == 200 and response.json().get("service") == SERVICE_NAME
                )
            except (httpx.HTTPError, ValueError):
                ready = False  # still starting up
            if ready:
                return
            time.sleep(0.1)
        raise StackError(f"fake Provider did not become ready at {self.base_url}")

    def reset(self) -> None:
        self._client.post("/_perf/reset").raise_for_status()

    def stats(self) -> list[dict[str, Any]]:
        response = self._client.get("/_perf/stats")
        response.raise_for_status()
        requests = response.json().get("requests")
        return requests if isinstance(requests, list) else []

    def stop(self) -> None:
        self._client.close()
        if self._process is not None:
            stop_process_tree(self._process.pid)
            self._process = None

    def __enter__(self) -> FakeProvider:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class VbotServer:
    """One disposable vBot server with a fresh data directory."""

    def __init__(self, *, data_dir: Path, log_dir: Path, provider_api_base_url: str) -> None:
        self.data_dir = data_dir
        self.port = free_port()
        self.base_url = f"http://{LOOPBACK}:{self.port}"
        self.console_log = log_dir / "server-console.log"
        self._provider_api_base_url = provider_api_base_url
        self._process: subprocess.Popen[bytes] | None = None
        self._serving_pid: int | None = None
        self.rpc = RpcClient(self.base_url)

    @property
    def pid(self) -> int:
        """PID of the process that serves HTTP (the interpreter, not a launcher)."""
        if self._serving_pid is None:
            raise StackError("server is not running")
        return self._serving_pid

    def _instance(self) -> ServerInstance:
        return resolve_instance(host=LOOPBACK, port=self.port, data_dir=self.data_dir)

    def start(self) -> None:
        initialize_data_directory(self.data_dir)
        (self.data_dir / "settings.json").write_text(
            json.dumps(provider_settings(self._provider_api_base_url), indent=2) + "\n",
            encoding="utf-8",
        )
        argv = [
            sys.executable,
            "-m",
            "server.main",
            "--host",
            LOOPBACK,
            "--port",
            str(self.port),
            "--data-dir",
            str(self.data_dir),
        ]
        self._process = spawn(argv, log_path=self.console_log, environment=child_environment())
        deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise StackError(
                    f"vBot server exited during startup:\n{log_tail(self.console_log)}"
                )
            try:
                response = self.rpc.get("/health")
                healthy = response.status_code == 200 and response.json() == {"status": "ok"}
            except (httpx.HTTPError, ValueError):
                healthy = False  # still starting up
            if healthy:
                self._serving_pid = self._resolve_serving_pid()
                return
            time.sleep(0.2)
        raise StackError(f"vBot server did not become healthy at {self.base_url}")

    def _resolve_serving_pid(self) -> int:
        """Find the listener inside the spawned tree (a venv launcher may sit on top)."""
        assert self._process is not None
        listener = find_listening_process(self._instance())
        if listener is None:
            raise StackError(f"no process listens on {self.base_url} although it is healthy")
        spawned = self._process.pid
        try:
            tree = {
                spawned,
                *(child.pid for child in psutil.Process(spawned).children(recursive=True)),
            }
        except psutil.NoSuchProcess as exc:
            raise StackError("the spawned vBot server process disappeared") from exc
        if listener.pid not in tree:
            raise StackError(
                f"port {self.port} is served by PID {listener.pid}, which the harness did not start"
            )
        return int(listener.pid)

    def serves_webui(self) -> bool:
        """Whether the server serves the built WebUI entry document."""
        try:
            response = self.rpc.get("/")
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and "text/html" in response.headers.get(
            "content-type", ""
        )

    def stop(self) -> None:
        """Request cooperative shutdown, then make sure the whole tree is gone."""
        self.rpc.close()
        if self._process is None:
            return
        pid = self._process.pid
        try:
            descendants = psutil.Process(pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        problem: str | None = None
        try:
            outcome = stop_server(
                self._instance(), shutdown_timeout_seconds=PROCESS_STOP_TIMEOUT_SECONDS
            )
            if not outcome.ok:
                problem = outcome.message
        except Exception as exc:  # noqa: BLE001 - the forced cleanup below still runs
            problem = f"{type(exc).__name__}: {exc}"
        if problem is not None:
            print(
                f"warning: cooperative shutdown of the vBot server on port {self.port} "
                f"failed ({problem}); terminating its process tree",
                file=sys.stderr,
            )
        stop_process_tree(pid)
        for process in descendants:
            if process.is_running():
                stop_process_tree(process.pid)
        self._process = None
        self._serving_pid = None

    def __enter__(self) -> VbotServer:
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
