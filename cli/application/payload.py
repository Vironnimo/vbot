"""Source-readable application payload collection for the package builders."""

from __future__ import annotations

import shutil
from pathlib import Path

SHAPES = ("server", "server-desktop", "desktop-client")
APP_COMMON = ("core", "cli")
APP_SERVER = ("server", "resources", "webui/dist")
APP_DESKTOP = ("desktop",)
APP_FILES = ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md")
IGNORED_NAMES = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


class PayloadError(RuntimeError):
    """Application source cannot form a safe, complete payload."""


def app_paths(shape: str) -> tuple[str, ...]:
    """Return source-relative application paths for an installation shape."""
    if shape not in SHAPES:
        raise PayloadError(f"unsupported install shape: {shape}")
    paths = list(APP_COMMON)
    if shape == "server":
        paths.append("desktop/icon.ico")
    if shape != "desktop-client":
        paths.extend(APP_SERVER)
    if shape != "server":
        paths.extend(APP_DESKTOP)
    paths.extend(APP_FILES)
    return tuple(paths)


def _reject_link(path: Path, source: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise PayloadError(f"source payload contains a link: {path.relative_to(source)}")


def _copy_tree(source: Path, destination: Path, payload_root: Path) -> None:
    _reject_link(source, payload_root)
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        _reject_link(child, payload_root)
        if child.name in IGNORED_NAMES or child.suffix in {".pyc", ".pyo"}:
            continue
        target = destination / child.name
        if child.relative_to(payload_root).parts == ("resources", "native"):
            continue
        if child.is_dir():
            _copy_tree(child, target, payload_root)
        elif child.is_file():
            shutil.copy2(child, target)


def copy_application(source: Path, destination: Path, shape: str, *, search_target: str) -> None:
    """Copy the safe runtime source surface while preserving repository-relative paths.

    *search_target* names the ripgrep build the package's platform runs.
    """
    source = source.resolve()
    for relative in app_paths(shape):
        item = source / relative
        if not item.exists():
            raise PayloadError(f"required application payload is missing: {relative}")
        _reject_link(item, source)
        target = destination / relative
        if item.is_dir():
            _copy_tree(item, target, source)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    if shape != "desktop-client":
        # Imported here, not at module level: native-host compilation imports
        # this module from a stdlib-only build environment. Search provisioning
        # must also work without installed application dependencies.
        from cli.search_runtime import provision_search_runtime
        from core.utils.search_binary import binary_spec

        output, _ = binary_spec(destination / "resources", search_target)
        if (source / "resources/ripgrep.lock.json").is_file():
            candidate, _ = binary_spec(source / "resources", search_target)
            if candidate.is_file() and not candidate.is_symlink():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, output)
        provision_search_runtime(destination / "resources", target=search_target)
