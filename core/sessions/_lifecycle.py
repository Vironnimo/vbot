"""Session creation, address transitions, working Project choice and title mutations."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions._archive_types import ArchiveEntryRef
from core.sessions._io import _SessionWriteLock
from core.sessions._metadata import (
    _normalize_session_title,
    _validate_agent_id,
    _validate_session_id,
)
from core.sessions._types import (
    AGENT_DEFAULT_PROJECT,
    SESSION_AUTO_TITLE_INITIALIZED_KEY,
    SESSION_AUTO_TITLE_KEY,
    SESSION_TITLE_KEY,
    JsonObject,
    SessionAddress,
    TemporarySessionBinding,
    WorkingProjectChoice,
)
from core.sessions.session import ChatSession
from core.sessions.store import SessionStore
from core.settings import is_valid_project_id
from core.utils.logging import get_logger

_LOGGER = get_logger("sessions")


class SessionLifecycle:
    """Apply Session lifecycle policy around the canonical transactional store."""

    def __init__(
        self,
        store: SessionStore,
        write_lock: Callable[[SessionAddress], _SessionWriteLock],
        notify_archive_changed: Callable[[], None],
    ) -> None:
        self._store = store
        self.write_lock = write_lock
        self._notify_archive_changed = notify_archive_changed
        self.agent_default_project: Callable[[str], str | None] | None = None
        self.title_changed_callbacks: list[Callable[[SessionAddress], None]] = []

    def _new_working_project(
        self, address: SessionAddress, choice: WorkingProjectChoice
    ) -> str | None:
        """Return the working Project a new Session at *address* stores for *choice*.

        A Project Session works in its address Project and stores none; the
        Agent's default Project is read only for a Session of an Identity Agent.
        """
        if address.project_id is not None:
            if choice is not AGENT_DEFAULT_PROJECT and choice is not None:
                raise ChatSessionError("A Project Session works in its own Project")
            return None
        if choice is AGENT_DEFAULT_PROJECT:
            provider = self.agent_default_project
            return None if provider is None else provider(address.agent_id)
        if choice is not None and not is_valid_project_id(choice):
            raise ChatSessionError("invalid working project id")
        return choice

    def create(
        self,
        agent_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
        *,
        actor: str | None = None,
        run_kind: RunKind | None = None,
        metadata: JsonObject | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> ChatSession:
        """Create a Session.

        ``actor`` names who asked for it (``rpc``, ``command``): that creation is a
        control-plane change and logs at INFO. A Session created as a side effect of
        other work (a triggered Run, a Sub-Agent) passes none and logs at DEBUG.
        ``run_kind`` labels the new Session and ``metadata`` sets metadata facade
        values (a title, open keys) in the same write, so no reader ever sees the
        Session without them (a Librarian pass Session and its binding).

        ``working_project_id`` is the Project a Session of an Identity Agent works
        in for its whole life: a Project id, ``None`` for the Agent's Workspace, or
        by default the Agent's default Project (:meth:`set_agent_default_project`).
        The caller checks that a named Project exists. A Project Session works in
        its address Project and accepts no other.
        """
        _validate_agent_id(agent_id)
        if project_id is not None and not is_valid_project_id(project_id):
            raise ChatSessionError("invalid project id")
        if session_id is not None:
            _validate_session_id(session_id)
        if run_kind is not None and not isinstance(run_kind, RunKind):
            raise ChatSessionError("run kind must be a RunKind")
        requested = SessionAddress(project_id, agent_id, session_id or "")
        working_project = self._new_working_project(requested, working_project_id)
        address, generation_id = self._store.create(
            requested,
            generate_id=session_id is None,
            run_kind=None if run_kind is None else run_kind.value,
            metadata=metadata,
            working_project_id=working_project,
        )
        _LOGGER.log(
            logging.INFO if actor is not None else logging.DEBUG,
            "Session created (agent=%s%s session=%s%s actor=%s)",
            address.agent_id,
            f" project={address.project_id}" if address.project_id else "",
            address.session_id,
            f" working_project={working_project}" if working_project else "",
            actor or "internal",
        )
        return ChatSession(self._store, address, generation_id=generation_id)

    def get(self, address: SessionAddress) -> ChatSession:
        """Resolve the live generation that later Run admission must still match."""
        _validate_session_id(address.session_id)
        generation_id = self._store.require_live(address)
        return ChatSession(self._store, address, generation_id=generation_id)

    def get_or_create(self, address: SessionAddress) -> ChatSession:
        _validate_creatable_address(address)
        self._store.ensure_live(
            address, lambda: self._new_working_project(address, AGENT_DEFAULT_PROJECT)
        )
        return ChatSession(self._store, address)

    def ensure_metadata(
        self,
        address: SessionAddress,
        mutation: Callable[[JsonObject], None],
        *,
        create_missing: bool = False,
    ) -> tuple[JsonObject, JsonObject]:
        if create_missing:
            _validate_creatable_address(address)
        return self._store.ensure_metadata(
            address,
            mutation,
            create_missing=create_missing,
            new_working_project=lambda: self._new_working_project(address, AGENT_DEFAULT_PROJECT),
        )

    async def move(self, source: SessionAddress, target: SessionAddress) -> ChatSession:
        """Give a Session a new address; history, forks and relations stay attached.

        A move into another scope leaves the Agent-bound prompt state behind and
        starts a new prompt-cache affinity. The Session keeps working where it
        worked: moved into an Identity scope it keeps its working Project (a
        Project Session takes its address Project along), and moved into a
        Project scope it works in that Project.
        """
        _validate_session_id(source.session_id)
        _validate_creatable_address(target)
        async with self.write_lock(source):
            await self._store.run_async(self._store.move, source, target)
        return ChatSession(self._store, target)

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        title: str | None = None,
        run_kind: RunKind | None = None,
    ) -> ChatSession:
        """Fork a Session under a fresh id in one write.

        The fork shares the source's current history up to its settled end
        without copying it. It belongs to ``target_agent_id`` (default: the
        source's Agent) in ``target_project_id`` (``None``: outside any
        Project). Channel, Sub-Agent and reflection bindings stay behind;
        ``title`` and ``run_kind`` label the fork instead. The fork works where
        the source works, by the scope rules of :meth:`move`.
        """
        _validate_session_id(source.session_id)
        agent_id = target_agent_id or source.agent_id
        _validate_agent_id(agent_id)
        if target_project_id is not None and not is_valid_project_id(target_project_id):
            raise ChatSessionError("invalid project id")
        if run_kind is not None and not isinstance(run_kind, RunKind):
            raise ChatSessionError("run kind must be a RunKind")
        normalized_title = None if title is None else _normalize_session_title(title) or ""
        same_scope = agent_id == source.agent_id and target_project_id == source.project_id
        async with self.write_lock(source):
            target = await self._store.run_async(
                lambda: self._store.fork(
                    source,
                    SessionAddress(target_project_id, agent_id, ""),
                    title=normalized_title,
                    run_kind=None if run_kind is None else run_kind.value,
                    allow_owner_managed_source=target_agent_id is not None and not same_scope,
                )
            )
        if title is not None:
            self._notify_callbacks("title_changed", self.title_changed_callbacks, target)
        return ChatSession(self._store, target)

    async def archive(self, address: SessionAddress) -> ArchiveEntryRef:
        """Archive one live Session as its own ``session`` archive entry.

        Its live review forks join the entry, except one with a running Run.
        An owner-managed Session refuses with a ``ChatSessionError``.
        """
        async with self.write_lock(address):
            entry = await self._store.run_async(self._store.archive, address)
        self._notify_archive_changed()
        return entry

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        normalized = _normalize_session_title(title)
        previous_metadata, _updated = self._store.mutate_metadata(
            address, lambda metadata: _set_title(metadata, normalized)
        )
        previous = previous_metadata.get(SESSION_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks("title_changed", self.title_changed_callbacks, address)
        return normalized

    def set_auto_title(
        self,
        address: SessionAddress,
        title: str,
        *,
        initialized: bool = True,
        expected_generation_id: str | None = None,
        expected_first_user_sequence: int | None = None,
    ) -> str | None:
        """Store the automatic title; with *expected_generation_id*, only on that generation.

        Raises ``SessionNotFoundError`` when the Session is missing, another
        generation holds its address, or its guarded first User entry changed.
        """
        normalized = _normalize_session_title(title)

        def update(metadata: JsonObject) -> None:
            if normalized is None:
                metadata.pop(SESSION_AUTO_TITLE_KEY, None)
            else:
                metadata[SESSION_AUTO_TITLE_KEY] = normalized
            if initialized:
                metadata[SESSION_AUTO_TITLE_INITIALIZED_KEY] = True

        previous_metadata, _updated = self._store.mutate_metadata(
            address,
            update,
            expected_generation_id=expected_generation_id,
            expected_first_user_sequence=expected_first_user_sequence,
        )
        previous = previous_metadata.get(SESSION_AUTO_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks("title_changed", self.title_changed_callbacks, address)
        return normalized

    def mark_auto_title_initialized(self, address: SessionAddress) -> None:
        self._store.mutate_metadata(
            address,
            lambda metadata: metadata.__setitem__(SESSION_AUTO_TITLE_INITIALIZED_KEY, True),
        )

    @staticmethod
    def _notify_callbacks(
        event: str,
        callbacks: Sequence[Callable[..., None]],
        address: SessionAddress,
        *args: Any,
    ) -> None:
        for callback in list(callbacks):
            try:
                callback(address, *args)
            except Exception:
                _LOGGER.exception(
                    "Session callback failed (event=%s agent=%s%s session=%s)",
                    event,
                    address.agent_id,
                    f" project={address.project_id}" if address.project_id else "",
                    address.session_id,
                )

    def create_bound_temporary_session(
        self,
        address: SessionAddress,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: JsonObject,
    ) -> TemporarySessionBinding:
        generation_id = self._store.execution.create_bound_temporary_session(
            address,
            owner_name=owner_name,
            group_id=group_id,
            participant_id=participant_id,
            config=config,
        )
        binding = self._store.execution.temporary_binding_by_participant(
            owner_name=owner_name, group_id=group_id, participant_id=participant_id
        )
        if binding is None or binding.generation_id != generation_id:
            raise ChatSessionError("temporary Session binding was not retained")
        return binding

    async def delete_temporary_group(self, *, owner_name: str, group_id: str) -> int:
        """Delete bound participant Sessions after their owner has drained execution."""
        deleted = await self._store.run_async(
            lambda: self._store.execution.delete_temporary_group(
                owner_name=owner_name, group_id=group_id
            )
        )
        if deleted:
            _LOGGER.info(
                "Temporary Sessions deleted (owner=%s group=%s sessions=%d)",
                owner_name,
                group_id,
                deleted,
            )
            self._notify_archive_changed()
        return deleted

    async def archive_temporary_group(self, *, owner_name: str, group_id: str, reason: str) -> int:
        """Archive bound participant Sessions after their owner has drained execution.

        They become one ``owner_group`` archive entry; ``reason`` records why
        (``extension`` when the owner asked, ``extension_removed`` when vBot
        cleaned up after a removed Extension). Their bindings stay, so the
        Sessions remain owner-managed provenance: usage keeps its attribution
        and a later group delete still removes them.
        """
        archived, entry = await self._store.run_async(
            lambda: self._store.execution.archive_temporary_group(
                owner_name=owner_name, group_id=group_id, reason=reason
            )
        )
        if entry is not None:
            _LOGGER.info(
                "Temporary Sessions archived (owner=%s group=%s sessions=%d entry=%s)",
                owner_name,
                group_id,
                archived,
                entry.entry_id,
            )
            self._notify_archive_changed()
        return archived


def _validate_creatable_address(address: SessionAddress) -> None:
    _validate_agent_id(address.agent_id)
    _validate_session_id(address.session_id)
    if address.project_id is not None and not is_valid_project_id(address.project_id):
        raise ChatSessionError("invalid project id")


def _set_title(metadata: JsonObject, normalized: str | None) -> None:
    if normalized is None:
        metadata.pop(SESSION_TITLE_KEY, None)
    else:
        metadata[SESSION_TITLE_KEY] = normalized
