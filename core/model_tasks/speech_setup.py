"""Fixed speech dependency recipes and per-engine installation lifecycle."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tomllib
from importlib import metadata, util
from pathlib import Path

from core.model_tasks.local_setup import LocalSetup
from core.utils.logging import get_logger

_LOGGER = get_logger("speech.setup")


class LocalSpeechSetup(LocalSetup):
    """One speech engine's installation: the server's STT stack or a managed environment.

    Without a directory it installs the shipped ``local-speech`` extra into a
    source installation's server interpreter (restart required). With a
    directory it creates a managed environment: the packaged STT stack, or a
    TTS engine's ``[tool.vbot.local-tts.<engine>]`` recipe.
    """

    def __init__(
        self,
        *,
        engine: str = "",
        directory: Path | None = None,
        install_lock: asyncio.Lock | None = None,
    ) -> None:
        super().__init__(
            name=engine or "stt",
            directory=directory,
            install_lock=install_lock,
            subject="Local speech",
            logger=_LOGGER,
        )
        self.engine = engine

    def _availability_error(self) -> str:
        if not self.engine and self.directory is None:
            return "" if _dependencies_available() else "dependencies_missing"
        return super()._availability_error()

    async def _perform(self) -> None:
        if self.engine:
            await self._install_tts()
            return
        if self.directory is not None:
            await self._install_stt()
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
            probe += "x=torch.ones((16,16),device='cuda'); assert (x@x).sum().item()==4096; "
        if await self._command([sys.executable, "-c", probe]) != 0:
            self._fail("gpu_unavailable" if use_cuda else "verification_failed")
            return
        self._state = "restart_required"
        _LOGGER.info("Local speech support installed; server restart required")

    async def _install_tts(self) -> None:
        assert self.directory is not None
        device = await self._install_recipe("local-tts", self.engine)
        if device is None:
            return
        worker = Path(__file__).with_name("speech_worker.py")
        if (
            await self._command(
                [str(self.python), "-I", "-B", str(worker), "--verify", self.engine, device]
            )
            != 0
        ):
            self._fail("verification_failed")
            return
        self._write_marker(self.directory / "verified.json")
        # Only a child environment changed; the server can use it immediately.
        self._state = "ready"
        _LOGGER.info("Local TTS support installed (engine=%s)", self.engine)

    async def _install_stt(self) -> None:
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
        if not self.python.exists():
            install = self._packaged_installation()
            if install is None:
                self._fail("setup_unavailable")
                return
            from cli.application.dependencies import environment_creation_command

            command, environment = environment_creation_command(install, self.directory)
            if await self._command(command, environment=environment) != 0:
                self._fail("install_failed")
                return
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
            return
        if await self._command([*pip, *requirements], progress=True) != 0:
            self._fail("install_failed")
            return
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
            return
        self._write_marker(marker)
        self._state = "ready"
        _LOGGER.info("Local STT support installed in its managed environment")

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
    except (metadata.PackageNotFoundError, ImportError, ValueError):
        return False
