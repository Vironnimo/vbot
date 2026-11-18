"""Global Skill roots, disabled-package exclusions and registry scanning."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.extensions import ExtensionRegistry
from core.skills._documentation import clear_documentation_cache, compose_vbot_docs
from core.skills._packages import PackageError
from core.skills.policy import SkillPackageRef
from core.skills.skills import SKILL_ORIGIN_BUNDLED, SKILL_ORIGIN_GLOBAL, SkillRegistry
from core.storage import StorageManager
from core.storage.layout import DataDirectoryLayout
from core.utils.file_status import is_dir_strict, is_file_strict
from core.utils.log_conditions import LoggedConditions

_SKILLS_DIRNAME = "skills"

_AGENTS_DIRNAME = "agents"
_DOCUMENTATION_FAILURES = LoggedConditions()


@dataclass(frozen=True)
class PreparedGlobalSkills:
    """One scan and its immutable documentation package, published together."""

    registry: SkillRegistry
    docs_path: Path | None


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
    # A package-root scan belongs to its containing home for Skill Policy.
    home: Path | None = None


def _global_roots(
    storage: StorageManager,
    resources_path: Path,
    settings: dict[str, object],
    extensions: ExtensionRegistry | None,
    logger: Any,
    docs_path: Path | None = None,
) -> list[_GlobalRoot]:
    """Return the global Skill scan roots in first-found-wins precedence order.

    Every global source outranks the bundled Skills: the user's own global home
    (``<data_dir>/skills``) first, then the configured ``skill_directories`` in
    their listed order, then each loaded Extension's ``skills/`` folder, and the
    bundled Skills last. A prepared vbot-docs package replaces only its bundled
    source package; the other bundled Skills keep their installation paths.
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
        *_bundled_roots(resources_path / _SKILLS_DIRNAME, docs_path),
    ]


def bundled_skill_path(root: Path, name: str, docs_path: Path | None) -> Path:
    """Use the published immutable generation for the bundled documentation package."""
    return docs_path if name == "vbot-docs" and docs_path is not None else root / name


def _bundled_roots(root: Path, docs_path: Path | None) -> list[_GlobalRoot]:
    if docs_path is None:
        return [_GlobalRoot(root, "bundled")]
    try:
        packages = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        # Let the ordinary scan retain its root-level access diagnostic.
        return [_GlobalRoot(root, "bundled")]
    roots: list[_GlobalRoot] = []
    for candidate in packages:
        package = bundled_skill_path(root, candidate.name, docs_path)
        try:
            if not is_dir_strict(package) or not is_file_strict(package / "SKILL.md"):
                continue
        except OSError:
            # Keep an unreadable package for the owning scanner's diagnostic.
            pass
        roots.append(_GlobalRoot(package, "bundled", home=package.parent))
    return roots


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
                excluded.add(((root.home or root.root).resolve(), ref.name))
    return frozenset(excluded)


def _origin_layers(roots: list[_GlobalRoot]) -> list[str | None]:
    """Return each root's origin without depending on how many packages it holds."""
    return [
        SKILL_ORIGIN_BUNDLED if root.kind == "bundled" else SKILL_ORIGIN_GLOBAL for root in roots
    ]


def load_global_skill_registry(
    *,
    storage: StorageManager,
    resources_path: Path,
    settings: dict[str, object],
    fallback_environment: dict[str, str],
    extensions: ExtensionRegistry | None,
    disabled_packages: Iterable[SkillPackageRef],
    logger: Any,
    reset_documentation_cache: bool = False,
) -> PreparedGlobalSkills:
    environment = dict(fallback_environment)
    environment.update(os.environ)
    source = resources_path / _SKILLS_DIRNAME / "vbot-docs"
    cache_root = DataDirectoryLayout(storage.data_dir).skill_documentation
    condition = (source, cache_root)
    try:
        if reset_documentation_cache:
            clear_documentation_cache(cache_root)
        docs_path = compose_vbot_docs(
            source, cache_root, extensions.records() if extensions is not None else ()
        )
    except (OSError, PackageError) as error:
        # A failed build never selects a previous generation. The original
        # bundled package remains inspectable and the normal scanner diagnoses
        # any unreadable source; other Skills still load.
        docs_path = None
        if _DOCUMENTATION_FAILURES.started(condition, type(error).__name__):
            logger.warning(
                "Could not assemble Extension documentation (error=%s)", type(error).__name__
            )
    else:
        if _DOCUMENTATION_FAILURES.ended(condition):
            logger.info("Restored Extension documentation assembly")
    global_roots = _global_roots(storage, resources_path, settings, extensions, logger, docs_path)
    roots = [root.root for root in global_roots]
    registry = SkillRegistry.load(
        roots[0],
        extra_dirs=roots[1:],
        environment=environment,
        origins=_origin_layers(global_roots),
        package_sources=[(root.kind, root.location) for root in global_roots],
        excluded_packages=_package_exclusions(
            global_roots,
            lambda agent_id: storage.data_dir / _AGENTS_DIRNAME / agent_id / _SKILLS_DIRNAME,
            disabled_packages,
        ),
    )
    return PreparedGlobalSkills(registry, docs_path)
