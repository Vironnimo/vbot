"""Global Skill roots, disabled-package exclusions and registry scanning."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.extensions import ExtensionRegistry
from core.skills.policy import SkillPackageRef
from core.skills.skills import SKILL_ORIGIN_BUNDLED, SKILL_ORIGIN_GLOBAL, SkillRegistry
from core.storage import StorageManager

_SKILLS_DIRNAME = "skills"

_AGENTS_DIRNAME = "agents"


@dataclass(frozen=True)
class _GlobalRoot:
    """One global Skill scan root and the package source it represents.

    ``kind`` is a Skill Policy package source (``home``, ``folder``,
    ``extension`` or ``bundled``); ``location`` names the configured folder (as
    written) or the Extension, ``None`` for the home and the bundled root.
    """

    root: Path
    kind: str
    location: str | None = None


def _global_roots(
    storage: StorageManager,
    resources_path: Path,
    settings: dict[str, object],
    extensions: ExtensionRegistry | None,
    logger: Any,
) -> list[_GlobalRoot]:
    """Return the global Skill scan roots in first-found-wins precedence order.

    Every global source outranks the bundled Skills: the user's own global home
    (``<data_dir>/skills``) first, then the configured ``skill_directories`` in
    their listed order, then each loaded Extension's ``skills/`` folder, and the
    bundled ``resources/skills`` last. :func:`_origin_layers` relies on the bundled
    root being the final entry.
    """
    raw_directories = settings.get("skill_directories", [])
    folders: list[_GlobalRoot] = []
    if not isinstance(raw_directories, list):
        logger.warning("settings.skill_directories must be a list; ignoring value")
    else:
        for raw_directory in raw_directories:
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                logger.warning("Ignoring invalid skill directory setting: %r", raw_directory)
                continue
            folders.append(_GlobalRoot(Path(raw_directory).expanduser(), "folder", raw_directory))
    extension_roots = (
        [
            _GlobalRoot(record.root_path / _SKILLS_DIRNAME, "extension", record.name)
            for record in extensions.records()
            if record.status == "loaded"
        ]
        if extensions is not None
        else []
    )
    return [
        _GlobalRoot(storage.data_dir / _SKILLS_DIRNAME, "home"),
        *folders,
        *extension_roots,
        _GlobalRoot(resources_path / _SKILLS_DIRNAME, "bundled"),
    ]


def _package_exclusions(
    roots: list[_GlobalRoot],
    agent_home: Callable[[str], Path],
    refs: Iterable[SkillPackageRef],
) -> frozenset[tuple[Path, str]]:
    """Map turned-off packages to the (resolved Skill home, name) pairs a load skips.

    A package whose source is not scanned (an unloaded Extension, a folder no
    longer configured) maps to nothing; its policy entry waits until it returns.
    """
    excluded: set[tuple[Path, str]] = set()
    for ref in refs:
        if ref.source == "agent" and ref.location is not None:
            excluded.add((agent_home(ref.location).resolve(), ref.name))
            continue
        for root in roots:
            if root.kind == ref.source and root.location == ref.location:
                excluded.add((root.root.resolve(), ref.name))
                break
    return frozenset(excluded)


def _origin_layers(scan_roots: list[Path]) -> list[str | None]:
    """Return the origin tags parallel to :func:`_global_roots` (bundled last)."""
    origins: list[str | None] = [SKILL_ORIGIN_GLOBAL for _ in scan_roots[:-1]]
    origins.append(SKILL_ORIGIN_BUNDLED)
    return origins


def load_global_skill_registry(
    *,
    storage: StorageManager,
    resources_path: Path,
    settings: dict[str, object],
    fallback_environment: dict[str, str],
    extensions: ExtensionRegistry | None,
    disabled_packages: Iterable[SkillPackageRef],
    logger: Any,
) -> SkillRegistry:
    environment = dict(fallback_environment)
    environment.update(os.environ)
    global_roots = _global_roots(storage, resources_path, settings, extensions, logger)
    roots = [root.root for root in global_roots]
    return SkillRegistry.load(
        roots[0],
        extra_dirs=roots[1:],
        environment=environment,
        origins=_origin_layers(roots),
        package_sources=[(root.kind, root.location) for root in global_roots],
        excluded_packages=_package_exclusions(
            global_roots,
            lambda agent_id: storage.data_dir / _AGENTS_DIRNAME / agent_id / _SKILLS_DIRNAME,
            disabled_packages,
        ),
    )
