"""Tool definitions, registry, result envelopes, and execution scheduling."""

from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("tools")

TOOL_ALLOWLIST_WILDCARD = "*"
DEFAULT_TOOL_CONCURRENCY_LIMIT = 50

JsonObject = dict[str, Any]
ToolEmitHook = Callable[[str, JsonObject], None | Awaitable[None]]
ToolCancellationHook = Callable[[], bool]
ToolCancelRegistrationHook = Callable[[Callable[[], None]], None]
ToolCancelCheckHook = Callable[[], bool]
ToolCallCancelRegistrar = Callable[[str, Callable[[], None]], None]
ToolCallCancelCheck = Callable[[str], bool]
ToolNoteHook = Callable[[str], None]
ToolSkillActivationHook = Callable[[str, JsonObject], JsonObject]
ToolHandler = Callable[["ToolContext", JsonObject], JsonObject | Awaitable[JsonObject]]
ToolSummaryBuilder = Callable[[JsonObject], str | None]
MAX_TOOL_DISPLAY_SUMMARY_LENGTH = 120


class ToolError(VBotError):
    """Base class for expected tool registry errors."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is unknown to the registry."""


class ToolNotAllowedError(ToolError):
    """Raised when a tool exists but is not on the caller's allowlist."""


class InvalidToolResultError(ValueError):
    """Raised when a tool handler returns a value that is not a valid result envelope.

    Subclasses ``ValueError`` so existing callers that catch ``ValueError`` keep
    working, while allowing the chat loop to distinguish an invalid handler
    result from invalid tool arguments without inspecting message text.
    """


class DuplicateToolError(ToolError):
    """Raised when registering a tool name more than once."""


@dataclass(frozen=True)
class ToolDisplay:
    """Presentation metadata for one tool invocation."""

    summary_fields: Sequence[str] = ()
    hidden_argument_keys: Sequence[str] = field(default_factory=tuple)
    summary_builder: ToolSummaryBuilder | None = None
    summary_separator: str = " · "

    def __post_init__(self) -> None:
        _validate_display_strings(self.summary_fields, "summary_fields")
        _validate_display_strings(self.hidden_argument_keys, "hidden_argument_keys")
        if self.summary_builder is not None and not callable(self.summary_builder):
            raise ValueError("Tool display summary_builder must be callable")
        object.__setattr__(self, "summary_fields", tuple(self.summary_fields))
        object.__setattr__(self, "hidden_argument_keys", tuple(self.hidden_argument_keys))

    def to_payload(self, arguments: Any) -> JsonObject:
        """Return the UI-safe display payload for one concrete invocation."""
        return {
            "summary": self.summary(arguments),
            "hidden_argument_keys": sorted(self.hidden_argument_keys),
        }

    def summary(self, arguments: Any) -> str:
        """Return a compact display summary, or an empty string when none applies."""
        if not isinstance(arguments, dict):
            return ""

        if self.summary_builder is not None:
            built_summary = _normalize_display_summary(self.summary_builder(arguments))
            if built_summary:
                return built_summary

        parts = [
            value.strip()
            for field_name in self.summary_fields
            if isinstance((value := arguments.get(field_name)), str) and value.strip()
        ]
        return _normalize_display_summary(self.summary_separator.join(parts))


@dataclass(frozen=True)
class ToolContext:
    """Runtime-owned execution identity passed to a single tool call."""

    agent_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    tool_call_index: int
    workspace: Path
    app_root: Path
    data_root: Path
    # Working directory for relative-path resolution by file/shell tools. ``None``
    # falls back to ``workspace`` (the identity-agent home) so every existing
    # caller and identity session keeps today's behavior; a project session
    # supplies the repo cwd, which is a runtime field separate from workspace
    # (workspace stays the memory-tool home).
    cwd: Path | None = None
    # Project the owning run belongs to, or ``None`` for an identity run. A tool
    # (the subagent tool especially) reads this to inherit the parent run's
    # project end-to-end: a child spawned from a project run gets a project-keyed
    # child session/run and a parent link that records the project. ``None`` means
    # the global/identity path, exactly unchanged.
    project_id: str | None = None
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    cancel_registration_hook: ToolCancelRegistrationHook | None = None
    cancel_check_hook: ToolCancelCheckHook | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    allowed_skills: Sequence[str] | None = None
    nesting_depth: int = 0

    @property
    def effective_cwd(self) -> Path:
        """Return the working directory for relative-path resolution.

        Falls back to ``workspace`` when no project cwd was supplied, so file and
        shell tools resolve against the project repo in a project session and
        against the agent workspace everywhere else.
        """
        return self.cwd if self.cwd is not None else self.workspace

    async def emit(self, event_type: str, payload: JsonObject) -> None:
        """Emit a tool lifecycle event through the runtime hook, when present."""
        if self.emit_hook is None:
            return

        result = self.emit_hook(event_type, payload)
        if inspect.isawaitable(result):
            await result

    def is_cancelled(self) -> bool:
        """Return whether the owning run has requested cancellation."""
        if self.cancellation_hook is None:
            return False

        return self.cancellation_hook()

    def on_cancel(self, callback: Callable[[], None]) -> None:
        """Register a cancel callback for this call when the runtime exposes a hook."""
        if self.cancel_registration_hook is None:
            return

        self.cancel_registration_hook(callback)

    def was_cancelled_by_user(self) -> bool:
        """Return whether this call was cancelled by the user, when the hook is wired."""
        if self.cancel_check_hook is None:
            return False

        return self.cancel_check_hook()

    def add_note(self, content: str) -> None:
        """Add a kernel-internal note through the runtime hook, when present."""
        if self.note_hook is None:
            return

        self.note_hook(content)

    def activate_skill(self, name: str, data: JsonObject) -> JsonObject | None:
        """Activate skill context through the runtime hook, when present."""
        if self.skill_activation_hook is None:
            return None

        return self.skill_activation_hook(name, data)


@dataclass(frozen=True)
class ToolCall:
    """A provider-requested tool invocation to schedule."""

    id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class ToolExecutionConfig:
    """Runtime fields shared by every tool call in one execution group."""

    agent_id: str
    session_id: str
    run_id: str
    workspace: Path
    app_root: Path
    data_root: Path
    # Working directory for relative-path resolution; ``None`` falls back to
    # ``workspace`` so existing execution groups keep today's behavior. See
    # ``ToolContext.cwd`` for the contract.
    cwd: Path | None = None
    # Project of the owning run, threaded onto every ``ToolContext`` built from
    # this group. ``None`` is the identity path. See ``ToolContext.project_id``.
    project_id: str | None = None
    allowed_tools: Sequence[str] | None = None
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    cancel_registration_hook: ToolCancelRegistrationHook | None = None
    cancel_check_hook: ToolCancelCheckHook | None = None
    tool_call_cancel_registrar: ToolCallCancelRegistrar | None = None
    tool_call_cancel_check: ToolCallCancelCheck | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    allowed_skills: Sequence[str] | None = None
    nesting_depth: int = 0


@dataclass(frozen=True)
class Tool:
    """A callable tool exposed to an agent."""

    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    internal: bool = False
    display: ToolDisplay = field(default_factory=ToolDisplay)


def tool_success(data: JsonObject, artifacts: list[JsonObject] | None = None) -> JsonObject:
    """Return a stable success envelope for a tool result."""
    if not isinstance(data, dict):
        raise ValueError("Tool success data must be a JSON object")

    return {
        "ok": True,
        "error": None,
        "data": data,
        "artifacts": _copy_artifacts(artifacts),
    }


def tool_failure(
    code: str,
    message: str,
    artifacts: list[JsonObject] | None = None,
    *,
    retryable: bool | None = None,
    attempts_made: int | None = None,
) -> JsonObject:
    """Return a stable failure envelope for a tool result.

    ``retryable``/``attempts_made`` are optional retry-signalling fields that go
    *inside* the ``error`` object — never as top-level envelope keys, which would
    break ``is_tool_result_envelope`` (it checks the top-level key set exactly).
    They let a tool tell the model whether the failure is transient and how many
    attempts the tool already made before giving up, so the model does not
    pointlessly re-invoke a tool that has already exhausted its own retries.
    """
    if not code:
        raise ValueError("Tool failure code is required")
    if not message:
        raise ValueError("Tool failure message is required")
    if retryable is not None and not isinstance(retryable, bool):
        raise ValueError("Tool failure retryable must be a boolean or None")
    if attempts_made is not None and (
        isinstance(attempts_made, bool) or not isinstance(attempts_made, int) or attempts_made < 0
    ):
        raise ValueError("Tool failure attempts_made must be a non-negative integer or None")

    error: JsonObject = {"code": code, "message": message}
    if retryable is not None:
        error["retryable"] = retryable
    if attempts_made is not None:
        error["attempts_made"] = attempts_made

    return {
        "ok": False,
        "error": error,
        "data": None,
        "artifacts": _copy_artifacts(artifacts),
    }


def is_tool_result_envelope(result: JsonObject) -> bool:
    """Return whether a JSON object matches the stable tool result envelope."""
    if set(result) != {"ok", "error", "data", "artifacts"}:
        return False
    if not isinstance(result["ok"], bool):
        return False
    if not isinstance(result["artifacts"], list):
        return False

    if result["ok"]:
        return result["error"] is None and isinstance(result["data"], dict)

    return result["data"] is None and _is_error_object(result["error"])


class ToolRegistry:
    """Register, filter, describe, and dispatch agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
        *,
        internal: bool = False,
        display: ToolDisplay | None = None,
    ) -> Tool:
        """Register a tool and return its immutable definition."""
        self._validate_tool(name, description, parameters, handler, display)
        if name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {name}")

        tool = Tool(
            name=name,
            description=description,
            parameters=dict(parameters),
            handler=handler,
            internal=internal,
            display=display or ToolDisplay(),
        )
        self._tools[name] = tool
        return tool

    def display_for_call(self, name: str, arguments: Any) -> JsonObject:
        """Return display metadata for a concrete tool invocation."""
        return self.get(name).display.to_payload(arguments)

    def get(self, name: str) -> Tool:
        """Return a registered tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"Tool not found: {name}") from None

    def unregister(self, name: str) -> None:
        """Remove a registered tool when it exists."""
        self._tools.pop(name, None)

    def list_tools(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[Tool]:
        """Return registered tools filtered by an allowlist."""
        if allowed_tools is not None and TOOL_ALLOWLIST_WILDCARD not in allowed_tools:
            allowed_names = set(allowed_tools)
            tools = [tool for name, tool in self._tools.items() if name in allowed_names]
        else:
            tools = list(self._tools.values())

        if not include_internal:
            tools = [tool for tool in tools if not tool.internal]

        return sorted(tools, key=lambda tool: tool.name)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[JsonObject]:
        """Return provider-ready tool definitions for allowed tools."""
        return [
            self._to_provider_definition(tool)
            for tool in self.list_tools(allowed_tools, include_internal=include_internal)
        ]

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[JsonObject]:
        """Return prompt-ready name and description pairs for allowed tools."""
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self.list_tools(allowed_tools, include_internal=include_internal)
        ]

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        """Execute a registered allowed tool through an async interface."""
        tool = self.get(context.tool_name)
        if not self._is_allowed(context.tool_name, allowed_tools, internal=tool.internal):
            raise ToolNotAllowedError(f"Tool not allowed: {context.tool_name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")

        result = tool.handler(context, arguments)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise InvalidToolResultError(
                f"Tool handler must return a JSON object: {context.tool_name}"
            )
        if not is_tool_result_envelope(result):
            raise InvalidToolResultError(
                f"Tool handler must return a valid result envelope: {context.tool_name}"
            )
        return result

    @staticmethod
    def _validate_tool(
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
        display: ToolDisplay | None = None,
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

    @staticmethod
    def _to_provider_definition(tool: Tool) -> JsonObject:
        return {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        }


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
    extension block. No built-in tool declares a block today; the seam exists and
    is proven by a test. Collisions are resolved first-wins with a warning, like
    tool-name registration.
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
    """Schedule tool calls concurrently while preserving returned call order."""

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
        """Execute tool calls concurrently and return results in request order."""
        per_run_semaphore = asyncio.Semaphore(self._per_run_limit)
        tasks = [
            asyncio.create_task(
                self._execute_one(tool_call, index, config, per_run_semaphore),
                name=f"tool:{tool_call.name}:{tool_call.id}",
            )
            for index, tool_call in enumerate(tool_calls)
        ]

        if not tasks:
            return []

        return await asyncio.gather(*tasks)

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
            context = ToolContext(
                agent_id=config.agent_id,
                session_id=config.session_id,
                run_id=config.run_id,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                tool_call_index=index,
                workspace=config.workspace,
                app_root=config.app_root,
                data_root=config.data_root,
                cwd=config.cwd,
                project_id=config.project_id,
                emit_hook=config.emit_hook,
                cancellation_hook=config.cancellation_hook,
                cancel_registration_hook=cancel_registration_hook,
                cancel_check_hook=cancel_check_hook,
                note_hook=config.note_hook,
                skill_activation_hook=config.skill_activation_hook,
                allowed_skills=config.allowed_skills,
                nesting_depth=config.nesting_depth,
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
        except ToolNotAllowedError as error:
            return tool_failure("tool_not_allowed", str(error))
        except ValueError as error:
            return tool_failure(
                "invalid_tool_result" if "return" in str(error) else "invalid_arguments", str(error)
            )
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


def _copy_artifacts(artifacts: list[JsonObject] | None) -> list[JsonObject]:
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise ValueError("Tool result artifacts must be a list")
    if not all(isinstance(artifact, dict) for artifact in artifacts):
        raise ValueError("Tool result artifacts must contain JSON objects")

    return [dict(artifact) for artifact in artifacts]


def _validate_display_strings(values: Sequence[str], field_name: str) -> None:
    if isinstance(values, str) or not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"Tool display {field_name} must contain non-empty strings")


def _normalize_display_summary(value: str | None) -> str:
    if not isinstance(value, str):
        return ""

    text = value.strip()
    if len(text) <= MAX_TOOL_DISPLAY_SUMMARY_LENGTH:
        return text

    return f"{text[: MAX_TOOL_DISPLAY_SUMMARY_LENGTH - 3]}..."


_REQUIRED_ERROR_KEYS = frozenset({"code", "message"})
_OPTIONAL_ERROR_KEYS = frozenset({"retryable", "attempts_made"})


def _is_error_object(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    keys = set(error)
    if not keys >= _REQUIRED_ERROR_KEYS or not keys <= (
        _REQUIRED_ERROR_KEYS | _OPTIONAL_ERROR_KEYS
    ):
        return False
    if not (isinstance(error["code"], str) and error["code"]):
        return False
    if not (isinstance(error["message"], str) and error["message"]):
        return False
    if "retryable" in error and not isinstance(error["retryable"], bool):
        return False
    if "attempts_made" in error:
        attempts_made = error["attempts_made"]
        if (
            isinstance(attempts_made, bool)
            or not isinstance(attempts_made, int)
            or attempts_made < 0
        ):
            return False
    return True


__all__ = [
    "DEFAULT_TOOL_CONCURRENCY_LIMIT",
    "DuplicateToolError",
    "JsonObject",
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
    "ToolRegistry",
    "is_tool_result_envelope",
    "tool_failure",
    "tool_success",
]
