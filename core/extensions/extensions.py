"""Extension hooks registry and loader for local Python extensions.

Loading is two-phase: ``register(api)`` only *collects declarations* into a
per-extension :class:`ExtensionRecord`; the loader applies hook declarations to
the dispatch table after **all** extensions have finished registering (async
``register()`` coroutines are awaited deterministically first). Extensions never
touch the live dispatch table directly.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import threading
import types
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.recall.recall import RecallBackendRegistry
    from core.tools.tools import ToolRegistry

_LOGGER = get_logger("extensions")
_EXTENSION_PARENT_PACKAGE = "vbot_ext"
_MANIFEST_FILENAME = "extension.json"

# Public extension API version. Bumped when the extension contract changes in a
# way third-party extensions can detect via their manifest ``api_version``.
API_VERSION = 1

HookHandler = Callable[..., Any]
LifecycleHandler = Callable[[], Any]
RegisteredHandler = tuple[str, HookHandler]
# Injected by chat so tool-result-envelope schema knowledge stays in the chat
# domain: given (extension_name, candidate dict) it returns the validated
# envelope or ``None`` when the candidate is rejected.
ToolResultValidator = Callable[[str, dict[str, Any]], "dict[str, Any] | None"]

ExtensionStatus = Literal["loaded", "failed", "disabled"]

# Sentinel distinguishing "handler raised and was skipped" from a handler that
# legitimately returned ``None``.
_HANDLER_FAILED = object()


def _ignore_note(text: str) -> None:
    """Default no-op note sink for contexts built without a live session."""
    return None


@dataclass(frozen=True)
class HookContext:
    """First positional argument to every handler. Constructed in ``core/chat/``.

    ``add_note`` appends a kernel-internal ``role: "note"`` entry to the active
    session; chat wires it to ``session.add_note`` when constructing the context.
    """

    session_id: str
    agent_id: str
    run_id: str
    add_note: Callable[[str], None] = _ignore_note


@dataclass(frozen=True)
class Deny:
    """``tool_call`` decision: stop the pipeline and refuse execution with a reason."""

    reason: str


@dataclass(frozen=True)
class Modify:
    """``tool_call`` decision: replace the tool input; the pipeline keeps going."""

    input: dict[str, Any]


@dataclass(frozen=True)
class Replace:
    """``tool_call`` decision: skip execution and use this result envelope instead."""

    result: dict[str, Any]


@dataclass(frozen=True)
class ToolCallDecision:
    """Outcome of the ``tool_call`` decision pipeline handed back to chat.

    Exactly one disposition holds:

    - proceed — both ``deny_reason`` and ``replacement`` are ``None``: execute the
      tool with ``effective_input`` (reflects any ``Modify`` applied in the pipeline).
    - denied — ``deny_reason``/``deny_extension`` set: the tool is not executed and
      chat builds a deny error envelope naming the extension.
    - replaced — ``replacement`` is a validated result envelope used as the result;
      the tool is not executed.
    """

    effective_input: dict[str, Any]
    deny_reason: str | None = None
    deny_extension: str | None = None
    replacement: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExtensionManifest:
    """Optional ``extension.json`` enrichment for a directory-form extension.

    Identity stays the filesystem name; ``display_name`` (the manifest ``name``
    field) is display-only. ``api_version`` greater than :data:`API_VERSION`
    fails the extension at load time.
    """

    version: str | None = None
    description: str | None = None
    api_version: int | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class ToolDeclaration:
    """One ``api.register_tool`` declaration, mirroring ``ToolRegistry.register``.

    Collected during ``register`` and applied into the runtime ``ToolRegistry``
    after the last built-in tool is registered. ``display`` is forwarded
    untouched (a ``core.tools.ToolDisplay`` or ``None``) so the extensions
    module needs no dependency on the tools domain.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    internal: bool = False
    display: Any = None


@dataclass(frozen=True)
class RecallBackendDeclaration:
    """One ``api.register_recall_backend`` declaration: name + backend factory.

    The factory is a ``core.recall.RecallBackendFactory``
    (``RecallBackendContext -> RecallBackend``); kept loosely typed so the
    extensions module stays decoupled from the recall domain.
    """

    name: str
    factory: Callable[..., Any]


@dataclass
class ExtensionDeclarations:
    """What an extension declares through :class:`ExtensionAPI` during ``register``.

    Collected per extension; applied to the dispatch table / domain registries
    or fired by the registry only after every extension has registered.
    """

    hooks: dict[str, list[HookHandler]] = field(default_factory=lambda: defaultdict(list))
    startup: list[LifecycleHandler] = field(default_factory=list)
    shutdown: list[LifecycleHandler] = field(default_factory=list)
    tools: list[ToolDeclaration] = field(default_factory=list)
    recall_backends: list[RecallBackendDeclaration] = field(default_factory=list)


@dataclass
class ExtensionRecord:
    """One discovered extension and the outcome of loading it.

    ``name`` is the identity (directory or file name). ``status`` is ``loaded``
    (importable and registered), ``failed`` (import/register/manifest error —
    ``error`` carries the detail), or ``disabled`` (listed disabled, never
    imported). ``declarations`` are only meaningful for ``loaded`` records.
    """

    name: str
    root_path: Path
    entry_path: Path
    status: ExtensionStatus
    error: str | None = None
    manifest: ExtensionManifest | None = None
    declarations: ExtensionDeclarations = field(default_factory=ExtensionDeclarations)
    # Non-fatal per-capability diagnostics (e.g. a tool name collision skipped a
    # single tool). The extension still ``loaded``; only that capability dropped.
    capability_errors: list[str] = field(default_factory=list)


class _ManifestError(Exception):
    """Raised when an ``extension.json`` manifest is missing required shape."""


class ExtensionAPI:
    """Registration facade passed into an extension's ``register(api)``.

    Every call only *collects a declaration* onto the extension's record;
    nothing goes live until the loader's apply phase runs after all extensions
    have registered. ``config`` is the per-extension settings object (empty dict
    by default) and ``logger`` is a ``vbot.extensions.<name>`` logger.
    """

    def __init__(
        self,
        extension_name: str,
        declarations: ExtensionDeclarations,
        *,
        config: dict[str, Any],
        logger: Any,
    ) -> None:
        self._extension_name = extension_name
        self._declarations = declarations
        self.config = config
        self.logger = logger

    def on(self, event: str, handler: HookHandler) -> None:
        """Declare a hook handler for *event*. Called as ``handler(ctx, **payload)``."""
        self._declarations.hooks[event].append(handler)

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        *,
        internal: bool = False,
        display: Any = None,
    ) -> None:
        """Declare an agent tool, mirroring ``ToolRegistry.register``.

        Only collects the declaration; the runtime applies it into the live
        ``ToolRegistry`` after the last built-in tool is registered. A name
        that collides with a built-in or another extension's tool is skipped
        and diagnosed on this extension's record — extensions never override
        an existing tool.
        """
        self._declarations.tools.append(
            ToolDeclaration(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                internal=internal,
                display=display,
            )
        )

    def register_recall_backend(self, name: str, factory: Callable[..., Any]) -> None:
        """Declare a session-recall backend (``RecallBackendContext -> RecallBackend``).

        Only collects the declaration; the runtime applies it onto the recall
        registry before the persisted ``recall.backend`` is resolved. A
        duplicate or non lowercase-snake_case name is skipped and diagnosed on
        this extension's record.
        """
        self._declarations.recall_backends.append(
            RecallBackendDeclaration(name=name, factory=factory)
        )

    def on_startup(self, handler: LifecycleHandler) -> None:
        """Declare a startup handler (sync or async, no args) fired post-bootstrap."""
        self._declarations.startup.append(handler)

    def on_shutdown(self, handler: LifecycleHandler) -> None:
        """Declare a shutdown handler (sync or async, no args) fired on runtime stop."""
        self._declarations.shutdown.append(handler)


class ExtensionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[RegisteredHandler]] = defaultdict(list)
        self._records: list[ExtensionRecord] = []

    @classmethod
    def load(
        cls,
        extensions_dir: Path,
        extra_dirs: list[Path] | None = None,
        *,
        disabled: set[str] | None = None,
        config: dict[str, dict[str, Any]] | None = None,
    ) -> ExtensionRegistry:
        """Discover, import, register, and apply extensions in two phases.

        Scans immediate children of each root in order (``extensions_dir``
        first). Extensions named in *disabled* are recorded as ``disabled`` and
        never imported. *config* maps extension name → its ``api.config`` object.
        Async ``register()`` coroutines are awaited to completion before hook
        declarations are applied to the dispatch table.
        """
        registry = cls()
        disabled_names = set(disabled or ())
        config_map = dict(config or {})
        pending: list[tuple[ExtensionRecord, Any]] = []
        scan_roots = [extensions_dir, *(extra_dirs or [])]
        for root in scan_roots:
            for discovered in _discover_extension_paths(root):
                record = _register_extension(discovered, disabled_names, config_map, pending)
                registry._records.append(record)
        _await_pending_registers(pending)
        registry._apply_declarations()
        return registry

    def install_handler(self, extension_name: str, event: str, handler: HookHandler) -> None:
        """Add one hook handler to the live dispatch table under *extension_name*.

        The apply phase's primitive: it threads each loaded extension's hook
        declarations into ``_handlers`` in load order. Tests build a dispatch
        table through this seam without the full filesystem load path.
        """
        self._handlers[event].append((extension_name, handler))

    def _apply_declarations(self) -> None:
        """Install hook declarations from every loaded record in load order."""
        for record in self._records:
            if record.status != "loaded":
                continue
            for event, handlers in record.declarations.hooks.items():
                for handler in handlers:
                    self.install_handler(record.name, event, handler)

    def apply_tools(self, tool_registry: ToolRegistry) -> None:
        """Register every loaded extension's declared tools into *tool_registry*.

        Called by the runtime after the last built-in tool is registered.
        Collision policy (load order is deterministic, so it must not silently
        decide behavior): a name already used by a built-in or by an
        earlier-loaded extension is **skipped** and diagnosed on the record;
        between two extensions declaring the same name the first-loaded wins and
        **both** sides are diagnosed. Per-tool registration errors fail open.
        """
        loaded = [record for record in self._records if record.status == "loaded"]
        declarers: dict[str, list[str]] = defaultdict(list)
        for record in loaded:
            for declaration in record.declarations.tools:
                declarers[declaration.name].append(record.name)

        builtin_names = {tool.name for tool in tool_registry.list_tools(include_internal=True)}
        applied: set[str] = set()
        for record in loaded:
            for declaration in record.declarations.tools:
                self._apply_one_tool(
                    tool_registry, record, declaration, declarers, builtin_names, applied
                )

    def _apply_one_tool(
        self,
        tool_registry: ToolRegistry,
        record: ExtensionRecord,
        declaration: ToolDeclaration,
        declarers: dict[str, list[str]],
        builtin_names: set[str],
        applied: set[str],
    ) -> None:
        name = declaration.name
        other_declarers = [other for other in declarers[name] if other != record.name]
        if name in builtin_names:
            self._diagnose_capability(
                record, f"tool {name!r} skipped: a built-in tool already uses this name"
            )
            return
        if name in applied:
            winner = repr(other_declarers[0]) if other_declarers else "another extension"
            self._diagnose_capability(
                record, f"tool {name!r} skipped: name already declared by extension {winner}"
            )
            return
        try:
            tool_registry.register(
                name,
                declaration.description,
                declaration.parameters,
                declaration.handler,
                internal=declaration.internal,
                display=declaration.display,
            )
        except Exception as exc:
            self._diagnose_capability(record, f"tool {name!r} registration failed: {exc}")
            return
        applied.add(name)
        if other_declarers:
            joined = ", ".join(repr(other) for other in other_declarers)
            self._diagnose_capability(
                record,
                f"tool {name!r} registered; also declared by extension(s) {joined} (skipped there)",
            )

    def apply_recall_backends(self, recall_registry: RecallBackendRegistry) -> None:
        """Register every loaded extension's recall backends into *recall_registry*.

        Called by the runtime on a ``with_builtins()`` registry before the
        persisted ``recall.backend`` is resolved (and again on every
        ``reload_recall_backend``). The registry's own rules hold: a duplicate
        name (built-ins are registered first) or a non lowercase-snake_case name
        raises ``ValueError``, which is caught, diagnosed on the record, and the
        backend skipped.
        """
        for record in self._records:
            if record.status != "loaded":
                continue
            for declaration in record.declarations.recall_backends:
                try:
                    recall_registry.register(declaration.name, declaration.factory)
                except ValueError as exc:
                    self._diagnose_capability(
                        record, f"recall backend {declaration.name!r} skipped: {exc}"
                    )

    def _diagnose_capability(self, record: ExtensionRecord, message: str) -> None:
        """Record a non-fatal capability diagnostic and log it at ``warning``."""
        record.capability_errors.append(message)
        _LOGGER.warning("Extension %r %s", record.name, message)

    def records(self) -> list[ExtensionRecord]:
        """Return every discovered extension record in load order."""
        return list(self._records)

    def diagnostics(self) -> list[ExtensionRecord]:
        """Return only the records that failed to load (mirrors skills diagnostics)."""
        return [record for record in self._records if record.status == "failed"]

    async def fire_startup(self) -> None:
        """Fire every loaded extension's startup handlers in load order, fail-open."""
        for record in self._records:
            if record.status != "loaded":
                continue
            for handler in record.declarations.startup:
                await self._invoke_lifecycle("startup", record.name, handler)

    async def fire_shutdown(self) -> None:
        """Fire every loaded extension's shutdown handlers in load order, fail-open."""
        for record in self._records:
            if record.status != "loaded":
                continue
            for handler in record.declarations.shutdown:
                await self._invoke_lifecycle("shutdown", record.name, handler)

    def fire_shutdown_blocking(self) -> None:
        """Run :meth:`fire_shutdown` to completion from synchronous shutdown paths."""
        _run_coroutine_to_completion(self.fire_shutdown())

    async def _invoke_lifecycle(
        self, phase: str, extension_name: str, handler: LifecycleHandler
    ) -> None:
        """Call one lifecycle handler with fail-open isolation (logs at ``error``)."""
        try:
            result = handler()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _LOGGER.error(
                "Extension %r %s handler raised: %s",
                extension_name,
                phase,
                exc,
                exc_info=True,
            )

    async def _invoke(
        self,
        event: str,
        extension_name: str,
        handler: HookHandler,
        ctx: HookContext,
        payload: dict[str, Any],
    ) -> Any:
        """Call one handler with per-handler exception isolation.

        Awaits async handlers. On failure logs at ``warning`` and returns the
        ``_HANDLER_FAILED`` sentinel so callers can skip the handler without
        confusing a raised handler with one that returned ``None``.
        """
        try:
            result = handler(ctx, **payload)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            _LOGGER.warning(
                "Extension %r %s handler raised: %s",
                extension_name,
                event,
                exc,
                exc_info=True,
            )
            return _HANDLER_FAILED

    async def dispatch_run_start(self, ctx: HookContext, *, session_id: str, agent_id: str) -> None:
        """Observer event: run all ``run_start`` handlers; ignore return values."""
        payload = {"session_id": session_id, "agent_id": agent_id}
        for extension_name, handler in self._handlers.get("run_start", []):
            await self._invoke("run_start", extension_name, handler, ctx, payload)

    async def dispatch_run_end(
        self, ctx: HookContext, *, session_id: str, agent_id: str, outcome: str
    ) -> None:
        """Observer event: run all ``run_end`` handlers; ignore return values."""
        payload = {"session_id": session_id, "agent_id": agent_id, "outcome": outcome}
        for extension_name, handler in self._handlers.get("run_end", []):
            await self._invoke("run_end", extension_name, handler, ctx, payload)

    async def dispatch_before_agent_start(
        self, ctx: HookContext, *, agent: Any, session: Any, messages: Any, run: Any
    ) -> list[str]:
        """Accumulator event: collect every handler's ``system_prompt_append``.

        Returns the appends in load order; applying them to the system message
        stays in chat (domain knowledge about message shape).
        """
        payload = {"agent": agent, "session": session, "messages": messages, "run": run}
        appends: list[str] = []
        for extension_name, handler in self._handlers.get("before_agent_start", []):
            result = await self._invoke("before_agent_start", extension_name, handler, ctx, payload)
            if result is _HANDLER_FAILED:
                continue
            if isinstance(result, dict) and isinstance(result.get("system_prompt_append"), str):
                appends.append(result["system_prompt_append"])
        return appends

    async def dispatch_context(self, ctx: HookContext, *, messages: list) -> list:
        """Pipeline event: each handler may replace the running message list.

        Threads the list through every handler in load order: a handler returning
        a list makes it the current list (the next handler sees it); any other
        return leaves the running list unchanged. Returns the final list. Chat
        passes a shallow per-message copy in, so this is safe to use as the
        request messages.
        """
        current = messages
        for extension_name, handler in self._handlers.get("context", []):
            payload = {"messages": current}
            result = await self._invoke("context", extension_name, handler, ctx, payload)
            if result is _HANDLER_FAILED:
                continue
            if isinstance(result, list):
                current = result
        return current

    async def dispatch_tool_call(
        self,
        ctx: HookContext,
        *,
        tool_name: str,
        tool_call_id: str,
        input: dict[str, Any],
        validator: ToolResultValidator,
    ) -> ToolCallDecision:
        """Decision pipeline: handlers may modify the input, deny, or replace.

        Each handler returns ``None`` (continue unchanged), ``Modify(input)``
        (the input is replaced and the next handler sees it), ``Deny(reason)``
        (stops the pipeline; the tool is not executed), or ``Replace(result)``
        (stops the pipeline; ``result`` must pass ``validator`` or it is logged
        and treated as continue). Any other return is ignored with a warning —
        plain dicts no longer short-circuit a tool call. Returns a
        ``ToolCallDecision`` describing the effective input and disposition.
        """
        current_input = input
        for extension_name, handler in self._handlers.get("tool_call", []):
            payload = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "input": current_input,
            }
            decision = await self._invoke("tool_call", extension_name, handler, ctx, payload)
            if decision is _HANDLER_FAILED or decision is None:
                continue
            if isinstance(decision, Modify):
                if isinstance(decision.input, dict):
                    current_input = decision.input
                else:
                    _LOGGER.warning(
                        "Extension %r tool_call Modify ignored: input is not a dict",
                        extension_name,
                    )
                continue
            if isinstance(decision, Deny):
                return ToolCallDecision(
                    effective_input=current_input,
                    deny_reason=decision.reason,
                    deny_extension=extension_name,
                )
            if isinstance(decision, Replace):
                validated = validator(extension_name, decision.result)
                if validated is None:
                    continue
                return ToolCallDecision(effective_input=current_input, replacement=validated)
            _LOGGER.warning(
                "Extension %r tool_call handler returned an unsupported value (%s); "
                "ignoring. Return None, Modify, Deny, or Replace.",
                extension_name,
                type(decision).__name__,
            )
        return ToolCallDecision(effective_input=current_input)

    async def dispatch_tool_result(
        self,
        ctx: HookContext,
        *,
        tool_name: str,
        tool_call_id: str,
        input: dict[str, Any],
        result: dict[str, Any],
        validator: ToolResultValidator,
    ) -> dict[str, Any]:
        """Replace-style pipeline: each handler may swap in a full envelope.

        Each handler receives the running envelope and returns a full
        replacement envelope (validated; valid replaces the running result,
        invalid is dropped) or ``None`` to leave it unchanged. There is no
        shallow-merge patching. Returns the final (possibly unchanged) envelope.
        """
        current = result
        for extension_name, handler in self._handlers.get("tool_result", []):
            payload = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "input": input,
                "result": current,
            }
            hook_result = await self._invoke("tool_result", extension_name, handler, ctx, payload)
            if hook_result is _HANDLER_FAILED or hook_result is None:
                continue
            if isinstance(hook_result, dict):
                validated = validator(extension_name, hook_result)
                if validated is not None:
                    current = validated
        return current


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
    for entry in extensions_dir.iterdir():
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
) -> ExtensionRecord:
    """Load one discovered extension into a record (collecting declarations).

    Disabled extensions are never imported. Manifest/import/``register()``
    failures produce a ``failed`` record with detail and never abort the others.
    Async ``register()`` coroutines are appended to *pending* for the loader to
    await before applying declarations.
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

    api = ExtensionAPI(
        name,
        record.declarations,
        config=config_map.get(name, {}),
        logger=get_logger(f"extensions.{name}"),
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


def _await_pending_registers(pending: list[tuple[ExtensionRecord, Any]]) -> None:
    """Drive every async ``register()`` coroutine to completion, fail-open."""
    for record, coro in pending:
        try:
            _run_coroutine_to_completion(coro)
        except Exception as exc:
            _LOGGER.error(
                "Extension %r async register() raised: %s", record.name, exc, exc_info=True
            )
            record.status = "failed"
            record.error = f"async register() raised: {exc}"


def _run_coroutine_to_completion(coro: Any) -> None:
    """Run *coro* to completion whether or not a loop runs in this thread.

    With no running loop we drive it directly. Inside a running loop (e.g.
    ``Runtime.start()`` called from the server's async lifespan) we cannot block
    on the loop, so we run the coroutine on a private loop in a worker thread and
    join — keeping load/shutdown deterministic in both situations.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    error: list[BaseException] = []

    def _runner() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # surfaced to the caller's thread
            error.append(exc)

    thread = threading.Thread(target=_runner, name="vbot-extension-async")
    thread.start()
    thread.join()
    if error:
        raise error[0]


def _load_manifest(directory: Path) -> ExtensionManifest | None:
    """Parse an optional ``extension.json``; raise ``_ManifestError`` if malformed."""
    manifest_path = directory / _MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
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


__all__ = [
    "API_VERSION",
    "Deny",
    "ExtensionAPI",
    "ExtensionManifest",
    "ExtensionRecord",
    "ExtensionRegistry",
    "HookContext",
    "Modify",
    "Replace",
    "ToolCallDecision",
    "ToolResultValidator",
]
