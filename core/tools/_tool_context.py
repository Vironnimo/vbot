"""Per-call identity, execution-group configuration, and runtime hooks."""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.runs import RunExecutionOwner, RunKind
from core.tools._display_diff import (
    MAX_DISPLAY_DIFF_LINES,
    display_diff_line_count,
    display_file_diff,
)
from core.tools._tool_display import (
    _normalize_display_fact,
    display_memory_changes,
    display_notice,
    display_results,
    display_text,
)
from core.tools.change_tracker import ChangeTracker
from core.tools.contracts import JsonObject, ToolContract, ToolContractError
from core.tools.model_names import model_tool_name
from core.utils.file_status import is_link_status

_DISPLAY_MEDIA_KINDS = ("image", "video", "audio")
# An allowlist entry that allows every Tool.
TOOL_ALLOWLIST_WILDCARD = "*"


def _path_argument(path: str | Path, *, windows: bool) -> str | Path:
    """Remove only quoting that cannot be a legal literal in the host path grammar."""
    if windows and isinstance(path, str):
        text = path.strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            return text[1:-1]
    # Single quotes are legal on Windows. POSIX also permits double quotes and
    # backslashes. Existence must not redirect a missing literal to another file.
    return path


def is_link_entry(path: Path) -> bool:
    """Return whether the final path component is a symbolic link or Windows junction."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return is_link_status(info)


ToolEmitHook = Callable[[str, JsonObject], None | Awaitable[None]]
ToolCancellationHook = Callable[[], bool]
# A cancel callback may return an awaitable; the Run awaits it within its
# cancellation cleanup budget before it ends (``core.runs.Run.request_cancel``).
ToolCancelCallback = Callable[[], Awaitable[object] | None]
ToolCancelRegistrationHook = Callable[[ToolCancelCallback], None]
ToolCancelCheckHook = Callable[[], bool]
ToolCallCancelRegistrar = Callable[[str, ToolCancelCallback], None]
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
# (tool_call_id, tool_name, payload) -> payload_id. Chat stages the payload and
# persists it with this call's Tool Result in the same Session transaction.
ToolResultPayloadHook = Callable[[str, str, Any], str]
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
    # The Agent whose Skills this call reads and changes when it is not the calling
    # Agent: the Agent a Librarian Session is bound to. ``None`` (every other
    # Session) is the calling Agent; read :attr:`skill_subject_id`.
    skill_agent_id: str | None = None
    # Kind of the Run that dispatched this call, or ``None`` outside Runs (direct
    # callers). ``core.runs.is_unattended_run_kind`` tells background Runs apart.
    run_kind: RunKind | None = None
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
    result_payload_hook: ToolResultPayloadHook | None = field(
        default=None, repr=False, compare=False
    )
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
    # Canonical dispatch retains the selected Tool's result contract so outer
    # dispatch adapters never have to resolve a changing catalog a second time.
    result_contract: ToolContract | None = field(
        default=None, init=False, repr=False, compare=False
    )
    # The Tool allowlist canonical dispatch checked this call against (``None``
    # allows every Tool), retained for ``can_call``.
    dispatch_allowed_tools: Sequence[str] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    presentation_facts: list[JsonObject] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )
    # Images, videos and audio the user sees in the call's details, recorded
    # via ``add_display_media``.
    presentation_media: list[JsonObject] = field(default_factory=list, repr=False, compare=False)
    # User-facing detail blocks in the order recorded, for Tools whose display
    # declares ``details``.
    presentation_details: list[JsonObject] = field(default_factory=list, repr=False, compare=False)
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
    def skill_subject_id(self) -> str:
        """Return the id of the Agent whose Skills this call works on."""
        return self.skill_agent_id or self.agent_id

    @property
    def effective_cwd(self) -> Path:
        """Return the working directory for relative-path resolution.

        Falls back to ``workspace`` when no project cwd was supplied, so file and
        shell tools resolve against the project repo in a project session and
        against the agent workspace everywhere else.
        """
        return self.cwd if self.cwd is not None else self.workspace

    def resolve_path(self, path: str | Path, *, follow_final_link: bool = True) -> Path:
        """Resolve one user-supplied path against this call's working directory.

        ``follow_final_link=False`` resolves only the parent directory, so a final
        symbolic link or junction names the link entry itself (as Delete or Move
        of that entry requires) and the final name keeps the requested spelling.
        A path containing NUL is refused before anything touches the filesystem.
        """
        if "\x00" in str(path):
            name = model_tool_name(self.tool_name)
            raise ToolContractError(
                f"{name} was not run: the path {json.dumps(str(path))} contains a NUL "
                f"character (U+0000), which no file path can contain. Remove it and call "
                f"{name} again."
            )
        candidate = Path(_path_argument(path, windows=os.name == "nt")).expanduser()
        target = candidate if candidate.is_absolute() else self.effective_cwd / candidate
        if follow_final_link or target.name in {"", ".", ".."}:
            return target.resolve()
        return target.parent.resolve() / target.name

    def can_call(self, tool_name: str) -> bool:
        """Return whether this Run lets the Agent call the Tool named *tool_name*.

        True when the Tool is in the allowlist canonical dispatch checked this call
        against and the Run does not deny it. A result names another Tool only when
        this is true, so the Agent is never pointed at a Tool it cannot call.
        *tool_name* is the registry name.
        """
        allowed = self.dispatch_allowed_tools
        if (
            allowed is not None
            and TOOL_ALLOWLIST_WILDCARD not in allowed
            and tool_name not in allowed
        ):
            return False
        denial = self.tool_denial_resolver
        return denial is None or denial(tool_name) is None

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

    def add_display_file_change(
        self,
        path: str,
        change: str,
        before: str | None,
        after: str | None,
        *,
        destination: str | None = None,
    ) -> JsonObject:
        """Record one changed file's bounded diff without changing the Tool result.

        ``None`` text means absent or not text. The changed files of one call
        form one ``file_changes`` detail block at the place of the first, and
        share one diff line budget. Returns the recorded change, whose ``added``
        and ``removed`` count every changed line.
        """
        block = next(
            (item for item in self.presentation_details if item["type"] == "file_changes"), None
        )
        if block is None:
            block = {"type": "file_changes", "files": []}
            self.presentation_details.append(block)
        shown = sum(display_diff_line_count(item) for item in block["files"])
        recorded = display_file_diff(
            path,
            change,
            before,
            after,
            destination=destination,
            line_budget=max(0, MAX_DISPLAY_DIFF_LINES - shown),
        )
        block["files"].append(recorded)
        return recorded

    def add_display_notice(self, level: str, text: str, *, subject: str | None = None) -> None:
        """Record one notice detail block (``info``, ``warning`` or ``error``) for the user."""
        self.presentation_details.append(display_notice(level, text, subject=subject))

    def add_display_text(self, label: str, text: str) -> None:
        """Record one labelled text detail block, such as output the result does not hold."""
        self.presentation_details.append(display_text(label, text=text))

    def add_display_results(self, items: Sequence[Mapping[str, str | None]]) -> None:
        """Record the things a call found as one results detail block (``display_results``)."""
        self.presentation_details.append(display_results(items))

    def add_display_media(
        self,
        path: Path | str,
        media_type: str,
        *,
        filename: str | None = None,
        source: bool = False,
    ) -> None:
        """Show one image, video or audio file in the call's details.

        ``media_type`` is a media type such as ``image/png`` or only its kind
        (``image``, ``video``, ``audio``); other kinds are not shown. A file the
        call read, looked at or produced is shown as media; ``source=True``
        marks one it started from, such as a generation's reference image.
        ``filename`` names a file whose path does not, such as a download. The
        display carries the absolute path only internally; the server turns it
        into a signed file address before any client sees it, and a missing
        file still shows as unavailable.
        """
        kind = media_type.partition("/")[0] if isinstance(media_type, str) else ""
        if kind not in _DISPLAY_MEDIA_KINDS:
            return
        absolute = Path(path).absolute()
        item: JsonObject = {
            "path": str(absolute),
            "kind": kind,
            "filename": filename or absolute.name,
        }
        if source:
            item["role"] = "source"
        self.presentation_media.append(item)

    def add_display_memory_changes(
        self,
        scope: str,
        changes: Sequence[Mapping[str, str | None]],
        *,
        revision: int | None = None,
    ) -> None:
        """Record the entries a call changed in one Memory scope, and its revision."""
        self.presentation_details.append(display_memory_changes(scope, changes, revision=revision))

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

    def on_cancel(self, callback: ToolCancelCallback) -> None:
        """Register a cancel callback for this call when the runtime exposes a hook.

        An awaitable the callback returns is awaited within the Run's cancellation
        cleanup budget, so the Run ends only after that cleanup (or the budget).
        """
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

    @property
    def result_payloads_available(self) -> bool:
        """Return whether this call runs in a Session that can keep result payloads."""
        return self.result_payload_hook is not None

    def attach_result_payload(self, payload: Any) -> str:
        """Keep one JSON *payload* with this call's Tool Result and return its id.

        The payload is persisted in the same transaction as the Tool Result, so a
        call that raises, is cancelled, or whose batch is never persisted leaves
        nothing behind. Only Extension Tools running in a Session can attach
        payloads; check :attr:`result_payloads_available` first. The owning
        Extension reads the payload back with ``ExtensionHost.load_result_payload``.
        """
        if self.result_payload_hook is None:
            raise RuntimeError("Result payloads are unavailable for this Tool call")
        return self.result_payload_hook(self.tool_call_id, self.tool_name, payload)

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

    def _retain_result_contract(self, contract: ToolContract) -> None:
        """Keep the canonical dispatch selection for this call's result checks."""
        object.__setattr__(self, "result_contract", contract)

    def _retain_dispatch_allowlist(self, allowed_tools: Sequence[str] | None) -> None:
        """Keep the allowlist canonical dispatch checked this call against."""
        object.__setattr__(
            self,
            "dispatch_allowed_tools",
            None if allowed_tools is None else tuple(allowed_tools),
        )


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
    # Agent whose Skills this group works on; see ``ToolContext.skill_agent_id``.
    skill_agent_id: str | None = None
    # Kind of the owning Run; see ``ToolContext.run_kind``.
    run_kind: RunKind | None = None
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
    tool_result_payload_registrar: ToolResultPayloadHook | None = None
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
