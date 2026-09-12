"""Per-call identity, execution-group configuration, and runtime hooks."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.runs import RunExecutionOwner
from core.tools._tool_display import _normalize_display_fact
from core.tools.change_tracker import ChangeTracker
from core.tools.contracts import JsonObject, ToolContract

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
ToolDeliveryReceipt = tuple[str, str, str]
ToolDeliveryReceiptHook = Callable[[str, ToolDeliveryReceipt], None]
ToolTurnEndHook = Callable[[str], None]
ToolHandler = Callable[["ToolContext", JsonObject], JsonObject | Awaitable[JsonObject]]


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
    execution_owner: RunExecutionOwner | None = field(default=None, repr=False)
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
    # ``None``) this is its home project â€” so the ``skill`` tool loads the same
    # skills the run's catalog advertises. Kept separate from ``project_id`` so
    # skill resolution stays rooted-aware without changing subagent inheritance.
    skill_project_id: str | None = None
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    cancel_registration_hook: ToolCancelRegistrationHook | None = None
    cancel_check_hook: ToolCancelCheckHook | None = None
    background_registration_hook: Callable[[Callable[[], bool]], None] | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    result_persisted_hook: ToolResultPersistedHook | None = None
    delivery_receipt_hook: ToolDeliveryReceiptHook | None = field(
        default=None, repr=False, compare=False
    )
    request_turn_end_hook: ToolTurnEndHook | None = field(default=None, repr=False, compare=False)
    _delivery_receipts: list[ToolDeliveryReceipt] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    _turn_end_requested: bool = field(default=False, init=False, repr=False, compare=False)
    allowed_skills: Sequence[str] | None = None
    # Environment credentials made available by Skills active in this Session.
    # Bash combines these transient grants with the Agent's permanent Tool settings.
    skill_env_keys: Sequence[str] = field(default_factory=tuple)
    tool_settings: Mapping[str, Any] | None = None
    # Delegated capabilities must inherit the same Run restrictions as direct calls.
    tool_restriction: Sequence[str] | None = None
    tool_denial_resolver: Callable[[str], str | None] | None = field(
        default=None, repr=False, compare=False
    )
    # Grants for Session-scoped tools whose authority is derived while building
    # the Session request state. Chat adds ``history`` only after a persisted
    # Compaction checkpoint.
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
    presentation_images: list[JsonObject] = field(default_factory=list, repr=False, compare=False)
    # Request-only media: never included in result envelopes or lifecycle events.
    result_media: list[JsonObject] = field(default_factory=list, repr=False, compare=False)
    # Session-scoped file-content tracker for git-style change statistics.
    # ``None`` keeps direct/legacy callers working without change tracking.
    change_tracker: ChangeTracker | None = field(
        default=None,
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

    def record_delivery_receipt(
        self,
        receipt_id: str,
        content_hash: str,
        effect_kind: str,
    ) -> None:
        """Request a receipt atomically persisted with this successful Tool Result."""
        if self.delivery_receipt_hook is None:
            raise RuntimeError("Delivery receipts are unavailable for this Tool call")
        if not all(
            isinstance(value, str) and value for value in (receipt_id, content_hash, effect_kind)
        ):
            raise ValueError("delivery receipt fields must be non-empty strings")
        self._delivery_receipts.append((receipt_id, content_hash, effect_kind))

    def request_turn_end(self) -> None:
        """Ask Chat to finish the Run after its complete Tool batch is durable."""
        if self.request_turn_end_hook is None:
            raise RuntimeError("Graceful turn completion is unavailable for this Tool call")
        object.__setattr__(self, "_turn_end_requested", True)

    def _commit_owned_effects(self) -> None:
        """Hand successful-call effects to the batch owner after validation."""
        if self.delivery_receipt_hook is not None:
            for receipt in self._delivery_receipts:
                self.delivery_receipt_hook(self.tool_call_id, receipt)
        if self._turn_end_requested and self.request_turn_end_hook is not None:
            self.request_turn_end_hook(self.tool_call_id)


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
    vbot_root: Path
    data_root: Path
    # Completed Agentic Loop Iteration that produced this execution group.
    iteration_number: int = 0
    execution_owner: RunExecutionOwner | None = field(default=None, repr=False)
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
    tool_delivery_receipt_registrar: ToolDeliveryReceiptHook | None = None
    tool_turn_end_registrar: ToolTurnEndHook | None = None
    allowed_skills: Sequence[str] | None = None
    skill_env_keys: Sequence[str] = field(default_factory=tuple)
    tool_settings: Mapping[str, Any] | None = None
    session_tool_grants: Sequence[str] = field(default_factory=tuple)
    nesting_depth: int = 0
    input_contracts: Mapping[str, ToolContract] = field(default_factory=dict)
    # Session-scoped file-content tracker for git-style change statistics.
    # ``None`` keeps direct/legacy execution groups without change tracking.
    change_tracker: ChangeTracker | None = field(
        default=None,
        repr=False,
        compare=False,
    )
