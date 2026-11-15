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
from pathlib import Path, PurePosixPath
from typing import Any

from core.extensions._api import ExtensionAPI
from core.extensions._callbacks import detach_extension_task
from core.extensions._declarations import (
    API_VERSION,
    ExtensionDocumentation,
    ExtensionManifest,
    ExtensionRecord,
    PageDeclaration,
    _diagnose_capability,
)
from core.extensions.contributions import EXTENSION_FEATURES
from core.utils.file_status import is_dir_strict, is_file_strict
from core.utils.ids import is_reserved_name
from core.utils.logging import get_logger

_LOGGER = get_logger("extensions")
_EXTENSION_PARENT_PACKAGE = "vbot_ext"
_MANIFEST_FILENAME = "extension.json"
_ASYNC_REGISTER_TIMEOUT_SECONDS = 10.0


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
    error: str | None = None


def _discover_extension_paths(extensions_dir: Path) -> list[_DiscoveredExtension] | None:
    """List one root's Extension entry points; ``None`` when the root is unavailable.

    A missing or unreadable root proves nothing about which Extensions exist.
    """
    discovered: list[_DiscoveredExtension] = []
    try:
        if not is_dir_strict(extensions_dir):
            return None
        entries = list(extensions_dir.iterdir())
    except OSError as exc:
        _LOGGER.warning("Skipping unreadable Extension directory %s: %s", extensions_dir, exc)
        return None

    for entry in entries:
        try:
            if is_file_strict(entry):
                if entry.suffix == ".py" and entry.stem != "__init__":
                    discovered.append(_DiscoveredExtension(entry.stem, entry, entry))
                continue
            if not is_dir_strict(entry):
                continue
        except OSError as exc:
            # The root listed this name, but its kind is unavailable. Reserve
            # both possible identities of a .py entry (module or directory),
            # so neither an owner cleanup nor a later root can replace it.
            names = {entry.name}
            if entry.suffix == ".py" and entry.stem != "__init__":
                names.add(entry.stem)
            discovered.extend(
                _DiscoveredExtension(name, entry, entry, str(exc)) for name in sorted(names)
            )
            continue
        for entry_path in (entry / "__init__.py", entry / "extension.py"):
            try:
                found = is_file_strict(entry_path)
            except OSError as exc:
                discovered.append(_DiscoveredExtension(entry.name, entry, entry_path, str(exc)))
                break
            if found:
                discovered.append(_DiscoveredExtension(entry.name, entry, entry_path))
                break
        else:
            manifest_path = entry / _MANIFEST_FILENAME
            try:
                if is_file_strict(manifest_path):
                    discovered.append(_DiscoveredExtension(entry.name, entry, manifest_path))
            except OSError as exc:
                discovered.append(_DiscoveredExtension(entry.name, entry, manifest_path, str(exc)))

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
    disabled = name in disabled_names
    manifest: ExtensionManifest | None = None
    if discovered.root_path != discovered.entry_path:
        try:
            manifest = _load_manifest(discovered.root_path)
        except _ManifestError as exc:
            _LOGGER.error("Extension %r manifest invalid: %s", name, exc, exc_info=True)
            return _failed_record(discovered, str(exc), disabled=disabled)

    # Documentation and display metadata remain readable while the code is
    # disabled or cannot load. Only the winning copy reaches this function.
    if disabled:
        return ExtensionRecord(
            name=name,
            root_path=discovered.root_path,
            entry_path=discovered.entry_path,
            status="disabled",
            manifest=manifest,
            error=discovered.error,
        )

    if discovered.error is not None:
        _LOGGER.warning(
            "Extension discovery failed (extension=%s path=%s error=%s)",
            name,
            discovered.entry_path,
            discovered.error,
        )
        return _failed_record(
            discovered, f"discovery failed: {discovered.error}", manifest=manifest
        )

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

    missing_features = set(manifest.required_features) - EXTENSION_FEATURES if manifest else set()
    if missing_features:
        return _failed_record(
            discovered,
            "unsupported required features: " + ", ".join(sorted(missing_features)),
            manifest=manifest,
        )

    try:
        module = (
            _import_extension_module(discovered)
            if discovered.entry_path.name != _MANIFEST_FILENAME
            else None
        )
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
        if manifest is not None:
            for page in manifest.pages:
                api.register_page(**page)
            for contribution in manifest.contributions:
                fields = dict(contribution)
                contribution_id = fields.pop("id", None)
                api.register_contribution(contribution_id, **fields)
        result = register_fn(api) if register_fn is not None else None
    except (Exception, asyncio.CancelledError) as exc:
        _LOGGER.error("Extension %r register() raised: %s", name, exc, exc_info=True)
        record.status = "failed"
        record.error = f"register() raised: {str(exc) or type(exc).__name__}"
        return record

    if inspect.iscoroutine(result):
        pending.append((record, result))
    return record


def _failed_record(
    discovered: _DiscoveredExtension,
    error: str,
    *,
    manifest: ExtensionManifest | None = None,
    disabled: bool = False,
) -> ExtensionRecord:
    return ExtensionRecord(
        name=discovered.name,
        root_path=discovered.root_path,
        entry_path=discovered.entry_path,
        status="disabled" if disabled else "failed",
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
        except (Exception, asyncio.CancelledError) as exc:
            _LOGGER.error(
                "Extension %r async register() raised: %s", record.name, exc, exc_info=True
            )
            record.status = "failed"
            record.error = f"async register() raised: {str(exc) or type(exc).__name__}"


def _detach_register_task(task: asyncio.Task[None], record: ExtensionRecord) -> None:
    """Request cancellation of abandoned registration work and keep it observable."""
    detach_extension_task(task, extension_name=record.name, description="register()")
    task.cancel()


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
    for index, (record, coro) in enumerate(pending):
        task = loop.create_task(coro, name=f"extension-register:{record.name}")
        try:
            done, _ = await asyncio.wait({task}, timeout=_ASYNC_REGISTER_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            _detach_register_task(task, record)
            for _, unstarted in pending[index + 1 :]:
                unstarted.close()
            raise
        if not done:
            message = str(_AsyncRegisterTimeoutError(_ASYNC_REGISTER_TIMEOUT_SECONDS))
            _LOGGER.error("Extension %r %s", record.name, message)
            record.status = "failed"
            record.error = message
            _detach_register_task(task, record)
            continue
        try:
            # The child is complete: its cancellation is a registration failure,
            # distinct from cancellation of the loader while it was waiting.
            task.result()
        except (Exception, asyncio.CancelledError) as exc:
            _LOGGER.error(
                "Extension %r async register() raised: %s", record.name, exc, exc_info=True
            )
            record.status = "failed"
            record.error = f"async register() raised: {str(exc) or type(exc).__name__}"


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
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
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
        documentation=_manifest_documentation(raw),
        pages=_manifest_objects(raw, "pages"),
        contributions=_manifest_objects(raw, "contributions"),
        required_features=_manifest_features(raw),
        desktop=_manifest_desktop(raw),
    )


def _manifest_objects(raw: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    value = raw.get(key, [])
    if (
        not isinstance(value, list)
        or len(value) > 256
        or any(not isinstance(item, dict) for item in value)
    ):
        raise _ManifestError(f"{key} must be a list of at most 256 declaration objects")
    return tuple(value)


def _manifest_features(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("required_features", [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _ManifestError("required_features must be a list of feature names")
    return tuple(value)


def _manifest_desktop(raw: dict[str, Any]) -> dict[str, Any] | None:
    value = raw.get("desktop")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"entry", "api_version"}:
        raise _ManifestError("desktop must declare entry and optional api_version")
    entry = value.get("entry")
    if (
        not isinstance(entry, str)
        or not entry
        or "\\" in entry
        or ":" in entry
        or "\x00" in entry
        or PurePosixPath(entry).is_absolute()
        or ".." in entry.split("/")
        or "" in entry.split("/")
        or "." in entry.split("/")
        or not entry.endswith(".py")
    ):
        raise _ManifestError("desktop.entry must be a relative Python file")
    version = value.get("api_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _ManifestError("desktop.api_version must be a positive integer")
    return {"entry": entry, "api_version": version}


def _manifest_optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise _ManifestError(f"{key} must be a string")
    if value is not None:
        _validate_manifest_text(value, key)
    return value


def _validate_manifest_text(value: str, key: str) -> None:
    """JSON escapes can carry lone surrogates that no UTF-8 document can retain."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _ManifestError(f"{key} must be valid Unicode text") from exc


def _manifest_documentation(raw: dict[str, Any]) -> tuple[ExtensionDocumentation, ...]:
    """Validate declared document identities; the Skill composer owns file reads."""
    declarations = raw.get("documentation", [])
    if not isinstance(declarations, list):
        raise _ManifestError("documentation must be an array")
    documents: list[ExtensionDocumentation] = []
    paths: set[str] = set()
    for index, entry in enumerate(declarations):
        field = f"documentation[{index}]"
        if not isinstance(entry, dict):
            raise _ManifestError(f"{field} must be an object")
        values: dict[str, str] = {}
        for key in ("path", "title", "description"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _ManifestError(f"{field}.{key} must be a non-empty string")
            _validate_manifest_text(value, f"{field}.{key}")
            values[key] = value
        path = values["path"]
        parts = path.split("/")
        if (
            any(part in {"", ".", ".."} or is_reserved_name(part) for part in parts)
            or PurePosixPath(path).suffix.lower() != ".md"
        ):
            raise _ManifestError(f"{field}.path must be a portable relative Markdown path")
        if path.casefold() in paths:
            raise _ManifestError(f"{field}.path duplicates another documentation path")
        paths.add(path.casefold())
        documents.append(ExtensionDocumentation(**values))
    return tuple(documents)


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


def _import_extension_module(discovered: _DiscoveredExtension) -> types.ModuleType:
    """Import one extension entry point under the synthetic ``vbot_ext`` namespace."""
    name = discovered.name
    entry_path = discovered.entry_path
    module_name = f"{_EXTENSION_PARENT_PACKAGE}.{name}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        entry_path,
        submodule_search_locations=(
            [str(discovered.root_path)] if discovered.root_path != entry_path else None
        ),
    )
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


def resolve_pages(
    records: list[ExtensionRecord],
) -> dict[tuple[str, str], tuple[ExtensionRecord, PageDeclaration, Path]]:
    """Resolve every loaded page entry once for this registration.

    An entry outside its Extension root is diagnosed here and never becomes
    a page of this registration. Cross-references are checked after synchronous
    and asynchronous registration have both finished, regardless of order.
    """
    pages: dict[tuple[str, str], tuple[ExtensionRecord, PageDeclaration, Path]] = {}
    for record in records:
        if record.status != "loaded":
            continue
        root = record.root_path.resolve()
        for declaration in record.declarations.pages:
            entry = (record.root_path / declaration.entry).resolve()
            try:
                entry.relative_to(root)
            except ValueError:
                _diagnose_capability(
                    record, f"page {declaration.page_id!r} skipped: entry escapes root"
                )
                continue
            pages[(record.name, declaration.page_id)] = (record, declaration, entry)
        _validate_contribution_references(record, pages)
        _validate_service_references(record)
    for record, declaration, entry in pages.values():
        refresh_page_availability(record, declaration, entry)
    return pages


def refresh_page_availability(
    record: ExtensionRecord, declaration: PageDeclaration, entry: Path
) -> bool:
    """Refresh transient page/view diagnostics without losing their registration.

    Projection and catalog workers may inspect the same record concurrently. The
    lock covers the disk observation and replaces only diagnostics owned here.
    """
    with record._diagnostic_lock:
        try:
            reason = None if is_file_strict(entry) else "entry is missing"
        except OSError:
            reason = "entry is unreadable"
        messages: tuple[str, ...] = ()
        if reason is not None:
            messages = (
                f"page {declaration.page_id!r} unavailable: {reason}",
                *(
                    f"contribution {item.id!r} unavailable: page {declaration.page_id!r} {reason}"
                    for item in record.declarations.contributions
                    if item.spec["kind"] == "view" and item.spec["page"] == declaration.page_id
                ),
            )
        previous = record._page_availability_errors.get(declaration.page_id, ())
        if previous != messages:
            remaining = [message for message in record.capability_errors if message not in previous]
            added = [message for message in messages if message not in remaining]
            record.capability_errors = remaining + added
            if messages:
                record._page_availability_errors[declaration.page_id] = messages
            else:
                record._page_availability_errors.pop(declaration.page_id, None)
            for message in added:
                _LOGGER.warning("Extension %r %s", record.name, message)
        return reason is None


def _validate_contribution_references(
    record: ExtensionRecord,
    pages: dict[tuple[str, str], tuple[ExtensionRecord, PageDeclaration, Path]],
) -> None:
    """Drop only contributions whose final owner declarations cannot serve them."""
    operations = record.declarations.operations
    declared_operations = set(operations.operations) if operations is not None else set()
    valid = []
    for contribution in record.declarations.contributions:
        reasons = []
        spec = contribution.spec
        if spec["kind"] == "view" and (record.name, spec["page"]) not in pages:
            reasons.append(f"page {spec['page']!r} is unknown or unavailable")
        missing_operations = set(spec["operations"]) - declared_operations
        if missing_operations:
            reasons.append(f"unknown operations: {', '.join(sorted(missing_operations))}")
        if reasons:
            _diagnose_capability(
                record, f"contribution {contribution.id!r} skipped: {'; '.join(reasons)}"
            )
        else:
            valid.append(contribution)
    record.declarations.contributions[:] = valid


def _validate_service_references(record: ExtensionRecord) -> None:
    """Resolve local resources only after the owner's complete registration."""
    resources = {resource.name for resource in record.declarations.resources}
    valid = []
    for service in record.declarations.services:
        missing = set(service.resources) - resources
        if missing:
            _diagnose_capability(
                record,
                f"service {service.name!r} skipped: unknown resources: "
                f"{', '.join(sorted(missing))}",
            )
        else:
            valid.append(service)
    record.declarations.services[:] = valid
