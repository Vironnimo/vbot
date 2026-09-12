"""Internal update command, release, and step result records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cli.server_management import (
    CommandResult,
    ServerInstance,
)

Restart = Callable[[ServerInstance], CommandResult]


ResolveInstance = Callable[..., ServerInstance]


@dataclass(frozen=True)
class CommandRun:
    """Result of one external command invocation."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], Path], CommandRun]


@dataclass(frozen=True)
class ReleaseInfo:
    """Latest release tag plus the prebuilt WebUI asset URL when present."""

    tag: str
    webui_asset_url: str | None


ReleaseLookup = Callable[[], ReleaseInfo]


@dataclass(frozen=True)
class _Step:
    """Outcome of one internal update step; an empty message means 'no note'."""

    ok: bool
    message: str
