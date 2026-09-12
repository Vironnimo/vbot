"""Extension hooks registry and loader for local Python extensions.

Loading is two-phase: ``register(api)`` only *collects declarations* into a
per-extension :class:`ExtensionRecord`; the loader applies hook declarations to
the dispatch table after **all** extensions have finished registering (async
``register()`` coroutines are awaited deterministically first). Extensions never
touch the live dispatch table directly.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.extensions._api import (
    ExtensionAPI,
)
from core.extensions._callbacks import (
    _log_slow_extension_handler,
    invoke_extension_handler,
)
from core.extensions._capabilities import ExtensionCapabilityInstaller
from core.extensions._declarations import (
    API_VERSION,
    CommandDeclaration,
    CommandHandler,
    Deny,
    ExtensionDeclarations,
    ExtensionManifest,
    ExtensionRecord,
    ExtensionRegistrationIdentity,
    ExtensionStatus,
    HookContext,
    HookHandler,
    LifecycleHandler,
    Modify,
    PageDeclaration,
    PreparedSessionDelivery,
    PromptBlockDeclaration,
    RecallBackendDeclaration,
    RegisteredHandler,
    Replace,
    SessionCapability,
    SessionCapabilityExpiredError,
    SessionPromptBlockDeclaration,
    SessionRequestContext,
    SessionRuntimeDeclaration,
    ToolBatchDecision,
    ToolCallDecision,
    ToolDeclaration,
    ToolFamilyDeclaration,
    ToolResultValidator,
    _diagnose_capability,
)
from core.extensions._loading import (
    _await_pending_registers,
    _await_pending_registers_async,
    _discover_extension_paths,
    _overridden_record,
    _register_extension,
    _run_coroutine_to_completion,
    purge_extension_modules,
)
from core.extensions.interactions import (
    RESERVED_INTERACTION_PREFIXES,
    InteractionEvent,
    InteractionHandlerDeclaration,
    InteractionResponder,
)
from core.extensions.operations import ExtensionHost, ExtensionOperations
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.commands import CommandDispatcher
    from core.recall.recall import RecallBackendRegistry
    from core.tools.tools import ToolRegistry

_LOGGER = get_logger("extensions")


# Sentinel distinguishing "handler raised and was skipped" from a handler that
# legitimately returned ``None``.
_HANDLER_FAILED = object()


class ExtensionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[RegisteredHandler]] = defaultdict(list)
        self._interaction_handlers: dict[str, RegisteredHandler] = {}
        self._records: list[ExtensionRecord] = []
        self._capabilities = ExtensionCapabilityInstaller(self._records)
        self._host: ExtensionHost | None = None
        self._owner_hosts: dict[ExtensionRegistrationIdentity, ExtensionHost] = {}
        self._quiesced: set[str] = set()
        self._epoch = uuid.uuid4().hex
        self._registration_retired = False

    def registration_identity(self, name: str) -> ExtensionRegistrationIdentity:
        """Return the current loaded identity for an Extension declaration owner."""
        if not self.is_registration_current(ExtensionRegistrationIdentity(name, self._epoch)):
            raise ValueError(f"Extension is not available: {name}")
        return ExtensionRegistrationIdentity(name, self._epoch)

    def is_registration_current(self, identity: ExtensionRegistrationIdentity) -> bool:
        """Whether an owner identity belongs to this live registry epoch."""
        return (
            isinstance(identity, ExtensionRegistrationIdentity)
            and identity.epoch == self._epoch
            and not self._registration_retired
            and any(
                record.name == identity.name and record.status == "loaded"
                for record in self._records
            )
        )

    def page_declarations(
        self,
    ) -> list[tuple[ExtensionRegistrationIdentity, PageDeclaration, Path]]:
        """Return loaded page declarations with their checked Extension roots."""
        pages: list[tuple[ExtensionRegistrationIdentity, PageDeclaration, Path]] = []
        for record in self._records:
            if record.status != "loaded":
                continue
            identity = ExtensionRegistrationIdentity(record.name, self._epoch)
            for declaration in record.declarations.pages:
                entry = (record.root_path / declaration.entry).resolve()
                try:
                    entry.relative_to(record.root_path.resolve())
                except ValueError:
                    _diagnose_capability(
                        record, f"page {declaration.page_id!r} skipped: entry escapes root"
                    )
                    continue
                if not entry.is_file():
                    _diagnose_capability(
                        record, f"page {declaration.page_id!r} skipped: entry is missing"
                    )
                    continue
                pages.append((identity, declaration, entry))
        return pages

    def retire_registration(self) -> None:
        """Invalidate owner capabilities before their declarations are removed."""
        self._registration_retired = True

    @classmethod
    def load(
        cls,
        extensions_dir: Path,
        extra_dirs: list[Path] | None = None,
        *,
        disabled: set[str] | None = None,
        config: dict[str, dict[str, Any]] | None = None,
        bundled_dir: Path | None = None,
        config_provider: Callable[[str], dict[str, Any]] | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> ExtensionRegistry:
        """Discover, import, register, and apply extensions in two phases.

        Blocking variant for callers without a running event loop (tests,
        synchronous bootstrap): async ``register()`` coroutines run on private
        worker loops behind a deadline-bounded join. On the serving loop use
        :meth:`aload`, which drives the same coroutines without freezing it.
        """
        registry, pending = cls._scan_and_collect(
            extensions_dir,
            extra_dirs,
            disabled=disabled,
            config=config,
            bundled_dir=bundled_dir,
            config_provider=config_provider,
            credential_resolver=credential_resolver,
        )
        _await_pending_registers(pending)
        registry._apply_declarations()
        registry._apply_interaction_handlers()
        return registry

    @classmethod
    async def aload(
        cls,
        extensions_dir: Path,
        extra_dirs: list[Path] | None = None,
        *,
        disabled: set[str] | None = None,
        config: dict[str, dict[str, Any]] | None = None,
        bundled_dir: Path | None = None,
        config_provider: Callable[[str], dict[str, Any]] | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> ExtensionRegistry:
        """Async :meth:`load` variant for the serving event loop.

        Same discovery, registration, and apply phases; async ``register()``
        coroutines run on the live loop under the same per-registration hard
        deadline instead of blocking it with a thread join. Runtime startup
        and ``reload_extensions`` use this so one slow Extension cannot stall
        every concurrent Session.
        """
        registry, pending = cls._scan_and_collect(
            extensions_dir,
            extra_dirs,
            disabled=disabled,
            config=config,
            bundled_dir=bundled_dir,
            config_provider=config_provider,
            credential_resolver=credential_resolver,
        )
        await _await_pending_registers_async(pending)
        registry._apply_declarations()
        registry._apply_interaction_handlers()
        return registry

    @classmethod
    def _scan_and_collect(
        cls,
        extensions_dir: Path,
        extra_dirs: list[Path] | None = None,
        *,
        disabled: set[str] | None = None,
        config: dict[str, dict[str, Any]] | None = None,
        bundled_dir: Path | None = None,
        config_provider: Callable[[str], dict[str, Any]] | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> tuple[ExtensionRegistry, list[tuple[ExtensionRecord, Any]]]:
        """Discovery + registration phase shared by :meth:`load` and :meth:`aload`.

        Scans immediate children of each root in this order: the data-dir
        ``extensions_dir`` first, then the user's *extra_dirs*, then the fixed
        *bundled_dir* (the install tree's shipped extensions) **last**. A ``None``
        *bundled_dir* is skipped. Identity is the filesystem name: the
        first-discovered occurrence of a name wins and every later same-name
        occurrence becomes an ``overridden`` record — never imported, its
        ``overridden_by`` pointing at the winner's entry path. A ``disabled`` name
        still **claims** its identity in the earlier root, so disabling a name can
        never silently activate a different copy of it in a later root.

        Extensions named in *disabled* are recorded as ``disabled`` and never
        imported. *config* maps extension name → its ``api.config`` snapshot.
        Async ``register()`` coroutines land in the returned ``pending`` list for
        the caller's awaiter; declarations are applied only after it drains.
        """
        registry = cls()
        disabled_names = set(disabled or ())
        config_map = dict(config or {})
        pending: list[tuple[ExtensionRecord, Any]] = []
        scan_roots = [extensions_dir, *(extra_dirs or []), bundled_dir]
        claimed: dict[str, ExtensionRecord] = {}
        for root in scan_roots:
            if root is None:
                continue
            for discovered in _discover_extension_paths(root):
                winner = claimed.get(discovered.name)
                if winner is not None:
                    registry._records.append(_overridden_record(discovered, winner))
                    continue
                record = _register_extension(
                    discovered,
                    disabled_names,
                    config_map,
                    pending,
                    config_provider=config_provider,
                    credential_resolver=credential_resolver,
                )
                registry._records.append(record)
                claimed[discovered.name] = record
        return registry, pending

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

    def _apply_interaction_handlers(self) -> None:
        """Build the prefix → (extension, handler) map from every loaded record.

        Called by :meth:`load` right after :meth:`_apply_declarations`. Load order
        is deterministic, so a collision must not silently decide behavior: a
        prefix already claimed by an earlier-loaded extension is **skipped** and
        diagnosed on the loser's record, and the first claimant is diagnosed
        naming the skipped extension(s) — the same first-wins policy as
        :meth:`_apply_one_tool`. A runtime-reserved prefix
        (:data:`RESERVED_INTERACTION_PREFIXES`, e.g. ``run``) is skipped before
        that so no extension can claim it.
        """
        loaded = [record for record in self._records if record.status == "loaded"]
        declarers: dict[str, list[str]] = defaultdict(list)
        for record in loaded:
            for declaration in record.declarations.interaction_handlers:
                declarers[declaration.prefix].append(record.name)

        for record in loaded:
            for declaration in record.declarations.interaction_handlers:
                self._apply_one_interaction_handler(record, declaration, declarers)

    def _apply_one_interaction_handler(
        self,
        record: ExtensionRecord,
        declaration: InteractionHandlerDeclaration,
        declarers: dict[str, list[str]],
    ) -> None:
        prefix = declaration.prefix
        if prefix in RESERVED_INTERACTION_PREFIXES:
            _diagnose_capability(
                record,
                f"interaction handler {prefix!r} skipped: prefix is reserved by the runtime",
            )
            return
        other_declarers = [other for other in declarers[prefix] if other != record.name]
        if prefix in self._interaction_handlers:
            winner = repr(other_declarers[0]) if other_declarers else "another extension"
            _diagnose_capability(
                record,
                f"interaction handler {prefix!r} skipped: prefix already declared "
                f"by extension {winner}",
            )
            return
        self._interaction_handlers[prefix] = (record.name, declaration.handler)
        if other_declarers:
            joined = ", ".join(repr(other) for other in other_declarers)
            _diagnose_capability(
                record,
                f"interaction handler {prefix!r} registered; also declared by "
                f"extension(s) {joined} (skipped there)",
            )

    def bind_host(self, host: ExtensionHost) -> None:
        self._host = host
        self._owner_hosts.clear()

    def host_for(self, identity: ExtensionRegistrationIdentity) -> ExtensionHost:
        """Return the cached host for one current loaded registration."""
        if not self.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        cached = self._owner_hosts.get(identity)
        if cached is not None:
            return cached
        if self._host is None:
            raise RuntimeError("Extension host is not bound")
        owner_host = (
            self._host.for_owner(identity) if self._host.for_owner is not None else self._host
        )
        self._owner_hosts[identity] = owner_host
        return owner_host

    def management(self, name: str) -> ExtensionOperations:
        record = next((item for item in self._records if item.name == name), None)
        if record is None or record.status != "loaded" or record.declarations.operations is None:
            raise ValueError(f"Extension is not available: {name}")
        return record.declarations.operations

    def apply_tools(self, tool_registry: ToolRegistry) -> None:
        self._capabilities.apply_tools(tool_registry)

    def apply_commands(self, command_dispatcher: CommandDispatcher) -> None:
        self._capabilities.apply_commands(command_dispatcher)

    def prompt_block_declarations(self) -> list[Any]:
        return self._capabilities.prompt_block_declarations()

    def apply_recall_backends(self, recall_registry: RecallBackendRegistry) -> None:
        self._capabilities.apply_recall_backends(recall_registry)

    def session_capability(
        self,
        binding: Any,
        tool_registry: ToolRegistry,
    ) -> SessionCapability | None:
        """Return an all-or-nothing private capability set for *binding*.

        A declaration only becomes a grant after its exact Tool handler survived
        collision handling in the live registry.  Bindings identify their owner by
        name, so a Session can never borrow similarly named declarations from a
        different Extension or a retired registry epoch.
        """
        if self._registration_retired:
            return None
        owner_name = getattr(binding, "owner_name", None)
        if not isinstance(owner_name, str):
            return None
        record = next(
            (item for item in self._records if item.name == owner_name and item.status == "loaded"),
            None,
        )
        if record is None:
            return None
        declarations = [
            declaration for declaration in record.declarations.tools if declaration.session_scoped
        ]
        if not declarations:
            return None
        for declaration in declarations:
            try:
                registered = tool_registry.get(declaration.name)
            except Exception:
                return None
            if (
                registered.handler is not declaration.handler
                or not registered.session_scoped
                or registered.catalog_visible
            ):
                return None
        return SessionCapability(
            tool_names=tuple(declaration.name for declaration in declarations),
            prompt_blocks=tuple(record.declarations.session_prompt_blocks),
            runtime=record.declarations.session_runtime,
            identity=ExtensionRegistrationIdentity(owner_name, self._epoch),
        )

    def _session_capability_current(
        self, binding: Any, capability: SessionCapability, tool_registry: ToolRegistry
    ) -> bool:
        """Re-resolve a callback owner after awaits cannot retain stale work."""
        if not self.is_registration_current(capability.identity):
            return False
        return (
            any(
                record.name == capability.identity.name
                and record.status == "loaded"
                and record.declarations.session_runtime is capability.runtime
                for record in self._records
            )
            and self.session_capability(binding, tool_registry) is not None
        )

    async def dispatch_session_before_request(
        self,
        binding: Any,
        tool_registry: ToolRegistry,
        context: SessionRequestContext,
    ) -> PreparedSessionDelivery | None:
        """Ask the current owner for one durable pre-request delivery batch."""
        capability = self.session_capability(binding, tool_registry)
        if capability is None:
            raise SessionCapabilityExpiredError("session capability expired")
        if capability.runtime is None:
            return None
        result = await invoke_extension_handler(capability.runtime.before_request, context)
        if not self._session_capability_current(binding, capability, tool_registry):
            raise SessionCapabilityExpiredError("session capability expired")
        if result is None:
            return None
        if not isinstance(result, PreparedSessionDelivery):
            raise TypeError("Session before_request must return PreparedSessionDelivery or None")
        if (
            not result.delivery_id
            or not result.content_hash
            or not result.settings_revision
            or not result.entries
            or not all(isinstance(entry, str) and entry for entry in result.entries)
        ):
            raise ValueError("Session delivery batch is invalid")
        return result

    async def acknowledge_session_delivery(
        self,
        binding: Any,
        tool_registry: ToolRegistry,
        context: SessionRequestContext,
        delivery: PreparedSessionDelivery,
    ) -> None:
        capability = self.session_capability(binding, tool_registry)
        if capability is None:
            raise SessionCapabilityExpiredError("session capability expired")
        runtime = capability.runtime
        if runtime is not None and runtime.acknowledge_delivery is not None:
            assert capability is not None
            await invoke_extension_handler(runtime.acknowledge_delivery, context, delivery)
            if not self._session_capability_current(binding, capability, tool_registry):
                raise SessionCapabilityExpiredError("session capability expired")

    async def reconcile_session_tool_batch(
        self,
        binding: Any,
        tool_registry: ToolRegistry,
        context: SessionRequestContext,
        receipts: tuple[tuple[str, str, str, str], ...],
        persisted_call_ids: tuple[str, ...],
        turn_end_requested: bool,
    ) -> ToolBatchDecision:
        capability = self.session_capability(binding, tool_registry)
        if capability is None:
            raise SessionCapabilityExpiredError("session capability expired")
        runtime = capability.runtime
        if runtime is None or runtime.reconcile_tool_batch is None:
            return ToolBatchDecision(end=turn_end_requested)
        assert capability is not None
        result = await invoke_extension_handler(
            runtime.reconcile_tool_batch,
            context,
            receipts=receipts,
            persisted_call_ids=persisted_call_ids,
            turn_end_requested=turn_end_requested,
        )
        if not self._session_capability_current(binding, capability, tool_registry):
            raise SessionCapabilityExpiredError("session capability expired")
        if not isinstance(result, ToolBatchDecision):
            raise TypeError("Session tool-batch reconciliation must return ToolBatchDecision")
        return result

    async def dispatch_session_run_finished(
        self,
        binding: Any,
        tool_registry: ToolRegistry,
        context: SessionRequestContext,
        outcome: str,
    ) -> None:
        capability = self.session_capability(binding, tool_registry)
        if capability is None:
            raise SessionCapabilityExpiredError("session capability expired")
        if capability.runtime is None:
            return
        await invoke_extension_handler(capability.runtime.run_finished, context, outcome=outcome)
        if not self._session_capability_current(binding, capability, tool_registry):
            raise SessionCapabilityExpiredError("session capability expired")

    def loaded_extension_names(self) -> set[str]:
        """Return the set of loaded extension names (gate 2's ``extension:<name>``).

        The prompt manager's owner-active gate checks ``extension:<name>`` presence
        against this set, so a block whose extension failed/disabled/unloaded never
        renders. Rebuilt and re-handed on every extension (re)load.
        """
        return {record.name for record in self._records if record.status == "loaded"}

    def has_tool_hooks(self) -> bool:
        """Return whether active Extensions can modify Tool calls or results."""
        return bool(self._handlers.get("tool_call") or self._handlers.get("tool_result"))

    async def deactivate(
        self,
        name: str,
        tool_registry: ToolRegistry | None = None,
        command_dispatcher: CommandDispatcher | None = None,
    ) -> bool:
        """Stop a currently-loaded extension's effects live, without a restart.

        The disable half of "values live, structure restart-bound": on disable we
        mark the extension inactive and immediately stop all its effects, but keep
        its code loaded (dormant) — reclaiming the code and *loading* code that was
        disabled at boot both still need a restart (the enable case). Concretely,
        for a currently ``loaded`` record this:

        1. removes its hook handlers from the dispatch table (all five events stop
           firing),
        2. unregisters its applied tools from *tool_registry* — only the tools this
           extension actually registered (matched by handler identity, so a
           collision-skipped name owned by a built-in / another extension is left
           alone),
        3. unregisters its applied Commands from the canonical dispatcher,
        4. fires its shutdown handlers (fail-open, resource cleanup), and
        5. flips the record to ``disabled`` and clears its declarations, so every
           status surface reports it exactly like a boot-disabled extension (no
           schema, no capabilities, no pending restart) and the prompt/recall
           refreshers (which key on ``loaded``) drop it on the next rebuild.

        Persisted permissions and config are untouched — re-enabling later (a
        restart) restores them. Returns ``True`` when it deactivated a live record;
        a name that is unknown, already ``disabled``, ``failed``, or ``overridden``
        is a clean no-op returning ``False`` (no crash, no double shutdown).
        """
        record = next((item for item in self._records if item.name == name), None)
        if record is None or record.status != "loaded":
            return False

        await self.quiesce(name)
        declarations = record.declarations
        if declarations.operations is not None:
            declarations.operations.retire()
        self._remove_handlers(name)
        self._remove_interaction_handlers(name)
        if tool_registry is not None:
            self._capabilities._unregister_extension_tools(tool_registry, declarations.tools)
            self._capabilities._unregister_extension_tool_families(
                tool_registry, name, declarations.tool_families
            )
        if command_dispatcher is not None:
            command_dispatcher.unregister_extension_commands(name)
        for handler in declarations.shutdown:
            await self._invoke_lifecycle("shutdown", name, handler)

        record.status = "disabled"
        record.declarations = ExtensionDeclarations()
        self._retire_owner_host(name)
        _LOGGER.info("Extension %r deactivated live (no restart)", name)
        return True

    async def quiesce(self, name: str) -> bool:
        """Drain one current owner before removing any of its capabilities."""
        record = next((item for item in self._records if item.name == name), None)
        if record is None or record.status != "loaded":
            return False
        if name in self._quiesced:
            return True
        runtime = record.declarations.session_runtime
        if runtime is not None:
            await invoke_extension_handler(runtime.quiesce)
        self._quiesced.add(name)
        return True

    async def quiesce_all(self) -> None:
        """Drain every loaded owner before this registry is replaced."""
        for record in self._records:
            if record.status == "loaded":
                await self.quiesce(record.name)

    def _retire_owner_host(self, name: str) -> None:
        """Forget cached owner hosts once their registration cannot be used."""
        self._owner_hosts = {
            identity: host for identity, host in self._owner_hosts.items() if identity.name != name
        }

    def remove_applied_tools(self, tool_registry: ToolRegistry) -> None:
        """Unregister every loaded extension's applied tools from *tool_registry*.

        The teardown counterpart to :meth:`apply_tools`, used by
        ``Runtime.reload_extensions`` to detach the old layer's tools before the
        fresh registry re-applies. For each ``loaded`` record it runs the same
        handler-identity match as live disable (:meth:`_unregister_extension_tools`),
        so a name skipped on a collision — owned by a built-in or another
        extension — is never yanked from its real owner. Record statuses are left
        untouched: the whole registry object is discarded right after the swap.
        """
        for record in self._records:
            if record.status != "loaded":
                continue
            if record.declarations.operations is not None:
                record.declarations.operations.retire()
            self._capabilities._unregister_extension_tools(tool_registry, record.declarations.tools)
            self._capabilities._unregister_extension_tool_families(
                tool_registry, record.name, record.declarations.tool_families
            )
            self._retire_owner_host(record.name)

    def remove_applied_commands(self, command_dispatcher: CommandDispatcher) -> None:
        """Remove every loaded Extension's commands from the live dispatcher."""
        for record in self._records:
            if record.status == "loaded":
                command_dispatcher.unregister_extension_commands(record.name)

    def _remove_handlers(self, extension_name: str) -> None:
        """Drop every hook handler registered under *extension_name* from dispatch."""
        for event in list(self._handlers):
            self._handlers[event] = [
                pair for pair in self._handlers[event] if pair[0] != extension_name
            ]

    def _remove_interaction_handlers(self, extension_name: str) -> None:
        """Drop every interaction prefix registered under *extension_name*."""
        self._interaction_handlers = {
            prefix: entry
            for prefix, entry in self._interaction_handlers.items()
            if entry[0] != extension_name
        }

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
            operations = record.declarations.operations
            if operations is not None and operations.startup:
                if self._host is None:
                    raise RuntimeError("Managed Extension requires a bound host")
                identity = self.registration_identity(record.name)
                owner_host = self.host_for(identity)
                for start in operations.startup:
                    await self._invoke_lifecycle("startup", record.name, partial(start, owner_host))
            for handler in record.declarations.startup:
                await self._invoke_lifecycle("startup", record.name, handler)

    async def fire_shutdown(self) -> None:
        """Fire every loaded extension's shutdown handlers in load order, fail-open."""
        for record in self._records:
            if record.status != "loaded":
                continue
            await self.quiesce(record.name)
            if record.declarations.operations is not None:
                record.declarations.operations.retire()
            self._retire_owner_host(record.name)
            for handler in record.declarations.shutdown:
                await self._invoke_lifecycle("shutdown", record.name, handler)

    def fire_shutdown_blocking(self) -> None:
        """Run :meth:`fire_shutdown` to completion from synchronous shutdown paths."""
        _run_coroutine_to_completion(self.fire_shutdown())

    async def _invoke_lifecycle(
        self, phase: str, extension_name: str, handler: LifecycleHandler
    ) -> None:
        """Call one lifecycle handler with fail-open isolation (logs at ``error``)."""
        started_at = time.perf_counter()
        try:
            await invoke_extension_handler(handler)
        except Exception as exc:
            _LOGGER.error(
                "Extension %r %s handler raised: %s",
                extension_name,
                phase,
                exc,
                exc_info=True,
            )
        finally:
            _log_slow_extension_handler(
                extension_name=extension_name,
                handler_kind=phase,
                started_at=started_at,
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
        started_at = time.perf_counter()
        try:
            return await invoke_extension_handler(handler, ctx, **payload)
        except Exception as exc:
            _LOGGER.warning(
                "Extension %r %s handler raised: %s",
                extension_name,
                event,
                exc,
                exc_info=True,
            )
            return _HANDLER_FAILED
        finally:
            _log_slow_extension_handler(
                extension_name=extension_name,
                handler_kind=event,
                started_at=started_at,
            )

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

    async def dispatch_channel_interaction(
        self, event: InteractionEvent, responder: InteractionResponder
    ) -> bool:
        """Route one channel button-tap to the extension registered for its prefix.

        The prefix is ``event.data`` up to the first ``":"``. Returns ``False``
        when no handler is registered for that prefix (the channel still
        acknowledges the tap itself). When a handler matches it is invoked
        ``handler(event, responder)`` with the same fail-open isolation as hook
        dispatch — a raising handler is logged at ``warning`` and swallowed — and
        ``True`` is returned (a handler *matched*, even if it raised), so the
        channel does not mistake a raising handler for an unhandled tap.
        """
        prefix = event.data.split(":", 1)[0]
        entry = self._interaction_handlers.get(prefix)
        if entry is None:
            return False
        extension_name, handler = entry
        started_at = time.perf_counter()
        try:
            await invoke_extension_handler(handler, event, responder)
        except Exception as exc:
            _LOGGER.warning(
                "Extension %r interaction handler %r raised: %s",
                extension_name,
                prefix,
                exc,
                exc_info=True,
            )
        finally:
            _log_slow_extension_handler(
                extension_name=extension_name,
                handler_kind=f"interaction:{prefix}",
                started_at=started_at,
            )
        return True


__all__ = [
    "API_VERSION",
    "CommandDeclaration",
    "ExtensionRegistrationIdentity",
    "Deny",
    "ExtensionAPI",
    "ExtensionManifest",
    "ExtensionRecord",
    "ExtensionRegistry",
    "HookContext",
    "Modify",
    "PromptBlockDeclaration",
    "PageDeclaration",
    "Replace",
    "ToolCallDecision",
    "ToolFamilyDeclaration",
    "ToolResultValidator",
    "purge_extension_modules",
    "HookHandler",
    "LifecycleHandler",
    "CommandHandler",
    "RegisteredHandler",
    "ExtensionStatus",
    "ToolDeclaration",
    "SessionPromptBlockDeclaration",
    "SessionRuntimeDeclaration",
    "SessionCapability",
    "SessionCapabilityExpiredError",
    "SessionRequestContext",
    "PreparedSessionDelivery",
    "ToolBatchDecision",
    "RecallBackendDeclaration",
    "ExtensionDeclarations",
    "invoke_extension_handler",
]
