"""Tool lifecycle dispatch and deterministic skill activation."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.agents import default_workspace_dir
from core.chat.events import _emit_tool_context_event, _timing_payload
from core.chat.messages import ChatMessage, JsonObject, ToolCall, ToolCallRejection
from core.extensions import ExtensionRegistry, HookContext
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT, Run
from core.sessions import ChatSession
from core.skills.requirements import SkillRequirements, environment_requirement_names
from core.skills.skill_validator import SKILL_NAME_CHARSET_FRAGMENT
from core.tools import (
    READ_MEDIA_ARTIFACT_KIND,
    ChangeTracker,
    InvalidToolResultError,
    SessionToolUnavailableError,
    ToolContext,
    ToolContract,
    ToolExecutionConfig,
    ToolExecutor,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolRegistry,
    ToolResultPersistedCallback,
    is_tool_result_envelope,
    tool_failure,
)
from core.tools import ToolCall as ScheduledToolCall
from core.tools.availability import (
    agent_tool_settings,
    resolve_tool_access,
)
from core.tools.skill import load_skill_content
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.continuation import ContinuationTracker
    from core.skills.skills import SkillRegistry

_LOGGER = get_logger("chat")

# Built from the same fragment skill authoring enforces as a hard name requirement
# (core.skills.skill_validator.SKILL_NAME_TRIGGER_PATTERN), so a newly authored skill
# is always matched here and the two can never drift to different length/charset rules.
SKILL_SLASH_TRIGGER_PATTERN = re.compile(rf"^/({SKILL_NAME_CHARSET_FRAGMENT})(?=\s|$)")
SKILL_INLINE_TRIGGER_PATTERN = re.compile(rf"\$({SKILL_NAME_CHARSET_FRAGMENT})")


@dataclass(frozen=True)
class ToolDispatchContext:
    """Run-local Tool execution inputs resolved by the Chat loop."""

    registry: ToolRegistry
    extension_registry: ExtensionRegistry | None
    agent: Any
    session: ChatSession
    run: Run
    nesting_depth: int
    vbot_root: Path
    data_root: Path
    project_cwd: Path | None = None
    project_id: str | None = None
    skill_project_id: str | None = None
    skill_registry: SkillRegistry | None = None
    tool_restriction: Sequence[str] | None = None
    tool_denial_resolver: Callable[[str], str | None] | None = None
    base_allowed_tools: Sequence[str] | None = None
    session_tool_grants: Sequence[str] = ()
    tool_contracts: Mapping[str, ToolContract] = field(default_factory=dict)
    change_tracker: ChangeTracker | None = None
    _result_persisted_callbacks: dict[str, list[ToolResultPersistedCallback]] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )

    def register_result_persisted(
        self,
        tool_call_id: str,
        callback: ToolResultPersistedCallback,
    ) -> None:
        """Register a side effect for the exact Tool Result persistence boundary."""
        self._result_persisted_callbacks.setdefault(tool_call_id, []).append(callback)

    def notify_result_persisted(self, tool_call_id: str) -> None:
        """Run and retire callbacks after one Tool Result was durably appended."""
        for callback in self._result_persisted_callbacks.pop(tool_call_id, []):
            try:
                callback()
            except Exception:
                _LOGGER.warning(
                    "Post-persistence callback failed for tool call %s",
                    tool_call_id,
                    exc_info=True,
                )


class _EmittingToolRegistry(ToolRegistry):
    """Adapter that emits public lifecycle events around registry dispatch."""

    def __init__(
        self,
        registry: Any,
        run: Run,
        extension_registry: ExtensionRegistry | None = None,
        note_hook: Callable[[str], None] | None = None,
        denial_resolver: Callable[[str], str | None] | None = None,
        rejections: Mapping[int, ToolCallRejection] | None = None,
    ) -> None:
        self._registry = registry
        self._run = run
        self._extension_registry = extension_registry
        self._note_hook = note_hook
        self._denial_resolver = denial_resolver
        self._rejections = dict(rejections or {})
        self._tool_timings: dict[str, JsonObject] = {}
        self._tool_displays: dict[str, JsonObject] = {}
        self._extension_hook_lock = asyncio.Lock()

    def _hook_context(self) -> HookContext:
        return HookContext(
            session_id=self._run.session_id,
            agent_id=self._run.agent_id,
            run_id=self._run.id,
            add_note=self._note_hook or (lambda _text: None),
        )

    def is_parallel_safe(self, name: str) -> bool:
        """Delegate the wrapped Tool's execution policy."""
        resolver = getattr(self._registry, "is_parallel_safe", None)
        return bool(callable(resolver) and resolver(name))

    def schema_fingerprint(self, name: str) -> str:
        """Return the wrapped registry's Tool schema fingerprint."""
        resolver = getattr(self._registry, "schema_fingerprint", None)
        if not callable(resolver):
            return ""
        return str(resolver(name))

    def validate_result(self, name: str, result: Any) -> JsonObject:
        """Validate through the wrapped canonical registry."""
        validator = getattr(self._registry, "validate_result", None)
        if not callable(validator):
            return _validated_tool_result(name, result)
        return cast(JsonObject, validator(name, result))

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        self._run.raise_if_cancelled()
        self._run.tool_call_count += 1
        self._run.tool_call_names.add(context.tool_name)
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        try:
            rejection = self._rejections.get(context.tool_call_index)
            if rejection is not None:
                rejected_result = tool_failure(
                    rejection.code,
                    rejection.message,
                    retryable=False,
                )
                timing = _timing_payload(started_at, started_perf)
                self._tool_timings[context.tool_call_id] = timing
                fingerprint = _tool_context_schema_fingerprint(self, context)
                display = _empty_tool_display_payload()
                self._tool_displays[context.tool_call_id] = display
                self._run.emit(
                    TOOL_CALL_STARTED_EVENT,
                    {
                        "tool_call": {
                            "id": context.tool_call_id,
                            "index": context.tool_call_index,
                            "name": context.tool_name,
                            "arguments": deepcopy(arguments),
                        },
                        "display": display,
                        "schema_fingerprint": fingerprint,
                    },
                )
                self._run.emit(
                    TOOL_CALL_RESULT_EVENT,
                    {
                        "tool_call": {
                            "id": context.tool_call_id,
                            "index": context.tool_call_index,
                            "name": context.tool_name,
                        },
                        "result": rejected_result,
                        "display": display,
                        "timing": timing,
                        "schema_fingerprint": fingerprint,
                        "error_code": rejection.code,
                    },
                )
                return rejected_result
            denial_message = (
                self._denial_resolver(context.tool_name)
                if self._denial_resolver is not None
                else None
            )
            if denial_message is not None:
                denied_result = tool_failure("tool_not_allowed", denial_message)
                timing = _timing_payload(started_at, started_perf)
                self._tool_timings[context.tool_call_id] = timing
                display = _tool_display_payload(
                    self._registry,
                    context.tool_name,
                    arguments,
                    context=context,
                    result=denied_result,
                )
                self._tool_displays[context.tool_call_id] = display
                self._run.emit(
                    TOOL_CALL_STARTED_EVENT,
                    {
                        "tool_call": {
                            "id": context.tool_call_id,
                            "index": context.tool_call_index,
                            "name": context.tool_name,
                            "arguments": deepcopy(arguments),
                        },
                        "display": display,
                        "schema_fingerprint": _tool_context_schema_fingerprint(self, context),
                    },
                )
                self._run.emit(
                    TOOL_CALL_RESULT_EVENT,
                    {
                        "tool_call": {
                            "id": context.tool_call_id,
                            "index": context.tool_call_index,
                            "name": context.tool_name,
                        },
                        "result": denied_result,
                        "display": display,
                        "timing": timing,
                        "schema_fingerprint": _tool_context_schema_fingerprint(self, context),
                        "error_code": "tool_not_allowed",
                    },
                )
                return denied_result
            # Decision pipeline runs before the started event so the timeline
            # shows the effective (possibly modified) arguments. A deny or
            # replace short-circuits execution; a modify rewrites the input the
            # tool runs with and that tool_result hooks observe.
            effective_arguments = arguments
            result: JsonObject | None = None
            if self._extension_registry is not None:
                async with self._extension_hook_lock:
                    decision = await self._extension_registry.dispatch_tool_call(
                        self._hook_context(),
                        tool_name=context.tool_name,
                        tool_call_id=context.tool_call_id,
                        input=arguments,
                        validator=lambda extension_name, candidate: (
                            _validated_extension_tool_hook_result(
                                registry=self,
                                tool_name=context.tool_name,
                                extension_name=extension_name,
                                hook_name="tool_call",
                                result=candidate,
                            )
                        ),
                    )
                effective_arguments = decision.effective_input
                if decision.deny_reason is not None:
                    _LOGGER.warning(
                        "Extension %r denied %s tool call: %s",
                        decision.deny_extension,
                        context.tool_name,
                        decision.deny_reason,
                    )
                    result = tool_failure(
                        "tool_call_denied",
                        f"Tool call denied by extension '{decision.deny_extension}': "
                        f"{decision.deny_reason}",
                    )
                elif decision.replacement is not None:
                    result = decision.replacement

            fingerprint = _tool_context_schema_fingerprint(self, context)
            started_display = _tool_display_payload(
                self._registry,
                context.tool_name,
                effective_arguments,
                context=context,
            )
            self._run.emit(
                TOOL_CALL_STARTED_EVENT,
                {
                    "tool_call": {
                        "id": context.tool_call_id,
                        "index": context.tool_call_index,
                        "name": context.tool_name,
                        "arguments": deepcopy(effective_arguments),
                    },
                    "display": started_display,
                    "schema_fingerprint": fingerprint,
                },
            )

            if result is None:
                result = await self._dispatch_with_failure_envelope(
                    context, effective_arguments, allowed_tools
                )

            if self._extension_registry is not None:
                async with self._extension_hook_lock:
                    result = await self._extension_registry.dispatch_tool_result(
                        self._hook_context(),
                        tool_name=context.tool_name,
                        tool_call_id=context.tool_call_id,
                        input=effective_arguments,
                        result=result,
                        validator=lambda extension_name, candidate: (
                            _validated_extension_tool_hook_result(
                                registry=self,
                                tool_name=context.tool_name,
                                extension_name=extension_name,
                                hook_name="tool_result",
                                result=candidate,
                            )
                        ),
                    )

            timing = _timing_payload(started_at, started_perf)
            self._tool_timings[context.tool_call_id] = timing
            completed_display = _tool_display_payload(
                self._registry,
                context.tool_name,
                effective_arguments,
                context=context,
                result=result,
            )
            self._tool_displays[context.tool_call_id] = completed_display
            error = result.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            _LOGGER.debug(
                "Tool %s completed (run=%s call=%s schema=%s duration_ms=%s ok=%s error=%s)",
                context.tool_name,
                self._run.id,
                context.tool_call_id,
                fingerprint[:12],
                timing["duration_ms"],
                result.get("ok"),
                error_code,
            )
            # Return a completed result even when cancellation was requested so
            # a cooperatively terminating batch can still persist it. A forceful
            # Run cancellation may stop the batch before Chat receives every
            # sibling; the next Provider request repairs those missing Results
            # from durable Session evidence instead.
            self._run.emit(
                TOOL_CALL_RESULT_EVENT,
                {
                    "tool_call": {
                        "id": context.tool_call_id,
                        "index": context.tool_call_index,
                        "name": context.tool_name,
                    },
                    "result": result,
                    "display": completed_display,
                    "timing": timing,
                    "schema_fingerprint": fingerprint,
                    "error_code": error_code,
                },
            )
            return result
        finally:
            # Per-call cancel registry entries are scoped to a single dispatch.
            # Clearing on every exit path keeps the registry bounded and lets a
            # later call that re-uses the same id start from a clean slate.
            self._run.clear_tool_cancel(context.tool_call_id)

    def timing_for_call(self, tool_call_id: str) -> JsonObject | None:
        """Return measured timing for a completed tool call."""
        timing = self._tool_timings.get(tool_call_id)
        return dict(timing) if timing is not None else None

    def display_for_completed_call(self, tool_call_id: str) -> JsonObject | None:
        """Return the final presentation snapshot for a completed Tool call."""
        display = self._tool_displays.get(tool_call_id)
        return deepcopy(display) if display is not None else None

    async def _dispatch_with_failure_envelope(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            return await self._dispatch_with_current_registry_signature(
                context,
                arguments,
                allowed_tools,
            )
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
            # The branches above are expected tool/input failures (the normal
            # tool contract); this catch-all is an unexpected crash inside the
            # handler. The crash is converted to a result and the run usually
            # continues, so Run.mark_failed never sees it — log it here.
            _LOGGER.error("Tool %s crashed unexpectedly", context.tool_name, exc_info=error)
            return tool_failure("tool_execution_error", str(error))

    async def _dispatch_with_current_registry_signature(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        result = await self._registry.dispatch(context, arguments, allowed_tools)
        return self.validate_result(context.tool_name, result)


def _safe_schema_fingerprint(registry: Any, tool_name: str) -> str:
    resolver = getattr(registry, "schema_fingerprint", None)
    if not callable(resolver):
        return ""
    try:
        return str(resolver(tool_name))
    except (KeyError, ToolNotFoundError, ValueError):
        return ""


def _tool_context_schema_fingerprint(registry: Any, context: ToolContext) -> str:
    contract = context.input_contract
    if contract is not None:
        return contract.schema_fingerprint
    return _safe_schema_fingerprint(registry, context.tool_name)


async def _dispatch_tool_calls(
    context: ToolDispatchContext,
    tool_calls: list[ToolCall],
    *,
    continuation_tracker: ContinuationTracker | None = None,
) -> tuple[list[ChatMessage], list[JsonObject]]:
    run = context.run
    session = context.session
    agent = context.agent
    run.raise_if_cancelled()
    if continuation_tracker is not None:
        await continuation_tracker.record_tool_starts(tool_calls)
    emitting_registry = _EmittingToolRegistry(
        context.registry,
        run,
        context.extension_registry,
        note_hook=session.add_note,
        denial_resolver=context.tool_denial_resolver,
        rejections={
            index: tool_call.rejection
            for index, tool_call in enumerate(tool_calls)
            if tool_call.rejection is not None
        },
    )
    executor = ToolExecutor(emitting_registry)
    workspace = _agent_workspace(agent, context.data_root)
    results = await executor.execute_many(
        [
            ScheduledToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=tool_call.arguments,
            )
            for tool_call in tool_calls
        ],
        ToolExecutionConfig(
            agent_id=run.agent_id,
            session_id=run.session_id,
            run_id=run.id,
            workspace=workspace,
            vbot_root=context.vbot_root,
            data_root=context.data_root,
            iteration_number=run.iteration_count,
            cwd=_resolve_tool_cwd(context.project_cwd, workspace),
            # The owning run's project rides onto every ToolContext so the
            # subagent tool can inherit it; None keeps the identity path.
            project_id=context.project_id,
            # The run's effective skill project (rooted-aware) so the skill tool
            # resolves the same pool the run's catalog advertises.
            skill_project_id=context.skill_project_id,
            allowed_tools=_dispatch_allowed_tools(
                agent,
                context.registry,
                context.tool_restriction,
                base_allowed_tools=context.base_allowed_tools,
                session_tool_grants=context.session_tool_grants,
            ),
            session_tool_grants=context.session_tool_grants,
            allowed_skills=getattr(agent, "allowed_skills", ["*"]),
            skill_env_keys=_active_skill_env_keys(session, context.skill_registry),
            tool_settings=agent_tool_settings(getattr(agent, "tools", {})),
            emit_hook=lambda event_type, payload: _emit_tool_context_event(
                run,
                event_type,
                payload,
            ),
            cancellation_hook=lambda: run.cancel_requested,
            tool_call_cancel_registrar=lambda tool_call_id, callback: run.register_tool_cancel(
                tool_call_id, callback
            ),
            tool_call_cancel_check=lambda tool_call_id: run.tool_call_cancelled(tool_call_id),
            note_hook=session.add_note,
            skill_activation_hook=session.register_skill_activation,
            tool_call_result_persisted_registrar=context.register_result_persisted,
            nesting_depth=context.nesting_depth,
            input_contracts=context.tool_contracts,
            change_tracker=context.change_tracker,
        ),
    )
    tool_messages: list[ChatMessage] = []
    media_outputs: list[JsonObject] = []
    for tool_call, result in zip(tool_calls, results, strict=True):
        tool_messages.append(
            ChatMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                timing=emitting_registry.timing_for_call(tool_call.id),
                tool_display=emitting_registry.display_for_completed_call(tool_call.id),
            )
        )
        media_outputs.extend(_read_media_outputs(result, tool_call_id=tool_call.id))
    return tool_messages, media_outputs


def _active_skill_env_keys(
    session: ChatSession,
    skill_registry: SkillRegistry | None,
) -> tuple[str, ...]:
    """Return Env grants declared by Skills active in the current Session state."""
    if skill_registry is None:
        return ()
    names: list[str] = []
    for skill_name in session.activated_skill_contents():
        try:
            skill = skill_registry.get(skill_name)
        except KeyError:
            continue
        names.extend(environment_requirement_names(skill.requirements))
    return tuple(dict.fromkeys(names))


def _fail_tool_calls_without_dispatch(
    context: ToolDispatchContext,
    tool_calls: list[ToolCall],
    *,
    code: str,
    message: str,
    retryable: bool | None = None,
) -> list[ChatMessage]:
    """Produce normal correlated failure Results without invoking any handler.

    Chat uses this when the Provider's terminal outcome proves the emitted Tool
    Calls are unsafe to execute. Lifecycle events and canonical envelopes stay
    identical in shape to ordinary dispatch; extensions and handlers are
    deliberately bypassed because no untrusted call may cross the execution
    boundary.
    """

    tool_messages: list[ChatMessage] = []
    for index, tool_call in enumerate(tool_calls):
        context.run.tool_call_count += 1
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        contract = context.tool_contracts.get(tool_call.name)
        fingerprint = (
            contract.schema_fingerprint
            if contract is not None
            else _safe_schema_fingerprint(context.registry, tool_call.name)
        )
        display = _tool_display_payload(
            context.registry,
            tool_call.name,
            tool_call.arguments,
        )
        context.run.emit(
            TOOL_CALL_STARTED_EVENT,
            {
                "tool_call": {
                    "id": tool_call.id,
                    "index": index,
                    "name": tool_call.name,
                    "arguments": deepcopy(tool_call.arguments),
                },
                "display": display,
                "schema_fingerprint": fingerprint,
            },
        )
        result = tool_failure(code, message, retryable=retryable)
        timing = _timing_payload(started_at, started_perf)
        context.run.emit(
            TOOL_CALL_RESULT_EVENT,
            {
                "tool_call": {
                    "id": tool_call.id,
                    "index": index,
                    "name": tool_call.name,
                },
                "result": result,
                "display": display,
                "timing": timing,
                "schema_fingerprint": fingerprint,
                "error_code": code,
            },
        )
        tool_messages.append(
            ChatMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                timing=timing,
                tool_display=display,
            )
        )
    return tool_messages


def _read_media_outputs(
    result: JsonObject,
    *,
    tool_call_id: str,
) -> list[JsonObject]:
    """Extract request-local rich-content descriptors from a Tool Result.

    ``read`` and ``web_fetch`` image results carry compact attachment
    references. Chat resolves them into media content on the correlated Tool
    Result for the active Run. Other artifact kinds yield nothing.
    """
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return []

    outputs: list[JsonObject] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") != READ_MEDIA_ARTIFACT_KIND:
            continue
        attachment_id = artifact.get("attachment_id")
        filename = artifact.get("filename")
        media_type = artifact.get("media_type")
        if (
            isinstance(attachment_id, str)
            and isinstance(filename, str)
            and isinstance(media_type, str)
        ):
            outputs.append(
                {
                    "tool_call_id": tool_call_id,
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "media_type": media_type,
                }
            )
    return outputs


def _activate_triggered_skills(
    agent: Any,
    session: ChatSession,
    content: str,
    skill_registry: SkillRegistry,
) -> None:
    if not _triggered_skill_names(content):
        return

    allowed_skills = getattr(agent, "allowed_skills", None)
    if allowed_skills is None:
        allowed_skills = ["*"]
    allowed_by_name = _allowed_loadable_skills(skill_registry, allowed_skills)
    for skill_name in _triggered_skill_names(content):
        skill = allowed_by_name.get(skill_name)
        if skill is None:
            _LOGGER.warning(
                "Ignored skill trigger '%s' for agent=%s session=%s "
                "because it is not allowed or loadable",
                skill_name,
                agent.id,
                session.id,
            )
            session.add_note(
                f"Skill trigger '{skill_name}' did not match an allowed loadable skill."
            )
            continue
        unavailable_reason = _unavailable_skill_reason(
            skill_registry,
            skill_name,
            allowed_skills,
        )
        if unavailable_reason is not None:
            _LOGGER.warning(
                "Ignored skill trigger '%s' for agent=%s session=%s because it is unavailable: %s",
                skill_name,
                agent.id,
                session.id,
                unavailable_reason,
            )
            session.add_note(
                f"Skill trigger '{skill_name}' matched a skill, but it is unavailable: "
                f"{unavailable_reason}"
            )
            continue
        try:
            data = load_skill_content(
                skill.name,
                skill.path,
                env_keys=environment_requirement_names(
                    getattr(skill, "requirements", SkillRequirements())
                ),
            )
        except OSError as error:
            _LOGGER.warning(
                "Failed to load triggered skill '%s' for agent=%s session=%s: %s",
                skill_name,
                agent.id,
                session.id,
                error,
            )
            session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
            continue
        except ValueError as error:
            _LOGGER.warning(
                "Failed to parse triggered skill '%s' for agent=%s session=%s: %s",
                skill_name,
                agent.id,
                session.id,
                error,
            )
            session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
            continue
        if session.activate_skill_context(skill.name, data):
            _LOGGER.info(
                "Activated triggered skill '%s' for agent=%s session=%s",
                skill.name,
                agent.id,
                session.id,
            )


def _tool_display_payload(
    registry: Any,
    tool_name: str,
    arguments: Any,
    *,
    context: ToolContext | None = None,
    result: JsonObject | None = None,
) -> JsonObject:
    display_for_call = getattr(registry, "display_for_call", None)
    if not callable(display_for_call):
        return _empty_tool_display_payload()

    try:
        payload = display_for_call(tool_name, arguments, context=context, result=result)
    except (ToolNotFoundError, TypeError, ValueError):
        return _empty_tool_display_payload()

    if not isinstance(payload, dict):
        return _empty_tool_display_payload()
    return payload


def _empty_tool_display_payload() -> JsonObject:
    return {
        "version": 1,
        "summary": "",
        "hidden_argument_keys": [],
        "primary": [],
        "facts": [],
    }


def _validated_tool_result(tool_name: str, result: Any) -> JsonObject:
    if not isinstance(result, dict):
        raise InvalidToolResultError(f"Tool handler must return a JSON object: {tool_name}")
    if not is_tool_result_envelope(result):
        raise InvalidToolResultError(
            f"Tool handler must return a valid result envelope: {tool_name}"
        )
    return result


def _validated_extension_tool_hook_result(
    *,
    registry: Any,
    tool_name: str,
    extension_name: str,
    hook_name: str,
    result: Any,
) -> JsonObject | None:
    try:
        validator = getattr(registry, "validate_result", None)
        validated = (
            validator(tool_name, result)
            if callable(validator)
            else _validated_tool_result(tool_name, result)
        )
        json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
        return validated
    except (TypeError, ValueError) as error:
        _LOGGER.warning(
            "Extension %r %s handler returned invalid tool result for %r: %s",
            extension_name,
            hook_name,
            tool_name,
            error,
        )
        return None


def _allowed_loadable_skills(
    skill_registry: SkillRegistry,
    allowed_skills: list[str],
) -> dict[str, Any]:
    return {
        skill.name: skill
        for skill in skill_registry.list_all()
        if skill_registry.is_allowed(skill.name, allowed_skills)
    }


def _unavailable_skill_reason(
    skill_registry: SkillRegistry,
    skill_name: str,
    allowed_skills: list[str],
) -> str | None:
    availability = skill_registry.availability_for(skill_name, allowed_skills)
    if availability.state == "available":
        return None
    missing = list(availability.missing)
    return "; ".join(missing) if missing else str(availability.state)


def _runtime_allowed_tools(
    agent: Any,
    tool_registry: ToolRegistry,
    *,
    session_tool_grants: Sequence[str] = (),
) -> Sequence[str]:
    return resolve_tool_access(
        agent.tool_access,
        tool_registry.list_tools(),
        getattr(agent, "memory_prompt_mode", "agent_user"),
        workspace=getattr(agent, "workspace", "") or "",
        session_tool_grants=session_tool_grants,
    ).allowed_tools


def _dispatch_allowed_tools(
    agent: Any,
    tool_registry: ToolRegistry,
    tool_restriction: Sequence[str] | None,
    *,
    base_allowed_tools: Sequence[str] | None = None,
    session_tool_grants: Sequence[str] = (),
) -> Sequence[str] | None:
    """Return the dispatch allowlist, narrowed by an optional per-run restriction.

    Without a restriction this is exactly ``_runtime_allowed_tools`` (today's
    behavior, byte-identical). With one, the run may dispatch only tools present
    in **both** the effective allowlist and the restriction — an intersection, so
    a tool named in the restriction but not effectively allowed stays denied. A
    ``None``/``["*"]`` effective allowlist is expanded to every registered normal
    tool name (exactly how ``ToolRegistry.list_tools`` reads it) before the
    intersection, so the restriction narrows a wildcard agent too.

    This is enforcement-only: it feeds ``ToolExecutionConfig.allowed_tools`` (the
    dispatch gate) and never the provider tool definitions or the system prompt,
    so a restricted run keeps a byte-identical prompt prefix (the prompt-cache
    invariant). A restricted-out call fails through the existing
    ``ToolNotAllowedError`` → ``tool_not_allowed`` path — no new denial code.
    """
    effective = (
        list(base_allowed_tools)
        if base_allowed_tools is not None
        else _runtime_allowed_tools(
            agent,
            tool_registry,
            session_tool_grants=session_tool_grants,
        )
    )
    if tool_restriction is None:
        return effective
    restriction = set(tool_restriction)
    return [tool.name for tool in tool_registry.list_tools(effective) if tool.name in restriction]


def _agent_workspace(agent: Any, data_root: Path) -> Path:
    workspace = getattr(agent, "workspace", None)
    if workspace is not None:
        return Path(workspace)

    return default_workspace_dir(data_root, agent.id)


def _resolve_tool_cwd(project_cwd: Path | None, workspace: Path) -> Path:
    """Choose the tool working directory: project cwd when set, else workspace.

    A project session supplies the repo ``project_cwd`` so file/shell tools
    resolve relative paths against the repo. Without one (identity sessions and
    every current caller, since the chat loop does not yet thread a project cwd),
    the working directory stays the agent workspace — today's behavior. The chat
    loop will pass the real project cwd later via ``_dispatch_tool_calls``.
    """
    return project_cwd if project_cwd is not None else workspace


def _triggered_skill_names(content: str) -> list[str]:
    names: list[str] = []
    slash_match = SKILL_SLASH_TRIGGER_PATTERN.search(content)
    if slash_match:
        names.append(slash_match.group(1))

    for inline_match in SKILL_INLINE_TRIGGER_PATTERN.finditer(content):
        name = inline_match.group(1)
        if name not in names:
            names.append(name)
    return names
