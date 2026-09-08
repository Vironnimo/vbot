"""Owner-bound temporary runtime Agents backed by canonical Session bindings."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from core.chat.messages import ChatMessage
from core.memory import MEMORY_PROMPT_MODE_OFF, MemoryPromptMode
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunAdmission,
    RunAdmissionBlockedError,
    RunExecutionOwner,
    RunNotFoundError,
)
from core.sessions import (
    ChatSessionManager,
    DeliveryReceipt,
    OwnedRunRecord,
    SessionAddress,
    SessionChatHistorySnapshot,
    TemporarySessionBinding,
)
from core.tools.availability import ToolAccess, normalize_tool_access
from core.utils.ids import new_id


@dataclass(frozen=True)
class TemporaryAgentConfig:
    """The protected runtime configuration for one owner-managed participant."""

    model: str
    cwd: Path
    tool_access: ToolAccess
    allowed_skills: list[str]
    tools: dict[str, Any]
    name: str
    temperature: float | None = None
    thinking_effort: str | None = None
    fallback_models: list[str] | None = None
    instructions: str = ""
    prompt_blocks: list[str] | None = None

    def __post_init__(self) -> None:
        """Validate and snapshot caller-owned mutable configuration at the boundary."""

        if not isinstance(self.model, str) or not self.model:
            raise ValueError("temporary model must be a non-empty string")
        cwd = Path(self.cwd)
        if not cwd.is_absolute():
            raise ValueError("temporary cwd must be absolute")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("temporary name must be a non-empty string")
        if not isinstance(self.instructions, str):
            raise ValueError("temporary instructions must be a string")
        if self.prompt_blocks is not None:
            if (
                not isinstance(self.prompt_blocks, list)
                or any(not isinstance(item, str) or ":" not in item for item in self.prompt_blocks)
                or len(set(self.prompt_blocks)) != len(self.prompt_blocks)
            ):
                raise ValueError("temporary prompt_blocks must contain unique block ids")
            object.__setattr__(self, "prompt_blocks", list(self.prompt_blocks))
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temporary temperature must be between 0 and 2")
        if self.thinking_effort is not None and not isinstance(self.thinking_effort, str):
            raise ValueError("temporary thinking_effort must be a string or null")
        if not all(isinstance(item, str) and item for item in self.allowed_skills):
            raise ValueError("temporary allowed_skills must contain non-empty strings")
        if self.fallback_models is not None and not all(
            isinstance(item, str) and item for item in self.fallback_models
        ):
            raise ValueError("temporary fallback_models must contain non-empty strings")
        normalized_access = normalize_tool_access(self.tool_access)
        if not isinstance(self.tools, dict):
            raise ValueError("temporary tools must be an object")
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "tool_access", normalized_access)
        object.__setattr__(self, "allowed_skills", list(self.allowed_skills))
        object.__setattr__(self, "tools", deepcopy(self.tools))
        object.__setattr__(self, "fallback_models", list(self.fallback_models or []))


@dataclass(frozen=True)
class TemporaryAgent:
    """A workspace-less RuntimeAgent resolved only from its Session binding."""

    id: str
    name: str
    model: str
    cwd: Path
    tool_access: ToolAccess
    allowed_skills: list[str]
    tools: dict[str, Any]
    fallback_models: list[str]
    instructions: str = ""
    prompt_blocks: list[str] | None = None
    workspace: str = ""
    root_project_id: str | None = None
    temperature: float | None = None
    thinking_effort: str | None = None
    memory_prompt_mode: MemoryPromptMode = MEMORY_PROMPT_MODE_OFF
    custom_system_prompt_enabled: bool = False
    current_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    compaction_policy: dict[str, Any] | None = None


class TemporaryAgentRegistry:
    """Creates and resolves participants without touching Identity Agent storage."""

    def __init__(self, sessions: ChatSessionManager) -> None:
        self._sessions = sessions

    def create(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: TemporaryAgentConfig,
        project_id: str | None = None,
    ) -> TemporarySessionBinding:
        # The store reconciles this key inside its serialized write transaction.
        # A concurrent creator can therefore win without making the same participant
        # fail merely because it generated a different unused address.
        address = SessionAddress(project_id, new_id("tmp"), new_id("ses"))
        return self._sessions.create_bound_temporary_session(
            address,
            owner_name=owner_name,
            group_id=group_id,
            participant_id=participant_id,
            config={
                "model": config.model,
                "cwd": str(config.cwd),
                "tool_access": config.tool_access.to_dict(),
                "allowed_skills": list(config.allowed_skills),
                "tools": deepcopy(config.tools),
                "name": config.name,
                "temperature": config.temperature,
                "thinking_effort": config.thinking_effort,
                "fallback_models": list(config.fallback_models or []),
                "instructions": config.instructions,
                "prompt_blocks": deepcopy(config.prompt_blocks),
            },
        )

    def resolve(self, address: SessionAddress, *, generation_id: str) -> TemporaryAgent | None:
        binding = self._sessions.temporary_binding(address)
        if binding is None or binding.generation_id != generation_id:
            return None
        config = binding.config
        try:
            return TemporaryAgent(
                id=address.agent_id,
                name=str(config["name"]),
                model=str(config["model"]),
                cwd=Path(str(config["cwd"])),
                tool_access=normalize_tool_access(config["tool_access"]),
                allowed_skills=list(config["allowed_skills"]),
                tools=dict(config["tools"]),
                fallback_models=list(config.get("fallback_models", [])),
                instructions=str(config.get("instructions", "")),
                prompt_blocks=deepcopy(config.get("prompt_blocks")),
                temperature=config.get("temperature"),
                thinking_effort=config.get("thinking_effort"),
                current_session_id=address.session_id,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("temporary Session binding configuration is invalid") from error


@dataclass(frozen=True)
class TemporaryGroupHandle:
    owner_name: str
    group_id: str
    registration_epoch: str
    epoch: str


@dataclass(frozen=True)
class TemporaryRunInput:
    kind: Literal["initial", "continuation"]
    content: str
    request_id: str


@dataclass(frozen=True)
class TemporaryRunAdmission:
    run_id: str
    status: str
    existing: bool


@dataclass(frozen=True)
class OwnedRunInspection:
    record: OwnedRunRecord
    run: Run | None


class ExecutionResources(Protocol):
    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None: ...
    def has_execution_work(self, owner: RunExecutionOwner) -> bool: ...


@dataclass
class _Group:
    epoch: str = ""
    open: bool = False
    bindings: dict[str, TemporarySessionBinding] = field(default_factory=dict)
    pending: set[asyncio.Future[None]] = field(default_factory=set)
    close_task: asyncio.Task[dict[str, Any]] | None = None
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    inputs: dict[str, TemporaryRunAdmission] = field(default_factory=dict)


class TemporaryExecutionGroups:
    """Owner-bound admission facade for temporary participant Sessions."""

    def __init__(
        self,
        registry: TemporaryAgentRegistry,
        chat: Any,
        is_current: Callable[[Any], bool],
        identity: Any,
        *,
        run_manager: ChatRunManager,
        resources: Sequence[ExecutionResources] = (),
        usage: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        validate_binding: Callable[[TemporarySessionBinding], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._chat = chat
        self._is_current = is_current
        self._identity = identity
        self._sessions = registry._sessions
        self._manager = run_manager
        self._resources = tuple(resources)
        self._usage = usage
        self._validate_binding = validate_binding
        self._groups: dict[str, _Group] = {}
        self._lifecycle = asyncio.Lock()
        self._retiring = False

    def _require_current(self) -> None:
        if self._retiring or not self._is_current(self._identity):
            raise RunAdmissionBlockedError(
                "This Session is no longer available. Check its state through its Extension."
            )

    def owns(self, owner: RunExecutionOwner) -> bool:
        state = self._groups.get(owner.group_id)
        return (
            owner.extension == self._identity.name
            and state is not None
            and state.epoch == owner.epoch
        )

    def validate(self, _address: SessionAddress, admission: RunAdmission) -> None:
        owner = admission.owner
        if owner is None:
            return
        state = self._groups.get(owner.group_id)
        binding = None if state is None else state.bindings.get(owner.participant_id)
        if (
            not self.owns(owner)
            or self._retiring
            or not self._is_current(self._identity)
            or state is None
            or not state.open
            or binding is None
            or binding.generation_id != owner.generation_id
        ):
            raise RunAdmissionBlockedError(
                "This Session is no longer available. Check its state through its Extension."
            )

    async def create(
        self,
        group_id: str,
        participant_id: str,
        config: TemporaryAgentConfig,
        *,
        project_id: str | None = None,
    ) -> TemporarySessionBinding:
        async with self._lifecycle:
            self._require_current()
            state = self._groups.setdefault(group_id, _Group())
            if state.open or state.close_task is not None and not state.close_task.done():
                raise RunAdmissionBlockedError(
                    "This Session is no longer available. Check its state through its Extension."
                )
            binding = await asyncio.to_thread(
                self._registry.create,
                owner_name=self._identity.name,
                group_id=group_id,
                participant_id=participant_id,
                config=config,
                project_id=project_id,
            )
            self._require_current()
            state.bindings[participant_id] = binding
            return binding

    async def list(
        self,
        group_id: str,
        *,
        after: str = "",
        limit: int = 100,
    ) -> list[TemporarySessionBinding]:
        self._require_current()
        bindings = await self._sessions.temporary_bindings_async(
            owner_name=self._identity.name,
            group_id=group_id,
            after=after,
            limit=limit,
        )
        self._require_current()
        self._groups.setdefault(group_id, _Group()).bindings.update(
            (binding.participant_id, binding) for binding in bindings
        )
        return bindings

    async def open_group(self, group_id: str) -> TemporaryGroupHandle:
        async with self._lifecycle:
            self._require_current()
            state = self._groups.setdefault(group_id, _Group())
            if state.close_task is not None:
                await asyncio.shield(state.close_task)
                state.close_task = None
            if not state.open:
                after = ""
                while True:
                    page = await self.list(group_id, after=after, limit=100)
                    if len(page) < 100:
                        break
                    after = page[-1].participant_id
                if not state.bindings:
                    raise RunAdmissionBlockedError(
                        "This Session is no longer available. "
                        "Check its state through its Extension."
                    )
                if self._validate_binding is not None:
                    for binding in state.bindings.values():
                        await self._validate_binding(binding)
                self._require_current()
                state.epoch = new_id("grp")
                state.open = True
            return TemporaryGroupHandle(
                self._identity.name, group_id, self._identity.epoch, state.epoch
            )

    def _checked_binding(
        self, handle: TemporaryGroupHandle, participant_id: str
    ) -> TemporarySessionBinding:
        if (
            handle.owner_name != self._identity.name
            or handle.registration_epoch != self._identity.epoch
        ):
            raise RunAdmissionBlockedError(
                "This Session is no longer available. Check its state through its Extension."
            )
        state = self._groups.get(handle.group_id)
        binding = None if state is None else state.bindings.get(participant_id)
        if binding is None:
            raise RunAdmissionBlockedError(
                "This Session is no longer available. Check its state through its Extension."
            )
        self.validate(
            binding.address,
            RunAdmission(
                owner=RunExecutionOwner(
                    handle.owner_name,
                    handle.group_id,
                    participant_id,
                    binding.generation_id,
                    handle.epoch,
                )
            ),
        )
        return binding

    async def start(
        self,
        handle: TemporaryGroupHandle,
        participant_id: str,
        input: TemporaryRunInput,
    ) -> TemporaryRunAdmission:
        binding = self._checked_binding(handle, participant_id)
        if (
            not isinstance(input, TemporaryRunInput)
            or input.kind not in {"initial", "continuation"}
            or not isinstance(input.content, str)
            or (input.kind == "initial" and not input.content.strip())
            or not isinstance(input.request_id, str)
            or not input.request_id
            or len(input.request_id) > 128
        ):
            raise ValueError("invalid temporary Run input")
        state = self._groups[handle.group_id]
        completion = asyncio.get_running_loop().create_future()
        state.pending.add(completion)
        try:
            async with state.locks.setdefault(participant_id, asyncio.Lock()):
                self._checked_binding(handle, participant_id)
                if self._validate_binding is not None:
                    await self._validate_binding(binding)
                    self._checked_binding(handle, participant_id)
                input_id = (
                    f"initial:{binding.generation_id}"
                    if input.kind == "initial"
                    else f"continuation:{binding.generation_id}:{input.request_id}"
                )
                digest = hashlib.sha256(input.content.encode("utf-8")).hexdigest()
                receipt = await self._sessions.lookup_delivery_receipt(
                    binding.address,
                    binding.generation_id,
                    handle.owner_name,
                    input_id,
                )
                self._checked_binding(handle, participant_id)
                if receipt is not None and receipt.content_hash != digest:
                    raise RunAdmissionBlockedError(
                        "This Session no longer matches this Run. "
                        "Ask the user to resume it through its Extension."
                    )
                previous = state.inputs.get(input_id)
                if previous is None:
                    after = 0
                    while True:
                        records = await self._sessions.owned_runs_async(
                            owner_name=handle.owner_name,
                            group_id=handle.group_id,
                            participant_id=participant_id,
                            after=after,
                        )
                        previous_record = next(
                            (record for record in records if record.input_id == input_id), None
                        )
                        if previous_record is not None:
                            previous = TemporaryRunAdmission(
                                previous_record.run_id,
                                previous_record.terminal_status or "interrupted",
                                True,
                            )
                            break
                        if len(records) < 100:
                            break
                        after = records[-1].record_key
                if previous is not None:
                    try:
                        status = self._manager.get(previous.run_id).status.value
                    except RunNotFoundError:
                        status = previous.status
                    return TemporaryRunAdmission(previous.run_id, status, True)
                self._checked_binding(handle, participant_id)
                if (
                    self._manager.active_run(
                        agent_id=binding.address.agent_id,
                        session_id=binding.address.session_id,
                        project_id=binding.address.project_id,
                    )
                    is not None
                ):
                    raise ActiveRunError(
                        "This Session is no longer available. "
                        "Check its state through its Extension."
                    )
                message = (
                    ChatMessage.user(input.content)
                    if input.kind == "initial"
                    else ChatMessage.note(input.content)
                )
                await self._sessions.append_messages_with_receipts_async(
                    binding.address,
                    generation_id=binding.generation_id,
                    owner_name=handle.owner_name,
                    messages=[message],
                    receipts=[(0, input_id, digest, input.kind, message.role)],
                    deduplicate_carrier=True,
                )
                self._checked_binding(handle, participant_id)
                owner = RunExecutionOwner(
                    handle.owner_name,
                    handle.group_id,
                    participant_id,
                    binding.generation_id,
                    handle.epoch,
                )
                run = await self._chat.start_temporary_run(
                    binding,
                    input.content,
                    owner=owner,
                    input_already_persisted=True,
                    input_id=input_id,
                )
                try:
                    await self._sessions.record_run_owner_async(
                        binding.address,
                        run_id=run.id,
                        owner=owner,
                        input_id=input_id,
                    )
                except BaseException:
                    await self._manager.cancel(run.id, reason="extension")
                    raise
                result = TemporaryRunAdmission(run.id, run.status.value, False)
                state.inputs[input_id] = result
                return result
        finally:
            completion.set_result(None)
            state.pending.discard(completion)

    async def delete_group(self, group_id: str) -> int:
        """Permanently remove a closed group's bound participant Sessions."""
        async with self._lifecycle:
            self._require_current()
            state = self._groups.get(group_id)
            if state is not None and state.open:
                raise ValueError("group_not_closed")
            await self.close_group(group_id)
            self._require_current()
            count = await self._sessions.delete_temporary_group(
                owner_name=self._identity.name, group_id=group_id
            )
            self._groups.pop(group_id, None)
            return count

    async def close_group(self, group_id: str, reason: str = "extension") -> dict[str, Any]:
        state = self._groups.setdefault(group_id, _Group())
        state.open = False
        if state.close_task is None:
            state.close_task = asyncio.create_task(self._drain(group_id, state, reason))
        return await asyncio.shield(state.close_task)

    async def _drain(self, group_id: str, state: _Group, reason: str) -> dict[str, Any]:
        if state.pending:
            await asyncio.gather(*state.pending)
        key = (self._identity.name, group_id, state.epoch)

        def matches(owner: RunExecutionOwner | None) -> bool:
            return owner is not None and (owner.extension, owner.group_id, owner.epoch) == key

        for address, item in self._manager.all_queued():
            if matches(item.admission.owner):
                self._manager.remove_queued(
                    address.agent_id,
                    address.session_id,
                    item.item_id,
                    project_id=address.project_id,
                )
        runs = [run for run in self._manager.active_runs() if matches(run.execution_owner)]
        await asyncio.gather(*(self._manager.cancel(run.id, reason=reason) for run in runs))
        await asyncio.gather(
            *(resource.close_execution_group(*key) for resource in self._resources)
        )
        return {"closed": True, "run_ids": [run.id for run in runs]}

    async def quiesce(self, reason: str = "extension") -> None:
        self._retiring = True
        for state in self._groups.values():
            state.open = False
        # Wait for protected creation to return before declaring the owner drained.
        async with self._lifecycle:
            group_ids = tuple(self._groups)
        await asyncio.gather(*(self.close_group(group_id, reason) for group_id in group_ids))

    async def continue_completion(
        self,
        address: SessionAddress,
        owner: RunExecutionOwner,
        content: str,
        notice_ids: tuple[str, ...],
        on_persisted: Callable[[], None],
    ) -> Run:
        """Continue exact lifetime-owned work through the normal admission boundary."""
        self.validate(address, RunAdmission(owner=owner))
        state = self._groups[owner.group_id]
        binding = state.bindings[owner.participant_id]
        request_id = hashlib.sha256("\0".join(sorted(notice_ids)).encode()).hexdigest()
        if address == binding.address:
            handle = TemporaryGroupHandle(
                owner.extension, owner.group_id, self._identity.epoch, owner.epoch
            )
            result = await self.start(
                handle,
                owner.participant_id,
                TemporaryRunInput("continuation", content, request_id),
            )
            on_persisted()
            return self._manager.get(result.run_id)
        return cast(
            Run,
            await self._chat.start_owned_continuation(
                address,
                owner,
                content,
                input_id=f"completion:{request_id}",
                input_persisted_hook=on_persisted,
            ),
        )

    def has_descendants(self, group_id: str, participant_id: str, run_id: str, epoch: str) -> bool:
        state = self._groups.get(group_id)
        binding = None if state is None else state.bindings.get(participant_id)
        if binding is None:
            return False
        owner = RunExecutionOwner(
            self._identity.name, group_id, participant_id, binding.generation_id, epoch
        )
        return (
            any(
                run.id != run_id and run.execution_owner == owner
                for run in self._manager.active_runs()
            )
            or any(item.admission.owner == owner for _, item in self._manager.all_queued())
            or any(resource.has_execution_work(owner) for resource in self._resources)
        )

    async def owned_run(self, group_id: str, run_id: str) -> OwnedRunInspection:
        self._require_current()
        after = 0
        while True:
            page = await self._sessions.owned_runs_async(
                owner_name=self._identity.name, group_id=group_id, after=after
            )
            record = next((record for record in page if record.run_id == run_id), None)
            if record is not None:
                break
            if len(page) < 100:
                raise RunNotFoundError(
                    "This Session is no longer available. Check its state through its Extension."
                )
            after = page[-1].record_key
        self._require_current()
        try:
            run = self._manager.get(run_id)
        except RunNotFoundError:
            run = None
        if run is not None and run.execution_owner != record.owner:
            run = None
        return OwnedRunInspection(record, run)

    async def inspect(
        self, group_id: str, participant_id: str, query: dict[str, Any]
    ) -> SessionChatHistorySnapshot:
        if (
            set(query) - {"limit", "before"}
            or type(query.get("limit", 50)) is not int
            or not 1 <= query.get("limit", 50) <= 100
        ):
            raise ValueError("invalid temporary history query")
        self._require_current()
        binding = await asyncio.to_thread(
            self._sessions.temporary_binding_by_participant,
            owner_name=self._identity.name,
            group_id=group_id,
            participant_id=participant_id,
        )
        if binding is None:
            raise RunNotFoundError(
                "This Session is no longer available. Check its state through its Extension."
            )

        def read() -> SessionChatHistorySnapshot:
            return self._sessions.get(binding.address).read_chat_history_snapshot(
                limit=query.get("limit", 50),
                before=query.get("before"),
            )

        result = await asyncio.to_thread(read)
        self._require_current()
        return result

    async def usage(self, group_id: str, query: dict[str, Any]) -> dict[str, Any]:
        """Read the canonical Statistics projection within this registered owner."""
        self._require_current()
        if self._usage is None:
            raise RuntimeError("Extension Statistics are unavailable")
        result = await self._usage(group_id, query)
        self._require_current()
        return result

    async def delivery_receipt(
        self, address: SessionAddress, generation_id: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        """Read one canonical receipt only within this owner's exact binding."""
        self._require_current()
        binding = await asyncio.to_thread(self._sessions.temporary_binding, address)
        self._require_current()
        if (
            binding is None
            or binding.owner_name != self._identity.name
            or binding.generation_id != generation_id
        ):
            raise RunNotFoundError(
                "This Session is no longer available. Check its state through its Extension."
            )
        result = await self._sessions.lookup_delivery_receipt(
            address, generation_id, self._identity.name, receipt_id
        )
        self._require_current()
        return result
