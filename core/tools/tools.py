"""Tool definitions, registry, result envelopes, and execution scheduling."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import weakref
from collections.abc import Callable, Sequence
from functools import wraps
from typing import Any, ClassVar, TypeVar

from core.tools._tool_context import (
    ToolCall,
    ToolCallCancelCheck,
    ToolCallCancelRegistrar,
    ToolCallResultPersistedRegistrar,
    ToolCancelCheckHook,
    ToolCancellationHook,
    ToolCancelRegistrationHook,
    ToolContext,
    ToolDeliveryReceipt,
    ToolDeliveryReceiptHook,
    ToolEmitHook,
    ToolExecutionConfig,
    ToolHandler,
    ToolNoteHook,
    ToolResultPersistedCallback,
    ToolResultPersistedHook,
    ToolSkillActivationHook,
    ToolTurnEndHook,
)
from core.tools._tool_definitions import (
    DuplicateToolError,
    InvalidToolResultError,
    SessionToolUnavailableError,
    Tool,
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDefinitionProfileResolver,
    ToolError,
    ToolFamily,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolReadinessPredicate,
    tool_is_ready,
)
from core.tools._tool_display import (
    DEFAULT_TOOL_DISPLAY_MAX_CHARACTERS,
    MAX_TOOL_DISPLAY_SUMMARY_LENGTH,
    MAX_TOOL_DISPLAY_VALUE_LENGTH,
    TOOL_DISPLAY_FACT_UNITS,
    TOOL_DISPLAY_LINE_CHANGES,
    TOOL_DISPLAY_TOOLTIP_MODES,
    TOOL_DISPLAY_TRUNCATION_MODES,
    TOOL_DISPLAY_VALUE_KINDS,
    ToolDisplay,
    ToolDisplayFactBuilder,
    ToolDisplayField,
    ToolDisplayPart,
    ToolDisplayPartBuilder,
    ToolSummaryBuilder,
    result_count_fact_builder,
)
from core.tools._tool_results import (
    READ_MEDIA_ARTIFACT_KIND,
    is_tool_result_envelope,
    read_media_artifact,
    tool_failure,
    tool_success,
)
from core.tools.availability import (
    TOOL_ACTIVATION_CONFIGURABLE,
)
from core.tools.contracts import ToolContract, compile_tool_contract
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("tools")

TOOL_ALLOWLIST_WILDCARD = "*"
DEFAULT_TOOL_CONCURRENCY_LIMIT = 50
DEFAULT_TOOL_WORKER_LIMIT = 8
BUILTIN_TOOL_FAMILY_LABELS = {
    "files": "Files",
    "execution": "Execution",
    "web": "Web",
    "sessions": "Sessions",
    "skills": "Skills",
    "media": "Media",
}

JsonObject = dict[str, Any]
_ToolWorkerResult = TypeVar("_ToolWorkerResult")

_TOOL_WORKERS = BoundedWorkerPool(
    name="tool",
    max_workers=DEFAULT_TOOL_WORKER_LIMIT,
)


async def run_tool_worker(
    function: Callable[..., _ToolWorkerResult],
    *arguments: Any,
    **keyword_arguments: Any,
) -> _ToolWorkerResult:
    """Run blocking Tool work through the dedicated backpressured worker pool."""
    return await _TOOL_WORKERS.run(function, *arguments, **keyword_arguments)


def _invoke_sync_tool_handler(
    handler: ToolHandler,
    context: ToolContext,
    arguments: JsonObject,
) -> Any:
    return handler(context, arguments)


def offload_tool_handler(handler: ToolHandler) -> ToolHandler:
    """Run one blocking Tool implementation in a worker without unsafe cancellation.

    Worker threads cannot be stopped once the handler starts. If the Tool task is
    cancelled, wait for the handler to finish before propagating cancellation so
    callers never treat an in-flight filesystem mutation as completed or abandoned.
    """

    @wraps(handler)
    async def offloaded(context: ToolContext, arguments: JsonObject) -> JsonObject:
        result = await run_tool_worker(handler, context, arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    return offloaded


class ToolRegistry:
    """Register, filter, describe, and dispatch agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.revision = 0
        self._families: dict[str, ToolFamily] = {
            family_id: ToolFamily(id=family_id, label=label)
            for family_id, label in BUILTIN_TOOL_FAMILY_LABELS.items()
        }
        self._definition_profile_cache: dict[
            tuple[str, str],
            tuple[str, ToolContract],
        ] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
        *,
        internal: bool = False,
        deferred: bool = False,
        session_scoped: bool = False,
        catalog_visible: bool = True,
        family: str | None = None,
        activation: str = TOOL_ACTIVATION_CONFIGURABLE,
        activation_source: str | None = None,
        requires_opt_in: bool = False,
        constraints: Sequence[str] = (),
        display: ToolDisplay | None = None,
        ready: ToolReadinessPredicate | None = None,
        readiness_hint: str | None = None,
        extension: str | None = None,
        result_schema: JsonObject | None = None,
        parallel_safe: bool = True,
        open_input_schema: bool = False,
        handler_validates_arguments: bool = False,
        coerce_arguments: bool = True,
        definition_profile_resolver: ToolDefinitionProfileResolver | None = None,
    ) -> Tool:
        """Register a tool and return its immutable definition.

        ``ready`` is an optional zero-arg readiness predicate (see :class:`Tool`):
        a not-ready tool stays registered but is filtered out of the model-facing
        surfaces and returns a failure envelope on a direct dispatch.
        ``readiness_hint`` is optional English text explaining the readiness
        precondition (surfaced by ``tool.list``); ``extension`` names the owning
        extension (``None`` for a built-in), set at extension-tool apply time.
        """
        family_definition = None
        if family is not None:
            family_definition = self._families.get(family)
            if family_definition is None:
                raise ValueError(f"Tool family is not registered: {family}")
        self._validate_tool(
            name,
            description,
            parameters,
            handler,
            display,
            ready,
            definition_profile_resolver,
        )
        if name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {name}")
        tool = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            result_schema=result_schema,
            internal=internal,
            deferred=deferred,
            session_scoped=session_scoped,
            catalog_visible=catalog_visible,
            family=family,
            family_label=family_definition.label if family_definition is not None else None,
            activation=activation,
            activation_source=activation_source,
            requires_opt_in=requires_opt_in,
            constraints=tuple(dict.fromkeys(constraints)),
            display=display or ToolDisplay(),
            ready=ready,
            readiness_hint=readiness_hint,
            extension=extension,
            parallel_safe=parallel_safe,
            open_input_schema=open_input_schema,
            handler_validates_arguments=handler_validates_arguments,
            coerce_arguments=coerce_arguments,
            definition_profile_resolver=definition_profile_resolver,
        )
        self._tools[name] = tool
        self.revision += 1
        return tool

    def register_family(
        self,
        family_id: str,
        label: str,
        *,
        extension: str | None = None,
    ) -> ToolFamily:
        """Register presentation metadata for Tools that share one family."""
        family = ToolFamily(id=family_id, label=label, extension=extension)
        if family.id in self._families:
            raise ValueError(f"Tool family already registered: {family.id}")
        self._families[family.id] = family
        return family

    def unregister_family(self, family_id: str, *, extension: str | None = None) -> None:
        """Remove an Extension-owned family after its Tools are removed."""
        if extension is None:
            return
        family = self._families.get(family_id)
        if family is None or family.extension != extension:
            return
        if any(tool.family == family_id for tool in self._tools.values()):
            return
        self._families.pop(family_id, None)

    def get_family(self, family_id: str) -> ToolFamily:
        """Return one registered family definition."""
        try:
            return self._families[family_id]
        except KeyError:
            raise ValueError(f"Tool family not found: {family_id}") from None

    def list_families(self) -> list[ToolFamily]:
        """Return registered family definitions in stable id order."""
        return sorted(self._families.values(), key=lambda family: family.id)

    def display_for_call(
        self,
        name: str,
        arguments: Any,
        *,
        context: ToolContext | None = None,
        result: JsonObject | None = None,
    ) -> JsonObject:
        """Return display metadata for a concrete tool invocation."""
        facts = context.presentation_facts if context is not None else ()
        payload = self.get(name).display.to_payload(
            arguments,
            context=context,
            result=result,
            facts=facts,
        )
        if context is not None and context.presentation_images:
            payload["image_files"] = [dict(image) for image in context.presentation_images]
        return payload

    def get(self, name: str) -> Tool:
        """Return a registered tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"Tool not found: {name}") from None

    def replace_owned_tools(self, previous: Sequence[Tool], candidates: Sequence[Tool]) -> None:
        """Atomically publish validated Tools, checking exact previous ownership."""
        owned = {tool.name: tool for tool in previous}
        for tool in candidates:
            existing = self._tools.get(tool.name)
            if existing is not None and existing is not owned.get(tool.name):
                raise DuplicateToolError(f"Tool already registered: {tool.name}")
        for name, tool in owned.items():
            if self._tools.get(name) is tool:
                self.unregister(name)
        self._tools.update({tool.name: tool for tool in candidates})
        self.revision += 1

    def unregister(self, name: str) -> None:
        """Remove a registered tool when it exists."""
        if self._tools.pop(name, None) is not None:
            self.revision += 1
        for cache_key in [
            cache_key for cache_key in self._definition_profile_cache if cache_key[0] == name
        ]:
            self._definition_profile_cache.pop(cache_key, None)

    def is_parallel_safe(self, name: str) -> bool:
        """Return whether a requested Tool Call may overlap a sibling call.

        Unknown Tools fail before invoking a handler, so they are safe to keep
        in the same parallel group instead of becoming accidental barriers.
        """
        tool = self._tools.get(name)
        return tool is None or tool.parallel_safe

    def schema_fingerprint(self, name: str) -> str:
        """Return the deterministic canonical schema fingerprint for a Tool."""
        return self.get(name).contract.schema_fingerprint

    def validate_result(self, name: str, result: Any) -> JsonObject:
        """Validate a Tool result envelope and its successful data contract."""
        if not isinstance(result, dict):
            raise InvalidToolResultError(f"Tool handler must return a JSON object: {name}")
        if not is_tool_result_envelope(result):
            raise InvalidToolResultError(
                f"Tool handler must return a valid result envelope: {name}"
            )
        if result["ok"]:
            try:
                self.get(name).contract.validate_success_data(result["data"])
            except ValueError as error:
                raise InvalidToolResultError(
                    f"Tool result violates its contract: {name}: {error}"
                ) from None
        try:
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise InvalidToolResultError(
                f"Tool result is not JSON-serializable: {name}: {error}"
            ) from None
        return result

    def list_tools(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        include_session_scoped: bool = True,
        include_catalog_hidden: bool = True,
        ready_only: bool = False,
    ) -> list[Tool]:
        """Return registered tools filtered by an allowlist.

        The filter order is **registered → allowed → ready**: when *ready_only*
        is true (opt-in; default ``False``), the readiness predicate is applied
        **after** the allowlist/internal filters, so a not-ready tool is dropped
        only from the model-facing surfaces — it stays registered and keeps its
        persisted permissions. Callers that must keep seeing not-ready tools
        (collision detection, the effective-allowlist computation, the startup
        inventory count) leave *ready_only* at its default.
        """
        if allowed_tools is not None and TOOL_ALLOWLIST_WILDCARD not in allowed_tools:
            allowed_names = set(allowed_tools)
            tools = [tool for name, tool in self._tools.items() if name in allowed_names]
        else:
            tools = list(self._tools.values())

        if not include_internal:
            tools = [tool for tool in tools if not tool.internal]

        if not include_session_scoped:
            tools = [tool for tool in tools if not tool.session_scoped]

        if not include_catalog_hidden:
            tools = [tool for tool in tools if tool.catalog_visible]

        if ready_only:
            tools = [tool for tool in tools if tool_is_ready(tool)]

        return sorted(tools, key=lambda tool: tool.name)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        ready_only: bool = True,
        profile_context: ToolDefinitionProfileContext | None = None,
    ) -> list[JsonObject]:
        """Return provider-ready tool definitions for allowed, ready tools.

        *ready_only* defaults to ``True``: provider definitions are a model-facing
        surface, so a not-ready tool is hidden by default.
        """
        definitions: list[JsonObject] = []
        for tool in self._model_facing_tools(
            allowed_tools,
            include_internal=include_internal,
            session_grants=session_grants,
            ready_only=ready_only,
        ):
            if tool.deferred:
                continue
            definition = self._to_provider_definition(tool, profile_context)
            if definition is not None:
                definitions.append(definition)
        return definitions

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        ready_only: bool = True,
        profile_context: ToolDefinitionProfileContext | None = None,
    ) -> list[JsonObject]:
        """Return prompt-ready name and description pairs for allowed, ready tools.

        *ready_only* defaults to ``True`` (a model-facing surface); a not-ready
        tool is absent from the prompt tool list and, through it, gate 2 of a
        ``tool:<name>``-owned prompt block.
        """
        definitions: list[JsonObject] = []
        for tool in self._model_facing_tools(
            allowed_tools,
            include_internal=include_internal,
            session_grants=session_grants,
            ready_only=ready_only,
        ):
            if tool.deferred:
                continue
            resolved = self._resolve_definition_profile(tool, profile_context)
            if resolved is None:
                continue
            description, _contract = resolved
            definitions.append({"name": tool.name, "description": description})
        return definitions

    def contracts_for_provider_definitions(
        self,
        definitions: Sequence[JsonObject],
    ) -> dict[str, ToolContract]:
        """Compile the exact model-facing input contracts for one Provider cycle."""
        contracts: dict[str, ToolContract] = {}
        for definition in definitions:
            name = definition.get("name")
            parameters = definition.get("parameters")
            if not isinstance(name, str) or not name:
                raise ValueError("Provider Tool definition name must be a non-empty string")
            if name in contracts:
                raise ValueError(f"Duplicate Provider Tool definition: {name}")
            if not isinstance(parameters, dict):
                raise ValueError(f"Provider Tool definition parameters must be an object: {name}")
            tool = self._tools.get(name)
            contracts[name] = compile_tool_contract(
                name=name,
                input_schema=parameters,
                result_schema=tool.contract.result_schema if tool is not None else None,
                parallel_safe=tool.parallel_safe if tool is not None else True,
                require_closed_input=not (tool is not None and tool.open_input_schema),
            )
        return contracts

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        """Execute a registered allowed tool through an async interface."""
        tool = self.get(context.tool_name)
        if tool.session_scoped and context.tool_name not in context.session_tool_grants:
            raise SessionToolUnavailableError(f"Session tool unavailable: {context.tool_name}")
        if (
            tool.requires_opt_in and context.tool_name not in (allowed_tools or ())
        ) or not self._is_allowed(context.tool_name, allowed_tools, internal=tool.internal):
            raise ToolNotAllowedError(f"Tool not allowed: {context.tool_name}")
        # Readiness safety net: dispatch is not list-filtered, so a prompt built
        # moments before the credential vanished could still request a now
        # not-ready tool. Re-evaluate live and return a clean failure envelope
        # instead of running the handler (no exception, so the model just gets a
        # normal failed result naming the cause).
        if not tool_is_ready(tool):
            return tool_failure(
                "tool_not_ready",
                f"tool '{context.tool_name}' is not available: its extension is not configured",
                retryable=False,
            )
        input_contract = context.input_contract or tool.contract
        normalized_arguments = (
            input_contract.normalize_arguments(arguments) if tool.coerce_arguments else arguments
        )
        if not tool.handler_validates_arguments:
            input_contract.validate_arguments(normalized_arguments)

        if tool.extension is not None and not inspect.iscoroutinefunction(tool.handler):
            result = await run_tool_worker(
                _invoke_sync_tool_handler,
                tool.handler,
                context,
                normalized_arguments,
            )
        else:
            result = _invoke_sync_tool_handler(
                tool.handler,
                context,
                normalized_arguments,
            )
        if inspect.isawaitable(result):
            result = await result
        return self.validate_result(context.tool_name, result)

    def _model_facing_tools(
        self,
        allowed_tools: Sequence[str] | None,
        *,
        include_internal: bool,
        session_grants: Sequence[str],
        ready_only: bool,
    ) -> list[Tool]:
        """Return one model-facing set from Agent policy plus Session grants."""
        grants = set(session_grants)
        tools: list[Tool] = []
        for tool in self._tools.values():
            if tool.internal and not include_internal:
                continue
            if tool.requires_opt_in and tool.name not in (allowed_tools or ()):
                continue
            if tool.session_scoped:
                if tool.name not in grants:
                    continue
            elif not self._is_allowed(tool.name, allowed_tools):
                continue
            if ready_only and not tool_is_ready(tool):
                continue
            tools.append(tool)
        return sorted(tools, key=lambda tool: tool.name)

    @staticmethod
    def _validate_tool(
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
        display: ToolDisplay | None = None,
        ready: ToolReadinessPredicate | None = None,
        definition_profile_resolver: ToolDefinitionProfileResolver | None = None,
    ) -> None:
        if not name:
            raise ValueError("Tool name is required")
        if not description:
            raise ValueError("Tool description is required")
        if not isinstance(parameters, dict):
            raise ValueError("Tool parameters must be a JSON Schema object")
        if not callable(handler):
            raise ValueError("Tool handler must be callable")
        if display is not None and not isinstance(display, ToolDisplay):
            raise ValueError("Tool display must be a ToolDisplay instance")
        if ready is not None and not callable(ready):
            raise ValueError("Tool ready predicate must be callable")
        if definition_profile_resolver is not None and not callable(definition_profile_resolver):
            raise ValueError("Tool definition profile resolver must be callable")

    @staticmethod
    def _is_allowed(
        name: str,
        allowed_tools: Sequence[str] | None,
        *,
        internal: bool = False,
    ) -> bool:
        if internal:
            return True
        return (
            allowed_tools is None
            or TOOL_ALLOWLIST_WILDCARD in allowed_tools
            or name in allowed_tools
        )

    def _to_provider_definition(
        self,
        tool: Tool,
        profile_context: ToolDefinitionProfileContext | None,
    ) -> JsonObject | None:
        resolved = self._resolve_definition_profile(tool, profile_context)
        if resolved is None:
            return None
        description, contract = resolved
        return {
            "name": tool.name,
            "description": description,
            "parameters": copy.deepcopy(contract.input_schema),
        }

    def _resolve_definition_profile(
        self,
        tool: Tool,
        profile_context: ToolDefinitionProfileContext | None,
    ) -> tuple[str, ToolContract] | None:
        resolver = tool.definition_profile_resolver
        if resolver is None or profile_context is None:
            return tool.description, tool.contract
        try:
            profile = resolver(profile_context)
        except Exception as error:
            _LOGGER.warning(
                "Tool %s definition profile resolver raised: %s",
                tool.name,
                error,
                exc_info=True,
            )
            return None
        if profile is None:
            return None

        cache_key = (tool.name, profile.key)
        cached = self._definition_profile_cache.get(cache_key)
        if cached is not None:
            return cached
        contract = compile_tool_contract(
            name=tool.name,
            input_schema=profile.parameters,
            result_schema=tool.contract.result_schema,
            parallel_safe=tool.parallel_safe,
            require_closed_input=not tool.open_input_schema,
        )
        resolved = (profile.description, contract)
        self._definition_profile_cache[cache_key] = resolved
        return resolved


class ToolPromptBlockRegistry:
    """Collect tool-owned System Prompt block declarations (D6).

    The tool-side of the unified contributor path: a tool that wants prompt
    content declares a block here at its ``register_*`` step, and the runtime
    gathers :meth:`block_definitions` and hands them to the prompt manager. This
    keeps the prompt domain free of tool internals — it only ever consumes a list
    of ``core.prompts.BlockDefinition`` objects, never imports a tool class.

    A declared block is id ``tool:<name>`` and owner ``tool:<name>`` (so gate 2
    renders it only when ``<name>`` is on the agent's effective allowlist), static
    (``default_text``) or dynamic (``render``) — the same split as a core or
    extension block. Project and Sub-Agent use this seam for dynamic catalogs and
    guidance. Collisions are resolved first-wins with a warning, like tool-name
    registration.
    """

    def __init__(self) -> None:
        self._declarations: dict[str, tuple[str | None, Callable[..., str] | None]] = {}

    def register(
        self,
        tool_name: str,
        *,
        default_text: str | None = None,
        render: Callable[..., str] | None = None,
    ) -> None:
        """Declare a prompt block for *tool_name* (exactly one text / render).

        Passing both or neither raises ``ValueError`` at declaration. A second
        declaration for the same tool name is ignored with a warning (first wins),
        mirroring how a duplicate tool name is handled.
        """
        if not tool_name:
            raise ValueError("Tool prompt block requires a tool name")
        has_text = default_text is not None
        has_render = render is not None
        if has_text == has_render:
            raise ValueError("Tool prompt block requires exactly one of default_text / render")
        if tool_name in self._declarations:
            _LOGGER.warning(
                "Tool prompt block for %r already declared; ignoring the duplicate",
                tool_name,
            )
            return
        self._declarations[tool_name] = (default_text, render)

    def block_definitions(self) -> list[Any]:
        """Return the declared blocks as ``core.prompts.BlockDefinition`` objects.

        Lazy ``core.prompts`` import so the tools domain carries no import-time
        dependency on the prompts domain (this runs at runtime collection, never at
        module load). Order is declaration order.
        """
        from core.prompts import BlockDefinition

        definitions: list[Any] = []
        for tool_name, (default_text, render) in self._declarations.items():
            definitions.append(
                BlockDefinition(
                    id=f"tool:{tool_name}",
                    owner=f"tool:{tool_name}",
                    default_text=default_text,
                    render=render,
                )
            )
        return definitions


class ToolExecutor:
    """Schedule concurrent Tool groups around explicit serial barriers."""

    _global_semaphores: ClassVar[
        weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]]
    ] = weakref.WeakKeyDictionary()

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        per_run_limit: int = DEFAULT_TOOL_CONCURRENCY_LIMIT,
        global_limit: int = DEFAULT_TOOL_CONCURRENCY_LIMIT,
    ) -> None:
        if per_run_limit < 1:
            raise ValueError("Per-run tool concurrency limit must be at least 1")
        if global_limit < 1:
            raise ValueError("Global tool concurrency limit must be at least 1")

        self._registry = registry
        self._per_run_limit = per_run_limit
        self._global_limit = global_limit

    async def execute_many(
        self,
        tool_calls: Sequence[ToolCall],
        config: ToolExecutionConfig,
    ) -> list[JsonObject]:
        """Execute parallel-by-default calls and return results in request order."""
        per_run_semaphore = asyncio.Semaphore(self._per_run_limit)
        results: list[JsonObject | None] = [None] * len(tool_calls)
        parallel_group: list[tuple[int, ToolCall]] = []

        async def flush_parallel_group() -> None:
            if not parallel_group:
                return
            tasks = [
                asyncio.create_task(
                    self._execute_one(tool_call, index, config, per_run_semaphore),
                    name=f"tool:{tool_call.name}:{tool_call.id}",
                )
                for index, tool_call in parallel_group
            ]
            try:
                group_results = await asyncio.gather(*tasks)
            except BaseException:
                # A child failure does not make gather cancel its siblings.
                # Keep ownership until their cancellation cleanup has settled,
                # then preserve the original abort for the Run boundary.
                for task in tasks:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for (index, _tool_call), result in zip(
                parallel_group,
                group_results,
                strict=True,
            ):
                results[index] = result
            parallel_group.clear()

        for index, tool_call in enumerate(tool_calls):
            if self._registry.is_parallel_safe(tool_call.name):
                parallel_group.append((index, tool_call))
                continue
            await flush_parallel_group()
            results[index] = await self._execute_one(
                tool_call,
                index,
                config,
                per_run_semaphore,
            )
        await flush_parallel_group()
        return [result for result in results if result is not None]

    async def _execute_one(
        self,
        tool_call: ToolCall,
        index: int,
        config: ToolExecutionConfig,
        per_run_semaphore: asyncio.Semaphore,
    ) -> JsonObject:
        async with per_run_semaphore, self._get_global_semaphore():
            # Per-call cancel hooks close over tool_call.id so concurrent sibling
            # tool calls in one execution group each register/inspect their own id.
            cancel_registration_hook, cancel_check_hook = _build_per_call_cancel_hooks(
                config, tool_call.id
            )
            result_persisted_hook: ToolResultPersistedHook | None = None
            result_persisted_registrar = config.tool_call_result_persisted_registrar
            if result_persisted_registrar is not None:

                def register_result_persisted(callback: ToolResultPersistedCallback) -> None:
                    result_persisted_registrar(tool_call.id, callback)

                result_persisted_hook = register_result_persisted
            context = ToolContext(
                agent_id=config.agent_id,
                session_id=config.session_id,
                run_id=config.run_id,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                tool_call_index=index,
                workspace=config.workspace,
                vbot_root=config.vbot_root,
                data_root=config.data_root,
                iteration_number=config.iteration_number,
                execution_owner=config.execution_owner,
                cwd=config.cwd,
                project_id=config.project_id,
                skill_project_id=config.skill_project_id,
                emit_hook=config.emit_hook,
                cancellation_hook=config.cancellation_hook,
                cancel_registration_hook=cancel_registration_hook,
                cancel_check_hook=cancel_check_hook,
                note_hook=config.note_hook,
                skill_activation_hook=config.skill_activation_hook,
                result_persisted_hook=result_persisted_hook,
                delivery_receipt_hook=config.tool_delivery_receipt_registrar,
                request_turn_end_hook=config.tool_turn_end_registrar,
                allowed_skills=config.allowed_skills,
                skill_env_keys=config.skill_env_keys,
                tool_settings=config.tool_settings,
                session_tool_grants=config.session_tool_grants,
                nesting_depth=config.nesting_depth,
                input_contract=config.input_contracts.get(tool_call.name),
                change_tracker=config.change_tracker,
            )
            return await self._dispatch_with_envelope(context, tool_call, config.allowed_tools)

    def _get_global_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        loop_semaphores = self._global_semaphores.setdefault(loop, {})
        semaphore = loop_semaphores.get(self._global_limit)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._global_limit)
            loop_semaphores[self._global_limit] = semaphore
        return semaphore

    async def _dispatch_with_envelope(
        self,
        context: ToolContext,
        tool_call: ToolCall,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            return await self._registry.dispatch(context, tool_call.arguments, allowed_tools)
        except ToolNotFoundError as error:
            return tool_failure("tool_not_found", str(error))
        except SessionToolUnavailableError as error:
            return tool_failure(f"{context.tool_name}_unavailable", str(error))
        except ToolNotAllowedError as error:
            return tool_failure("tool_not_allowed", str(error))
        except InvalidToolResultError as error:
            return tool_failure("invalid_tool_result", str(error))
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        except Exception as error:
            _LOGGER.error("Tool %s crashed unexpectedly", context.tool_name, exc_info=error)
            return tool_failure("tool_execution_error", str(error))


def _build_per_call_cancel_hooks(
    config: ToolExecutionConfig, tool_call_id: str
) -> tuple[ToolCancelRegistrationHook | None, ToolCancelCheckHook | None]:
    """Return per-call cancel hooks that close over *tool_call_id*.

    When the config carries a registrar/check that takes a tool call id, this
    binds the per-call id so concurrent sibling tool calls each see their own
    registry entry. Falls back to the group-wide hooks when the per-call fields
    are absent (e.g., executor tests that wire hooks directly).
    """
    registration_hook: ToolCancelRegistrationHook | None
    if config.tool_call_cancel_registrar is not None:
        registrar = config.tool_call_cancel_registrar

        def registration_hook(callback: Callable[[], None]) -> None:
            registrar(tool_call_id, callback)

    else:
        registration_hook = config.cancel_registration_hook

    check_hook: ToolCancelCheckHook | None
    if config.tool_call_cancel_check is not None:
        check = config.tool_call_cancel_check

        def check_hook() -> bool:
            return check(tool_call_id)

    else:
        check_hook = config.cancel_check_hook

    return registration_hook, check_hook


__all__ = [
    "DEFAULT_TOOL_CONCURRENCY_LIMIT",
    "DuplicateToolError",
    "JsonObject",
    "SessionToolUnavailableError",
    "TOOL_ALLOWLIST_WILDCARD",
    "Tool",
    "ToolCall",
    "ToolCancelCheckHook",
    "ToolCancelRegistrationHook",
    "ToolCancellationHook",
    "ToolContext",
    "ToolEmitHook",
    "ToolError",
    "ToolExecutionConfig",
    "ToolExecutor",
    "ToolHandler",
    "ToolNoteHook",
    "ToolNotAllowedError",
    "ToolNotFoundError",
    "ToolPromptBlockRegistry",
    "ToolReadinessPredicate",
    "ToolRegistry",
    "READ_MEDIA_ARTIFACT_KIND",
    "is_tool_result_envelope",
    "read_media_artifact",
    "tool_failure",
    "tool_is_ready",
    "tool_success",
    "ToolCallCancelCheck",
    "ToolCallCancelRegistrar",
    "ToolCallResultPersistedRegistrar",
    "ToolDeliveryReceipt",
    "ToolDeliveryReceiptHook",
    "ToolResultPersistedCallback",
    "ToolResultPersistedHook",
    "ToolSkillActivationHook",
    "ToolTurnEndHook",
    "InvalidToolResultError",
    "ToolDefinitionProfile",
    "ToolDefinitionProfileContext",
    "ToolDefinitionProfileResolver",
    "ToolFamily",
    "DEFAULT_TOOL_DISPLAY_MAX_CHARACTERS",
    "MAX_TOOL_DISPLAY_SUMMARY_LENGTH",
    "MAX_TOOL_DISPLAY_VALUE_LENGTH",
    "TOOL_DISPLAY_FACT_UNITS",
    "TOOL_DISPLAY_LINE_CHANGES",
    "TOOL_DISPLAY_TOOLTIP_MODES",
    "TOOL_DISPLAY_TRUNCATION_MODES",
    "TOOL_DISPLAY_VALUE_KINDS",
    "ToolDisplay",
    "ToolDisplayFactBuilder",
    "ToolDisplayField",
    "ToolDisplayPart",
    "ToolDisplayPartBuilder",
    "ToolSummaryBuilder",
    "result_count_fact_builder",
]
