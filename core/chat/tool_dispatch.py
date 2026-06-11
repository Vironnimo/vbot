"""Tool lifecycle dispatch and deterministic skill activation."""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat.events import _emit_tool_context_event, _timing_payload
from core.chat.messages import ChatMessage, JsonObject, ToolCall
from core.extensions import ExtensionRegistry, HookContext
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT, Run
from core.sessions import ChatSession
from core.tools import (
    InvalidToolResultError,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolRegistry,
    is_tool_result_envelope,
    tool_failure,
)
from core.tools import ToolCall as ScheduledToolCall
from core.tools.availability import effective_agent_allowed_tools
from core.tools.skill import load_skill_content
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.skills.skills import SkillRegistry

_LOGGER = get_logger("chat")

SKILL_SLASH_TRIGGER_PATTERN = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=\s|$)")
SKILL_INLINE_TRIGGER_PATTERN = re.compile(r"\$([A-Za-z0-9][A-Za-z0-9_-]{0,63})")


class _EmittingToolRegistry(ToolRegistry):
    """Adapter that emits public lifecycle events around registry dispatch."""

    def __init__(
        self,
        registry: Any,
        run: Run,
        extension_registry: ExtensionRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._run = run
        self._extension_registry = extension_registry
        self._tool_timings: dict[str, JsonObject] = {}

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        self._run.raise_if_cancelled()
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        original_arguments = deepcopy(arguments)
        self._run.emit(
            TOOL_CALL_STARTED_EVENT,
            {
                "tool_call": {
                    "id": context.tool_call_id,
                    "index": context.tool_call_index,
                    "name": context.tool_name,
                    "arguments": original_arguments,
                },
                "display": _tool_display_payload(
                    self._registry,
                    context.tool_name,
                    original_arguments,
                ),
            },
        )
        try:
            result: JsonObject | None = None
            if self._extension_registry is not None:
                ctx = HookContext(session_id=self._run.session_id, agent_id=self._run.agent_id)
                for extension_name, handler in self._extension_registry._handlers.get(
                    "tool_call",
                    [],
                ):
                    try:
                        hook_result = handler(
                            ctx,
                            tool_name=context.tool_name,
                            tool_call_id=context.tool_call_id,
                            input=arguments,
                        )
                        if inspect.isawaitable(hook_result):
                            hook_result = await hook_result
                    except Exception as exc:
                        _LOGGER.warning(
                            "Extension %r tool_call handler raised: %s",
                            extension_name,
                            exc,
                        )
                        continue
                    if isinstance(hook_result, dict):
                        validated_override = _validated_extension_tool_hook_result(
                            tool_name=context.tool_name,
                            extension_name=extension_name,
                            hook_name="tool_call",
                            result=hook_result,
                        )
                        if validated_override is None:
                            continue
                        result = validated_override
                        break

            if result is None:
                result = await self._dispatch_with_failure_envelope(
                    context, arguments, allowed_tools
                )

            if self._extension_registry is not None:
                ctx = HookContext(session_id=self._run.session_id, agent_id=self._run.agent_id)
                for extension_name, handler in self._extension_registry._handlers.get(
                    "tool_result",
                    [],
                ):
                    try:
                        hook_result = handler(
                            ctx,
                            tool_name=context.tool_name,
                            tool_call_id=context.tool_call_id,
                            input=arguments,
                            result=result,
                        )
                        if inspect.isawaitable(hook_result):
                            hook_result = await hook_result
                    except Exception as exc:
                        _LOGGER.warning(
                            "Extension %r tool_result handler raised: %s",
                            extension_name,
                            exc,
                        )
                        continue
                    if isinstance(hook_result, dict):
                        patched_result = dict(result)
                        patched_result.update(hook_result)
                        validated_patch = _validated_extension_tool_hook_result(
                            tool_name=context.tool_name,
                            extension_name=extension_name,
                            hook_name="tool_result",
                            result=patched_result,
                        )
                        if validated_patch is not None:
                            result = validated_patch

            timing = _timing_payload(started_at, started_perf)
            self._tool_timings[context.tool_call_id] = timing
            # Do not discard a completed tool result when the run is
            # cancelled: the executor already returned the result, the
            # per-tool cancel callback was the proper signal to interrupt
            # the handler, and the chat loop's persist loop will record
            # the result before honoring the run cancel.
            self._run.emit(
                TOOL_CALL_RESULT_EVENT,
                {
                    "tool_call": {
                        "id": context.tool_call_id,
                        "index": context.tool_call_index,
                        "name": context.tool_name,
                    },
                    "result": result,
                    "timing": timing,
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
        except ToolNotAllowedError as error:
            return tool_failure("tool_not_allowed", str(error))
        except InvalidToolResultError as error:
            return tool_failure("invalid_tool_result", str(error))
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        except Exception as error:
            return tool_failure("tool_execution_error", str(error))

    async def _dispatch_with_current_registry_signature(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        result = await self._registry.dispatch(context, arguments, allowed_tools)
        return _validated_tool_result(context.tool_name, result)


async def _dispatch_tool_calls(
    runtime: RuntimeServices,
    agent: Any,
    tool_calls: list[ToolCall],
    session: ChatSession,
    run: Run,
    *,
    nesting_depth: int,
) -> list[ChatMessage]:
    run.raise_if_cancelled()
    emitting_registry = _EmittingToolRegistry(
        runtime.tools,
        run,
        runtime.extensions,
    )
    executor = ToolExecutor(emitting_registry)
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
            workspace=_agent_workspace(agent, Path(runtime.storage.data_dir)),
            app_root=Path(runtime.system_prompts.app_dir),
            data_root=Path(runtime.storage.data_dir),
            allowed_tools=_runtime_allowed_tools(agent, runtime.tools),
            allowed_skills=getattr(agent, "allowed_skills", ["*"]),
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
            skill_activation_hook=session.activate_skill_context,
            nesting_depth=nesting_depth,
        ),
    )
    return [
        ChatMessage.tool(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            timing=emitting_registry.timing_for_call(tool_call.id),
        )
        for tool_call, result in zip(tool_calls, results, strict=True)
    ]


def _activate_triggered_skills(
    runtime: RuntimeServices, agent: Any, session: ChatSession, content: str
) -> None:
    if not _triggered_skill_names(content):
        return

    skill_registry = runtime.skills
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
            data = load_skill_content(skill.name, skill.path)
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
        session.activate_skill_context(skill.name, data)
        _LOGGER.info(
            "Activated triggered skill '%s' for agent=%s session=%s",
            skill.name,
            agent.id,
            session.id,
        )


def _tool_display_payload(registry: Any, tool_name: str, arguments: Any) -> JsonObject:
    display_for_call = getattr(registry, "display_for_call", None)
    if not callable(display_for_call):
        return _empty_tool_display_payload()

    try:
        payload = display_for_call(tool_name, arguments)
    except ToolNotFoundError:
        return _empty_tool_display_payload()

    if not isinstance(payload, dict):
        return _empty_tool_display_payload()
    return payload


def _empty_tool_display_payload() -> JsonObject:
    return {"summary": "", "hidden_argument_keys": []}


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
    tool_name: str,
    extension_name: str,
    hook_name: str,
    result: Any,
) -> JsonObject | None:
    try:
        validated = _validated_tool_result(tool_name, result)
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


def _runtime_allowed_tools(agent: Any, tool_registry: ToolRegistry) -> Sequence[str] | None:
    return effective_agent_allowed_tools(
        getattr(agent, "allowed_tools", ["*"]),
        getattr(agent, "memory_prompt_mode", "agent_user"),
        registered_tool_names=[tool.name for tool in tool_registry.list_tools()],
    )


def _agent_workspace(agent: Any, data_root: Path) -> Path:
    workspace = getattr(agent, "workspace", None)
    if workspace is not None:
        return Path(workspace)

    return data_root / f"workspace-{agent.id}"


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


def _sync_skill_context_messages(messages: list[JsonObject], session: ChatSession) -> None:
    existing = {
        message.get("content")
        for message in messages
        if message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and str(message.get("content", "")).startswith("<skill_content ")
    }
    for skill_message in session.skill_context_messages():
        if skill_message["content"] not in existing:
            messages.insert(1, skill_message)
            existing.add(skill_message["content"])
