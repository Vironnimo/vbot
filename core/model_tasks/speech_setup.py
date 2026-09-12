"""Fixed speech dependency recipes and per-engine installation lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tomllib
from collections.abc import Sequence
from contextlib import suppress
from importlib import metadata, util
from pathlib import Path
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("speech.setup")


class LocalSpeechSetup:
    """One server-owned, fixed-recipe dependency installation; no client commands."""

    def __init__(
        self,
        *,
        engine: str = "",
        directory: Path | None = None,
        install_lock: asyncio.Lock | None = None,
    ) -> None:
        self.engine = engine
        self.directory = directory
        self._install_lock = install_lock or asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._state = "idle"
        self._phase = "checking"
        self._error = ""
        self._closed = False

    @property
    def blocks_execution(self) -> bool:
        return self._state in {"installing", "restart_required", "failed"}

    def status(self) -> dict[str, Any]:
        state = self._state
        if state == "idle":
            state = "ready" if self.available() else "missing"
        return {"state": state, "phase": self._phase, "error": self._error}

    @property
    def python(self) -> Path:
        assert self.directory is not None
        return self.directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def available(self) -> bool:
        if not self.engine:
            return _dependencies_available()
        if self.directory is None or not self.python.is_file():
            return False
        try:
            return (self.directory / "verified.json").read_text() == self._recipe_key()
        except (OSError, ValueError, KeyError):
            return False

    def _config(self) -> dict[str, Any]:
        project = Path(__file__).resolve().parents[2] / "pyproject.toml"
        return tomllib.loads(project.read_text(encoding="utf-8"))

    def _recipe_key(self) -> str:
        return json.dumps(self._config()["tool"]["vbot"]["local-tts"][self.engine], sort_keys=True)

    def install(self) -> dict[str, Any]:
        if self._closed or self._state in {"installing", "restart_required"}:
            return self.status()
        if self.status()["state"] == "ready":
            return self.status()
        self._state, self._phase, self._error = "installing", "checking", ""
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

    async def _run_install(self) -> None:
        try:
            async with asyncio.timeout(3600):
                if self.engine:
                    await self._install_tts()
                    return
                # Only the shipped extra is installable, never packages or paths
                # supplied by an RPC caller. Install dependencies, not vBot's
                # launchers, which may be locked by an open Windows Desktop.
                project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
                requirements = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"][
                    "optional-dependencies"
                ]["local-speech"]
                if await self._command([sys.executable, "-m", "pip", "--version"]) != 0:
                    self._fail("pip_unavailable")
                    return
                gpu_tool = shutil.which("nvidia-smi")
                use_cuda = bool(gpu_tool) and await self._command([str(gpu_tool), "-L"]) == 0
                self._phase = "gpu" if use_cuda else "downloading"
                torch_check = (
                    "import torch; "
                    "v=tuple(int(p) for p in torch.__version__.split('.')[:2]); "
                    "assert (2,10) <= v < (3,); "
                )
                if use_cuda:
                    torch_check += (
                        "x=torch.ones((16,16),device='cuda'); assert (x@x).sum().item()==4096"
                    )
                if await self._command([sys.executable, "-c", torch_check]) != 0:
                    torch_requirement = next(
                        item for item in requirements if item.startswith("torch")
                    )
                    index = (
                        "https://download.pytorch.org/whl/cu128"
                        if use_cuda
                        else "https://download.pytorch.org/whl/cpu"
                    )
                    if sys.platform == "darwin":
                        index = "https://pypi.org/simple"
                    if (
                        await self._pip(
                            [
                                torch_requirement,
                                "--force-reinstall",
                                "--no-deps",
                                "--index-url",
                                index,
                            ]
                        )
                        != 0
                    ):
                        self._fail("install_failed")
                        return
                if await self._pip(requirements) != 0:
                    self._fail("install_failed")
                    return
                self._phase = "verifying"
                # A fresh process proves imports without contaminating the live
                # server with a mixture of old and newly installed libraries.
                probe = (
                    "import torch, av, librosa; "
                    "from transformers import AutoProcessor, AutoModelForMultimodalLM; "
                    "from transformers import AutoModelForTDT, AutoModelForRNNT; "
                    "from core.model_tasks.speech_local import _dependencies_available; "
                    "assert _dependencies_available(); "
                )
                if use_cuda:
                    probe += (
                        "x=torch.ones((16,16),device='cuda'); assert (x@x).sum().item()==4096; "
                    )
                if await self._command([sys.executable, "-c", probe]) != 0:
                    self._fail("gpu_unavailable" if use_cuda else "verification_failed")
                    return
                self._state = "restart_required"
                _LOGGER.info("Local speech support installed; server restart required")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail("timeout")
        except (OSError, ValueError, KeyError, StopIteration):
            self._fail("setup_unavailable")
        except Exception:
            self._fail("install_failed")

    async def _install_tts(self) -> None:
        assert self.directory is not None
        config = self._config()
        recipe = config["tool"]["vbot"]["local-tts"][self.engine]
        # The bootstrap is the only package installed into the server. No ML
        # dependency from a TTS SDK can replace the live STT stack.
        if await self._pip(config["project"]["optional-dependencies"]["local-tts"]) != 0:
            self._fail("install_failed")
            return
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
            return
        marker = self.directory / "verified.json"
        marker.unlink(missing_ok=True)
        gpu_tool = shutil.which("nvidia-smi")
        use_cuda = bool(gpu_tool) and await self._command([str(gpu_tool), "-L"]) == 0
        self._phase = "gpu" if use_cuda else "downloading"
        version = recipe["torch"]
        index = "cu126" if version == "2.6.0" else "cu128"
        index = f"https://download.pytorch.org/whl/{index if use_cuda else 'cpu'}"
        if sys.platform == "darwin":
            index = "https://pypi.org/simple"
        pip = [*uv, "pip", "install", "--python", str(self.python)]
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
            return
        self._phase = "installing"
        if (
            await self._command([*pip, "--only-binary=:all:", *recipe["packages"]], progress=True)
            != 0
        ):
            self._fail("install_failed")
            return
        if (
            recipe.get("source")
            and await self._command(
                [*pip, "--no-deps", "--reinstall-package", "chatterbox-tts", recipe["source"]]
            )
            != 0
        ):
            self._fail("install_failed")
            return
        self._phase = "verifying"
        worker = Path(__file__).with_name("speech_worker.py")
        if (
            await self._command(
                [
                    str(self.python),
                    "-I",
                    str(worker),
                    "--verify",
                    self.engine,
                    "cuda" if use_cuda else "cpu",
                ]
            )
            != 0
        ):
            self._fail("verification_failed")
            return
        marker.write_text(self._recipe_key())
        # Only a child environment changed; the server can use it immediately.
        self._state = "ready"
        _LOGGER.info("Local TTS support installed (engine=%s)", self.engine)

    def _fail(self, code: str) -> None:
        self._state, self._error = "failed", code
        _LOGGER.warning("Local speech installation failed (reason=%s)", code)

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

    async def _command(self, arguments: Sequence[str], *, progress: bool = False) -> int:
        from core.utils.processes import kill_process_tree_async, subprocess_creation_flags

        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess_creation_flags(),
            start_new_session=os.name != "nt",
            env={**os.environ, "PYTHONUTF8": "1", "PIP_NO_INPUT": "1"},
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


def _dependencies_available() -> bool:
    # Inspect metadata only: startup/status must not import torch or load models.
    try:
        version = tuple(int(part) for part in metadata.version("transformers").split(".")[:3])
        torch_version = tuple(int(part) for part in metadata.version("torch").split(".")[:2])
        hub_version = tuple(
            int(part) for part in metadata.version("huggingface-hub").split(".")[:2]
        )
        return (
            (5, 16, 1) <= version < (6,)
            and (2, 10) <= torch_version < (3,)
            and (1, 30) <= hub_version < (2,)
            and all(
                util.find_spec(name) is not None for name in ("torch", "numpy", "av", "librosa")
            )
        )
    except (metadata.PackageNotFoundError, ImportError, ValueError):
        return False
