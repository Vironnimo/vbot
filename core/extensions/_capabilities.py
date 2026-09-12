"""Apply Extension declarations to their owning capability registries."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.chat.commands import CommandDispatcher
    from core.recall.recall import RecallBackendRegistry
    from core.tools.tools import ToolRegistry

from core.extensions._declarations import (
    CommandDeclaration,
    ExtensionRecord,
    PromptBlockDeclaration,
    ToolDeclaration,
    ToolFamilyDeclaration,
    _diagnose_capability,
)


class ExtensionCapabilityInstaller:
    """Apply loaded records with collision diagnosis and identity-safe Tool removal."""

    def __init__(self, records: list[ExtensionRecord]) -> None:
        self._records = records

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

        for record in loaded:
            if record.declarations.operations is not None:
                record.declarations.operations.bind(tool_registry)

        applied_families = self._apply_tool_families(tool_registry, loaded)
        builtin_names = {tool.name for tool in tool_registry.list_tools(include_internal=True)}
        applied: set[str] = set()
        for record in loaded:
            for declaration in record.declarations.tools:
                self._apply_one_tool(
                    tool_registry,
                    record,
                    declaration,
                    declarers,
                    builtin_names,
                    applied,
                    applied_families.get(record.name, {}),
                )

    def _apply_tool_families(
        self,
        tool_registry: ToolRegistry,
        loaded: list[ExtensionRecord],
    ) -> dict[str, dict[str, str]]:
        """Register namespaced Extension family declarations, isolating failures."""
        applied: dict[str, dict[str, str]] = {}
        for record in loaded:
            local_families: dict[str, str] = {}
            for declaration in record.declarations.tool_families:
                if not isinstance(declaration.id, str) or not declaration.id.strip():
                    _diagnose_capability(
                        record, "tool family skipped: id must be a non-empty string"
                    )
                    continue
                local_id = declaration.id.strip()
                if local_id in local_families:
                    _diagnose_capability(
                        record, f"tool family {local_id!r} duplicate declaration skipped"
                    )
                    continue
                qualified_id = self._extension_tool_family_id(record.name, local_id)
                try:
                    tool_registry.register_family(
                        qualified_id,
                        declaration.label,
                        extension=record.name,
                    )
                except Exception as exc:
                    _diagnose_capability(
                        record, f"tool family {local_id!r} registration failed: {exc}"
                    )
                    continue
                local_families[local_id] = qualified_id
            applied[record.name] = local_families
        return applied

    @staticmethod
    def _extension_tool_family_id(extension_name: str, local_id: str) -> str:
        return f"extension:{extension_name}:{local_id}"

    def apply_commands(self, command_dispatcher: CommandDispatcher) -> None:
        """Apply loaded Extensions' commands to the canonical Chat dispatcher.

        Built-ins always win. Extension load order is first-wins, including
        duplicate declarations inside one Extension. Invalid declarations and
        collisions remain non-fatal capability diagnostics.
        """
        claimed: dict[str, tuple[ExtensionRecord, CommandDeclaration]] = {}
        built_in_names = command_dispatcher.built_in_command_names()
        for record in self._records:
            if record.status != "loaded":
                continue
            for declaration in record.declarations.commands:
                name = declaration.name
                if not isinstance(name, str):
                    _diagnose_capability(
                        record,
                        f"command {name!r} skipped: name must be a string",
                    )
                    continue
                if name in built_in_names:
                    _diagnose_capability(
                        record,
                        f"command {name!r} skipped: a Built-in Command already uses this name",
                    )
                    continue
                winner = claimed.get(name)
                if winner is not None:
                    winner_record, _winner_declaration = winner
                    if winner_record is record:
                        _diagnose_capability(
                            record,
                            f"command {name!r} duplicate declaration skipped",
                        )
                    else:
                        _diagnose_capability(
                            record,
                            f"command {name!r} skipped: name already declared by extension "
                            f"{winner_record.name!r}",
                        )
                        _diagnose_capability(
                            winner_record,
                            f"command {name!r} registered; also declared by extension "
                            f"{record.name!r} (skipped there)",
                        )
                    continue
                try:
                    command_dispatcher.register_extension_command(
                        record.name,
                        name=name,
                        description=declaration.description,
                        handler=declaration.handler,
                        argument=declaration.argument,
                        catalog_result=declaration.catalog_result,
                        execution_mode=declaration.execution_mode,
                        argument_execution_mode=declaration.argument_execution_mode,
                        unavailable_surfaces=declaration.unavailable_surfaces,
                        page_ids=frozenset(page.page_id for page in record.declarations.pages),
                    )
                except ValueError as exc:
                    _diagnose_capability(record, f"command {name!r} skipped: {exc}")
                    continue
                claimed[name] = (record, declaration)

    def _apply_one_tool(
        self,
        tool_registry: ToolRegistry,
        record: ExtensionRecord,
        declaration: ToolDeclaration,
        declarers: dict[str, list[str]],
        builtin_names: set[str],
        applied: set[str],
        applied_families: dict[str, str],
    ) -> None:
        name = declaration.name
        other_declarers = [other for other in declarers[name] if other != record.name]
        if name in builtin_names:
            _diagnose_capability(
                record, f"tool {name!r} skipped: a built-in tool already uses this name"
            )
            return
        if name in applied:
            winner = repr(other_declarers[0]) if other_declarers else "another extension"
            _diagnose_capability(
                record, f"tool {name!r} skipped: name already declared by extension {winner}"
            )
            return
        family = None
        if declaration.family is not None:
            local_family = declaration.family.strip() if isinstance(declaration.family, str) else ""
            family = applied_families.get(local_family)
            if family is None:
                _diagnose_capability(
                    record,
                    f"tool {name!r} references undeclared tool family {declaration.family!r}; "
                    "registered without a family",
                )
        try:
            tool_registry.register(
                name,
                declaration.description,
                declaration.parameters,
                declaration.handler,
                internal=declaration.internal,
                catalog_visible=declaration.catalog_visible,
                requires_opt_in=declaration.requires_opt_in,
                display=declaration.display,
                ready=declaration.ready,
                readiness_hint=declaration.readiness_hint,
                extension=record.name,
                family=family,
                result_schema=declaration.result_schema,
                parallel_safe=declaration.parallel_safe,
                open_input_schema=declaration.open_input_schema,
                coerce_arguments=declaration.coerce_arguments,
                session_scoped=declaration.session_scoped,
                activation=declaration.activation,
            )
        except Exception as exc:
            _diagnose_capability(record, f"tool {name!r} registration failed: {exc}")
            return
        applied.add(name)
        if other_declarers:
            joined = ", ".join(repr(other) for other in other_declarers)
            _diagnose_capability(
                record,
                f"tool {name!r} registered; also declared by extension(s) {joined} (skipped there)",
            )

    def prompt_block_declarations(self) -> list[Any]:
        """Return the loaded extensions' blocks as ``core.prompts.BlockDefinition``s.

        The runtime calls this after extensions load (and on every reload) and
        hands the list to the prompt manager — the contributor path D6 unifies with
        tools. Only ``status == "loaded"`` records contribute; a disabled/failed
        extension yields nothing, and gate 2 additionally requires the owning
        extension to be in :meth:`loaded_extension_names`.

        Each declaration becomes a block with id ``extension:<slug>`` and owner
        ``extension:<extension-name>``. Id collisions (two extensions choosing the
        same slug, or a slug colliding with a core/tool id later) are resolved
        first-wins **with a diagnostic** on the losing record — the same policy as
        :meth:`_apply_one_tool` — so a collision never silently changes behavior.

        The ``core.prompts`` import is lazy so the extensions module carries no
        import-time dependency on the prompts domain (this runs at collection, not
        at module load).
        """
        from core.prompts import BlockDefinition

        loaded = [record for record in self._records if record.status == "loaded"]
        declarers: dict[str, list[str]] = defaultdict(list)
        for record in loaded:
            for declaration in record.declarations.prompt_blocks:
                declarers[declaration.slug].append(record.name)

        definitions: list[Any] = []
        claimed: set[str] = set()
        for record in loaded:
            for declaration in record.declarations.prompt_blocks:
                definition = self._build_one_prompt_block(
                    BlockDefinition, record, declaration, declarers, claimed
                )
                if definition is not None:
                    definitions.append(definition)
        return definitions

    def _build_one_prompt_block(
        self,
        block_definition_cls: Any,
        record: ExtensionRecord,
        declaration: PromptBlockDeclaration,
        declarers: dict[str, list[str]],
        claimed: set[str],
    ) -> Any | None:
        """Turn one block declaration into a ``BlockDefinition``, first-wins on slug.

        Mirrors :meth:`_apply_one_tool`: an id already claimed by an earlier-loaded
        extension is skipped and diagnosed on this record (naming the winner); the
        first claimant also gets a diagnostic naming the skipped extension(s). A
        construction error (e.g. a malformed slug yielding a bad id) is diagnosed
        and the block dropped — never aborting the others.
        """
        slug = declaration.slug
        block_id = f"extension:{slug}"
        owner = f"extension:{record.name}"
        other_declarers = [other for other in declarers[slug] if other != record.name]
        if slug in claimed:
            winner = repr(other_declarers[0]) if other_declarers else "another extension"
            _diagnose_capability(
                record,
                f"prompt block {block_id!r} skipped: slug already declared by extension {winner}",
            )
            return None
        try:
            definition = block_definition_cls(
                id=block_id,
                owner=owner,
                default_text=declaration.default_text,
                render=declaration.render,
            )
        except Exception as exc:
            _diagnose_capability(record, f"prompt block {block_id!r} skipped: {exc}")
            return None
        claimed.add(slug)
        if other_declarers:
            joined = ", ".join(repr(other) for other in other_declarers)
            _diagnose_capability(
                record,
                f"prompt block {block_id!r} registered; also declared by extension(s) "
                f"{joined} (skipped there)",
            )
        return definition

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
                    _diagnose_capability(
                        record, f"recall backend {declaration.name!r} skipped: {exc}"
                    )

    def _unregister_extension_tools(
        self, tool_registry: ToolRegistry, tools: list[ToolDeclaration]
    ) -> None:
        """Unregister only the tools this extension actually applied.

        A declared name is unregistered only when the live registry entry's handler
        is this declaration's handler — so a name skipped on a collision (owned by a
        built-in or another extension) is never yanked out from under its real owner.
        """
        for declaration in tools:
            try:
                registered = tool_registry.get(declaration.name)
            except Exception:
                continue
            if registered.handler is declaration.handler:
                tool_registry.unregister(declaration.name)

    def _unregister_extension_tool_families(
        self,
        tool_registry: ToolRegistry,
        extension_name: str,
        families: list[ToolFamilyDeclaration],
    ) -> None:
        """Remove only namespaced family metadata owned by this Extension."""
        for declaration in families:
            if not isinstance(declaration.id, str) or not declaration.id.strip():
                continue
            tool_registry.unregister_family(
                self._extension_tool_family_id(extension_name, declaration.id.strip()),
                extension=extension_name,
            )
