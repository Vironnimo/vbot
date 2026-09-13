"""Provision packaged local speech recipes without downloading model weights."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from cli.application.state import discover
from core.model_tasks.speech_setup import LocalSpeechSetup
from core.utils.processes import kill_process_tree_async, subprocess_creation_flags


async def _diagnose_verification(setup: LocalSpeechSetup, label: str) -> None:
    module = sys.modules[LocalSpeechSetup.__module__]
    module_file = getattr(module, "__file__", None)
    if not module_file:
        print(f"{label} standalone verification unavailable: setup module has no file")
        return
    worker = Path(module_file).with_name("speech_worker.py")
    command = [str(setup.python), "-I", "-B", str(worker)]
    if setup.engine:
        command.extend(("--verify", setup.engine, "cpu"))
    else:
        command.extend(("--verify-stt", str(worker.parents[2])))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess_creation_flags(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError:
        print(f"{label} standalone verification timed out after 120 seconds")
        return
    finally:
        if process.returncode is None:
            await kill_process_tree_async(process)
            await process.wait()
    detail = (stdout + stderr).decode("utf-8", errors="replace")[-6000:]
    print(
        f"{label} standalone verification exit code {process.returncode}\n"
        f"{detail or '(no output)'}"
    )


async def _install(setup: LocalSpeechSetup, label: str) -> None:
    setup.install()
    try:
        while setup.status()["state"] == "installing":
            await asyncio.sleep(0.25)
        status = setup.status()
        if status["state"] != "ready" or not setup.available():
            if status["phase"] == "verifying":
                await _diagnose_verification(setup, label)
            raise RuntimeError(
                f"{label} setup failed: state={status['state']} "
                f"phase={status['phase']} error={status['error']}"
            )
    finally:
        await setup.aclose()


async def _smoke(data_dir: Path) -> None:
    install = discover()
    data_dir = data_dir.resolve()
    if (
        install is None
        or install.root.name != "application"
        or not install.root.parent.name.startswith("vbot-native-smoke-")
        or install.root.parent.parent.resolve() != Path(tempfile.gettempdir()).resolve()
        or data_dir != install.root.parent.resolve() / "data"
        or install.server_data_directory is None
        or data_dir != Path(install.server_data_directory).resolve()
    ):
        raise RuntimeError("Speech smoke requires the disposable native smoke installation")
    speech_root = data_dir / "speech-engines"
    failures: list[str] = []

    async def attempt(setup: LocalSpeechSetup, label: str) -> bool:
        try:
            await _install(setup, label)
        except Exception as error:
            failures.append(f"{label}: {error}")
            return False
        return True

    await attempt(LocalSpeechSetup(directory=speech_root / "stt"), "stt")
    await attempt(
        LocalSpeechSetup(engine="qwen3-tts", directory=speech_root / "qwen3-tts"),
        "qwen3-tts",
    )
    chatterbox = speech_root / "chatterbox"
    chatterbox_ready = await attempt(
        LocalSpeechSetup(engine="chatterbox", directory=chatterbox),
        "chatterbox fresh setup",
    )
    if chatterbox_ready:
        (chatterbox / "verified.json").unlink()
        await attempt(
            LocalSpeechSetup(engine="chatterbox", directory=chatterbox),
            "chatterbox repeated setup",
        )
    if failures:
        raise RuntimeError("; ".join(failures))
    print(
        json.dumps(
            {
                "ok": True,
                "stt": "ready",
                "qwen3_tts": "ready",
                "chatterbox": "ready",
                "chatterbox_repeated": "ready",
            }
        )
    )


async def _bounded_smoke(data_dir: Path) -> None:
    try:
        async with asyncio.timeout(1750):
            await _smoke(data_dir)
    except TimeoutError as error:
        raise RuntimeError("Packaged local speech setup timed out") from error


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    asyncio.run(_bounded_smoke(parser.parse_args().data_dir))
