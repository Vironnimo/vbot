"""Local Extension declarations for app-owned native execution.

Only locally installed manifests are scanned. Registration collects lazy
factories; ComputerSharing owns grants, instances and every handler lifetime.
This module deliberately imports no server or Runtime services.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import os
import re
import stat
import sys
import uuid
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any, Protocol, override

from core.utils.file_status import is_dir_strict, is_link_status
from core.utils.native_capabilities import (
    MAX_CAPABILITY_CATALOG_BYTES,
    qualified_capability_id,
    valid_capability_id,
    validate_capability_ids,
)

DESKTOP_EXTENSION_API_VERSION = 2
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LOGGER = logging.getLogger("vbot.desktop.extensions")
_PACKAGE_MAX_ENTRIES = 10_000
_PACKAGE_MAX_BYTES = 256 * 1024 * 1024
_PACKAGE_MAX_DEPTH = 64
_PACKAGE_IGNORED_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".tox",
        ".nox",
    }
)


class ExecutionHandler(Protocol):
    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def disconnected(self) -> None: ...

    async def close(self) -> None: ...


type EventSink = Callable[[str, dict[str, Any]], None]
type HandlerFactory = Callable[[EventSink], ExecutionHandler]
type BeforeReload = Callable[[frozenset[str]], None]


@dataclass(frozen=True)
class NativeCapability:
    """One locally installed capability and its lazy native handler factory."""

    id: str
    owner: str
    label: str
    description: str
    factory: HandlerFactory
    available: bool = True
    source: str = ""
    revision: str = ""

    @property
    def identity(self) -> tuple[str, str]:
        return self.source, self.revision

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "label": self.label,
            "description": self.description,
        }


class DesktopExtensionAPI:
    """Declaration-only API passed to a local Extension's ``register`` function."""

    def __init__(
        self, owner: str, *, source: str = "", revision: str = "", legacy: bool = False
    ) -> None:
        self._owner = owner
        self._source = source
        self._revision = revision
        self._legacy = legacy
        self._capabilities: dict[str, NativeCapability] = {}

    def register_capability(
        self,
        name: str,
        label: str,
        factory: HandlerFactory,
        *,
        description: str = "",
        available: bool = True,
    ) -> None:
        if not isinstance(name, str) or not _CAPABILITY_ID.fullmatch(name) or name == "name":
            raise ValueError("A capability needs an identifier of up to 64 lowercase characters")
        if name in self._capabilities:
            raise ValueError("A capability is already declared by this Extension")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 120
            or not label.isprintable()
        ):
            raise ValueError("A capability needs a printable label of up to 120 characters")
        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError("A capability description must be text of up to 2000 characters")
        if not callable(factory) or not isinstance(available, bool):
            raise ValueError("A capability needs a factory and boolean availability")
        capability_id = (
            name
            if self._legacy and name == self._owner
            else qualified_capability_id(self._owner, name)
        )
        if not valid_capability_id(capability_id):
            raise ValueError("The Extension folder name cannot form a native capability identity")
        self._capabilities[name] = NativeCapability(
            capability_id,
            self._owner,
            label.strip(),
            description,
            factory,
            available,
            self._source,
            self._revision,
        )


@dataclass
class DesktopExtensionCatalog:
    """One local snapshot; failed or shadowing identities remain visible."""

    capabilities: dict[str, NativeCapability] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    _cache: dict[tuple[str, str], tuple[str, tuple[NativeCapability, ...] | None]] = field(
        default_factory=dict
    )

    @property
    def factories(self) -> Mapping[str, HandlerFactory]:
        return {name: declaration.factory for name, declaration in self.capabilities.items()}


def load_desktop_extensions(
    roots: Iterable[Path],
    *,
    disabled: Iterable[str] = (),
    previous: DesktopExtensionCatalog | None = None,
    before_load: BeforeReload | None = None,
) -> DesktopExtensionCatalog:
    """Collect trusted local native entries in first-identity-wins root order.

    A server entry point is never imported. A disabled, invalid or server-only
    earlier folder still claims its identity, so it cannot activate a later copy.
    An explicit local reload may pass its previous snapshot. Unchanged packages
    retain their declarations; changed packages get an isolated source namespace.
    All package fingerprints are collected before before_load can approve the
    affected existing capability ids. Declining it imports no replacement code.
    """
    catalog = DesktopExtensionCatalog()
    packages: list[tuple[Path, dict[str, Any], str, str, bool]] = []
    claimed: set[str] = set()
    try:
        disabled_values = tuple(disabled)
        if isinstance(disabled, str | bytes | Mapping) or any(
            not isinstance(name, str) for name in disabled_values
        ):
            raise ValueError("Disabled Extension names must be a list of strings")
        disabled_names = set(disabled_values)
    except TypeError, ValueError:
        catalog.records.append(
            {"name": "extensions.disabled", "status": "failed", "error": "invalid_disabled_list"}
        )
        _LOGGER.warning("Desktop Extensions disabled because their local settings are invalid")
        if before_load is not None:
            before_load(frozenset(previous.capabilities if previous else ()))
        return catalog
    for root in roots:
        try:
            candidates = sorted(root.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            continue
        except OSError as error:
            catalog.records.append(
                {"name": root.name, "status": "failed", "error": type(error).__name__}
            )
            _LOGGER.warning("Desktop Extension root unavailable (error=%s)", type(error).__name__)
            continue
        for candidate in candidates:
            name = candidate.name
            try:
                if not is_dir_strict(candidate):
                    continue
            except OSError as error:
                claimed.add(name)
                catalog.records.append(
                    {"name": name, "status": "failed", "error": type(error).__name__}
                )
                _LOGGER.warning(
                    "Desktop Extension unavailable (extension=%s error=%s)",
                    name,
                    type(error).__name__,
                )
                continue
            record: dict[str, Any] = {"name": name, "status": "loaded", "capability_errors": []}
            catalog.records.append(record)
            if name in claimed:
                record["status"] = "overridden"
                continue
            claimed.add(name)
            if name in disabled_names:
                record["status"] = "disabled"
                continue
            try:
                source, revision, legacy = _source_identity(candidate)
                packages.append((candidate, record, source, revision, legacy))
            except Exception as error:
                record.update(status="failed", error=f"{type(error).__name__}: {error}")
                _LOGGER.warning(
                    "Desktop Extension registration failed (extension=%s error=%s)",
                    name,
                    type(error).__name__,
                )
    if before_load is not None:
        observed = {
            (source, candidate.name): revision for candidate, _, source, revision, _ in packages
        }
        affected = frozenset(
            name
            for name, declaration in (previous.capabilities.items() if previous else ())
            if observed.get((declaration.source, declaration.owner)) != declaration.revision
        )
        before_load(affected)
    for candidate, record, source, revision, legacy in packages:
        name = candidate.name
        try:
            cache_key = (source, name)
            cached = previous._cache.get(cache_key) if previous is not None else None
            declarations = (
                cached[1]
                if cached is not None and cached[0] == revision
                else _load_declarations(candidate, source, revision, legacy)
            )
            if (cached is None or cached[0] != revision) and _source_identity(candidate) != (
                source,
                revision,
                legacy,
            ):
                raise ValueError("Desktop Extension package changed during registration")
            catalog._cache[cache_key] = (revision, declarations)
            if declarations is None:
                record["status"] = "server_only"
                continue
            combined = {**catalog.capabilities}
            for capability in declarations:
                if capability.id in combined:
                    raise ValueError(f"Native capability {capability.id} is already registered")
                combined[capability.id] = capability
            validate_capability_ids(list(combined))
            if (
                len(json.dumps([item.describe() for item in combined.values()]).encode())
                > MAX_CAPABILITY_CATALOG_BYTES
            ):
                raise ValueError("Native capability catalog exceeds the 4 MiB transport budget")
            catalog.capabilities = combined
            record["capabilities"] = [item.id for item in declarations]
            if previous is not None:
                record["change"] = (
                    "unchanged"
                    if cached is not None and cached[0] == revision
                    else "updated"
                    if cached is not None
                    else "added"
                )
        except Exception as error:
            record.update(status="failed", error=f"{type(error).__name__}: {error}")
            _LOGGER.warning(
                "Desktop Extension registration failed (extension=%s error=%s)",
                name,
                type(error).__name__,
            )
    if previous is not None:
        present = {record["name"] for record in catalog.records}
        for owner in sorted({item.owner for item in previous.capabilities.values()} - present):
            catalog.records.append({"name": owner, "status": "removed", "capability_errors": []})
    return catalog


def _source_identity(root: Path) -> tuple[str, str, bool]:
    resolved = root.resolve()
    builtin_root = Path(__file__).resolve().parent.parent / "resources" / "extensions"
    legacy = (
        root.name in {"terminal", "computer_use"}
        and resolved == (builtin_root / root.name).resolve()
    )
    source = (
        f"builtin:{root.name}"
        if legacy
        else hashlib.sha256(os.path.normcase(str(resolved)).encode()).hexdigest()
    )
    return source, _package_revision(resolved), legacy


def _package_revision(root: Path) -> str:
    """Fingerprint installed inputs, never mutable caches or linked directory trees.

    Root aliases are resolved by the caller. Every other traversal or read failure
    fails the package instead of reusing declarations from an incomplete scan.
    """
    digest = hashlib.sha256()
    entry_count = 0
    byte_count = 0

    def check_directory(directory: Path, expected: os.stat_result) -> None:
        current = directory.lstat()
        if (
            is_link_status(current)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_mtime_ns)
            != (expected.st_dev, expected.st_ino, expected.st_mtime_ns)
        ):
            raise ValueError("Desktop Extension package changed during reload")

    def visit(directory: Path, depth: int, expected: os.stat_result) -> None:
        nonlocal entry_count, byte_count
        if depth > _PACKAGE_MAX_DEPTH:
            raise ValueError("Desktop Extension package exceeds the directory depth budget")
        check_directory(directory, expected)
        entries = []
        with os.scandir(directory) as scan:
            for entry in scan:
                entry_count += 1
                if entry_count > _PACKAGE_MAX_ENTRIES:
                    raise ValueError("Desktop Extension package exceeds the file entry budget")
                if entry.name.casefold() not in _PACKAGE_IGNORED_NAMES:
                    entries.append(entry)
        check_directory(directory, expected)
        for entry in sorted(entries, key=lambda item: item.name):
            path = Path(entry.path)
            # DirEntry.stat leaves inode/device zero on Windows; path lstat
            # supplies the identity needed to check the subsequently opened file.
            status = path.lstat()
            if is_link_status(status):
                raise ValueError("Desktop Extension package must not contain links or junctions")
            if stat.S_ISDIR(status.st_mode):
                visit(path, depth + 1, status)
                continue
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("Desktop Extension package must contain only regular files")
            if path.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            if byte_count + status.st_size > _PACKAGE_MAX_BYTES:
                raise ValueError("Desktop Extension package exceeds the content byte budget")
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(status.st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    status.st_dev,
                    status.st_ino,
                    status.st_size,
                    status.st_mtime_ns,
                ):
                    raise ValueError("Desktop Extension package changed during reload")
                read_bytes = 0
                while chunk := stream.read(1024 * 1024):
                    read_bytes += len(chunk)
                    byte_count += len(chunk)
                    if byte_count > _PACKAGE_MAX_BYTES:
                        raise ValueError(
                            "Desktop Extension package exceeds the content byte budget"
                        )
                    digest.update(chunk)
                finished = os.fstat(stream.fileno())
                if read_bytes != status.st_size or finished.st_mtime_ns != status.st_mtime_ns:
                    raise ValueError("Desktop Extension package changed during reload")
        check_directory(directory, expected)

    visit(root, 0, root.lstat())
    return digest.hexdigest()


class _SourceLoader(importlib.machinery.SourceFileLoader):
    @override
    def get_code(self, fullname: str) -> CodeType:
        # Native reloads must not reuse timestamp/size based .pyc caches.
        return compile(self.get_data(self.path), self.path, "exec", dont_inherit=True)


def _check_import_path(root: Path, path: Path, *, directory: bool = False) -> None:
    """Apply the package fingerprint's inclusion and link rules to imports."""
    # On Windows an 8.3 alias can name a directory the scanner excluded.
    resolved_parts = path.resolve(strict=True).relative_to(root).parts
    if any(part.casefold() in _PACKAGE_IGNORED_NAMES for part in resolved_parts):
        raise ValueError("Desktop Extension imports must stay in the fingerprinted package")
    current = root
    parts = path.relative_to(root).parts
    for part in parts:
        current /= part
        if part == ".." or is_link_status(current.lstat()):
            raise ValueError("Desktop Extension imports must stay in the fingerprinted package")
    mode = path.lstat().st_mode
    if directory and stat.S_ISDIR(mode):
        return
    if directory or not stat.S_ISREG(mode) or path.suffix.casefold() in {".pyc", ".pyo"}:
        raise ValueError("Desktop Extension modules must be fingerprinted package files")


class _SourceFinder(importlib.abc.MetaPathFinder):
    @override
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        namespace, _, _ = fullname.partition(".")
        if not namespace.startswith("vbot_desktop_ext_") or namespace not in sys.modules:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            raise ModuleNotFoundError(fullname)
        root = Path(sys.modules[namespace].__path__[0])
        for directory in spec.submodule_search_locations or ():
            _check_import_path(root, Path(directory), directory=True)
        if spec.origin is not None:
            _check_import_path(root, Path(spec.origin))
        if isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = _SourceLoader(fullname, spec.loader.path)
        elif spec.loader is not None and not isinstance(
            spec.loader, importlib.machinery.ExtensionFileLoader
        ):
            raise ImportError("Unsupported Desktop Extension module loader")
        return spec


def _load_declarations(
    root: Path, source: str, revision: str, legacy: bool
) -> tuple[NativeCapability, ...] | None:
    try:
        manifest = json.loads((root / "extension.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(manifest, dict):
        raise ValueError("An Extension manifest must be an object")
    declaration = manifest.get("desktop")
    if declaration is None:
        return None
    if not isinstance(declaration, dict):
        raise ValueError("A Desktop declaration must be an object")
    version = declaration.get("api_version", 1)
    if type(version) is not int or version < 1 or version > DESKTOP_EXTENSION_API_VERSION:
        raise ValueError("The Desktop Extension API version is unsupported")
    entry = declaration.get("entry")
    if (
        not isinstance(entry, str)
        or not entry
        or "\\" in entry
        or ":" in entry
        or "\x00" in entry
        or entry.startswith("/")
        or any(part in {"", ".", ".."} for part in entry.split("/"))
        or not entry.endswith(".py")
    ):
        raise ValueError("A Desktop entry must be a relative Python file")
    owner_name = root.name
    root = root.resolve(strict=True)
    path = root / entry
    _check_import_path(root, path)
    # A fresh package namespace allows relative imports without executing a
    # server-side __init__.py, or mixing edited modules with an earlier load.
    digest = hashlib.sha256(f"{root.resolve()}:{revision}".encode()).hexdigest()[:16]
    package_name = f"vbot_desktop_ext_{digest}_{uuid.uuid4().hex[:12]}"
    if not any(isinstance(finder, _SourceFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _SourceFinder())
    package = ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    entry_package = package_name
    entry_directory = root
    for part in Path(entry).parts[:-1]:
        entry_package += f".{part}"
        entry_directory /= part
        namespace = ModuleType(entry_package)
        namespace.__path__ = [str(entry_directory)]
        sys.modules[entry_package] = namespace
    spec = importlib.util.spec_from_file_location(
        f"{entry_package}.entry", path, loader=_SourceLoader(f"{entry_package}.entry", str(path))
    )
    if spec is None or spec.loader is None:
        raise ValueError("The Desktop entry cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        api = DesktopExtensionAPI(owner_name, source=source, revision=revision, legacy=legacy)
        register = getattr(module, "register", None)
        if not callable(register):
            raise ValueError("The Desktop entry needs a register function")
        result = register(api)
        if result is not None:
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise ValueError("Desktop registration must be synchronous and return None")
        capabilities = tuple(api._capabilities.values())
    except BaseException:
        _remove_namespace(package_name)
        raise
    remaining = len(capabilities)

    def release_namespace() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining <= 0:
            _remove_namespace(package_name)

    # A live handler's event sink retains its declaration. Old relative imports
    # therefore remain valid until both the catalog and its handlers retire.
    for capability in capabilities:
        weakref.finalize(capability, release_namespace)
    if not capabilities:
        release_namespace()
    return capabilities


def _remove_namespace(package_name: str) -> None:
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(package_name + "."):
            sys.modules.pop(name, None)
