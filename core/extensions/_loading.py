"""Discover, import, and register filesystem Extensions with bounded deadlines."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.extensions._api import ExtensionAPI
from core.extensions._declarations import (
    API_VERSION,
    ExtensionManifest,
    ExtensionRecord,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("extensions")
_EXTENSION_PARENT_PACKAGE = "vbot_ext"
_MANIFEST_FILENAME = "extension.json"
_ASYNC_REGISTER_TIMEOUT_SECONDS = 10.0
_detached_register_tasks: set[asyncio.Task[None]] = set()


class _ManifestError(Exception):
    """Raised when an ``extension.json`` manifest is missing required shape."""


class _AsyncRegisterTimeoutError(TimeoutError):
    """Raised when an async Extension registration exceeds its hard deadline."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"async register() timed out after {timeout_seconds:g} seconds")


@dataclass(frozen=True)
class _DiscoveredExtension:
    """One discovered entry point: identity plus on-disk paths."""

    name: str
    root_path: Path
    entry_path: Path


def _discover_extension_paths(extensions_dir: Path) -> list[_DiscoveredExtension]:
    if not extensions_dir.is_dir():
        return []

    discovered: list[_DiscoveredExtension] = []
    try:
        entries = list(extensions_dir.iterdir())
    except OSError as exc:
        _LOGGER.warning("Skipping unreadable Extension directory %s: %s", extensions_dir, exc)
        return []

    for entry in entries:
        if entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            discovered.append(_DiscoveredExtension(entry.stem, entry, entry))
            continue

        if not entry.is_dir():
            continue

        init_entry = entry / "__init__.py"
        if init_entry.is_file():
            discovered.append(_DiscoveredExtension(entry.name, entry, init_entry))
            continue

        extension_entry = entry / "extension.py"
        if extension_entry.is_file():
            discovered.append(_DiscoveredExtension(entry.name, entry, extension_entry))

    return sorted(discovered, key=lambda item: item.name)


def _register_extension(
    discovered: _DiscoveredExtension,
    disabled_names: set[str],
    config_map: dict[str, dict[str, Any]],
    pending: list[tuple[ExtensionRecord, Any]],
    *,
    config_provider: Callable[[str], dict[str, Any]] | None = None,
    credential_resolver: Callable[[str], str] | None = None,
) -> ExtensionRecord:
    """Load one discovered extension into a record (collecting declarations).

    Disabled extensions are never imported. Manifest/import/``register()``
    failures produce a ``failed`` record with detail and never abort the others.
    Async ``register()`` coroutines are appended to *pending* for the loader to
    await before applying declarations. *config_provider* / *credential_resolver*
    are bound to this extension's *name* when its ``ExtensionAPI`` is built, so
    the extension's live reads see only its own config / all credentials.
    """
    name = discovered.name
    if name in disabled_names:
        return ExtensionRecord(
            name=name,
            root_path=discovered.root_path,
            entry_path=discovered.entry_path,
            status="disabled",
        )

    manifest: ExtensionManifest | None = None
    if discovered.root_path.is_dir():
        try:
            manifest = _load_manifest(discovered.root_path)
        except _ManifestError as exc:
            _LOGGER.error("Extension %r manifest invalid: %s", name, exc, exc_info=True)
            return _failed_record(discovered, str(exc))
        if (
            manifest is not None
            and manifest.api_version is not None
            and manifest.api_version > API_VERSION
        ):
            message = (
                f"manifest api_version {manifest.api_version} is newer than supported "
                f"API_VERSION {API_VERSION}"
            )
            _LOGGER.error("Extension %r %s", name, message)
            return _failed_record(discovered, message, manifest=manifest)

    try:
        module = _import_extension_module(name, discovered.entry_path)
    except Exception as exc:
        _LOGGER.error(
            "Failed to load extension %r from %s: %s",
            name,
            discovered.entry_path,
            exc,
            exc_info=True,
        )
        return _failed_record(discovered, f"import failed: {exc}", manifest=manifest)

    record = ExtensionRecord(
        name=name,
        root_path=discovered.root_path,
        entry_path=discovered.entry_path,
        status="loaded",
        manifest=manifest,
    )

    register_fn = getattr(module, "register", None)
    if register_fn is None:
        return record

    bound_config_provider = (lambda: config_provider(name)) if config_provider is not None else None
    api = ExtensionAPI(
        name,
        record.declarations,
        config=config_map.get(name, {}),
        logger=get_logger(f"extensions.{name}"),
        config_provider=bound_config_provider,
        credential_resolver=credential_resolver,
    )
    try:
        result = register_fn(api)
    except Exception as exc:
        _LOGGER.error("Extension %r register() raised: %s", name, exc, exc_info=True)
        record.status = "failed"
        record.error = f"register() raised: {exc}"
        return record

    if inspect.iscoroutine(result):
        pending.append((record, result))
    return record


def _failed_record(
    discovered: _DiscoveredExtension,
    error: str,
    *,
    manifest: ExtensionManifest | None = None,
) -> ExtensionRecord:
    return ExtensionRecord(
        name=discovered.name,
        root_path=discovered.root_path,
        entry_path=discovered.entry_path,
        status="failed",
        error=error,
        manifest=manifest,
    )


def _overridden_record(
    discovered: _DiscoveredExtension,
    winner: ExtensionRecord,
) -> ExtensionRecord:
    """Record a same-name copy an earlier root already claimed (never imported)."""
    return ExtensionRecord(
        name=discovered.name,
        root_path=discovered.root_path,
        entry_path=discovered.entry_path,
        status="overridden",
        overridden_by=str(winner.entry_path),
    )


def _await_pending_registers(pending: list[tuple[ExtensionRecord, Any]]) -> None:
    """Drive every async ``register()`` coroutine to completion, fail-open."""
    for record, coro in pending:
        try:
            _run_coroutine_to_completion(coro, _ASYNC_REGISTER_TIMEOUT_SECONDS)
        except _AsyncRegisterTimeoutError as exc:
            _LOGGER.error("Extension %r %s", record.name, exc)
            record.status = "failed"
            record.error = str(exc)
        except Exception as exc:
            _LOGGER.error(
                "Extension %r async register() raised: %s", record.name, exc, exc_info=True
            )
            record.status = "failed"
            record.error = f"async register() raised: {exc}"


async def _await_pending_registers_async(pending: list[tuple[ExtensionRecord, Any]]) -> None:
    """Drive every async ``register()`` on the live loop, fail-open.

    Same contract as :func:`_await_pending_registers` - sequential order, one
    hard deadline per registration, a timeout failing only that Extension -
    but without the blocking thread join that froze the whole serving loop for
    up to the deadline per Extension during startup and reload. A coroutine
    that suppresses its timeout cancellation is detached after the deadline:
    it may keep running, but it can no longer hold server start or reload.
    """
    loop = asyncio.get_running_loop()
    for record, coro in pending:
        task = loop.create_task(coro)
        _detached_register_tasks.add(task)
        task.add_done_callback(_detached_register_tasks.discard)
        done, _ = await asyncio.wait({task}, timeout=_ASYNC_REGISTER_TIMEOUT_SECONDS)
        if not done:
            task.cancel()
            message = str(_AsyncRegisterTimeoutError(_ASYNC_REGISTER_TIMEOUT_SECONDS))
            _LOGGER.error("Extension %r %s", record.name, message)
            record.status = "failed"
            record.error = message
            continue
        try:
            await task
        except Exception as exc:
            _LOGGER.error(
                "Extension %r async register() raised: %s", record.name, exc, exc_info=True
            )
            record.status = "failed"
            record.error = f"async register() raised: {exc}"


def _run_coroutine_to_completion(coro: Any, timeout_seconds: float | None = None) -> None:
    """Run *coro* on a private loop, enforcing a hard deadline when supplied.

    A timed daemon worker is required even when this thread has no running loop:
    an asyncio timeout still waits for cancellation, which uncooperative
    Extension code can suppress. The loader instead stops waiting at the hard
    deadline and requests cancellation as best effort; a coroutine that ignores
    it may keep its daemon worker alive, but cannot hold server start or reload
    hostage. Callers without a deadline retain deterministic blocking behavior.
    """
    if timeout_seconds is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return

    error: list[BaseException] = []
    worker_state: list[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = []
    worker_state_lock = threading.Lock()

    async def _drive_coroutine() -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        with worker_state_lock:
            worker_state.append((loop, task))
        await task

    def _runner() -> None:
        try:
            asyncio.run(_drive_coroutine())
        except BaseException as exc:  # surfaced to the caller's thread
            error.append(exc)

    thread = threading.Thread(
        target=_runner,
        name="vbot-extension-async",
        daemon=timeout_seconds is not None,
    )
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        with worker_state_lock:
            state = worker_state[0] if worker_state else None
        if state is not None:
            loop, task = state
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                _LOGGER.debug("Async Extension registration loop closed at its timeout boundary")
        if timeout_seconds is None:  # defensive: an unbounded join cannot time out
            raise RuntimeError("Coroutine worker remained alive after an unbounded join")
        raise _AsyncRegisterTimeoutError(timeout_seconds)
    if error:
        raise error[0]


def _load_manifest(directory: Path) -> ExtensionManifest | None:
    """Parse an optional ``extension.json``; raise ``_ManifestError`` if malformed."""
    manifest_path = directory / _MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except UnicodeError as exc:
        raise _ManifestError(f"{_MANIFEST_FILENAME} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _ManifestError(f"invalid JSON in {_MANIFEST_FILENAME}: {exc.msg}") from exc
    except OSError as exc:
        raise _ManifestError(f"cannot read {_MANIFEST_FILENAME}: {exc}") from exc

    if not isinstance(raw, dict):
        raise _ManifestError(f"{_MANIFEST_FILENAME} must be a JSON object")

    api_version = raw.get("api_version")
    if api_version is not None and (
        isinstance(api_version, bool) or not isinstance(api_version, int)
    ):
        raise _ManifestError("api_version must be an integer")

    return ExtensionManifest(
        version=_manifest_optional_str(raw, "version"),
        description=_manifest_optional_str(raw, "description"),
        api_version=api_version,
        display_name=_manifest_optional_str(raw, "name"),
    )


def _manifest_optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise _ManifestError(f"{key} must be a string")
    return value


def _ensure_extension_parent_package() -> None:
    parent_module = sys.modules.get(_EXTENSION_PARENT_PACKAGE)
    if parent_module is None:
        parent_module = types.ModuleType(_EXTENSION_PARENT_PACKAGE)
        parent_module.__package__ = _EXTENSION_PARENT_PACKAGE
        parent_module.__path__ = []
        sys.modules[_EXTENSION_PARENT_PACKAGE] = parent_module
        return

    if not isinstance(getattr(parent_module, "__path__", None), list):
        parent_module.__path__ = []


def purge_extension_modules() -> None:
    """Drop the synthetic ``vbot_ext`` namespace and every extension module.

    Removes from ``sys.modules`` the parent package ``vbot_ext`` plus every
    ``vbot_ext.<name>`` entry point **and** ``vbot_ext.<name>.<sub>`` submodule.
    ``Runtime.reload_extensions`` calls this between tearing the old layer down and
    building the new one. Without it, a fresh :meth:`ExtensionRegistry.load`
    replaces only an extension's **entry-point** module, so an edited **submodule**
    of a package extension would silently keep its stale cached version — a
    relative ``import`` inside the reloaded entry point resolves through
    ``sys.modules`` first and finds the old submodule. Purging is safe for a
    still-referenced old registry: its handlers hold direct function references
    that never go back through ``sys.modules``, and
    :func:`_ensure_extension_parent_package` recreates the parent namespace on the
    next load.
    """
    prefix = f"{_EXTENSION_PARENT_PACKAGE}."
    for module_name in list(sys.modules):
        if module_name == _EXTENSION_PARENT_PACKAGE or module_name.startswith(prefix):
            del sys.modules[module_name]


def _extension_spec(module_name: str, entry_path: Path) -> Any:
    if entry_path.name == "__init__.py":
        return importlib.util.spec_from_file_location(
            module_name,
            entry_path,
            submodule_search_locations=[str(entry_path.parent)],
        )

    return importlib.util.spec_from_file_location(module_name, entry_path)


def _import_extension_module(name: str, entry_path: Path) -> types.ModuleType:
    """Import one extension entry point under the synthetic ``vbot_ext`` namespace."""
    module_name = f"{_EXTENSION_PARENT_PACKAGE}.{name}"
    spec = _extension_spec(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"No loader for extension entry point: {entry_path}")

    _ensure_extension_parent_package()
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
        raise

    parent_module = sys.modules.get(_EXTENSION_PARENT_PACKAGE)
    if parent_module is not None:
        setattr(parent_module, name, module)
    return module
