"""Explicit installation of the pinned, optional WhatsApp Node bridge."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.channels._network_adapter import channel_io
from core.channels.config import ChannelError
from core.utils.ids import new_id

BRIDGE_SOURCE = Path(__file__).with_name("whatsapp_bridge")
BRIDGE_FILES = ("package.json", "package-lock.json", "bridge.js", "gate.js")


def bridge_directory(state_dir: Path) -> Path:
    return state_dir / "bridge"


def node_executable() -> str:
    node = shutil.which("node")
    if node is None:
        raise ChannelError(
            "WhatsApp needs Node.js 22 or newer on the vBot server. "
            "Install Node.js, then retry setup."
        )
    return node


def bridge_digest() -> str:
    digest = hashlib.sha256()
    for name in BRIDGE_FILES:
        digest.update((BRIDGE_SOURCE / name).read_bytes())
    return digest.hexdigest()


def bridge_ready(state_dir: Path) -> bool:
    target = bridge_directory(state_dir)
    try:
        return (target / ".ready").read_text() == bridge_digest() and (
            target / "node_modules/@whiskeysockets/baileys/package.json"
        ).is_file()
    except OSError:
        return False


def child_options() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def reset_pairing(state_dir: Path) -> None:
    root = (state_dir / "whatsapp").resolve()
    auth = (root / "auth").resolve()
    archive = (root / new_id("revoked")).resolve()
    if auth.parent != root or archive.parent != root:
        raise ChannelError("Invalid WhatsApp credential directory")
    if auth.exists():
        auth.rename(archive)


async def install_bridge(state_dir: Path) -> None:
    node = node_executable()
    npm = shutil.which("npm")
    npm_cli = Path(npm).resolve().parent / "node_modules/npm/bin/npm-cli.js" if npm else Path()
    # On Unix npm is commonly a symlink directly to npm-cli.js.
    if npm and not npm_cli.is_file() and Path(npm).resolve().name == "npm-cli.js":
        npm_cli = Path(npm).resolve()
    if not npm_cli.is_file():
        raise ChannelError("WhatsApp setup needs npm alongside Node.js on the server")
    process = await asyncio.create_subprocess_exec(
        node,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **child_options(),
    )
    try:
        async with asyncio.timeout(15):
            version, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode or int(version.decode().strip().lstrip("v").split(".")[0]) < 22:
        raise ChannelError("WhatsApp needs Node.js 22 or newer")
    target = bridge_directory(state_dir)

    def prepare() -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / ".ready").unlink(missing_ok=True)
        for name in BRIDGE_FILES:
            shutil.copyfile(BRIDGE_SOURCE / name, target / name)

    await channel_io(prepare)
    process = await asyncio.create_subprocess_exec(
        node,
        str(npm_cli),
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        cwd=target,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **child_options(),
    )
    try:
        async with asyncio.timeout(300):
            code = await process.wait()
        if code:
            raise ChannelError(
                "WhatsApp dependency installation failed. "
                "Check server network access and retry setup."
            )
        # Verify the exact imports before publishing readiness.
        verify = await asyncio.create_subprocess_exec(
            node,
            "--input-type=module",
            "-e",
            "import '@whiskeysockets/baileys'; import 'qrcode'; import 'pino';",
            cwd=target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **child_options(),
        )
        try:
            async with asyncio.timeout(30):
                if await verify.wait():
                    raise ChannelError("WhatsApp dependency verification failed")
        finally:
            if verify.returncode is None:
                verify.kill()
                await verify.wait()
        await channel_io((target / ".ready").write_text, bridge_digest())
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
