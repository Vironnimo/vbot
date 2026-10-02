"""Installation lifecycle shared by every local engine setup.

A :class:`LocalSetup` is one server-owned, fixed-recipe installation: clients
can start it and read its status, but never pass packages, paths or commands.
It owns the state machine (``missing``/``installing``/``ready``/``failed``/
``restart_required``), the coarse phase and optional byte progress the WebUI
shows, a shared lock that serializes installations, the completion receipt
(``verified.json``) and the subprocess runner that hides package-manager
output. Subclasses supply the installation body and their environment check;
:meth:`LocalSetup._install_recipe` creates a uv-managed child environment from
a ``[tool.vbot.*]`` recipe in ``pyproject.toml``.

A setup may also own one pinned model (:mod:`core.model_tasks.model_files`):
its files live in ``<models_dir>/<name>/<revision>`` with their own receipt,
the setup is available only once both the environment and the model are,
and :meth:`LocalSetup._fetch_model` reports the model download in bytes.

Packaged releases never install into their own runtime: managed environments
live in the data directory, and the uv bootstrap only reaches the server
interpreter of a development checkout.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.model_tasks.model_files import (
    ModelFilesCancelledError,
    ModelFilesError,
    PinnedModel,
    fetch_model_files,
)
from core.utils.file_status import is_dir_strict, is_file_strict
from core.utils.logging import get_logger

_LOGGER = get_logger("local_engines.setup")
# The optional extra that brings uv to a development checkout's server interpreter.
UV_BOOTSTRAP_EXTRA = "local-tts"
# Longest one package-manager or verification command may run. Model
# downloads have no overall limit: their network reads time out instead.
SETUP_TIMEOUT_S = 3600


class LocalSetup:
    """One fixed-recipe installation with a status the WebUI can poll."""

    def __init__(
        self,
        *,
        name: str,
        directory: Path | None,
        install_lock: asyncio.Lock | None = None,
        subject: str = "Local engine",
        logger: Any = None,
        model: PinnedModel | None = None,
        models_dir: Path | None = None,
    ) -> None:
        self.directory = directory
        self.model = model
        # Without a models directory a pinned model cannot be installed.
        self.model_directory = (
            models_dir / name / model.revision if model is not None and models_dir else None
        )
        self._name = name
        self._subject = subject
        self._logger = logger or _LOGGER
        self._install_lock = install_lock or asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._state = "idle"
        self._phase = "checking"
        self._error = ""
        self._progress: tuple[int, int] | None = None
        self._closed = False
        self._cancelled = False
        self._reported_unavailability = ""

    @property
    def blocks_execution(self) -> bool:
        return self._state in {"installing", "restart_required", "failed"}

    def status(self, *, log_unavailable: bool = False) -> dict[str, Any]:
        """Return ``{state, phase, error}`` plus ``progress`` while bytes are counted."""
        state = self._state
        error = self._error
        if state in {"idle", "ready"}:
            error = self._availability_error()
            state = "missing" if error else "ready"
        if log_unavailable:
            reason = error or (state if self.blocks_execution else "")
            if reason and reason != self._reported_unavailability:
                self._logger.warning(
                    f"{self._subject} unavailable (engine=%s, reason=%s, environment=%s)",
                    self._name,
                    reason,
                    self.directory or "server",
                )
            elif not reason and self._reported_unavailability:
                self._logger.info(f"{self._subject} available again (engine=%s)", self._name)
            self._reported_unavailability = reason
        status: dict[str, Any] = {"state": state, "phase": self._phase, "error": error}
        if state == "installing" and self._progress is not None:
            completed, total = self._progress
            status["progress"] = {"completed": completed, "total": total}
        return status

    def activity(self) -> dict[str, Any] | None:
        """This installation as background activity, or None when there is nothing to show.

        ``{state: running, phase, progress?}`` while it installs, ``{state:
        completed}`` once an installation in this process finished, ``{state:
        failed, error}`` after a failure and ``{state: action_required, phase:
        restart_required}`` while a restart is due. A cancelled installation
        reports nothing. Reads only in-memory state, so it is cheap to poll.
        """
        if self._state == "installing":
            activity: dict[str, Any] = {"state": "running", "phase": self._phase}
            if self._progress is not None:
                completed, total = self._progress
                activity["progress"] = {"completed": completed, "total": total}
            return activity
        if self._state == "failed":
            return {"state": "failed", "error": self._error}
        if self._state == "restart_required":
            return {"state": "action_required", "phase": "restart_required"}
        if self._state == "ready":
            return {"state": "completed"}
        return None

    @property
    def python(self) -> Path:
        assert self.directory is not None
        return self.directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def available(self) -> bool:
        return not self.blocks_execution and not self._availability_error()

    def _availability_error(self) -> str:
        """Return a stable reason code while the installation cannot execute."""
        return self._environment_error() or self._model_error()

    def _environment_error(self) -> str:
        """Why the engine's environment cannot run, or ``""``."""
        if self.directory is None:
            return "environment_missing"
        return environment_error(self.directory, self.python, self._python_version())

    def _model_error(self) -> str:
        """Why the pinned model is not installed, or ``""`` (also without one)."""
        if self.model is None:
            return ""
        if self.model_directory is None:
            return "model_missing"
        try:
            if not is_file_strict(self.model_directory / "verified.json"):
                return "model_missing"
        except OSError:
            return "environment_unreadable"
        return ""

    def _python_version(self) -> str | None:
        """The ``major.minor`` the environment's Python must have, or None for any."""
        return None

    def _environment_needed(self) -> bool:
        """Remove an environment whose Python cannot serve; return whether one must be created.

        Its interpreter is gone, or the environment is based on a Python that no
        longer exists or has the wrong version. Nothing in the environment
        directory outlives its interpreter.
        """
        assert self.directory is not None
        error = environment_error(self.directory, self.python, self._python_version())
        if error not in {"python_missing", "python_changed"}:
            return False
        if self.directory.exists():
            shutil.rmtree(self.directory)
        return True

    def install(self) -> dict[str, Any]:
        if self._closed or self._state in {"installing", "restart_required"}:
            return self.status()
        if self.status()["state"] == "ready":
            return self.status()
        self._state, self._phase, self._error, self._progress = "installing", "checking", "", None
        self._task = asyncio.create_task(self._install(), name=f"local-setup:{self._name}")
        return self.status()

    async def cancel(self) -> dict[str, Any]:
        """Stop a running installation and wait until it has stopped.

        The setup is then simply not installed: nothing failed, and verified
        model files and partial downloads stay for the next installation.
        """
        task = self._task
        if self._state == "installing" and task is not None and not task.done():
            self._cancelled = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before it started never ran its handler.
            if self._state == "installing":
                self._stopped()
        return self.status()

    def _stopped(self) -> None:
        self._state, self._phase, self._error = "idle", "checking", ""
        self._logger.info(f"{self._subject} installation cancelled (engine=%s)", self._name)

    async def _install(self) -> None:
        try:
            if self._install_lock.locked():
                self._phase = "queued"
            async with self._install_lock:
                await self._run_install()
        except asyncio.CancelledError:
            if self._cancelled and not self._closed:
                self._stopped()
                if (task := asyncio.current_task()) is not None:
                    task.uncancel()
                return
            self._fail("interrupted")
            raise
        finally:
            self._progress = None
            self._cancelled = False

    async def _run_install(self) -> None:
        try:
            await self._perform()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail("timeout")
        except OSError, ValueError, KeyError, StopIteration:
            self._fail("setup_unavailable")
        except Exception:
            self._fail("install_failed")

    async def _perform(self) -> None:
        """Install, verify and leave the state ``ready``, ``restart_required`` or failed."""
        raise NotImplementedError

    async def _fetch_model(self) -> bool:
        """Fetch the pinned model's files with byte progress; False after a reported failure.

        Removes the model's receipt first; the caller verifies what it needs
        and then publishes the model with :meth:`_publish_model`.
        """
        assert self.model is not None
        if self.model_directory is None:
            self._fail("setup_unavailable")
            return False
        model, directory = self.model, self.model_directory
        (directory / "verified.json").unlink(missing_ok=True)
        try:
            # Earlier revisions supply the files a newer one did not change.
            earlier = [
                entry
                for entry in directory.parent.iterdir()
                if entry.is_dir() and entry != directory
            ]
        except OSError:
            earlier = []
        total = model.download_bytes
        self._phase = "downloading"
        self._progress = (0, total)

        def progress(completed: int) -> None:
            self._progress = (min(completed, total), total)

        cancelled = threading.Event()
        work = asyncio.create_task(
            asyncio.to_thread(
                fetch_model_files,
                model,
                directory,
                progress=progress,
                cancelled=cancelled,
                reuse=earlier,
            )
        )
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # The thread stops within one chunk; its files stay for a retry.
            cancelled.set()
            await asyncio.wait([work])
            raise
        except ModelFilesCancelledError:
            self._fail("interrupted")
            return False
        except ModelFilesError as error:
            self._fail(error.code)
            return False
        return True

    def _publish_model(self) -> None:
        """Write the model's receipt and remove revisions this release no longer pins."""
        assert self.model is not None and self.model_directory is not None
        receipt = {"repo": self.model.repo, "revision": self.model.revision}
        self._write_marker(self.model_directory / "verified.json", json.dumps(receipt) + "\n")
        for entry in self.model_directory.parent.iterdir():
            if entry.is_dir() and entry != self.model_directory:
                shutil.rmtree(entry, ignore_errors=True)

    def _config(self) -> dict[str, Any]:
        """Read this release's ``pyproject.toml``, the only source of recipes."""
        source = Path(__file__).resolve()
        project = source.parents[2] / "pyproject.toml"
        if not project.is_file():
            project = next(
                parent / "app" / "pyproject.toml"
                for parent in source.parents
                if (parent / "release.json").is_file()
                and (parent / "app" / "pyproject.toml").is_file()
            )
        return tomllib.loads(project.read_text(encoding="utf-8"))

    async def _install_recipe(self, *section: str) -> str | None:
        """Create or update this setup's uv-managed environment from a fixed recipe.

        *section* addresses the recipe below ``[tool.vbot]`` in ``pyproject.toml``,
        e.g. ``("local-tts", "chatterbox")``. A recipe names the Python version,
        exact ``packages`` and optionally a ``torch`` version (installed first,
        for CUDA when a GPU is present) and a pinned ``source`` archive that
        replaces ``source-package``. The receipt is removed before anything,
        even the recipe, is read; the caller verifies and writes it. Returns the
        device the packages target, or ``None`` after a reported failure.
        """
        assert self.directory is not None
        (self.directory / "verified.json").unlink(missing_ok=True)
        config = self._config()
        recipe: Mapping[str, Any] = config["tool"]["vbot"]
        for key in section:
            recipe = recipe[key]
        bootstrap = config["project"]["optional-dependencies"][UV_BOOTSTRAP_EXTRA]
        # Packaged roles ship uv. Development checkouts retain their existing
        # fixed bootstrap recipe, while immutable packaged roles are untouched.
        if not self._packaged() and await self._pip(bootstrap) != 0:
            self._fail("install_failed")
            return None
        uv = [sys.executable, "-m", "uv"]
        self._phase = "python"
        if (
            self._environment_needed()
            and await self._command(
                [*uv, "venv", "--python", recipe["python"], "--managed-python", str(self.directory)]
            )
            != 0
        ):
            self._fail("install_failed")
            return None
        pip = [*uv, "pip", "install", "--python", str(self.python)]
        device = "cpu"
        if version := recipe.get("torch"):
            gpu_tool = shutil.which("nvidia-smi")
            use_cuda = bool(gpu_tool) and await self._command([str(gpu_tool), "-L"]) == 0
            device = "cuda" if use_cuda else "cpu"
            self._phase = "gpu" if use_cuda else "downloading"
            index = "cu126" if version == "2.6.0" else "cu128"
            index = f"https://download.pytorch.org/whl/{index if use_cuda else 'cpu'}"
            if sys.platform == "darwin":
                index = "https://pypi.org/simple"
            if (
                await self._command(
                    [
                        *pip,
                        "--only-binary=:all:",
                        f"torch=={version}",
                        f"torchaudio=={version}",
                        "--index-url",
                        index,
                    ],
                    progress=True,
                )
                != 0
            ):
                self._fail("install_failed")
                return None
        self._phase = "installing"
        source = recipe.get("source")
        # A previous setup may have installed the pinned source with the same
        # version as PyPI. Resolve dependency metadata from PyPI again before
        # restoring that source; its metadata may contain transitive Git URLs.
        reinstall = ["--reinstall-package", recipe["source-package"]] if source else []
        if (
            await self._command(
                [*pip, "--only-binary=:all:", *reinstall, *recipe["packages"]], progress=True
            )
            != 0
        ):
            self._fail("install_failed")
            return None
        if (
            source
            and await self._command(
                [*pip, "--no-deps", "--reinstall-package", recipe["source-package"], source]
            )
            != 0
        ):
            self._fail("install_failed")
            return None
        self._phase = "verifying"
        return device

    def _packaged(self) -> bool:
        return any(
            (parent / "release.json").is_file() for parent in Path(__file__).resolve().parents
        )

    def _write_marker(self, marker: Path, content: str = "{}\n") -> None:
        write_receipt(marker, content)

    def _fail(self, code: str) -> None:
        self._state, self._error = "failed", code
        self._logger.warning(f"{self._subject} installation failed (reason=%s)", code)

    async def _pip(self, arguments: Sequence[str]) -> int:
        return await self._command(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--progress-bar",
                "off",
                "--only-binary=:all:",
                *arguments,
            ],
            progress=True,
        )

    async def _command(
        self,
        arguments: Sequence[str],
        *,
        progress: bool = False,
        environment: dict[str, str] | None = None,
        on_line: Callable[[bytes], None] | None = None,
    ) -> int:
        """Run one fixed command; *on_line* sees every output line, nobody else does."""
        from core.utils.processes import kill_process_tree_async, subprocess_creation_flags

        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess_creation_flags(),
            start_new_session=os.name != "nt",
            env=environment
            or {
                **os.environ,
                "PYTHONUTF8": "1",
                "PIP_NO_INPUT": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        self._process = process
        try:
            assert process.stdout is not None
            async with asyncio.timeout(SETUP_TIMEOUT_S):
                while line := await process.stdout.readline():
                    # Never expose package-manager output: custom indexes can carry
                    # credentials. Surface only fixed, translated phase identifiers.
                    if progress and line.startswith(b"Installing collected packages"):
                        self._phase = "installing"
                    elif progress and line.startswith(b"Downloading"):
                        self._phase = "downloading"
                    if on_line is not None:
                        on_line(line)
                return await process.wait()
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    await kill_process_tree_async(process)
                await process.wait()
            self._process = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        # The task's finally block owns the process tree and reaping. Killing
        # only a Windows venv launcher would orphan its Python/uv children.

    async def aclose(self) -> None:
        self.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def environment_error(directory: Path, python: Path, python_version: str | None = None) -> str:
    """Return why the managed environment in *directory* cannot run, or ``""``.

    The environment's ``pyvenv.cfg`` names the Python it is based on: when that
    Python is gone (``python_missing``) or *python_version* (``major.minor``)
    differs from it (``python_changed``), the environment must be created again.
    What cannot be read is ``environment_unreadable``, never missing: a missing
    Python lets setup delete the environment.
    """
    try:
        if not is_file_strict(python):
            return "python_missing"
        base = _environment_base(directory)
        if base is not None:
            home, version = base
            if not is_dir_strict(home):
                return "python_missing"
            if python_version is not None and version != python_version:
                return "python_changed"
        # This receipt proves that setup finished, not that the installed
        # packages or application source are identical to today's recipe.
        # Updates must not revoke a completed setup based on text or hashes.
        if not is_file_strict(directory / "verified.json"):
            return "setup_incomplete"
    except OSError:
        return "environment_unreadable"
    return ""


def _environment_base(directory: Path) -> tuple[Path, str] | None:
    """Return the base Python's directory and ``major.minor`` from ``pyvenv.cfg``."""
    config = directory / "pyvenv.cfg"
    if not is_file_strict(config):
        return None
    values: dict[str, str] = {}
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    home = values.get("home")
    if not home:
        return None
    version = ".".join(values.get("version_info", values.get("version", "")).split(".")[:2])
    return Path(home), version


def write_receipt(marker: Path, content: str = "{}\n") -> None:
    """Atomically publish a completion receipt."""
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, marker)
