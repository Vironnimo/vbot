"""Tool definitions, registry, result envelopes, and execution scheduling."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from core.tools.contracts import ToolContract, compile_tool_contract
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.paths import model_path

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
# (skill_name, wrapped_content) -> newly_activated. The content is passed for the
# session's in-memory activation record only; the tool result is the durable carrier.
ToolSkillActivationHook = Callable[[str, str], bool]
ToolResultPersistedCallback = Callable[[], None]
ToolResultPersistedHook = Callable[[ToolResultPersistedCallback], None]
ToolCallResultPersistedRegistrar = Callable[[str, ToolResultPersistedCallback], None]
ToolHandler = Callable[["ToolContext", JsonObject], JsonObject | Awaitable[JsonObject]]
_ToolWorkerResult = TypeVar("_ToolWorkerResult")
ToolReadinessPredicate = Callable[[], bool]
ToolSummaryBuilder = Callable[[JsonObject], str | None]
ToolDisplayFactBuilder = Callable[[JsonObject, JsonObject | None], Sequence[JsonObject]]
MAX_TOOL_DISPLAY_SUMMARY_LENGTH = 120
MAX_TOOL_DISPLAY_VALUE_LENGTH = 8192
DEFAULT_TOOL_DISPLAY_MAX_CHARACTERS = 64
TOOL_DISPLAY_VALUE_KINDS = frozenset(
    {"command", "description", "identifier", "path", "query", "text", "url"}
)
TOOL_DISPLAY_TRUNCATION_MODES = frozenset({"start", "end", "middle", "never"})
TOOL_DISPLAY_TOOLTIP_MODES = frozenset({"always", "none", "truncated"})
TOOL_DISPLAY_FACT_UNITS = frozenset({"matches", "results"})
TOOL_DISPLAY_LINE_CHANGES = frozenset({"added", "removed"})


class ToolError(VBotError):
    """Base class for expected tool registry errors."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is unknown to the registry."""


class ToolNotAllowedError(ToolError):
    """Raised when a tool exists but is not on the caller's allowlist."""


class SessionToolUnavailableError(ToolError):
    """Raised when a Session-scoped tool has no grant in the current Session."""


class InvalidToolResultError(ValueError):
    """Raised when a tool handler returns a value that is not a valid result envelope.

    Subclasses ``ValueError`` so existing callers that catch ``ValueError`` keep
    working, while allowing the chat loop to distinguish an invalid handler
    result from invalid tool arguments without inspecting message text.
    """


class DuplicateToolError(ToolError):
    """Raised when registering a tool name more than once."""


@dataclass(frozen=True)
class ToolDisplayField:
    """One ordered argument candidate for a Tool row's flexible primary value."""

    argument_key: str
    kind: str = "text"
    truncate: str = "end"
    tooltip: str = "truncated"
    quote: bool = False
    copyable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.argument_key, str) or not self.argument_key:
            raise ValueError("Tool display argument_key must be a non-empty string")
        if self.kind not in TOOL_DISPLAY_VALUE_KINDS:
            raise ValueError(f"Unsupported Tool display value kind: {self.kind}")
        if self.truncate not in TOOL_DISPLAY_TRUNCATION_MODES:
            raise ValueError(f"Unsupported Tool display truncation mode: {self.truncate}")
        if self.tooltip not in TOOL_DISPLAY_TOOLTIP_MODES:
            raise ValueError(f"Unsupported Tool display tooltip mode: {self.tooltip}")
        if not isinstance(self.quote, bool) or not isinstance(self.copyable, bool):
            raise ValueError("Tool display quote and copyable flags must be booleans")


@dataclass(frozen=True)
class ToolDisplayPart:
    """One computed semantic value returned by a Tool-specific row builder."""

    value: str
    kind: str = "text"
    truncate: str = "end"
    tooltip: str = "truncated"
    full_value: str | None = None
    quote: bool = False
    copyable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Tool display part value must be a non-empty string")
        if self.full_value is not None and not isinstance(self.full_value, str):
            raise ValueError("Tool display part full_value must be a string or None")
        if self.kind not in TOOL_DISPLAY_VALUE_KINDS:
            raise ValueError(f"Unsupported Tool display value kind: {self.kind}")
        if self.truncate not in TOOL_DISPLAY_TRUNCATION_MODES:
            raise ValueError(f"Unsupported Tool display truncation mode: {self.truncate}")
        if self.tooltip not in TOOL_DISPLAY_TOOLTIP_MODES:
            raise ValueError(f"Unsupported Tool display tooltip mode: {self.tooltip}")
        if not isinstance(self.quote, bool) or not isinstance(self.copyable, bool):
            raise ValueError("Tool display quote and copyable flags must be booleans")


ToolDisplayPartBuilder = Callable[[JsonObject], Sequence[ToolDisplayPart]]


async def run_tool_worker(
    function: Callable[..., _ToolWorkerResult], *arguments: Any
) -> _ToolWorkerResult:
    """Run blocking Tool work off-loop and settle it before cancellation escapes."""
    task = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            raise
        raise


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


def result_count_fact_builder(
    data_field: str,
    *,
    when_arguments: Mapping[str, Any] | None = None,
    at_least_field: str | None = None,
    unit: str = "results",
) -> ToolDisplayFactBuilder:
    """Build a UI-only count fact from one successful Tool result field."""
    if not isinstance(data_field, str) or not data_field:
        raise ValueError("Tool display result count data_field must be a non-empty string")
    if at_least_field is not None and (not isinstance(at_least_field, str) or not at_least_field):
        raise ValueError("Tool display result count at_least_field must be a non-empty string")
    if unit not in TOOL_DISPLAY_FACT_UNITS:
        raise ValueError(f"Unsupported Tool display fact unit: {unit}")
    conditions = dict(when_arguments or {})
    if not all(isinstance(key, str) and key for key in conditions):
        raise ValueError("Tool display result count conditions must use non-empty string keys")

    def build(arguments: JsonObject, result: JsonObject | None) -> Sequence[JsonObject]:
        if any(arguments.get(key) != expected for key, expected in conditions.items()):
            return ()
        if not isinstance(result, dict) or result.get("ok") is not True:
            return ()
        data = result.get("data")
        if not isinstance(data, dict):
            return ()
        raw_count = data.get(data_field)
        if isinstance(raw_count, list):
            count = len(raw_count)
        elif isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            count = raw_count
        else:
            return ()
        return (
            {
                "kind": "count",
                "value": count,
                "unit": unit,
                "at_least": (
                    count > 0 and data.get(at_least_field) is True if at_least_field else False
                ),
            },
        )

    return build


@dataclass(frozen=True)
class ToolDisplay:
    """Presentation metadata for one tool invocation."""

    summary_fields: Sequence[str] = ()
    hidden_argument_keys: Sequence[str] = field(default_factory=tuple)
    summary_builder: ToolSummaryBuilder | None = None
    summary_separator: str = " · "
    primary_candidates: Sequence[ToolDisplayField] = ()
    secondary_fields: Sequence[ToolDisplayField] = ()
    parts_builder: ToolDisplayPartBuilder | None = None
    fact_builder: ToolDisplayFactBuilder | None = None
    max_characters: int = DEFAULT_TOOL_DISPLAY_MAX_CHARACTERS

    def __post_init__(self) -> None:
        _validate_display_strings(self.summary_fields, "summary_fields")
        _validate_display_strings(self.hidden_argument_keys, "hidden_argument_keys")
        if self.summary_builder is not None and not callable(self.summary_builder):
            raise ValueError("Tool display summary_builder must be callable")
        if self.parts_builder is not None and not callable(self.parts_builder):
            raise ValueError("Tool display parts_builder must be callable")
        if self.fact_builder is not None and not callable(self.fact_builder):
            raise ValueError("Tool display fact_builder must be callable")
        if isinstance(self.max_characters, bool) or not isinstance(self.max_characters, int):
            raise ValueError("Tool display max_characters must be an integer")
        if self.max_characters <= 0:
            raise ValueError("Tool display max_characters must be positive")
        for field_name, values in (
            ("primary_candidates", self.primary_candidates),
            ("secondary_fields", self.secondary_fields),
        ):
            if not all(isinstance(value, ToolDisplayField) for value in values):
                raise ValueError(f"Tool display {field_name} must contain ToolDisplayField values")
        object.__setattr__(self, "summary_fields", tuple(self.summary_fields))
        object.__setattr__(self, "hidden_argument_keys", tuple(self.hidden_argument_keys))
        object.__setattr__(self, "primary_candidates", tuple(self.primary_candidates))
        object.__setattr__(self, "secondary_fields", tuple(self.secondary_fields))

    def to_payload(
        self,
        arguments: Any,
        *,
        context: ToolContext | None = None,
        result: JsonObject | None = None,
        facts: Sequence[JsonObject] = (),
    ) -> JsonObject:
        """Return the UI-safe display payload for one concrete invocation."""
        primary = self._primary_payload(arguments, context=context)
        payload: JsonObject = {
            "version": 1,
            "summary": self._payload_summary(arguments, primary),
            "hidden_argument_keys": sorted(self.hidden_argument_keys),
            "primary": primary,
            "facts": self._fact_payload(arguments, result=result, facts=facts),
        }
        return payload

    def _primary_payload(
        self,
        arguments: Any,
        *,
        context: ToolContext | None,
    ) -> list[JsonObject]:
        if not isinstance(arguments, dict):
            return []

        if self.parts_builder is not None:
            computed_parts = self.parts_builder(arguments)
            if isinstance(computed_parts, (str, bytes)) or not isinstance(computed_parts, Sequence):
                raise ValueError("Tool display parts_builder must return a sequence")
            if not all(isinstance(part, ToolDisplayPart) for part in computed_parts):
                raise ValueError("Tool display parts_builder must return ToolDisplayPart values")
            return [self._part_payload(part, context=context) for part in computed_parts[:2]]

        parts: list[JsonObject] = []
        for candidate in self.primary_candidates:
            value = _display_argument_value(arguments.get(candidate.argument_key))
            if not value:
                continue
            parts.append(self._field_payload(candidate, value, context=context))
            break
        for configured_field in self.secondary_fields:
            value = _display_argument_value(arguments.get(configured_field.argument_key))
            if value:
                parts.append(self._field_payload(configured_field, value, context=context))
        if parts:
            return parts[:2]

        summary = self.summary(arguments)
        if not summary:
            return []
        return [
            {
                "kind": "text",
                "value": summary,
                "full_value": summary,
                "truncate": "end",
                "tooltip": "truncated",
                "max_characters": self.max_characters,
                "quote": False,
                "copyable": False,
            }
        ]

    def _field_payload(
        self,
        configured_field: ToolDisplayField,
        value: str,
        *,
        context: ToolContext | None,
    ) -> JsonObject:
        return self._part_payload(
            ToolDisplayPart(
                value=value,
                kind=configured_field.kind,
                truncate=configured_field.truncate,
                tooltip=configured_field.tooltip,
                quote=configured_field.quote,
                copyable=configured_field.copyable,
            ),
            context=context,
        )

    def _part_payload(
        self,
        configured_part: ToolDisplayPart,
        *,
        context: ToolContext | None,
    ) -> JsonObject:
        visible_value = (
            model_path(configured_part.value)
            if configured_part.kind == "path"
            else configured_part.value
        )
        full_value = configured_part.full_value or visible_value
        if configured_part.kind == "path" and context is not None:
            try:
                full_value = model_path(context.resolve_path(full_value))
            except (OSError, RuntimeError, ValueError):
                full_value = visible_value
        return {
            "kind": configured_part.kind,
            "value": _normalize_display_value(visible_value),
            "full_value": _normalize_display_value(full_value),
            "truncate": configured_part.truncate,
            "tooltip": configured_part.tooltip,
            "max_characters": self.max_characters,
            "quote": configured_part.quote,
            "copyable": configured_part.copyable,
        }

    def _payload_summary(self, arguments: Any, primary: Sequence[JsonObject]) -> str:
        if primary:
            return _normalize_display_summary(
                self.summary_separator.join(
                    str(part.get("value", "")) for part in primary if part.get("value")
                )
            )
        return self.summary(arguments)

    def _fact_payload(
        self,
        arguments: Any,
        *,
        result: JsonObject | None,
        facts: Sequence[JsonObject],
    ) -> list[JsonObject]:
        raw_facts = list(facts)
        if self.fact_builder is not None and isinstance(arguments, dict):
            raw_facts.extend(self.fact_builder(arguments, result))
        normalized: list[JsonObject] = []
        for fact in raw_facts:
            prepared = _normalize_display_fact(fact)
            if prepared is not None:
                normalized.append(prepared)
        return normalized

    def summary(self, arguments: Any) -> str:
        """Return a compact display summary, or an empty string when none applies."""
        if not isinstance(arguments, dict):
            return ""

        if self.summary_builder is not None:
            built_summary = _normalize_display_summary(self.summary_builder(arguments))
            if built_summary:
                return built_summary

        if self.parts_builder is not None:
            computed_parts = self.parts_builder(arguments)
            if isinstance(computed_parts, Sequence) and not isinstance(
                computed_parts, (str, bytes)
            ):
                built_summary = _normalize_display_summary(
                    self.summary_separator.join(
                        part.value
                        for part in computed_parts[:2]
                        if isinstance(part, ToolDisplayPart)
                    )
                )
                if built_summary:
                    return built_summary

        parts = [
            value.strip()
            for field_name in self.summary_fields
            if isinstance((value := arguments.get(field_name)), str) and value.strip()
        ]
        return _normalize_display_summary(self.summary_separator.join(parts))


@dataclass(frozen=True)
class ToolDefinitionProfileContext:
    """Stable configuration identity used to select model-facing Tool profiles."""

    agent_id: str


@dataclass(frozen=True)
class ToolDefinitionProfile:
    """One immutable model-facing definition selected from stable configuration."""

    key: str
    description: str
    parameters: JsonObject = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Tool definition profile key must be a non-empty string")
        if not isinstance(self.description, str) or not self.description:
            raise ValueError("Tool definition profile description must be a non-empty string")
        if not isinstance(self.parameters, dict):
            raise ValueError("Tool definition profile parameters must be a JSON Schema object")
        object.__setattr__(self, "parameters", copy.deepcopy(self.parameters))


ToolDefinitionProfileResolver = Callable[
    [ToolDefinitionProfileContext],
    ToolDefinitionProfile | None,
]


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
    vbot_root: Path
    data_root: Path
    # Completed Agentic Loop Iteration whose Assistant response requested this
    # Tool Call. Direct callers that do not execute inside Chat leave it at 0.
    iteration_number: int = 0
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
    # The project whose skill pool this run resolves against. Equals ``project_id``
    # for a project run and ``None`` for a plain identity run, but for a *rooted*
    # identity agent (its workspace is a registered repo, so ``project_id`` is
    # ``None``) this is its home project — so the ``skill`` tool loads the same
    # skills the run's catalog advertises. Kept separate from ``project_id`` so
    # skill resolution stays rooted-aware without changing subagent inheritance.
    skill_project_id: str | None = None
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    cancel_registration_hook: ToolCancelRegistrationHook | None = None
    cancel_check_hook: ToolCancelCheckHook | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    result_persisted_hook: ToolResultPersistedHook | None = None
    allowed_skills: Sequence[str] | None = None
    # Environment credentials made available by Skills active in this Session.
    # Bash combines these transient grants with the Agent's permanent Tool settings.
    skill_env_keys: Sequence[str] = field(default_factory=tuple)
    tool_settings: Mapping[str, Any] | None = None
    # Grants for Session-scoped tools whose authority is derived while building
    # the Session request state. Chat grants ``skill_list`` with the stable
    # ``skill`` capability from the first request and adds ``history`` only after
    # a persisted Compaction checkpoint.
    session_tool_grants: Sequence[str] = field(default_factory=tuple)
    nesting_depth: int = 0
    # Exact model-facing contract used for this Provider cycle. Direct callers and
    # legacy execution paths leave it unset and use the Tool's canonical contract.
    input_contract: ToolContract | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    presentation_facts: list[JsonObject] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    @property
    def effective_cwd(self) -> Path:
        """Return the working directory for relative-path resolution.

        Falls back to ``workspace`` when no project cwd was supplied, so file and
        shell tools resolve against the project repo in a project session and
        against the agent workspace everywhere else.
        """
        return self.cwd if self.cwd is not None else self.workspace

    def resolve_path(self, path: str | Path) -> Path:
        """Resolve one user-supplied path against this call's working directory."""

        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.effective_cwd / candidate).resolve()

    def add_display_count(self, value: int, unit: str, *, at_least: bool = False) -> None:
        """Record one presentation-only count without changing the Tool result."""
        fact = _normalize_display_fact(
            {"kind": "count", "value": value, "unit": unit, "at_least": at_least}
        )
        if fact is None:
            raise ValueError("Invalid Tool display count")
        self.presentation_facts.append(fact)

    def add_display_line_changes(self, *, added: int, removed: int) -> None:
        """Record added and removed line counts without changing the Tool result."""
        for change, value in (("added", added), ("removed", removed)):
            fact = _normalize_display_fact(
                {"kind": "line_change", "change": change, "value": value}
            )
            if fact is None:
                raise ValueError("Invalid Tool display line change")
            self.presentation_facts.append(fact)

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

    def activate_skill(self, name: str, content: str) -> bool | None:
        """Record a skill activation through the session hook, when present.

        Returns ``True`` for a fresh activation, ``False`` when the skill was
        already active in the session, ``None`` when no hook is wired (the
        caller then treats the activation as fresh).
        """
        if self.skill_activation_hook is None:
            return None

        return self.skill_activation_hook(name, content)

    def after_result_persisted(self, callback: ToolResultPersistedCallback) -> None:
        """Run *callback* only after this Tool Result enters Session history."""
        if self.result_persisted_hook is None:
            return
        self.result_persisted_hook(callback)


@dataclass(frozen=True)
class ToolCall:
    """A provider-requested tool invocation to schedule."""

    id: str
    name: str
    arguments: Any
    force_serial: bool = False


@dataclass(frozen=True)
class ToolExecutionConfig:
    """Runtime fields shared by every tool call in one execution group."""

    agent_id: str
    session_id: str
    run_id: str
    workspace: Path
    vbot_root: Path
    data_root: Path
    # Completed Agentic Loop Iteration that produced this execution group.
    iteration_number: int = 0
    # Working directory for relative-path resolution; ``None`` falls back to
    # ``workspace`` so existing execution groups keep today's behavior. See
    # ``ToolContext.cwd`` for the contract.
    cwd: Path | None = None
    # Project of the owning run, threaded onto every ``ToolContext`` built from
    # this group. ``None`` is the identity path. See ``ToolContext.project_id``.
    project_id: str | None = None
    # Effective skill project for this group; see ``ToolContext.skill_project_id``.
    skill_project_id: str | None = None
    allowed_tools: Sequence[str] | None = None
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    cancel_registration_hook: ToolCancelRegistrationHook | None = None
    cancel_check_hook: ToolCancelCheckHook | None = None
    tool_call_cancel_registrar: ToolCallCancelRegistrar | None = None
    tool_call_cancel_check: ToolCallCancelCheck | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    tool_call_result_persisted_registrar: ToolCallResultPersistedRegistrar | None = None
    allowed_skills: Sequence[str] | None = None
    skill_env_keys: Sequence[str] = field(default_factory=tuple)
    tool_settings: Mapping[str, Any] | None = None
    session_tool_grants: Sequence[str] = field(default_factory=tuple)
    nesting_depth: int = 0
    input_contracts: Mapping[str, ToolContract] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    """A callable tool exposed to an agent."""

    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    result_schema: JsonObject | None = field(default=None, repr=False)
    contract: ToolContract = field(init=False, repr=False, compare=False)
    internal: bool = False
    # A Session-scoped tool is configurable nowhere and model-visible only when
    # the current Session supplies a matching persisted-state grant.
    session_scoped: bool = False
    display: ToolDisplay = field(default_factory=ToolDisplay)
    # Optional readiness predicate (zero-arg, cheap, I/O-free) — e.g. "the token
    # is a non-empty string", never a network ping, since it runs on every
    # prompt/tool-definition build. ``None`` means always ready. A predicate that
    # raises is treated as **not ready** (logged once at ``warning``). Readiness
    # is a separate axis from the allowlist: a not-ready tool stays registered
    # (its persisted permissions survive) but is filtered out of the model-facing
    # surfaces and returns a clean failure envelope on a direct dispatch.
    ready: ToolReadinessPredicate | None = None
    # Optional human-readable hint explaining what makes this tool ready — shown by
    # the ``tool.list`` RPC so an accessor can tell the user why a not-ready tool is
    # unavailable (e.g. "set the extension's token"). Server-delivered English text
    # like the description, never frontend i18n. ``None`` when the tool has no
    # readiness precondition to explain.
    readiness_hint: str | None = None
    # The name of the extension that registered this tool, or ``None`` for a
    # built-in tool. Set at extension-tool apply time so ``tool.list`` can attribute
    # a tool to its owning extension.
    extension: str | None = None
    parallel_safe: bool = True
    open_input_schema: bool = False
    definition_profile_resolver: ToolDefinitionProfileResolver | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        contract = compile_tool_contract(
            name=self.name,
            input_schema=self.parameters,
            result_schema=self.result_schema,
            parallel_safe=self.parallel_safe,
            require_closed_input=not self.open_input_schema,
        )
        object.__setattr__(self, "parameters", copy.deepcopy(contract.input_schema))
        object.__setattr__(self, "result_schema", copy.deepcopy(contract.result_schema))
        object.__setattr__(self, "contract", contract)


def tool_is_ready(tool: Tool) -> bool:
    """Return whether *tool* is ready to be offered right now.

    A tool with no predicate is always ready. A predicate that raises is logged
    once at ``warning`` and treated as **not ready** — a broken predicate must
    never take a prompt/tool-definition build down or make a tool spuriously
    available.
    """
    if tool.ready is None:
        return True
    try:
        return bool(tool.ready())
    except Exception as error:
        _LOGGER.warning("Tool %s readiness predicate raised: %s", tool.name, error)
        return False


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


READ_MEDIA_ARTIFACT_KIND = "read_media"


def read_media_artifact(*, attachment_id: str, filename: str, media_type: str) -> JsonObject:
    """Build a ``read_media`` artifact describing a stored media blob.

    A Tool emits this artifact so Chat can attach the stored image as Run-local
    rich content on the correlated Tool Result. Both ``read`` (local image
    files) and ``web_fetch`` (fetched image URLs) produce it, so the contract
    shape lives here once instead of being duplicated in each Tool.
    """
    return {
        "kind": READ_MEDIA_ARTIFACT_KIND,
        "attachment_id": attachment_id,
        "filename": filename,
        "media_type": media_type,
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
        session_scoped: bool = False,
        display: ToolDisplay | None = None,
        ready: ToolReadinessPredicate | None = None,
        readiness_hint: str | None = None,
        extension: str | None = None,
        result_schema: JsonObject | None = None,
        parallel_safe: bool = True,
        open_input_schema: bool = False,
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
            session_scoped=session_scoped,
            display=display or ToolDisplay(),
            ready=ready,
            readiness_hint=readiness_hint,
            extension=extension,
            parallel_safe=parallel_safe,
            open_input_schema=open_input_schema,
            definition_profile_resolver=definition_profile_resolver,
        )
        self._tools[name] = tool
        return tool

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
        return self.get(name).display.to_payload(
            arguments,
            context=context,
            result=result,
            facts=facts,
        )

    def get(self, name: str) -> Tool:
        """Return a registered tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"Tool not found: {name}") from None

    def unregister(self, name: str) -> None:
        """Remove a registered tool when it exists."""
        self._tools.pop(name, None)
        for cache_key in [
            cache_key for cache_key in self._definition_profile_cache if cache_key[0] == name
        ]:
            self._definition_profile_cache.pop(cache_key, None)

    def is_parallel_safe(self, name: str) -> bool:
        """Return whether a registered Tool may overlap a sibling call."""
        tool = self._tools.get(name)
        return bool(tool is not None and tool.parallel_safe)

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
        if not self._is_allowed(context.tool_name, allowed_tools, internal=tool.internal):
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
        normalized_arguments = input_contract.normalize_arguments(arguments)
        input_contract.validate_arguments(normalized_arguments)

        result = tool.handler(context, normalized_arguments)
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
            group_results = await asyncio.gather(*tasks)
            for (index, _tool_call), result in zip(
                parallel_group,
                group_results,
                strict=True,
            ):
                results[index] = result
            parallel_group.clear()

        for index, tool_call in enumerate(tool_calls):
            if not tool_call.force_serial and self._registry.is_parallel_safe(tool_call.name):
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
                allowed_skills=config.allowed_skills,
                skill_env_keys=config.skill_env_keys,
                tool_settings=config.tool_settings,
                session_tool_grants=config.session_tool_grants,
                nesting_depth=config.nesting_depth,
                input_contract=config.input_contracts.get(tool_call.name),
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


def _display_argument_value(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_display_value(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _normalize_display_value(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= MAX_TOOL_DISPLAY_VALUE_LENGTH:
        return text
    return f"{text[: MAX_TOOL_DISPLAY_VALUE_LENGTH - 1]}…"


def _normalize_display_fact(value: Any) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "line_range":
        start = value.get("start")
        end = value.get("end")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 1
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end < start
        ):
            return None
        return {"kind": "line_range", "start": start, "end": end}
    if kind == "line_change":
        count = value.get("value")
        change = value.get("change")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or change not in TOOL_DISPLAY_LINE_CHANGES
        ):
            return None
        return {"kind": "line_change", "change": change, "value": count}
    if kind != "count":
        return None
    count = value.get("value")
    unit = value.get("unit")
    at_least = value.get("at_least", False)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    if unit not in TOOL_DISPLAY_FACT_UNITS or not isinstance(at_least, bool):
        return None
    return {
        "kind": "count",
        "value": count,
        "unit": unit,
        "at_least": at_least,
    }


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
]
