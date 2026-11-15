"""Local Extension declarations for app-owned native execution.

Only locally installed manifests are scanned. Registration collects lazy
factories; ComputerSharing owns grants, instances and every handler lifetime.
This module deliberately imports no server or Runtime services.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from core.utils.file_status import is_dir_strict, is_file_strict

DESKTOP_EXTENSION_API_VERSION = 1
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LOGGER = logging.getLogger("vbot.desktop.extensions")


class ExecutionHandler(Protocol):
    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def disconnected(self) -> None: ...

    async def close(self) -> None: ...


type EventSink = Callable[[str, dict[str, Any]], None]
type HandlerFactory = Callable[[EventSink], ExecutionHandler]


@dataclass(frozen=True)
class NativeCapability:
    """One locally installed capability and its lazy native handler factory."""

    id: str
    owner: str
    label: str
    description: str
    factory: HandlerFactory
    available: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "label": self.label,
            "description": self.description,
        }


class DesktopExtensionAPI:
    """Declaration-only API passed to a local Extension's ``register`` function."""

    def __init__(self, owner: str) -> None:
        self._owner = owner
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
        self._capabilities[name] = NativeCapability(
            name, self._owner, label.strip(), description, factory, available
        )


@dataclass
class DesktopExtensionCatalog:
    """A startup snapshot; failed or shadowing local identities remain visible."""

    capabilities: dict[str, NativeCapability] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def factories(self) -> Mapping[str, HandlerFactory]:
        return {name: declaration.factory for name, declaration in self.capabilities.items()}


def load_desktop_extensions(
    roots: Iterable[Path], *, disabled: Iterable[str] = ()
) -> DesktopExtensionCatalog:
    """Collect trusted local native entries in first-identity-wins root order.

    A server entry point is never imported. A disabled, invalid or server-only
    earlier folder still claims its identity, so it cannot activate a later copy.
    Entry changes take effect on the next Desktop launch.
    """
    catalog = DesktopExtensionCatalog()
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
                declarations = _load_declarations(candidate)
                if declarations is None:
                    record["status"] = "server_only"
                    continue
                for capability in declarations:
                    if capability.id in catalog.capabilities or len(catalog.capabilities) >= 32:
                        record["capability_errors"].append(capability.id)
                        continue
                    catalog.capabilities[capability.id] = capability
            except Exception as error:
                record.update(status="failed", error=type(error).__name__)
                _LOGGER.warning(
                    "Desktop Extension registration failed (extension=%s error=%s)",
                    name,
                    type(error).__name__,
                )
    return catalog


def _load_declarations(root: Path) -> tuple[NativeCapability, ...] | None:
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
    path = (root / entry).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not is_file_strict(path):
        raise ValueError("A Desktop entry must stay inside its Extension")
    # A fresh package namespace allows relative imports without executing a
    # server-side __init__.py, or mixing edited modules with an earlier load.
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    package_name = f"vbot_desktop_ext_{digest}"
    for imported in tuple(sys.modules):
        if imported == package_name or imported.startswith(package_name + "."):
            del sys.modules[imported]
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
    spec = importlib.util.spec_from_file_location(f"{entry_package}.entry", path)
    if spec is None or spec.loader is None:
        raise ValueError("The Desktop entry cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = DesktopExtensionAPI(root.name)
    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError("The Desktop entry needs a register function")
    result = register(api)
    if result is not None:
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise ValueError("Desktop registration must be synchronous and return None")
    return tuple(api._capabilities.values())
