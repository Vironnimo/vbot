"""Installation lifecycle shared by every local engine setup.

A :class:`LocalSetup` is one server-owned, fixed-recipe installation: clients
can start it and read its status, but never pass packages, paths or commands.
It owns the state machine (``missing``/``installing``/``ready``/``failed``/
``restart_required``), the coarse phase and optional byte progress the WebUI
shows, a shared lock that serializes installations, the completion receipt
(``verified.json``) and the subprocess runner that hides package-manager
output. Subclasses supply the installation body and their availability check;
:meth:`LocalSetup._install_recipe` creates a uv-managed child environment from
a ``[tool.vbot.*]`` recipe in ``pyproject.toml``.

Packaged releases never install into their own runtime: managed environments
live in the data directory, and the uv bootstrap only reaches the server
interpreter of a source installation.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("local_engines.setup")
# The optional extra that brings uv to a source installation's server interpreter.
UV_BOOTSTRAP_EXTRA = "local-tts"
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
    ) -> None:
        self.directory = directory
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

    @property
    def python(self) -> Path:
        assert self.directory is not None
        return self.directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def available(self) -> bool:
        return not self.blocks_execution and not self._availability_error()

    def _availability_error(self) -> str:
        """Return a stable reason code while the installation cannot execute."""
        if self.directory is None:
            return "environment_missing"
        return environment_error(self.directory, self.python)

    def install(self) -> dict[str, Any]:
        if self._closed or self._state in {"installing", "restart_required"}:
            return self.status()
        if self.status()["state"] == "ready":
            return self.status()
        self._state, self._phase, self._error, self._progress = "installing", "checking", "", None
        self._task = asyncio.create_task(self._install())
        return self.status()

    async def _install(self) -> None:
        try:
            if self._install_lock.locked():
                self._phase = "queued"
            async with self._install_lock:
                await self._run_install()
        except asyncio.CancelledError:
            self._fail("interrupted")
            raise
        finally:
            self._progress = None

    async def _run_install(self) -> None:
        try:
            async with asyncio.timeout(SETUP_TIMEOUT_S):
                await self._perform()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail("timeout")
        except (OSError, ValueError, KeyError, StopIteration):
            self._fail("setup_unavailable")
        except Exception:
            self._fail("install_failed")

    async def _perform(self) -> None:
        """Install, verify and leave the state ``ready``, ``restart_required`` or failed."""
        raise NotImplementedError

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
        # Packaged roles ship uv. Source installations retain their existing
        # fixed bootstrap recipe, while immutable packaged roles are untouched.
        if not self._packaged() and await self._pip(bootstrap) != 0:
            self._fail("install_failed")
            return None
        uv = [sys.executable, "-m", "uv"]
        self._phase = "python"
        if (
            not self.python.exists()
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


def environment_error(directory: Path, python: Path) -> str:
    """Return why the managed environment in *directory* cannot run, or ``""``."""
    try:
        if not python.is_file():
            return "python_missing"
        # This receipt proves that setup finished, not that the installed
        # packages or application source are identical to today's recipe.
        # Updates must not revoke a completed setup based on text or hashes.
        if not (directory / "verified.json").is_file():
            return "setup_incomplete"
    except OSError:
        return "environment_unreadable"
    return ""


def write_receipt(marker: Path, content: str = "{}\n") -> None:
    """Atomically publish a completion receipt."""
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, marker)
