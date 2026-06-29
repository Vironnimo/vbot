"""Tool lifecycle dispatch and deterministic skill activation."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
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
    EDIT_TOOL_NAME,
    GLOB_TOOL_NAME,
    GREP_TOOL_NAME,
    READ_TOOL_NAME,
    WRITE_TOOL_NAME,
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

# File tools that take a ``path`` argument the agent can point at any absolute
# location. A visiting identity agent reaches into a project repo by absolute
# path (its cwd stays its own home), so these are the calls that can reveal a
# visit. ``bash``/``process`` are intentionally excluded: their arguments are
# free command lines with no single resolvable path.
_VISITING_PATH_TOOLS = frozenset(
    {READ_TOOL_NAME, WRITE_TOOL_NAME, EDIT_TOOL_NAME, GREP_TOOL_NAME, GLOB_TOOL_NAME}
)


def _visiting_candidate_paths(tool_calls: Sequence[ToolCall]) -> list[Path]:
    """Return the absolute paths a tool-call batch points the file tools at.

    Only the path-bearing file tools are considered, and only absolute ``path``
    arguments: a visiting agent reaches into a project by absolute path, while a
    relative path resolves against the agent's own home (never a project repo).
    These are candidates for visit detection, not validated targets — containment
    against a registered project decides the rest.
    """
    paths: list[Path] = []
    for tool_call in tool_calls:
        if tool_call.name not in _VISITING_PATH_TOOLS:
            continue
        arguments = tool_call.arguments
        if not isinstance(arguments, dict):
            continue
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            paths.append(candidate)
    return paths


def _project_containing_path(path: Path, projects: Sequence[Any]) -> Any | None:
    """Return the registered project whose repo cwd contains ``path``, or ``None``.

    Containment is decided on real (symlink-resolved) paths, case-folded only on
    Windows — the same host-explicit rule the projects domain uses for cwd
    identity, so detection agrees with how a project's cwd was stored. When
    nested project repos both contain the path, the most specific (longest cwd)
    wins.
    """
    target = os.path.normcase(os.path.realpath(path))
    best: Any | None = None
    best_root_length = -1
    for project in projects:
        root = os.path.normcase(os.path.realpath(project.cwd))
        contained = target == root or target.startswith(root + os.sep)
        if contained and len(root) > best_root_length:
            best = project
            best_root_length = len(root)
    return best


class _EmittingToolRegistry(ToolRegistry):
    """Adapter that emits public lifecycle events around registry dispatch."""

    def __init__(
        self,
        registry: Any,
        run: Run,
        extension_registry: ExtensionRegistry | None = None,
        note_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._registry = registry
        self._run = run
        self._extension_registry = extension_registry
        self._note_hook = note_hook
        self._tool_timings: dict[str, JsonObject] = {}

    def _hook_context(self) -> HookContext:
        return HookContext(
            session_id=self._run.session_id,
            agent_id=self._run.agent_id,
            run_id=self._run.id,
            add_note=self._note_hook or (lambda _text: None),
        )

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        self._run.raise_if_cancelled()
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        try:
            # Decision pipeline runs before the started event so the timeline
            # shows the effective (possibly modified) arguments. A deny or
            # replace short-circuits execution; a modify rewrites the input the
            # tool runs with and that tool_result hooks observe.
            effective_arguments = arguments
            result: JsonObject | None = None
            if self._extension_registry is not None:
                decision = await self._extension_registry.dispatch_tool_call(
                    self._hook_context(),
                    tool_name=context.tool_name,
                    tool_call_id=context.tool_call_id,
                    input=arguments,
                    validator=lambda extension_name, candidate: (
                        _validated_extension_tool_hook_result(
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

            self._run.emit(
                TOOL_CALL_STARTED_EVENT,
                {
                    "tool_call": {
                        "id": context.tool_call_id,
                        "index": context.tool_call_index,
                        "name": context.tool_name,
                        "arguments": deepcopy(effective_arguments),
                    },
                    "display": _tool_display_payload(
                        self._registry,
                        context.tool_name,
                        effective_arguments,
                    ),
                },
            )

            if result is None:
                result = await self._dispatch_with_failure_envelope(
                    context, effective_arguments, allowed_tools
                )

            if self._extension_registry is not None:
                result = await self._extension_registry.dispatch_tool_result(
                    self._hook_context(),
                    tool_name=context.tool_name,
                    tool_call_id=context.tool_call_id,
                    input=effective_arguments,
                    result=result,
                    validator=lambda extension_name, candidate: (
                        _validated_extension_tool_hook_result(
                            tool_name=context.tool_name,
                            extension_name=extension_name,
                            hook_name="tool_result",
                            result=candidate,
                        )
                    ),
                )

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
        return _validated_tool_result(context.tool_name, result)


async def _dispatch_tool_calls(
    runtime: RuntimeServices,
    agent: Any,
    tool_calls: list[ToolCall],
    session: ChatSession,
    run: Run,
    *,
    nesting_depth: int,
    project_cwd: Path | None = None,
    project_id: str | None = None,
    skill_project_id: str | None = None,
) -> tuple[list[ChatMessage], list[JsonObject]]:
    run.raise_if_cancelled()
    emitting_registry = _EmittingToolRegistry(
        runtime.tools,
        run,
        runtime.extensions,
        note_hook=session.add_note,
    )
    executor = ToolExecutor(emitting_registry)
    workspace = _agent_workspace(agent, Path(runtime.storage.data_dir))
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
            app_root=Path(runtime.system_prompts.app_dir),
            data_root=Path(runtime.storage.data_dir),
            cwd=_resolve_tool_cwd(project_cwd, workspace),
            # The owning run's project rides onto every ToolContext so the
            # subagent tool can inherit it; None keeps the identity path.
            project_id=project_id,
            # The run's effective skill project (rooted-aware) so the skill tool
            # resolves the same pool the run's catalog advertises.
            skill_project_id=skill_project_id,
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
    tool_messages: list[ChatMessage] = []
    media_injections: list[JsonObject] = []
    for tool_call, result in zip(tool_calls, results, strict=True):
        tool_messages.append(
            ChatMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                timing=emitting_registry.timing_for_call(tool_call.id),
            )
        )
        media_injections.extend(_read_media_injections(result))
    return tool_messages, media_injections


def _read_media_injections(result: JsonObject) -> list[JsonObject]:
    """Extract ``read_media`` injection descriptors from a tool result envelope.

    A tool (currently ``read`` on an image) emits a ``read_media`` artifact to
    ask the chat loop to inject the media as a synthetic current-turn user
    message so a vision model actually sees it. Each descriptor carries the
    attachment reference the loop needs to build a ``MediaBlock``. Artifacts of
    any other ``kind`` (e.g. ``image_generation`` output) yield nothing.
    """
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return []

    injections: list[JsonObject] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") != "read_media":
            continue
        attachment_id = artifact.get("attachment_id")
        filename = artifact.get("filename")
        media_type = artifact.get("media_type")
        if (
            isinstance(attachment_id, str)
            and isinstance(filename, str)
            and isinstance(media_type, str)
        ):
            injections.append(
                {
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "media_type": media_type,
                }
            )
    return injections


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


def _is_skill_context_message(message: JsonObject) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, str)
        and content.startswith("<skill_content ")
    )


def _skill_context_insert_index(messages: list[JsonObject]) -> int:
    """Position right after a leading system message and any existing skill contexts.

    Mirrors the build-time layout (`[system?] [skill_context...] [history...]`) so
    mid-run activations land at the front of the skill-context block — at index 0 when
    no system message exists — and keep their activation order instead of reversing.
    """
    index = 0
    if index < len(messages) and messages[index].get("role") == "system":
        index += 1
    while index < len(messages) and _is_skill_context_message(messages[index]):
        index += 1
    return index


def _sync_skill_context_messages(messages: list[JsonObject], session: ChatSession) -> None:
    existing = {
        message.get("content") for message in messages if _is_skill_context_message(message)
    }
    insert_index = _skill_context_insert_index(messages)
    for skill_message in session.skill_context_messages():
        if skill_message["content"] not in existing:
            messages.insert(insert_index, skill_message)
            existing.add(skill_message["content"])
            insert_index += 1
