"""Durable ordinary Accessor input, using the shared Session store and Run Queue."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from core.chat._queued_input import QueuedChatInput
from core.chat._run_state import _RunRequest
from core.chat.content_blocks import ContentBlock, masked_content
from core.chat.errors import ChatError
from core.chat.messages import (
    ChatMessage,
    InputOrigin,
    MessageSender,
    ReplySurface,
    _display_content_preview,
    queue_content_is_editable,
)
from core.chat.model_resolution import _ensure_provider_exists, _resolve_agent_connection
from core.projects import AgentOverrides
from core.runs import QueuedRunItem, Run, RunAdmission, RunKind, RunNotFoundError
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    AccessorInputRecord,
    SessionAddress,
    WorkingProjectChoice,
)
from core.sessions.errors import SessionNotFoundError
from core.utils.ids import new_id
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.chat.chat import ChatLoop


@dataclass(frozen=True)
class AccessorChatSubmission:
    """Durable acceptance plus its optional current-process execution projection."""

    record: AccessorInputRecord
    run: Run | None = None
    queued_item: QueuedRunItem | None = None


class AccessorInputs:
    """Chat-owned serialization and recovery; Runs remains the only scheduler."""

    def __init__(self, loop: ChatLoop) -> None:
        self.loop = loop
        self._lock = asyncio.Lock()
        self._restore_lock = asyncio.Lock()
        self._restored = False

    async def lookup(self, input_id: str, request_hash: str) -> AccessorChatSubmission | None:
        record = await self.loop._dependencies.sessions.lookup_accessor_input_async(
            input_id, request_hash=request_hash
        )
        return self.project(record) if record is not None else None

    def project(self, record: AccessorInputRecord) -> AccessorChatSubmission:
        manager = self.loop._dependencies.run_manager
        run = None
        if record.live and record.run_id is not None:
            with suppress(RunNotFoundError):
                run = manager.get(record.run_id)
        queued = (
            next(
                (
                    item
                    for address, item in manager.all_queued()
                    if address == record.address and item.item_id == record.queue_id
                ),
                None,
            )
            if record.live and record.state == "queued"
            else None
        )
        return AccessorChatSubmission(record, run=run, queued_item=queued)

    async def submit(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        input_id: str,
        request_hash: str,
        session_id: str | None = None,
        new_session: bool = False,
        agent_overrides: AgentOverrides | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        ephemeral: bool = False,
        fast_mode: bool = False,
        execution: dict[str, Any] | None = None,
        input_origin: InputOrigin | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        context_note: str | ChatMessage | None = None,
        sender: MessageSender | None = None,
        expected_generation_id: str | None = None,
        expected_working_project_id: str | None = None,
        source: SessionAddress | None = None,
        expected_source_generation_id: str | None = None,
    ) -> AccessorChatSubmission:
        await self.restore()
        # Once mutation begins, cancellation waits for both durable acceptance
        # and installation of its live projection. A lost response remains retryable.
        async with self._lock:
            return await settle_before_cancelling(
                self._submit(
                    agent_id,
                    content,
                    input_id=input_id,
                    request_hash=request_hash,
                    session_id=session_id,
                    new_session=new_session,
                    agent_overrides=agent_overrides,
                    working_project_id=working_project_id,
                    ephemeral=ephemeral,
                    fast_mode=fast_mode,
                    execution=execution,
                    input_origin=input_origin,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    context_note=context_note,
                    sender=sender,
                    expected_generation_id=expected_generation_id,
                    expected_working_project_id=expected_working_project_id,
                    source=source,
                    expected_source_generation_id=expected_source_generation_id,
                )
            )

    async def _submit(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        input_id: str,
        request_hash: str,
        session_id: str | None,
        new_session: bool,
        agent_overrides: AgentOverrides | None,
        working_project_id: WorkingProjectChoice,
        ephemeral: bool,
        fast_mode: bool,
        execution: dict[str, Any] | None,
        input_origin: InputOrigin | None,
        reply_surface: ReplySurface | None,
        project_id: str | None,
        context_note: str | ChatMessage | None,
        sender: MessageSender | None,
        expected_generation_id: str | None,
        expected_working_project_id: str | None,
        source: SessionAddress | None,
        expected_source_generation_id: str | None,
    ) -> AccessorChatSubmission:
        from core.chat.chat import _NewSession, _reject_live_input, _stored_working_project

        if reply_surface is not None and reply_surface.kind not in {"webui", "tui"}:
            raise ChatError("ordinary Accessor input requires a WebUI or TUI reply surface")
        prior = await self.lookup(input_id, request_hash)
        if prior is not None:
            return prior
        async with self.loop._dependencies.run_manager.prepare_admission(
            agent_id, project_id=project_id, session_id=session_id
        ) as preparation:
            deps = self.loop._dependencies
            resolver, sessions = deps.agent_resolver, deps.sessions
            new = (
                _NewSession(
                    session_id=session_id,
                    agent_overrides=agent_overrides or AgentOverrides(),
                    working_project_id=working_project_id,
                    ephemeral=ephemeral,
                    fast_mode=fast_mode,
                    execution=execution,
                    actor="rpc",
                )
                if new_session
                else None
            )
            if new is None:
                if execution is not None:
                    raise ChatError("Computer selection can accompany only a new Session.")
                if session_id is None:
                    raise ChatError("ordinary input requires an existing or explicitly new Session")
                session = await self.loop._get_session_async(
                    agent_id, session_id, create_missing=False, project_id=project_id
                )
                if (
                    expected_generation_id is not None
                    and session.generation_id != expected_generation_id
                ):
                    raise SessionNotFoundError("Session generation changed before input acceptance")
                await self.loop._reject_owner_managed_session(project_id, agent_id, session_id)
                agent = await resolver.resolve_agent_async(
                    project_id, agent_id, session_id=session_id
                )
                working = await resolver.resolve_working_project_async(
                    project_id, agent, session_id=session_id
                )
            else:
                agent = await resolver.resolve_agent_async(
                    project_id, agent_id, new_session_overrides=new.agent_overrides
                )
                working = await resolver.resolve_working_project_async(
                    project_id, agent, requested=new.working_project_id
                )
            await preparation.bind_working_project(working)
            if expected_working_project_id is not None and working != expected_working_project_id:
                raise ChatError("The receiving Session is not in the same Working Project.")
            run_kind = (
                RunKind.SESSION_MESSAGE if sender and sender.kind == "session" else RunKind.USER
            )
            _reject_live_input(agent, run_kind)
            provider_id, _ = _resolve_agent_connection(deps, agent)
            _ensure_provider_exists(deps.providers, provider_id)
            content = masked_content(content, deps.secret_masker)
            if isinstance(context_note, str):
                # Execution and the crash/terminal fallback share the same Note
                # identity, including when the input append itself never succeeds.
                context_note = (
                    ChatMessage.note(deps.secret_masker.mask(context_note))
                    if context_note
                    else None
                )
            elif context_note is not None and isinstance(context_note.content, str):
                context_note = replace(
                    context_note, content=deps.secret_masker.mask(context_note.content)
                )
            request = _RunRequest(
                content=content,
                sender=sender,
                input_origin=input_origin,
                reply_surface=reply_surface,
                context_note=context_note,
            )
            payload = _payload(request, working)
            if new is not None:
                session = await sessions.create_async(
                    agent_id,
                    session_id=new.session_id,
                    project_id=project_id,
                    actor=new.actor,
                    metadata=new.metadata(agent, None),
                    working_project_id=_stored_working_project(project_id, working),
                )
            else:
                await sessions.run_async(resolver.capture_session_settings, session.address, agent)
            queue_id = new_id("que")

            async def accept() -> None:
                await sessions.accept_accessor_input_async(
                    session.address,
                    input_id,
                    request_hash=request_hash,
                    payload=payload,
                    queue_id=queue_id,
                    expected_generation_id=session.generation_id,
                    attachment_ids=_attachment_ids(payload),
                    fallback_input=ChatMessage.from_dict(payload["message"]),
                    fallback_notes=_fallback_notes(payload),
                    source=source,
                    expected_source_generation_id=expected_source_generation_id,
                    expected_working_project_id=expected_working_project_id,
                )

            try:
                item = await deps.run_manager.enqueue(
                    session.address,
                    QueuedChatInput(self.loop, request),
                    display_content=_display_content_preview(content),
                    editable=not request.session_message and queue_content_is_editable(content),
                    steerable=request.supports_steering,
                    steering=request.session_message,
                    admission=RunAdmission(
                        run_kind=run_kind,
                        working_project_id=working,
                        expected_session_generation_id=session.generation_id,
                        accessor_input_id=input_id,
                    ),
                    item_id=queue_id,
                    persist_input=accept,
                )
            except BaseException:
                # No accepted receipt: roll back only our still-unused new Session.
                if new is not None and await sessions.lookup_accessor_input_async(input_id) is None:
                    await preparation.release()
                    await self.loop._discard_new_session(session)
                raise
            if item.future.done() and not item.future.cancelled():
                run = item.future.result()
                try:
                    await run.wait_admitted()
                except BaseException:
                    await sessions.remove_accessor_input_async(session.address, input_id)
                    raise
            record = await sessions.lookup_accessor_input_async(input_id)
            assert record is not None
            return self.project(record)

    async def restore(self) -> None:
        async with self._restore_lock:
            if not self._restored:
                self.prepare(await self.loop._dependencies.sessions.queued_accessor_inputs_async())

    def prepare(self, records: Sequence[AccessorInputRecord]) -> None:
        if self._restored:
            return
        event_loop = asyncio.get_running_loop()
        # Decode every payload before starting any task: malformed durable data
        # fails recovery without dropping accepted work or partially admitting it.
        prepared = [
            (record, _request(record.payload, fallback_notes=record.fallback_notes))
            for record in records
        ]
        prepared.sort(key=lambda pair: not bool(pair[0].payload.get("steering")))
        inputs: list[tuple[SessionAddress, QueuedRunItem]] = []
        for record, request in prepared:
            assert request.content is not None
            inputs.append(
                (
                    record.address,
                    QueuedRunItem(
                        item_id=record.queue_id,
                        display_content=_display_content_preview(request.content),
                        executor=QueuedChatInput(self.loop, request),
                        internal=False,
                        future=event_loop.create_future(),
                        editable=not request.session_message
                        and queue_content_is_editable(request.content),
                        steerable=request.supports_steering,
                        steering=bool(record.payload.get("steering")),
                        admission=RunAdmission(
                            run_kind=RunKind.SESSION_MESSAGE
                            if request.session_message
                            else RunKind.USER,
                            working_project_id=record.payload.get("working_project_id"),
                            expected_session_generation_id=record.generation_id,
                            accessor_input_id=record.input_id,
                        ),
                        created_at=record.created_at,
                    ),
                )
            )
        self.loop._dependencies.run_manager.restore_queued_inputs(inputs)
        self._restored = True

    async def remove(self, address: SessionAddress, item_id: str) -> bool:
        manager = self.loop._dependencies.run_manager
        async with manager.queued_input_mutation():

            async def change() -> bool:
                item = self._item(address, item_id)
                if item is None or item.steering_in_flight:
                    return False
                if item.admission.accessor_input_id is not None:
                    await self.loop._dependencies.sessions.remove_accessor_input_async(
                        address, item.admission.accessor_input_id
                    )
                return manager.remove_queued(
                    address.agent_id, address.session_id, item_id, project_id=address.project_id
                )

            return await settle_before_cancelling(change())

    async def steer(self, address: SessionAddress, item_id: str) -> QueuedRunItem:
        manager = self.loop._dependencies.run_manager
        async with manager.queued_input_mutation():

            async def change() -> QueuedRunItem:
                item = self._item(address, item_id)
                if item is None:
                    raise RunNotFoundError(f"queued item not found: {item_id}")
                if not item.steerable or item.internal or item.admission.owner is not None:
                    raise ChatError("this queued input cannot steer a Run")
                if item.admission.accessor_input_id is not None:
                    await self.loop._dependencies.sessions.update_accessor_input_async(
                        address, item.admission.accessor_input_id, payload_patch={"steering": True}
                    )
                return manager.steer_queued(
                    address.agent_id, address.session_id, item_id, project_id=address.project_id
                )

            return await settle_before_cancelling(change())

    async def update(
        self,
        address: SessionAddress,
        item_id: str,
        content: str | list[ContentBlock],
        *,
        input_origin: InputOrigin | None,
        reply_surface: ReplySurface | None,
    ) -> bool:
        content = masked_content(content, self.loop._dependencies.secret_masker)
        item = self._item(address, item_id)
        if item is None:
            return False
        _, executor, preview = await self.loop.build_queue_update(
            address.agent_id,
            address.session_id,
            content,
            item,
            input_origin=input_origin,
            project_id=address.project_id,
            reply_surface=reply_surface,
        )
        assert isinstance(executor, QueuedChatInput)

        manager = self.loop._dependencies.run_manager
        async with manager.queued_input_mutation():

            async def change() -> bool:
                current = self._item(address, item_id)
                if current is not item or current.steering or not current.editable:
                    return False
                current_executor = current.executor
                if not isinstance(current_executor, QueuedChatInput):
                    raise ChatError("queued input is not an editable Chat request")
                replacement = current_executor.with_edited_content(
                    content, input_origin, reply_surface
                )
                if item.admission.accessor_input_id is not None:
                    payload = _payload(replacement.request, item.admission.working_project_id)
                    await self.loop._dependencies.sessions.update_accessor_input_async(
                        address,
                        item.admission.accessor_input_id,
                        payload_patch=payload,
                        attachment_ids=_attachment_ids(payload),
                        fallback_input=ChatMessage.from_dict(payload["message"]),
                        fallback_notes=_fallback_notes(payload),
                    )
                return manager.update_queued(
                    address.agent_id,
                    address.session_id,
                    item_id,
                    replacement,
                    preview,
                    project_id=address.project_id,
                    editable=queue_content_is_editable(content),
                )

            return await settle_before_cancelling(change())

    def _item(self, address: SessionAddress, item_id: str) -> QueuedRunItem | None:
        return next(
            (
                item
                for item in self.loop._dependencies.run_manager.list_queued(
                    address.agent_id, address.session_id, project_id=address.project_id
                )
                if item.item_id == item_id
            ),
            None,
        )


def _payload(request: _RunRequest, working_project_id: str | None) -> dict[str, Any]:
    assert request.content is not None
    note = request.context_note
    return {
        "format_version": 1,
        "message": ChatMessage.user(
            request.content, input_origin=request.input_origin, sender=request.sender
        ).to_dict(),
        "reply_surface": request.reply_surface.kind if request.reply_surface is not None else None,
        "context_note": note.to_dict() if isinstance(note, ChatMessage) else note,
        "working_project_id": working_project_id,
        "steering": request.session_message,
    }


def _request(payload: dict[str, Any], *, fallback_notes: Sequence[ChatMessage] = ()) -> _RunRequest:
    if payload.get("format_version") != 1:
        raise ChatError("unsupported accepted-input payload version")
    message = ChatMessage.from_dict(payload["message"])
    if message.role != "user":
        raise ChatError("accepted input must contain a User message")
    surface = payload.get("reply_surface")
    if surface not in (None, "webui", "tui"):
        raise ChatError("accepted input has an invalid reply surface")
    note = payload.get("context_note")
    if isinstance(note, str):
        # Older accepted inputs kept text here and the prepared Note only in
        # their canonical fallback. Reuse that identity when restoring them.
        note = next((saved for saved in fallback_notes if saved.content == note), note)
    return _RunRequest(
        content=message.content,
        sender=message.sender,
        input_origin=message.input_origin,
        reply_surface=ReplySurface(kind=surface) if surface is not None else None,
        context_note=ChatMessage.from_dict(note) if isinstance(note, dict) else note,
    )


def _attachment_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    content = payload["message"]["content"]
    return (
        tuple(
            dict.fromkeys(
                block["attachment_id"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("attachment_id"), str)
            )
        )
        if isinstance(content, list)
        else ()
    )


def _fallback_notes(payload: dict[str, Any]) -> tuple[ChatMessage, ...]:
    note = payload.get("context_note")
    if isinstance(note, str):
        return (ChatMessage.note(note),)
    if isinstance(note, dict):
        return (ChatMessage.from_dict(note),)
    return ()
