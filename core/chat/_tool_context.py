"""Run-local Tool authority, staged payloads and result acknowledgements."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import default_workspace_dir
from core.chat.messages import JsonObject, ReplySurface
from core.extensions import ExtensionRegistry
from core.permissions import PermissionBoundary
from core.runs import Run
from core.sessions import ChatSession, ToolResultFacts, ToolResultPayload
from core.tools import (
    ChangeTracker,
    ToolContract,
    ToolNotFoundError,
    ToolRegistry,
    ToolResultPersistedCallback,
    builtin_payload_owner,
    edit_tool_siblings,
)
from core.tools.availability import (
    resolve_tool_access,
)
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME
from core.utils.ids import new_id
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.skills.skills import SkillRegistry


_LOGGER = get_logger("chat")


@dataclass(frozen=True)
class ToolDispatchContext:
    """Run-local Tool execution inputs resolved by the Chat loop."""

    registry: ToolRegistry
    extension_registry: ExtensionRegistry | None
    agent: Any
    session: ChatSession
    run: Run
    vbot_root: Path
    data_root: Path
    # The Run's Permission Boundary, which every Tool Call checks its accesses
    # against. Required: a dispatch that forgets it must not run unrestricted.
    permissions: PermissionBoundary
    project_cwd: Path | None = None
    project_id: str | None = None
    skill_project_id: str | None = None
    skill_registry: SkillRegistry | None = None
    reply_surface: ReplySurface | None = None
    subagent_session: bool = False
    tool_restriction: Sequence[str] | None = None
    tool_denial_resolver: Callable[[str], str | None] | None = None
    base_allowed_tools: Sequence[str] | None = None
    session_tool_grants: Sequence[str] = ()
    tool_contracts: Mapping[str, ToolContract] = field(default_factory=dict)
    # Tools announced as removed in this prompt epoch; calls to them fail with
    # ``tool_removed`` before any hook or handler runs.
    removed_tool_names: Collection[str] = frozenset()
    # On-demand Tools a ``load_tools`` call may load: registry name -> the
    # definition it returns (``RequestState.loadable_tools``).
    loadable_tools: Mapping[str, JsonObject] = field(default_factory=dict)
    # On-demand Tools the Model was told it can load that cannot be loaded now
    # (``RequestState.unloadable_tools``).
    unloadable_tools: Collection[str] = frozenset()
    change_tracker: ChangeTracker | None = None
    allow_owned_effects: bool = False
    _result_persisted_callbacks: dict[str, list[ToolResultPersistedCallback]] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _delivery_receipts: list[tuple[str, str, str, str]] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    _turn_end_requested: bool = field(default=False, init=False, repr=False, compare=False)
    _result_payloads: dict[str, list[ToolResultPayload]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _closed_payload_calls: set[str] = field(
        default_factory=set, init=False, repr=False, compare=False
    )
    _tool_loads: list[tuple[str, tuple[str, ...]]] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    _worked_paths: list[tuple[str, tuple[Path, ...]]] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    _invocation_keys: dict[str, int] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    async def record_tool_started(self, call_id: str, name: str, started_at: str) -> None:
        """Admit a canonical invocation before publishing or running its call.

        Standalone dispatch helpers without a bound persisted Run still expose
        their process-local lifecycle; production Chat and ExternalRun bind one.
        """
        if self.session.run_id is not None:
            key = await self.session.start_tool_invocation_async(
                call_id=call_id, name=name, started_at=started_at
            )
            self._invocation_keys[call_id] = key

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

    def register_delivery_receipt(self, tool_call_id: str, receipt: tuple[str, str, str]) -> None:
        receipt_id, content_hash, effect_kind = receipt
        self._delivery_receipts.append((tool_call_id, receipt_id, content_hash, effect_kind))

    def request_turn_end(self, _tool_call_id: str) -> None:
        object.__setattr__(self, "_turn_end_requested", True)

    def stage_result_payload(self, tool_call_id: str, tool_name: str, payload: Any) -> str:
        """Keep one payload for this call's Tool Result; persisted with that result.

        The owner is the Extension that registered *tool_name*, or
        ``builtin_payload_owner(tool_name)`` for a built-in Tool. The payload is
        serialized now, so later changes to the handler's object have no effect.
        """
        if tool_call_id in self._closed_payload_calls:
            raise RuntimeError("Result payloads can be attached only while the Tool call runs")
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError:
            raise RuntimeError(f"Result payloads need a registered Tool: {tool_name}") from None
        owner_name = tool.extension or builtin_payload_owner(tool_name)
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        payload_id = new_id("res")
        self._result_payloads.setdefault(tool_call_id, []).append(
            ToolResultPayload(payload_id, owner_name, payload_json)
        )
        return payload_id

    def discard_result_payloads(self, tool_call_id: str) -> None:
        """Drop the payloads of a call whose handler did not produce its result."""
        self._result_payloads.pop(tool_call_id, None)

    def close_result_payloads(self, tool_call_id: str, *, keep: bool) -> None:
        """Stop accepting payloads for a finished call; drop them unless *keep*."""
        self._closed_payload_calls.add(tool_call_id)
        if not keep:
            self.discard_result_payloads(tool_call_id)

    def with_result_payloads(
        self, facts: Mapping[str, ToolResultFacts]
    ) -> dict[str, ToolResultFacts]:
        """Return *facts* carrying each call's kept payloads, for one atomic append."""
        return {
            call_id: replace(
                fact,
                payloads=tuple(self._result_payloads.get(call_id, fact.payloads)),
                invocation_key=self._invocation_keys.get(call_id, fact.invocation_key),
            )
            for call_id, fact in facts.items()
        }

    def record_tool_load(self, tool_call_id: str, names: Sequence[str]) -> None:
        """Keep the Tools a successful ``load_tools`` call loaded, for its Result's batch."""
        self._tool_loads.append((tool_call_id, tuple(names)))

    def record_worked_paths(self, tool_call_id: str, paths: Sequence[Path]) -> None:
        """Keep the paths a finished call worked in, for its Result's batch."""
        self._worked_paths.append((tool_call_id, tuple(paths)))

    @property
    def worked_paths(self) -> tuple[tuple[str, tuple[Path, ...]], ...]:
        """``(tool_call_id, absolute paths)`` of each call that recorded paths."""
        return tuple(self._worked_paths)

    @property
    def tool_loads(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """``(tool_call_id, registry names)`` of each successful ``load_tools`` call."""
        return tuple(self._tool_loads)

    @property
    def delivery_receipts(self) -> tuple[tuple[str, str, str, str], ...]:
        return tuple(self._delivery_receipts)

    @property
    def turn_end_requested(self) -> bool:
        return self._turn_end_requested


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
        project_memory_enabled=getattr(agent, "project_memory_enabled", False),
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

    Without a restriction this is ``base_allowed_tools``, the Tools the Model was
    told about, else ``_runtime_allowed_tools``. With one, the run may dispatch
    only tools present in **both** the effective allowlist and the restriction —
    an intersection, so a tool named in the restriction but not effectively
    allowed stays denied. A ``None``/``["*"]`` effective allowlist is expanded to
    every registered normal tool name (exactly how ``ToolRegistry.list_tools``
    reads it) before the intersection, so the restriction narrows a wildcard
    agent too.

    This is enforcement-only: it feeds ``ToolExecutionConfig.allowed_tools`` (the
    dispatch gate) and never the provider tool definitions or the system prompt,
    so a restricted run keeps a byte-identical prompt prefix (the prompt-cache
    invariant). A restricted-out call fails through the existing
    ``ToolNotAllowedError`` → ``tool_not_allowed`` path — no new denial code.

    The file edit Tools (``apply_patch``, ``edit``, ``write``) count as one: a
    route offers one dialect of them, and a call to a sibling it did not offer
    runs as that Tool when the Agent's Tool policy allows it. A restriction that
    names one of them admits all three. ``load_tools`` stays allowed under a
    restriction: it grants nothing and loads only Tools the restriction allows.
    """
    if base_allowed_tools is None:
        effective = list(
            _runtime_allowed_tools(
                agent,
                tool_registry,
                session_tool_grants=session_tool_grants,
            )
        )
    else:
        effective = list(base_allowed_tools)
        if siblings := edit_tool_siblings(effective):
            permitted = set(
                _runtime_allowed_tools(
                    agent,
                    tool_registry,
                    session_tool_grants=session_tool_grants,
                )
            )
            effective.extend(name for name in siblings if name in permitted)
    if tool_restriction is None:
        return effective
    restriction = {*tool_restriction, *edit_tool_siblings(tool_restriction)}
    allowed = [
        tool.name for tool in tool_registry.list_tools(effective) if tool.name in restriction
    ]
    if LOAD_TOOLS_TOOL_NAME in effective:
        allowed.append(LOAD_TOOLS_TOOL_NAME)
    return allowed


def _agent_workspace(agent: Any, data_root: Path) -> Path:
    workspace = getattr(agent, "workspace", None)
    if workspace is not None:
        return Path(workspace)

    return default_workspace_dir(data_root, agent.id)


def _resolve_tool_cwd(project_cwd: Path | None, workspace: Path) -> Path:
    """Choose the tool working directory: project cwd when set, else workspace.

    Chat supplies the admitted working Project's cwd for Project Agents and
    Identity Sessions working in a Project. Without a working Project, relative
    file/shell paths resolve against the Identity Agent's Workspace.
    """
    return project_cwd if project_cwd is not None else workspace
