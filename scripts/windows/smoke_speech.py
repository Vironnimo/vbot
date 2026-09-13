"""Provision packaged local speech recipes without downloading model weights."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from cli.application.state import discover
from core.model_tasks.speech_setup import LocalSpeechSetup


async def _install(setup: LocalSpeechSetup, label: str) -> None:
    setup.install()
    try:
        while setup.status()["state"] == "installing":
            await asyncio.sleep(0.25)
        status = setup.status()
        if status["state"] != "ready" or not setup.available():
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
    await _install(LocalSpeechSetup(directory=speech_root / "stt"), "stt")
    await _install(
        LocalSpeechSetup(engine="qwen3-tts", directory=speech_root / "qwen3-tts"),
        "qwen3-tts",
    )
    chatterbox = speech_root / "chatterbox"
    await _install(
        LocalSpeechSetup(engine="chatterbox", directory=chatterbox),
        "chatterbox fresh setup",
    )
    (chatterbox / "verified.json").unlink()
    await _install(
        LocalSpeechSetup(engine="chatterbox", directory=chatterbox),
        "chatterbox repeated setup",
    )
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
