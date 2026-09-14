"""Source-readable application payload collection shared by builds and customization."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

SHAPES = ("server", "server-desktop", "desktop-client")
APP_COMMON = ("core", "cli")
APP_SERVER = ("server", "resources", "webui/dist")
APP_DESKTOP = ("desktop",)
APP_FILES = ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md")
IGNORED_NAMES = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
NATIVE_SOURCE_FILES = (
    "scripts/windows/launcher.c",
    "scripts/windows/launcher.rc",
    "scripts/windows/launcher.manifest",
    "scripts/windows/desktop.manifest",
    "desktop/icon.ico",
    "scripts/build_windows.py",
)


class PayloadError(RuntimeError):
    """Application source cannot form a safe, complete payload."""


def native_source_digest(source: Path) -> str:
    """Fingerprint every source input that can change a compiled native host."""

    source = source.resolve()
    value = hashlib.sha256()
    for relative in NATIVE_SOURCE_FILES:
        path = source / relative
        if not path.is_file():
            raise PayloadError(f"required native host source is missing: {relative}")
        value.update(relative.encode("utf-8"))
        value.update(b"\0")
        contents = path.read_bytes()
        if path.suffix != ".ico":
            contents = contents.replace(b"\r\n", b"\n")
        value.update(hashlib.sha256(contents).digest())
    return value.hexdigest()


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


def _copy_tree(
    source: Path, destination: Path, payload_root: Path, *, assets: Path | None = None
) -> None:
    _reject_link(source, payload_root)
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        _reject_link(child, payload_root)
        if child.name in IGNORED_NAMES or child.suffix in {".pyc", ".pyo"}:
            continue
        target = destination / child.name
        relative = child.relative_to(payload_root)
        if (
            assets is not None
            and relative.parts[:2] == ("resources", "extensions")
            and (
                len(relative.parts) == 4
                and child.name == "web"
                and (child.parent / "ui" / "page.html").is_file()
            )
        ):
            continue
        if child.is_dir():
            _copy_tree(child, target, payload_root, assets=assets)
        elif child.is_file():
            shutil.copy2(child, target)


def copy_application(
    source: Path, destination: Path, shape: str, *, assets: Path | None = None
) -> None:
    """Copy the safe runtime source surface while preserving repository-relative paths."""
    source = source.resolve()
    for relative in app_paths(shape):
        origin = assets if assets is not None and relative == "webui/dist" else source
        item = origin / relative
        if not item.exists():
            raise PayloadError(f"required application payload is missing: {relative}")
        _reject_link(item, origin)
        target = destination / relative
        if item.is_dir():
            _copy_tree(item, target, origin, assets=assets)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    if assets is not None and shape != "desktop-client":
        for page in (source / "resources" / "extensions").glob("*/ui/page.html"):
            page_assets = page.parent.parent.relative_to(source) / "web"
            cached = assets / page_assets
            if not (cached / "page.html").is_file():
                raise PayloadError(f"required Extension page payload is missing: {page_assets}")
            _copy_tree(cached, destination / page_assets, assets)
