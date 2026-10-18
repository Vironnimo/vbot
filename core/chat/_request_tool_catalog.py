"""Prompt-epoch Tool catalogs, route projection, contracts and persisted pins."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any

from core.chat._request_targets import RequestTargets
from core.chat._run_state import _RequestState
from core.chat._tool_epoch import (
    LiveToolCatalog,
    ToolChange,
    ToolEpochPin,
    ToolEpochView,
    definition_source,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.events import _close_adapter
from core.chat.messages import (
    JsonObject,
)
from core.chat.model_resolution import (
    _model_family,
)
from core.prompts.pinned_context import (
    PINNED_TOOL_DEFINITIONS_SLOT,
)
from core.sessions import (
    ChatSession,
    SessionAddress,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    EditDialect,
    Tool,
    ToolAccess,
    ToolContract,
    ToolNotFoundError,
    edit_dialect,
    known_edit_dialect,
    offer_edit_dialect,
    tool_is_ready,
)
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME, on_demand_tool_entries, on_demand_tools
from core.tools.terminal import project_terminal_tool_definitions

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, _RunExecutionContext

from core.chat._request_epoch import _live_session_tool_grants


class RequestToolCatalog:
    """Measure the available catalog and evolve the Model's prompt-epoch Tool knowledge."""

    def __init__(self, dependencies: ChatLoopDependencies, targets: RequestTargets) -> None:
        self._dependencies = dependencies
        self.targets = targets

    async def live_tool_catalog(
        self,
        context: _RunExecutionContext,
        *,
        known: Collection[str] = (),
        told: Collection[str] = (),
    ) -> LiveToolCatalog:
        """Measure the Tools the Run's Agent may use now, on the Run's primary route.

        *known* names the Tools the Model may call now; only for these does a
        withdrawn image-understanding backend count as a removal. *told* names
        every Tool the Model was told about in this prompt epoch; its file edit
        Tools keep their dialect after a Model change.
        """
        capability = None
        extension_registry = self._dependencies.get_extension_registry()
        if context.request.temporary_binding is not None and extension_registry is not None:
            capability = extension_registry.session_capability(
                context.request.temporary_binding, self._dependencies.tools
            )
        target = context.primary_target
        return await self._live_tool_catalog(
            context.agent,
            session_tool_grants=_live_session_tool_grants(
                capability,
                subagent_session=context.subagent_session,
                goal_session=context.goal_session,
            ),
            input_modalities=target.input_modalities,
            wire_media_types=target.wire_media_types,
            model_family=target.model_family,
            known=known,
            told=told,
        )

    async def announce_tool_changes(
        self,
        state: _RequestState,
        catalog: LiveToolCatalog,
        session: ChatSession,
        *,
        unlisted_tool_calls: bool,
        list_announced: bool,
    ) -> _RequestState:
        """Add a note for each Tool change since *state*'s Tool epoch and adopt it.

        Returns *state* with the dispatch allowlist, contracts and request Tools
        of the announced knowledge, and with the definitions *catalog* gives
        the On-demand Tools the Model may load; unchanged when nothing changed.
        """
        tool_epoch = state.tool_epoch
        if tool_epoch is None:
            return state
        changes = tool_epoch.plan(catalog, unlisted_tool_calls=unlisted_tool_calls)
        for change in changes:
            session.add_note(change.note_content())
        tool_epoch = tool_epoch.with_changes(changes)
        tool_loads = tool_epoch.loads(catalog)
        if not changes and tool_loads == (state.tool_loads or {}):
            if state.tool_loads is None and tool_epoch.loadable_names:
                return replace(state, tool_loads=tool_loads)
            return state
        tools, allowed_tool_names, session_tool_grants, tool_contracts = await self._tool_fields(
            tool_epoch,
            catalog.session_tool_grants,
            list_announced=list_announced,
            tool_loads=tool_loads,
        )
        return replace(
            state,
            tools=tools,
            allowed_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
            tool_contracts=tool_contracts,
            tool_epoch=tool_epoch,
            tool_loads=tool_loads,
        )

    async def adopt_tool_changes(
        self, state: _RequestState, changes: Sequence[ToolChange], *, list_announced: bool
    ) -> _RequestState:
        """Return *state* after *changes* the Run persisted itself (Tools ``load_tools`` loaded).

        The loaded Tools join the request Tools on routes that list announced
        Tools; every other route keeps sending the same Tool list.
        """
        tool_epoch = state.tool_epoch
        if tool_epoch is None or not changes:
            return state
        tool_epoch = tool_epoch.with_changes(changes)
        tools, allowed_tool_names, session_tool_grants, tool_contracts = await self._tool_fields(
            tool_epoch,
            state.session_tool_grants,
            list_announced=list_announced,
            tool_loads=state.tool_loads,
        )
        return replace(
            state,
            tools=tools,
            allowed_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
            tool_contracts=tool_contracts,
            tool_epoch=tool_epoch,
        )

    async def _tool_fields(
        self,
        tool_epoch: ToolEpochView,
        live_tool_grants: Sequence[str],
        *,
        list_announced: bool,
        tool_loads: Mapping[str, ToolChange] | None = None,
    ) -> tuple[list[JsonObject], tuple[str, ...], tuple[str, ...], Mapping[str, ToolContract]]:
        """Return request Tools, allowlist, grants and contracts of *tool_epoch*.

        An On-demand Tool the Model may load gets its contract from *tool_loads*
        (the definition a load would return), so a call made without loading
        it first validates against the same definition.
        """
        allowed_tool_names = tool_epoch.allowed_names
        allowed = set(allowed_tool_names)
        loads = tool_loads or {}
        tool_contracts = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.tools.contracts_for_provider_definitions,
            [
                *tool_epoch.definitions(),
                *(
                    definition
                    for name in tool_epoch.loadable_names
                    if (change := loads.get(name)) is not None
                    and (definition := change.definition) is not None
                ),
            ],
        )
        return (
            tool_epoch.request_tools(list_announced=list_announced),
            allowed_tool_names,
            tuple(name for name in live_tool_grants if name in allowed),
            tool_contracts,
        )

    async def _live_tool_catalog(
        self,
        agent: Any,
        *,
        session_tool_grants: Sequence[str],
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
        model_family: str,
        known: Collection[str] = (),
        told: Collection[str] = (),
    ) -> LiveToolCatalog:
        definitions = await self._dependencies.get_system_prompts().provider_tool_definitions_async(
            agent,
            session_tool_grants=session_tool_grants,
            ready_only=False,
        )
        ready, sources, announce_changes = await _CHAT_TRANSFORM_WORKERS.run(
            self._measure_tool_definitions, definitions
        )
        offered = await self._route_tool_definitions(
            ready,
            tool_access=agent.tool_access,
            input_modalities=input_modalities,
            wire_media_types=wire_media_types,
            # A prompt epoch keeps the file edit dialect its Model was told about.
            edit_dialect=known_edit_dialect(told) or edit_dialect(model_family),
        )
        usable = {str(definition["name"]) for definition in definitions}
        if (
            ANALYZE_IMAGE_TOOL_NAME in usable
            and ANALYZE_IMAGE_TOOL_NAME in known
            and all(definition.get("name") != ANALYZE_IMAGE_TOOL_NAME for definition in offered)
            and not await self._dependencies.image_understanding_available()
        ):
            usable.discard(ANALYZE_IMAGE_TOOL_NAME)
        on_demand = await _CHAT_TRANSFORM_WORKERS.run(
            partial(
                on_demand_tool_entries,
                self._dependencies.tools,
                agent,
                offered,
                session_tool_grants=session_tool_grants,
            )
        )
        # load_tools is offered while a Tool is there to load, and usable while
        # any usable Tool is on demand: readiness and route gates alone never
        # remove it from a prompt epoch that knows it.
        if not on_demand:
            offered = [
                definition
                for definition in offered
                if definition.get("name") != LOAD_TOOLS_TOOL_NAME
            ]
        if not on_demand_tools(agent, usable, session_tool_grants=session_tool_grants):
            usable.discard(LOAD_TOOLS_TOOL_NAME)
        return LiveToolCatalog(
            usable=frozenset(usable),
            offered=tuple(offered),
            sources=sources,
            session_tool_grants=tuple(session_tool_grants),
            announce_changes={
                name: parts for name, parts in announce_changes.items() if name in usable
            },
            on_demand=dict(on_demand),
        )

    def _measure_tool_definitions(
        self, definitions: Sequence[JsonObject]
    ) -> tuple[list[JsonObject], dict[str, str], dict[str, tuple[str, ...]]]:
        """Return the ready *definitions*, every fingerprint and announced definition parts.

        Runs on a Chat worker: readiness checks and fingerprints cost time per Tool.
        """
        ready: list[JsonObject] = []
        sources: dict[str, str] = {}
        announce_changes: dict[str, tuple[str, ...]] = {}
        for definition in definitions:
            name = str(definition["name"])
            tool = self._registered_tool(name)
            if tool is None or tool_is_ready(tool):
                ready.append(definition)
            sources[name] = definition_source(definition)
            if tool is not None and tool.announce_changes:
                announce_changes[name] = tool.announce_changes
        return ready, sources, announce_changes

    def _registered_tool(self, name: str) -> Tool | None:
        try:
            return self._dependencies.tools.get(name)
        except ToolNotFoundError:
            return None

    async def _read_tool_epoch_pin(self, session: ChatSession) -> ToolEpochPin | None:
        return await _CHAT_TRANSFORM_WORKERS.run(self._stored_tool_epoch_pin, session.address)

    def _stored_tool_epoch_pin(self, address: SessionAddress) -> ToolEpochPin | None:
        return ToolEpochPin.from_payload(
            self._dependencies.sessions.prompt_pin(address, PINNED_TOOL_DEFINITIONS_SLOT)
        )

    async def _ensure_tool_epoch_pin(self, session: ChatSession, pin: ToolEpochPin) -> ToolEpochPin:
        """Pin *pin* unless a concurrent first request pinned a readable one meanwhile."""
        return await _CHAT_TRANSFORM_WORKERS.run(self._pin_tool_epoch, session.address, pin)

    def _pin_tool_epoch(self, address: SessionAddress, pin: ToolEpochPin) -> ToolEpochPin:
        pinned = self._dependencies.sessions.ensure_prompt_pin(
            address,
            PINNED_TOOL_DEFINITIONS_SLOT,
            pin.to_payload(),
            lambda current: ToolEpochPin.from_payload(current) is not None,
        )
        return ToolEpochPin.from_payload(pinned) or pin

    async def _route_tool_definitions(
        self,
        tools: list[JsonObject],
        *,
        tool_access: ToolAccess,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
        edit_dialect: EditDialect,
    ) -> list[JsonObject]:
        """Apply effective Model-route gates to route-dependent Tools.

        Of the file edit Tools, only those of *edit_dialect* are offered.
        """

        tools = offer_edit_dialect(tools, edit_dialect)
        tools = project_terminal_tool_definitions(tools)
        if not any(definition.get("name") == ANALYZE_IMAGE_TOOL_NAME for definition in tools):
            return tools
        route_can_view_images = "image" in input_modalities and any(
            media_type.startswith("image/") for media_type in wire_media_types
        )
        image_task_available = (
            False
            if route_can_view_images and ANALYZE_IMAGE_TOOL_NAME not in tool_access.granted
            else await self._dependencies.image_understanding_available()
        )
        if image_task_available:
            return tools
        return [
            definition for definition in tools if definition.get("name") != ANALYZE_IMAGE_TOOL_NAME
        ]

    async def preview_tool_definitions(
        self, agent: Any, *, session_tool_grants: Sequence[str] = ()
    ) -> list[JsonObject]:
        """Apply the production Tool-route rules without starting a Run or calling a Model.

        Every Tool the Agent may use is listed with its definition, so
        ``load_tools`` is left out.
        """
        definitions = await self._dependencies.get_system_prompts().provider_tool_definitions_async(
            agent,
            session_tool_grants=session_tool_grants,
        )
        tools = [tool for tool in definitions if tool.get("name") != LOAD_TOOLS_TOOL_NAME]
        dialect = edit_dialect(_model_family(self._dependencies, agent))
        if not any(tool.get("name") == ANALYZE_IMAGE_TOOL_NAME for tool in tools):
            return await self._route_tool_definitions(
                tools,
                tool_access=agent.tool_access,
                input_modalities=frozenset(),
                wire_media_types=frozenset(),
                edit_dialect=dialect,
            )
        target = await self.targets.for_agent(agent)
        try:
            return await self._route_tool_definitions(
                tools,
                tool_access=agent.tool_access,
                input_modalities=target.input_modalities,
                wire_media_types=target.wire_media_types,
                edit_dialect=dialect,
            )
        finally:
            await _close_adapter(target.adapter)
