"""Execution groups of temporary Agents: owner-bound Runs, their admission and release."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
from core.chat.messages import ChatMessage
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
from core.sessions.errors import SessionNotFoundError
from core.utils.ids import new_id

if TYPE_CHECKING:
    from core.tools.scratch_areas import ScratchAreas


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
    """Group-owned resources closed after every Run of the group has settled.

    Because no owned Run can start more work by then, a resource may release
    its per-group admission state once its own drain completes.
    """

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None: ...
    def has_execution_work(self, owner: RunExecutionOwner) -> bool: ...


# Stops the terminals and commands of a Session that is about to be removed.
SessionWorkCloser = Callable[[SessionAddress], Awaitable[None]]


async def release_temporary_group(
    sessions: ChatSessionManager,
    run_manager: ChatRunManager,
    owner_name: str,
    group_id: str,
    *,
    delete: bool,
    close_session_work: SessionWorkCloser | None,
    scratch_areas: ScratchAreas | None,
    archive_reason: str = "extension",
) -> int:
    """Archive or delete one owner group's participant Sessions; return how many.

    Like an ordinary Session removal, it first closes the Terminal Sessions and
    commands of each live participant, all under one no-Run boundary. Raises
    :class:`RunAdmissionBlockedError` while any of them has active, queued or
    guarded work; nothing changes then. Archiving makes them one archive entry
    that records ``archive_reason``: ``extension`` when the owner asked,
    ``extension_removed`` when vBot cleans up after a removed Extension.
    Deleting also removes archived ones. Root Scratch Areas are captured before
    their live Sessions disappear and removed only after the mutation succeeds.
    """
    addresses: list[SessionAddress] = []
    after = ""
    while True:
        page = await sessions.temporary_bindings_async(
            owner_name=owner_name, group_id=group_id, after=after, limit=1000
        )
        addresses.extend(binding.address for binding in page)
        if len(page) < 1000:
            break
        after = page[-1].participant_id
    async with AsyncExitStack() as boundary:
        if addresses:
            await boundary.enter_async_context(run_manager.session_admission_guard(*addresses))
        if close_session_work is not None:
            for address in addresses:
                await close_session_work(address)
        scratch_roots = (
            await scratch_areas.roots_for_archive(addresses) if scratch_areas is not None else ()
        )
        if delete:
            count = await sessions.delete_temporary_group(owner_name=owner_name, group_id=group_id)
        elif not addresses:
            return 0
        else:
            count = await sessions.archive_temporary_group(
                owner_name=owner_name, group_id=group_id, reason=archive_reason
            )
        if scratch_areas is not None:
            await scratch_areas.remove_areas(scratch_roots)
        return count


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
        title: Callable[[str, str], Awaitable[str | None]] | None = None,
        close_session_work: SessionWorkCloser | None = None,
        scratch_areas: ScratchAreas | None = None,
    ) -> None:
        self._registry = registry
        self._chat = chat
        self._is_current = is_current
        self._identity = identity
        self._sessions = registry._sessions
        self._manager = run_manager
        self._resources = tuple(resources)
        self._usage = usage
        self._title = title
        self._validate_binding = validate_binding
        self._close_session_work = close_session_work
        self._scratch_areas = scratch_areas
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
            binding = await self._sessions.run_async(
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
                    previous_record = await self._sessions.owned_run_by_input_async(
                        binding.address, input_id
                    )
                    if previous_record is not None and (
                        previous_record.owner.extension,
                        previous_record.owner.group_id,
                        previous_record.owner.participant_id,
                    ) == (handle.owner_name, handle.group_id, participant_id):
                        previous = TemporaryRunAdmission(
                            previous_record.run_id,
                            previous_record.terminal_status or "interrupted",
                            True,
                        )
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
                    ChatMessage.user(self._chat.mask_user_input(input.content))
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
                    # The owner learns the Run only once its admission, which
                    # records this owner and input, has committed.
                    await run.wait_admitted()
                except BaseException:
                    await self._manager.cancel(
                        run.id, reason="extension", initiator=f"extension:{self._identity.name}"
                    )
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
            count = await release_temporary_group(
                self._sessions,
                self._manager,
                self._identity.name,
                group_id,
                delete=True,
                close_session_work=self._close_session_work,
                scratch_areas=self._scratch_areas,
            )
            self._groups.pop(group_id, None)
            return count

    async def archive_group(self, group_id: str) -> int:
        """Archive a closed group's live participant Sessions and keep their history.

        Archived participants leave this owner's listings and can no longer be
        opened or re-created; :meth:`delete_group` still removes them.
        """
        async with self._lifecycle:
            self._require_current()
            state = self._groups.get(group_id)
            if state is not None and state.open:
                raise ValueError("group_not_closed")
            await self.close_group(group_id)
            self._require_current()
            count = await release_temporary_group(
                self._sessions,
                self._manager,
                self._identity.name,
                group_id,
                delete=False,
                close_session_work=self._close_session_work,
                scratch_areas=self._scratch_areas,
            )
            self._groups.pop(group_id, None)
            return count

    async def groups(self, *, after: str = "", limit: int = 100) -> builtins.list[str]:
        """Page the ids of this owner's groups that still have live participant Sessions."""
        self._require_current()
        group_ids = await self._sessions.temporary_groups_async(
            owner_name=self._identity.name, after=after, limit=limit
        )
        self._require_current()
        return group_ids

    async def close_group(self, group_id: str, reason: str = "extension") -> dict[str, Any]:
        state = self._groups.setdefault(group_id, _Group())
        state.open = False
        if state.close_task is None:
            state.close_task = asyncio.create_task(
                self._drain(group_id, state, reason),
                name=f"extension-group-close:{self._identity.name}",
            )
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
        initiator = f"extension:{self._identity.name}"
        await asyncio.gather(
            *(self._manager.cancel(run.id, reason=reason, initiator=initiator) for run in runs)
        )
        # Owned Runs are terminal here; resources rely on this ordering.
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
        *,
        expected_session_generation_id: str | None = None,
    ) -> Run:
        """Continue exact lifetime-owned work through the normal admission boundary."""
        self.validate(address, RunAdmission(owner=owner))
        state = self._groups[owner.group_id]
        binding = state.bindings[owner.participant_id]
        request_id = hashlib.sha256("\0".join(sorted(notice_ids)).encode()).hexdigest()
        if address == binding.address:
            if expected_session_generation_id not in (None, binding.generation_id):
                raise SessionNotFoundError("The completion's Session was replaced")
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
                expected_session_generation_id=expected_session_generation_id,
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
        inspection = (await self.owned_runs(group_id, (run_id,))).get(run_id)
        if inspection is None:
            raise RunNotFoundError(
                "This Session is no longer available. Check its state through its Extension."
            )
        return inspection

    async def owned_runs(
        self, group_id: str, run_ids: Sequence[str]
    ) -> dict[str, OwnedRunInspection]:
        """Inspect exact Runs of one group with one indexed read.

        Ids this owner never recorded in the group are absent from the result.
        """
        self._require_current()
        records = await self._sessions.owned_runs_by_id_async(
            owner_name=self._identity.name, group_id=group_id, run_ids=run_ids
        )
        self._require_current()
        inspections: dict[str, OwnedRunInspection] = {}
        for run_id, record in records.items():
            try:
                run: Run | None = self._manager.get(run_id)
            except RunNotFoundError:
                run = None
            if run is not None and run.execution_owner != record.owner:
                run = None
            inspections[run_id] = OwnedRunInspection(record, run)
        return inspections

    async def inspect(
        self, group_id: str, participant_id: str, query: dict[str, Any]
    ) -> SessionChatHistorySnapshot:
        if (
            set(query) - {"limit", "before", "after"}
            or type(query.get("limit", 50)) is not int
            or not 1 <= query.get("limit", 50) <= 100
        ):
            raise ValueError("invalid temporary history query")
        self._require_current()
        binding = await self._sessions.run_async(
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
                after=query.get("after"),
                excluded_roles=("note", "history_edit"),
                complete_run_segment=True,
                complete_runs=True,
            )

        result = await self._sessions.run_async(read)
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

    async def title_group(self, group_id: str, source_text: str) -> str | None:
        """Name this group from its originating request and return the stored title.

        A local title is stored immediately; the shared title generator may
        replace it before this returns. Group titles are display metadata for
        this owner's page and for Statistics, which keeps showing them after
        the owner is disabled. They never authorize access.
        """
        self._require_current()
        if self._title is None:
            raise RuntimeError("Extension group titles are unavailable")
        return await self._title(group_id, source_text)

    async def group_titles(self, group_ids: Sequence[str]) -> dict[str, str]:
        """Read stored titles of this owner's groups, keyed by group id."""
        self._require_current()
        return await self._sessions.temporary_group_titles_async(
            owner_name=self._identity.name, group_ids=group_ids
        )

    async def delivery_receipt(
        self, address: SessionAddress, generation_id: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        """Read one canonical receipt only within this owner's exact binding."""
        self._require_current()
        binding = await self._sessions.run_async(self._sessions.temporary_binding, address)
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
