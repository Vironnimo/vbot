"""Fixed speech dependency recipes and per-target installation lifecycle."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata, util
from pathlib import Path
from typing import Any, override

from core.model_tasks.local_setup import LocalSetup
from core.model_tasks.model_files import PinnedModel
from core.utils.logging import get_logger

_LOGGER = get_logger("speech.setup")


@dataclass
class ServerSpeechStack:
    """The development checkout's STT packages, installed into the server's interpreter.

    Every local STT target of that checkout shares them. Once they changed,
    the server must restart before any of those targets runs.
    """

    restart_required: bool = False


class LocalSpeechSetup(LocalSetup):
    """One local speech target's installation: its engine environment and pinned model.

    *engine* names a TTS environment (``[tool.vbot.local-tts.<engine>]``);
    empty means the STT stack. With a directory the stack is a managed
    environment, shared by every target of the same engine; without one, a
    development checkout installs the ``local-speech`` extra into the server's
    interpreter (*server_stack*, restart required). The target's *model* lives
    under *models_dir* and is fetched after the environment is ready.
    """

    def __init__(
        self,
        *,
        name: str = "",
        engine: str = "",
        directory: Path | None = None,
        install_lock: asyncio.Lock | None = None,
        model: PinnedModel | None = None,
        models_dir: Path | None = None,
        server_stack: ServerSpeechStack | None = None,
    ) -> None:
        super().__init__(
            name=name or engine or "stt",
            directory=directory,
            # Managed STT has no recipe: its worker imports vBot's source and
            # therefore runs on the server's Python.
            recipe=("local-tts", engine) if engine else (),
            install_lock=install_lock,
            subject="Local speech",
            logger=_LOGGER,
            model=model,
            models_dir=models_dir,
        )
        self.engine = engine
        self._server_stack = (
            (server_stack or ServerSpeechStack()) if not engine and directory is None else None
        )

    @property
    def _restart_pending(self) -> bool:
        return self._server_stack is not None and self._server_stack.restart_required

    @property
    @override
    def blocks_execution(self) -> bool:
        return super().blocks_execution or self._restart_pending

    @override
    def status(self, *, log_unavailable: bool = False) -> dict[str, Any]:
        status = super().status(log_unavailable=log_unavailable)
        if self._restart_pending and status["state"] in {"ready", "missing"}:
            status = {**status, "state": "restart_required", "error": ""}
            status.pop("progress", None)
        return status

    @override
    def install(self) -> dict[str, Any]:
        if self._restart_pending:
            return self.status()
        return super().install()

    @override
    def activity(self) -> dict[str, Any] | None:
        # The target whose installation changed the server's packages asks for the restart.
        if self._restart_pending and self._state == "ready":
            return {"state": "action_required", "phase": "restart_required"}
        return super().activity()

    @override
    def _environment_error(self) -> str:
        if self._server_stack is not None:
            return "" if _dependencies_available() else "dependencies_missing"
        return super()._environment_error()

    @override
    async def _perform(self) -> None:
        if self._environment_error():
            if self.engine:
                installed = await self._install_tts()
            elif self._server_stack is None:
                installed = await self._install_stt()
            else:
                installed = await self._install_server_stack()
            if not installed:
                return
        if self.model is not None and self._model_error():
            if not await self._fetch_model():
                return
            await self._publish_model()
            _LOGGER.info(
                "Local speech model installed (target=%s revision=%s)",
                self._name,
                self.model.revision[:12],
            )
        self._state = "ready"

    async def _install_server_stack(self) -> bool:
        """Install the shipped extra into a development checkout's server interpreter."""
        assert self._server_stack is not None
        # Only the shipped extra is installable, never packages or paths
        # supplied by an RPC caller. Install dependencies, not vBot's
        # launchers, which may be locked by an open Windows Desktop.
        project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
        requirements = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]["local-speech"]
        if await self._command([sys.executable, "-m", "pip", "--version"]) != 0:
            self._fail("pip_unavailable")
            return False
        gpu_tool = shutil.which("nvidia-smi")
        use_cuda = bool(gpu_tool) and await self._command([str(gpu_tool), "-L"]) == 0
        self._phase = "gpu" if use_cuda else "downloading"
        torch_check = (
            "import torch; "
            "v=tuple(int(p) for p in torch.__version__.split('.')[:2]); "
            "assert (2,10) <= v < (3,); "
        )
        if use_cuda:
            torch_check += "x=torch.ones((16,16),device='cuda'); assert (x@x).sum().item()==4096"
        if await self._command([sys.executable, "-c", torch_check]) != 0:
            torch_requirement = next(item for item in requirements if item.startswith("torch"))
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
                return False
        if await self._pip(requirements) != 0:
            self._fail("install_failed")
            return False
        self._phase = "verifying"
        # A fresh process proves imports without contaminating the live
        # server with a mixture of old and newly installed libraries.
        probe = (
            "import torch, av, librosa; "
            "from transformers import AutoProcessor, AutoModelForMultimodalLM; "
            "from transformers import AutoModelForTDT, AutoModelForRNNT; "
            "from core.model_tasks.speech_setup import _dependencies_available; "
            "assert _dependencies_available(); "
        )
        if use_cuda:
            probe += "x=torch.ones((16,16),device='cuda'); assert (x@x).sum().item()==4096; "
        if await self._command([sys.executable, "-c", probe]) != 0:
            self._fail("gpu_unavailable" if use_cuda else "verification_failed")
            return False
        self._server_stack.restart_required = True
        _LOGGER.info("Local speech support installed; server restart required")
        return True

    async def _install_tts(self) -> bool:
        assert self.directory is not None
        device = await self._install_recipe()
        if device is None:
            return False
        worker = Path(__file__).with_name("speech_worker.py")
        if (
            await self._command(
                [str(self.python), "-I", "-B", str(worker), "--verify", self.engine, device]
            )
            != 0
        ):
            self._fail("verification_failed")
            return False
        self._write_marker(self.directory / "verified.json")
        # Only a child environment changed; the server can use it immediately.
        _LOGGER.info("Local TTS support installed (engine=%s)", self.engine)
        return True

    async def _install_stt(self) -> bool:
        """Install the shipped STT recipe only inside its managed environment."""
        assert self.directory is not None
        marker = self.directory / "verified.json"
        marker.unlink(missing_ok=True)
        project = self._config()["project"]
        # The worker imports vBot's speech engine source, which also needs the
        # declared core dependencies. Its isolated environment must provide
        # those itself rather than borrowing the immutable server runtime.
        requirements = [
            *project["dependencies"],
            *project["optional-dependencies"]["local-speech"],
        ]
        uv = [sys.executable, "-m", "uv"]
        self._phase = "python"
        if await self._environment_needed():
            install = self._packaged_installation()
            if install is None:
                self._fail("setup_unavailable")
                return False
            from cli.application.dependencies import environment_creation_command

            command, environment = environment_creation_command(install, self.directory)
            if await self._command(command, environment=environment) != 0:
                self._fail("install_failed")
                return False
        gpu_tool = shutil.which("nvidia-smi")
        use_cuda = bool(gpu_tool) and await self._command([str(gpu_tool), "-L"]) == 0
        self._phase = "gpu" if use_cuda else "downloading"
        torch_requirement = next(item for item in requirements if item.startswith("torch"))
        index = (
            "https://download.pytorch.org/whl/cu128"
            if use_cuda
            else "https://download.pytorch.org/whl/cpu"
        )
        if sys.platform == "darwin":
            index = "https://pypi.org/simple"
        pip = [*uv, "pip", "install", "--python", str(self.python), "--only-binary=:all:"]
        if await self._command([*pip, torch_requirement, "--index-url", index], progress=True) != 0:
            self._fail("install_failed")
            return False
        if await self._command([*pip, *requirements], progress=True) != 0:
            self._fail("install_failed")
            return False
        self._phase = "verifying"
        worker = Path(__file__).with_name("speech_worker.py")
        source = Path(__file__).resolve()
        app = source.parents[2]
        for parent in source.parents:
            candidate = parent / "app"
            if (parent / "release.json").is_file() and (
                candidate / "core" / "model_tasks" / "speech_local.py"
            ).is_file():
                app = candidate
                break
        if (
            await self._command(
                [str(self.python), "-I", "-B", str(worker), "--verify-stt", str(app)]
            )
            != 0
        ):
            self._fail("verification_failed")
            return False
        self._write_marker(marker)
        _LOGGER.info("Local STT support installed in its managed environment")
        return True

    def _packaged_installation(self):
        from cli.application.state import load_installation

        for parent in Path(__file__).resolve().parents:
            if (parent / "release.json").is_file() and parent.parent.name == "versions":
                return load_installation(parent.parent.parent)
        return None


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
    except metadata.PackageNotFoundError, ImportError, ValueError:
        return False
