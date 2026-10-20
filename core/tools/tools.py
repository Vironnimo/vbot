"""Tool definitions, registry, result envelopes, and execution scheduling."""

from __future__ import annotations

import copy
import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any, cast

from core.secrets.agent_secrets import (
    EMPTY_MASKER,
    AgentSecrets,
    SecretMasker,
    SecretUse,
    SecretUseError,
    SinkClass,
    describe_uses,
    has_reference,
)
from core.tools._tool_context import (
    TOOL_ALLOWLIST_WILDCARD,
    ToolCall,
    ToolCallCancelCheck,
    ToolCallCancelRegistrar,
    ToolCallResultPersistedRegistrar,
    ToolCancelCallback,
    ToolCancelCheckHook,
    ToolCancellationHook,
    ToolCancelRegistrationHook,
    ToolContext,
    ToolDeliveryReceipt,
    ToolDeliveryReceiptHook,
    ToolEmitHook,
    ToolExecutionConfig,
    ToolHandler,
    ToolLoadHook,
    ToolNoteHook,
    ToolResultPayloadHook,
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
    definition_change_announced,
    tool_is_ready,
    tool_summary,
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
    ToolDisplayDetailBuilder,
    ToolDisplayFactBuilder,
    ToolDisplayField,
    ToolDisplayPart,
    ToolDisplayPartBuilder,
    ToolSummaryBuilder,
    display_notice,
    display_results,
    display_text,
    result_count_fact_builder,
)
from core.tools._tool_execution import (
    DEFAULT_TOOL_CONCURRENCY_LIMIT as DEFAULT_TOOL_CONCURRENCY_LIMIT,
)
from core.tools._tool_execution import ToolBatch as ToolBatch
from core.tools._tool_execution import ToolExecutor as ToolExecutor
from core.tools._tool_prompt_blocks import ToolPromptBlockRegistry as ToolPromptBlockRegistry
from core.tools._tool_results import (
    READ_MEDIA_ARTIFACT_KIND,
    TOOL_EXECUTION_ERROR_CODE,
    is_tool_result_envelope,
    read_media_artifact,
    tool_failure,
    tool_failure_for_exception,
    tool_success,
    with_call_note,
)
from core.tools._tool_workers import DEFAULT_TOOL_WORKER_LIMIT as DEFAULT_TOOL_WORKER_LIMIT
from core.tools._tool_workers import _invoke_sync_tool_handler
from core.tools._tool_workers import offload_tool_handler as offload_tool_handler
from core.tools._tool_workers import run_tool_worker as run_tool_worker
from core.tools.availability import (
    TOOL_ACTIVATION_CONFIGURABLE,
)
from core.tools.contracts import (
    ToolContract,
    ToolContractError,
    compile_tool_contract,
    ignored_fields_note,
)
from core.tools.model_names import model_tool_name, reserved_model_name
from core.utils.logging import get_logger

_LOGGER = get_logger("tools")
BUILTIN_TOOL_FAMILY_LABELS = {
    "files": "Files",
    "execution": "Execution",
    "web": "Web",
    "sessions": "Sessions",
    "skills": "Skills",
    "media": "Media",
    "live": "Live voice",
}

JsonObject = dict[str, Any]


def _arguments_fit(contract: ToolContract, arguments: Any) -> bool:
    """Return whether ``arguments`` read as a call of the Tool with ``contract``.

    Every field is one of its parameters, every required one is present, and a
    value of a parameter with fixed values, such as ``action``, is one of them.
    """
    if not isinstance(arguments, dict) or not arguments:
        return False
    properties = contract.input_schema.get("properties")
    if not isinstance(properties, dict) or not arguments.keys() <= properties.keys():
        return False
    required = contract.input_schema.get("required", ())
    if not set(required) <= arguments.keys():
        return False
    return all(
        not isinstance(properties[field], dict)
        or "enum" not in properties[field]
        or value in properties[field]["enum"]
        for field, value in arguments.items()
    )


# Characters of tool-call markup (``<function=x>``, ``parameter="y"``) that no
# Tool or parameter name contains; a Provider's parser leaves them in a call it
# split wrongly.
_CALL_MARKUP = re.compile(r'[<>="]')
_DAMAGED_NAME = re.compile(r'[<>="\s]')
_NAME_SEPARATORS = re.compile(r"[\W_]+")


def _call_damage(name: str, arguments: Any) -> str:
    """Say what a call to an unknown Tool shows of damage in transit, or return "".

    A Provider's parser that splits the Model's tool-call markup wrongly leaves a
    garbled name, often with no arguments at all, or markup in the names. A call
    without arguments can also be meant that way, so only the Agent can tell.
    """
    if not isinstance(arguments, dict) or not arguments:
        return (
            "the call arrived with no arguments at all. If you sent some, the call was "
            "garbled on the way: send the complete call again."
        )
    if _DAMAGED_NAME.search(name):
        where = "its Tool name"
    else:
        key = next((key for key in arguments if _CALL_MARKUP.search(key)), None)
        if key is None:
            return ""
        where = f'the argument name "{key}"'
    return (
        f"the call arrived with tool-call markup in {where}, so it was garbled on the way. "
        "Send the complete call again."
    )


def _spelling(name: str) -> str:
    return _NAME_SEPARATORS.sub("", name).casefold()


def _closest_tool_name(name: str, candidates: Sequence[str]) -> str | None:
    """Return the candidate that ``name`` clearly resembles most, else None.

    Clearly: at least half alike and well ahead of the next candidate, so a
    garbled ``powerhellsparameter`` names ``powershell`` while ``swarm`` names
    none of the ``swarm_*`` Tools.
    """
    damaged = _DAMAGED_NAME.search(name)
    key = _spelling(name[: damaged.start()] if damaged else name)
    if not key:
        return None
    scores = sorted(
        (
            (SequenceMatcher(None, key, _spelling(candidate)).ratio(), candidate)
            for candidate in candidates
        ),
        reverse=True,
    )
    if not scores or scores[0][0] < 0.5:
        return None
    if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.1:
        return None
    return scores[0][1]


def _checked_arguments(
    tool: Tool, contract: ToolContract, arguments: Any
) -> tuple[Any, tuple[str, ...]]:
    """Return the arguments the handler receives, or refuse the call before it runs.

    Also returns the unknown fields the call runs without, which its result
    names as without effect (``ToolContract.drop_inert_fields``); a refused call
    names them as unknown, since it ran without anything. Whatever argument
    repair and validation raise refuses the call as a ``ToolContractError``,
    because nothing has run yet. A call refused that arrived as ``{}`` lost its
    arguments on the way and is told so. A repair that crashes for another
    reason than a ``ValueError`` is a defect and is logged.
    """
    received = arguments
    ignored: tuple[str, ...] = ()
    try:
        if tool.coerce_arguments:
            # A call sent as one name/value pair reaches the owner as that field.
            arguments = contract.read_name_value_pair(
                arguments, unadvertised=tool.unadvertised_parameters or {}
            )
        if tool.argument_normalizer is not None:
            arguments = tool.argument_normalizer(copy.deepcopy(arguments))
        normalized = contract.normalize_arguments(arguments) if tool.coerce_arguments else arguments
        if not tool.handler_validates_arguments:
            unadvertised = tool.unadvertised_parameters
            # Parameters a Model request's definition left out are never ignored.
            withheld = (
                ()
                if contract is tool.contract
                else tool.contract.input_schema.get("properties", {})
            )
            kept, ignored = contract.drop_inert_fields(
                normalized, unadvertised=unadvertised, withheld=withheld
            )
            try:
                contract.validate_arguments(kept, unadvertised=unadvertised)
            except ToolContractError:
                if ignored:
                    contract.validate_arguments(normalized, unadvertised=unadvertised)
                raise
            normalized = kept
    except ValueError as error:  # ToolContractError included
        if isinstance(received, dict) and not received:
            raise ToolContractError(contract.no_arguments_message()) from error
        if isinstance(error, ToolContractError):
            raise
        raise ToolContractError(str(error)) from error
    except Exception as error:
        _LOGGER.error("Tool %s argument repair crashed unexpectedly", tool.name, exc_info=error)
        cause = str(error).strip() or type(error).__name__
        raise ToolContractError(f"{model_tool_name(tool.name)} was not run: {cause}") from error
    return normalized, ignored


def _detail_blocks(
    context: ToolContext | None, result: JsonObject | None, built: Sequence[JsonObject]
) -> list[JsonObject]:
    """Return the call's recorded detail blocks, which follow the ``built`` ones.

    A failed call without an error notice, such as one rejected before its
    handler ran, shows the result's error message.
    """
    blocks = copy.deepcopy(context.presentation_details) if context is not None else []
    error = result.get("error") if isinstance(result, dict) and result.get("ok") is False else None
    message = error.get("message") if isinstance(error, dict) else None
    if (
        isinstance(message, str)
        and message.strip()
        and not any(
            block["type"] == "notice" and block["level"] == "error" for block in (*built, *blocks)
        )
    ):
        blocks.append(display_notice("error", message))
    return blocks


class ToolRegistry:
    """Register, filter, describe, and dispatch agent tools."""

    def __init__(self) -> None:
        # Prompt assembly reads the registry from worker threads while the loop
        # registers Extension and MCP Tools. Mutations therefore publish a new
        # dict instead of changing the current one, so a reader iterating a
        # snapshot never sees it change size.
        self._tools: dict[str, Tool] = {}
        self.revision = 0
        self._families: dict[str, ToolFamily] = {
            family_id: ToolFamily(id=family_id, label=label)
            for family_id, label in BUILTIN_TOOL_FAMILY_LABELS.items()
        }
        # Compiled definition profiles by Tool name, then profile key, so
        # removing a Tool drops its entries without iterating the cache.
        self._definition_profiles: dict[str, dict[str, tuple[str, ToolContract]]] = {}
        # Resolves ``{{secret:NAME}}`` references in declared sink arguments.
        self._secrets: AgentSecrets | None = None

    def set_secrets(self, secrets: AgentSecrets | None) -> None:
        """Use ``secrets`` to substitute references in the sink arguments of every Tool."""
        self._secrets = secrets

    @property
    def secret_masker(self) -> SecretMasker:
        """Masks known Secret values in what Tools return (Chat applies it)."""
        return self._secrets.masker if self._secrets is not None else EMPTY_MASKER

    def register(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
        *,
        summary: str | None = None,
        internal: bool = False,
        deferred: bool = False,
        session_scoped: bool = False,
        catalog_visible: bool = True,
        family: str | None = None,
        activation: str = TOOL_ACTIVATION_CONFIGURABLE,
        activation_source: str | Sequence[str] | None = None,
        requires_opt_in: bool = False,
        constraints: Sequence[str] = (),
        display: ToolDisplay | None = None,
        ready: ToolReadinessPredicate | None = None,
        readiness_hint: str | None = None,
        readiness_retryable: bool = False,
        extension: str | None = None,
        result_schema: JsonObject | None = None,
        parallel_safe: bool = True,
        execution_slot_required: bool = True,
        open_input_schema: bool = False,
        unadvertised_parameters: JsonObject | None = None,
        handler_validates_arguments: bool = False,
        coerce_arguments: bool = True,
        argument_normalizer: Callable[[Any], Any] | None = None,
        definition_profile_resolver: ToolDefinitionProfileResolver | None = None,
        announce_changes: Sequence[str] = (),
        secret_sinks: Mapping[str, str] | None = None,
    ) -> Tool:
        """Register a tool and return its immutable definition.

        ``ready`` is an optional zero-arg readiness predicate (see :class:`Tool`):
        a not-ready tool stays registered but is filtered out of the model-facing
        surfaces and returns a failure envelope on a direct dispatch.
        ``readiness_hint`` is optional English text explaining the readiness
        precondition (surfaced by ``tool.list``) and ``readiness_retryable``
        whether the not-ready failure is transient; ``extension`` names the owning
        extension (``None`` for a built-in), set at extension-tool apply time.
        ``announce_changes`` names the parts of the definition whose change
        reaches a Session that already knows the Tool (see :class:`Tool`).
        ``summary`` is the optional one-sentence, Model-facing text that lists
        the Tool where its full definition is not shown (:func:`tool_summary`).
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
        if name in self._tools or reserved_model_name(name):
            # A reserved name is how the Model already sees another Tool.
            raise DuplicateToolError(f"Tool already registered: {name}")
        tool = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            result_schema=result_schema,
            summary=summary,
            internal=internal,
            deferred=deferred,
            session_scoped=session_scoped,
            catalog_visible=catalog_visible,
            family=family,
            family_label=family_definition.label if family_definition is not None else None,
            activation=activation,
            activation_source=(
                activation_source
                if activation_source is None or isinstance(activation_source, str)
                else tuple(activation_source)
            ),
            requires_opt_in=requires_opt_in,
            constraints=tuple(dict.fromkeys(constraints)),
            display=display or ToolDisplay(),
            ready=ready,
            readiness_hint=readiness_hint,
            readiness_retryable=readiness_retryable,
            extension=extension,
            parallel_safe=parallel_safe,
            execution_slot_required=execution_slot_required,
            open_input_schema=open_input_schema,
            unadvertised_parameters=unadvertised_parameters,
            handler_validates_arguments=handler_validates_arguments,
            coerce_arguments=coerce_arguments,
            argument_normalizer=argument_normalizer,
            definition_profile_resolver=definition_profile_resolver,
            # Tool validates and normalizes the entries into a tuple.
            announce_changes=cast(tuple[str, ...], announce_changes),
            secret_sinks=dict(secret_sinks or {}),
        )
        self._tools = {**self._tools, name: tool}
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
        self._families = {**self._families, family.id: family}
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
        self._families = {key: value for key, value in self._families.items() if key != family_id}

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
        if context is not None and context.presentation_media:
            payload["media_files"] = [dict(item) for item in context.presentation_media]
        if context is not None and context.presentation_secret_uses:
            payload["secrets_used"] = list(context.presentation_secret_uses)
        if "details" in payload:
            payload["details"].extend(_detail_blocks(context, result, payload["details"]))
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
            if (
                existing is not None and existing is not owned.get(tool.name)
            ) or reserved_model_name(tool.name):
                # A reserved name is how the Model already sees another Tool.
                raise DuplicateToolError(f"Tool already registered: {tool.name}")
        tools = dict(self._tools)
        for name, tool in owned.items():
            if tools.get(name) is tool:
                del tools[name]
                self._definition_profiles.pop(name, None)
        tools.update({tool.name: tool for tool in candidates})
        self._tools = tools
        self.revision += 1

    def unregister(self, name: str) -> None:
        """Remove a registered tool when it exists."""
        if name in self._tools:
            self._tools = {key: tool for key, tool in self._tools.items() if key != name}
            self.revision += 1
        self._definition_profiles.pop(name, None)

    def is_parallel_safe(self, name: str) -> bool:
        """Return whether a requested Tool Call may overlap a sibling call.

        Unknown Tools fail before invoking a handler, so they are safe to keep
        in the same parallel group instead of becoming accidental barriers.
        """
        tool = self._tools.get(name)
        return tool is None or tool.parallel_safe

    def requires_execution_slot(self, name: str) -> bool:
        """Keep child-Run coordinators outside the leaf execution semaphore."""
        tool = self._tools.get(name)
        return tool is None or tool.execution_slot_required

    def schema_fingerprint(self, name: str) -> str:
        """Return the deterministic canonical schema fingerprint for a Tool."""
        return self.get(name).contract.schema_fingerprint

    def validate_result(
        self, name: str, result: Any, *, contract: ToolContract | None = None
    ) -> JsonObject:
        """Validate against the executing Tool's contract when a call retained it."""
        if not isinstance(result, dict):
            raise InvalidToolResultError(f"Tool handler must return a JSON object: {name}")
        if not is_tool_result_envelope(result):
            raise InvalidToolResultError(
                f"Tool handler must return a valid result envelope: {name}"
            )
        if result["ok"]:
            try:
                (contract or self.get(name).contract).validate_success_data(result["data"])
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
        """Compile the exact model-facing input contracts for one Provider cycle.

        A definition whose Tool is no longer registered gets no contract: nothing
        can dispatch it, and only its registration would say whether its schema
        may stay open, as a Session's pinned definition of a removed Tool does.
        """
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
            if tool is None:
                continue
            contracts[name] = compile_tool_contract(
                name=name,
                input_schema=parameters,
                result_schema=tool.contract.result_schema,
                parallel_safe=tool.parallel_safe,
                require_closed_input=not tool.open_input_schema,
            )
        return contracts

    def authorize(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> Tool:
        """Resolve a call's Tool and enforce its existing dispatch authority.

        Chat checks this before Extension hooks, which can replace a call without
        running its handler. Dispatch checks again after those asynchronous hooks
        so a changed registration cannot inherit the earlier Tool's authority.
        """
        try:
            tool = self.get(context.tool_name)
        except ToolNotFoundError:
            raise ToolNotFoundError(
                self._unknown_tool_message(context, allowed_tools, arguments)
            ) from None
        context._retain_result_contract(tool.contract)
        context._retain_dispatch_allowlist(allowed_tools)
        if tool.session_scoped and context.tool_name not in context.session_tool_grants:
            raise SessionToolUnavailableError(f"Session tool unavailable: {context.tool_name}.")
        if (
            tool.requires_opt_in and context.tool_name not in (allowed_tools or ())
        ) or not self._is_allowed(context.tool_name, allowed_tools, internal=tool.internal):
            raise ToolNotAllowedError(f"Tool not allowed: {model_tool_name(context.tool_name)}.")
        return tool

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        """Execute a registered allowed tool through an async interface."""
        tool = self.authorize(context, arguments, allowed_tools)
        # Readiness safety net: dispatch is not list-filtered, so a prompt built
        # moments before the credential vanished could still request a now
        # not-ready tool. Re-evaluate live and return a clean failure envelope
        # instead of running the handler (no exception, so the model just gets a
        # normal failed result naming the cause).
        if not tool_is_ready(tool):
            return tool_failure(
                "tool_not_ready",
                tool.readiness_hint
                or f"tool '{model_tool_name(context.tool_name)}' is not available: "
                "its extension is not configured",
                retryable=tool.readiness_retryable,
            )
        normalized_arguments, ignored = _checked_arguments(
            tool, context.input_contract or tool.contract, arguments
        )
        note = ignored_fields_note(context.tool_name, ignored) if ignored else None
        if tool.secret_sinks and self._secrets is not None:
            try:
                normalized_arguments = await self._substitute_secrets(
                    context, tool, normalized_arguments
                )
            except SecretUseError as error:
                return tool_failure(
                    "secret_unavailable", f"{model_tool_name(tool.name)} was not run: {error}"
                )
        # From here on the handler runs: only a ToolContractError it raises
        # still means nothing happened; any other exception leaves the call's
        # effects unknown (``tool_failure_for_exception``).
        try:
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
        except ToolContractError as error:
            if note is None:
                raise
            raise ToolContractError(f"{error}\n{note}") from error
        result = self.validate_result(context.tool_name, result, contract=tool.contract)
        return result if note is None else with_call_note(result, note)

    async def _substitute_secrets(
        self, context: ToolContext, tool: Tool, arguments: JsonObject
    ) -> JsonObject:
        """The handler's copy of ``arguments`` with sink references replaced by values.

        Only declared sink arguments change; the persisted call, its events and
        its display keep the references. A refusal leaves nothing done.
        """
        secrets = self._secrets
        sinks = {
            name: cast(SinkClass, sink)
            for name, sink in tool.secret_sinks.items()
            if name in arguments and has_reference(arguments[name])
        }
        if secrets is None or not sinks:
            return arguments

        def substitute() -> tuple[JsonObject, list[SecretUse]]:
            changed = dict(arguments)
            uses: list[SecretUse] = []
            for name, sink in sinks.items():
                changed[name], found = secrets.substitute(
                    context.agent_id,
                    context.session_id,
                    arguments[name],
                    sink,
                    passed_only=context.subagent_session,
                )
                uses.extend(use for use in found if use not in uses)
            return changed, uses

        changed, uses = await run_tool_worker(substitute)
        if uses:
            context.record_secret_uses(describe_uses(uses))
        return changed

    def _unknown_tool_message(
        self, context: ToolContext, allowed_tools: Sequence[str] | None, arguments: Any
    ) -> str:
        """Name the Tools this caller can use instead of an unknown one.

        These are the Tools the Model was shown and the On-demand Tools it can
        load; an internal Tool counts only when the Model request listed it. When
        the arguments fit exactly one of them, the message names that Tool alone;
        the call never runs it, because the name alone did not say which Tool was
        meant.
        """
        available = [
            tool
            for tool in self.list_tools(allowed_tools, include_internal=True, ready_only=True)
            if not tool.deferred
            and (not tool.session_scoped or tool.name in context.session_tool_grants)
            and (not tool.requires_opt_in or tool.name in (allowed_tools or ()))
            and (
                context.offers(tool.name)
                or (tool.name in context.loadable_tools and context.can_call(tool.name))
            )
            and (not tool.internal or tool.name in (context.offered_tools or ()))
        ]
        if not available:
            return f"Unknown Tool: {context.tool_name}. No Tools are available in this Run."
        fitting = [tool for tool in available if _arguments_fit(tool.contract, arguments)]
        if len(fitting) == 1:
            name = model_tool_name(fitting[0].name)
            return (
                f"Unknown Tool: {context.tool_name}. These arguments fit {name}; "
                f"call {name} with them."
            )
        names = [model_tool_name(tool.name) for tool in available]
        damage = _call_damage(context.tool_name, arguments)
        if damage:
            closest = _closest_tool_name(context.tool_name, names)
            choice = (
                f"The closest available Tool is {closest}."
                if closest is not None
                else f"Available Tools: {', '.join(names)}."
            )
            return f"Unknown Tool: {context.tool_name}. Nothing was run: {damage} {choice}"
        return (
            f"Unknown Tool: {context.tool_name}. Call one of the available Tools instead: "
            f"{', '.join(names)}."
        )

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

        profiles = self._definition_profiles.setdefault(tool.name, {})
        cached = profiles.get(profile.key)
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
        profiles[profile.key] = resolved
        return resolved


__all__ = [
    "DEFAULT_TOOL_CONCURRENCY_LIMIT",
    "DuplicateToolError",
    "JsonObject",
    "SessionToolUnavailableError",
    "TOOL_ALLOWLIST_WILDCARD",
    "TOOL_EXECUTION_ERROR_CODE",
    "Tool",
    "ToolCall",
    "ToolCancelCallback",
    "ToolCancelCheckHook",
    "ToolCancelRegistrationHook",
    "ToolCancellationHook",
    "ToolContext",
    "ToolEmitHook",
    "ToolError",
    "ToolBatch",
    "ToolExecutionConfig",
    "ToolExecutor",
    "ToolHandler",
    "ToolLoadHook",
    "ToolNoteHook",
    "ToolNotAllowedError",
    "ToolNotFoundError",
    "ToolPromptBlockRegistry",
    "ToolReadinessPredicate",
    "ToolRegistry",
    "definition_change_announced",
    "READ_MEDIA_ARTIFACT_KIND",
    "is_tool_result_envelope",
    "read_media_artifact",
    "tool_failure",
    "tool_failure_for_exception",
    "tool_is_ready",
    "tool_summary",
    "tool_success",
    "ToolCallCancelCheck",
    "ToolCallCancelRegistrar",
    "ToolCallResultPersistedRegistrar",
    "ToolDeliveryReceipt",
    "ToolDeliveryReceiptHook",
    "ToolResultPayloadHook",
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
    "ToolDisplayDetailBuilder",
    "ToolDisplayFactBuilder",
    "ToolDisplayField",
    "ToolDisplayPart",
    "ToolDisplayPartBuilder",
    "ToolSummaryBuilder",
    "display_notice",
    "display_results",
    "display_text",
    "result_count_fact_builder",
]
