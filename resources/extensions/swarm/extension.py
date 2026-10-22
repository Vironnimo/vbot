"""Swarm coordination through owner-bound Extension capabilities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from core.agents.temporary_execution import TemporaryRunInput
from core.chat import CommandFeedback, CommandNavigation, CommandOutcome, ExtensionCommandContext
from core.extensions import (
    ExtensionAPI,
    PreparedSessionDelivery,
    SessionRequestContext,
    ToolBatchDecision,
)
from core.extensions.operations import ExtensionHost
from core.runs import RunAdmission
from core.sessions import SessionAddress, TemporarySessionBinding
from core.tools import ToolContext, tool_failure, tool_success
from core.tools.contracts import compile_tool_contract
from core.utils.ids import new_id
from core.utils.logging import get_logger

from ._board_tool import BoardCall, prepare_board_call
from ._board_view import (
    Roster,
    automatic_delivery,
    call_text,
    delivery_text,
    messages_text,
    resolve_recipients,
    status_data,
    unknown_recipients_message,
    with_notes,
)
from ._change_pacing import ChangePacing
from ._extension_values import (
    AgentCallError,
    Json,
    _clamped_limit,
    _failure,
    _inbox_available,
    _initial_message,
    _participant_config,
    _profile_cwd,
    _reminder,
    _swarm_command_argument,
    _validate_profile_catalog,
    _validate_state,
)
from ._management import SwarmManagement
from ._registration import register as register
from ._tool_calls import normalize_board, normalize_inbox, normalize_state, normalize_wiki
from ._wake_pacing import WakePacing
from ._wiki_tool import WikiCall
from .agent_text import (
    BOARD_PARAMETERS,
    EMPTY_INBOX,
    INBOX_MORE,
    INBOX_PARAMETERS,
    STATE_LIMIT,
    STATE_MORE,
    STATE_PARAMETERS,
)
from .store import DATABASE_NAME, SCHEMA_SQL, SwarmStore, SwarmStoreError
from .wiki_text import WIKI_PARAMETERS

__all__ = ["Json", "SwarmExtension", "register"]

_LOGGER = get_logger("extensions.swarm")


_TOOL_PARAMETERS = (
    ("swarm_board", BOARD_PARAMETERS, None),
    ("swarm_inbox", INBOX_PARAMETERS, "receive"),
    ("swarm_state", STATE_PARAMETERS, "status"),
    ("swarm_wiki", WIKI_PARAMETERS, None),
)
# Actions that only read; with swarm_inbox and swarm_state they are not acting
# for wake pacing.
_BOARD_READS = frozenset({"list", "read"})
_WIKI_READS = frozenset({"list", "read", "history"})
# Accepted without being offered: a Participant's own identity copied from its
# reminders, the single action of the inbox and state Tools, explicit Board
# recipients, which "@" before names in the post text replaces, the offset
# other harnesses page with, which ``before`` replaces, and a Wiki update's
# list of passage edits, the form of the file edit Tool.
UNADVERTISED_PARAMETERS: dict[str, Json] = {
    name: {
        **({"action": {"type": "string", "enum": [action]}} if action else {}),
        **{field: {"type": "string"} for field in ("swarm_id", "participant_id", "sender")},
        # Agents address participants with @Name; explicit recipients and reply_to stay
        # accepted for callers that send them.
        **(
            {
                "recipients": {"type": "array", "items": {"type": "string"}},
                "reply_to": {"type": "string"},
                "offset": {"type": "integer"},
            }
            if name == "swarm_board"
            else {}
        ),
        **({"edits": {"type": "array"}} if name == "swarm_wiki" else {}),
    }
    for name, _parameters, action in _TOOL_PARAMETERS
}
_RUNTIME_CONTRACTS = {
    name: compile_tool_contract(
        name=name,
        input_schema={
            **parameters,
            "properties": {**parameters["properties"], **UNADVERTISED_PARAMETERS[name]},
        },
        require_closed_input=False,
    )
    for name, parameters, _action in _TOOL_PARAMETERS
}


_NORMALIZERS = {
    "swarm_board": normalize_board,
    "swarm_inbox": normalize_inbox,
    "swarm_state": normalize_state,
    "swarm_wiki": normalize_wiki,
}


def _normalize_arguments(name: str, arguments: Json) -> Any:
    return _NORMALIZERS[name](_RUNTIME_CONTRACTS[name], arguments)


class SwarmExtension:
    """Own registration state, bound participants, and complete Board operations."""

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.store: SwarmStore | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._title_tasks: set[asyncio.Task[None]] = set()
        self._wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._wake_dirty: set[str] = set()
        # The lifecycle Run this registration last stored per (Swarm, participant).
        # Every write of a participant's lifecycle Run goes through
        # ``_record_run_started`` or wake admission, which keep it equal to the
        # stored value; an unknown participant, as after a restart, asks the Store.
        self._lifecycle_runs: dict[tuple[str, str], str] = {}
        self._pacing = WakePacing(self._enqueue_wakes)
        self._changes = ChangePacing(self._publish_change)
        self.management = SwarmManagement(self)

    async def start(self, host: ExtensionHost) -> None:
        if host.open_database is None or host.temporary_agents is None:
            raise RuntimeError("Swarm requires the owner-bound Extension host")
        self.host = host
        groups = host.temporary_agents

        async def receipt(address: SessionAddress, generation: str, owner: str, receipt_id: str):
            if owner != "swarm":
                return None
            return await groups.delivery_receipt(address, generation, receipt_id)

        database = await host.open_database(DATABASE_NAME, SCHEMA_SQL)
        self.store = SwarmStore(database, lookup_delivery_receipt=receipt)
        await self.store.open()
        await self.store.recover_interrupted()
        reconciled = await self.store.reconcile_prepared_deliveries()
        # Unreachable batches recur on every start, so they alone stay silent.
        if reconciled["acknowledged"] or reconciled["conflicting"]:
            _LOGGER.log(
                logging.WARNING if reconciled["conflicting"] else logging.INFO,
                "Swarm saved deliveries reconciled (acknowledged=%d conflicting=%d unreachable=%d)",
                reconciled["acknowledged"],
                reconciled["conflicting"],
                reconciled["unreachable"],
            )
        await self._archive_unknown_groups(groups)

    async def _archive_unknown_groups(self, groups: Any) -> None:
        """Archive participant Sessions of Swarms this database does not hold.

        They remain when the database was restored to an older state or
        replaced. Without their Board they can never resume, and only this
        Extension may remove them; archiving keeps their history.
        """
        after = ""
        while True:
            # Groups are read before the Board: a Swarm's row exists before its
            # first participant Session, so a Swarm starting now is never unknown.
            group_ids = await groups.groups(after=after, limit=100)
            known = await self._store().existing_swarm_ids(group_ids) if group_ids else set()
            for group_id in group_ids:
                if group_id in known:
                    continue
                try:
                    count = await groups.archive_group(group_id)
                except Exception:
                    _LOGGER.warning(
                        "Participant Sessions of an unknown Swarm kept (swarm=%s)",
                        group_id,
                        exc_info=True,
                    )
                    continue
                _LOGGER.warning(
                    "Participant Sessions of an unknown Swarm archived (swarm=%s sessions=%d)",
                    group_id,
                    count,
                )
            if len(group_ids) < 100:
                return
            after = group_ids[-1]

    async def close(self) -> None:
        # The host drains this owner (``_quiesce``) before it runs shutdown
        # handlers, so no Swarm Run is left to stop here.
        self._pacing.close()
        self._changes.close()
        # Wake schedulers await shielded workers; cancel both before awaiting
        # either. A pending title request keeps the stored local title.
        tasks = (*self._cleanup_tasks, *self._title_tasks, *self._wake_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.store is not None:
            await self.store.close()
            self.store = None
        self._lifecycle_runs.clear()
        self.host = None

    def _forget(self, swarm_id: str) -> None:
        """Drop what this registration remembers of a stopped or deleted Swarm's Runs."""
        self._pacing.forget(swarm_id)
        for key in [key for key in self._lifecycle_runs if key[0] == swarm_id]:
            del self._lifecycle_runs[key]

    async def _quiesce(self) -> None:
        if self.host is not None and self.host.temporary_agents is not None:
            await self.host.temporary_agents.quiesce()

    def _store(self) -> SwarmStore:
        if self.store is None or self.host is None:
            raise SwarmStoreError("swarm_closed")
        return self.store

    async def _participant(self, context: ToolContext) -> tuple[TemporarySessionBinding, Json]:
        store = self._store()
        owner = context.execution_owner
        if owner is None or owner.extension != "swarm":
            raise SwarmStoreError("participant_inactive")
        assert self.host is not None
        groups = self.host.temporary_agents
        assert groups is not None
        address = SessionAddress(context.project_id, context.agent_id, context.session_id)
        groups.validate(address, RunAdmission(owner=owner))
        after = ""
        binding = None
        while binding is None:
            bindings = await groups.list(owner.group_id, after=after)
            binding = next(
                (item for item in bindings if item.participant_id == owner.participant_id), None
            )
            if binding is not None or len(bindings) < 100:
                break
            after = bindings[-1].participant_id
        if (
            binding is None
            or binding.address != address
            or binding.generation_id != owner.generation_id
        ):
            raise SwarmStoreError("participant_inactive")
        swarm = await store.get_swarm(owner.group_id)
        groups.validate(address, RunAdmission(owner=owner))
        if swarm["execution_epoch"] != owner.epoch:
            raise SwarmStoreError("swarm_closed")
        return binding, swarm

    async def _bound_arguments(
        self, context: ToolContext, tool: str, arguments: Json
    ) -> tuple[TemporarySessionBinding, Json, Json]:
        arguments = _normalize_arguments(tool, arguments)
        binding, swarm = await self._participant(context)
        identities = {
            "swarm_id": binding.group_id,
            "participant_id": binding.participant_id,
            "sender": binding.participant_id,
        }
        for field, identity in identities.items():
            if field in arguments and arguments.pop(field) != identity:
                raise SwarmStoreError("scope_mismatch", field=field)
        return binding, swarm, arguments

    async def board(self, context: ToolContext, arguments: Json) -> Json:
        try:
            binding, swarm, arguments = await self._bound_arguments(
                context, "swarm_board", arguments
            )
            action, notes = prepare_board_call(arguments)
            if action in _BOARD_READS:
                self._pacing.read_only(
                    binding.group_id, binding.participant_id, context.tool_call_id
                )
            call = BoardCall(self, context, binding, swarm, notes)
            if action in {"post", "create"} and "recipients" in arguments:
                recipients = resolve_recipients(arguments["recipients"], call.roster)
                if recipients.unknown:
                    raise AgentCallError(
                        "invalid_recipient",
                        unknown_recipients_message(
                            arguments["recipients"], recipients, call.roster
                        ),
                    )
                arguments["recipients"] = recipients.ids
                notes.extend(recipients.notes)
            data = await getattr(call, action)(arguments)
            if call.discussions_changed:
                self._changed("discussions", binding.group_id, swarm["settings_revision"])
            for discussion_id in call.post_discussions:
                self._changed(
                    "discussion_posts",
                    f"{binding.group_id}/{discussion_id}",
                    swarm["settings_revision"],
                )
            if action in {"post", "create"}:
                self._enqueue_wakes(binding.group_id)
            return tool_success(with_notes(data, notes))
        except AgentCallError as error:
            return tool_failure(error.code, error.message)
        except SwarmStoreError as error:
            return _failure(error, arguments, BOARD_PARAMETERS)

    async def _record_read(
        self, context: ToolContext, binding: TemporarySessionBinding, entries: Any
    ) -> None:
        prepared = await self._store().prepare_board_read_delivery(
            binding.group_id, binding.participant_id, [entry["id"] for entry in entries]
        )
        if prepared.get("receipt_id"):
            context.record_delivery_receipt(
                prepared["receipt_id"], prepared["content_hash"], prepared["effect_kind"]
            )

    async def inbox(self, context: ToolContext, arguments: Json) -> Json:
        try:
            binding, swarm, arguments = await self._bound_arguments(
                context, "swarm_inbox", arguments
            )
            self._pacing.read_only(binding.group_id, binding.participant_id, context.tool_call_id)
            if arguments.get("action") == "receive":
                arguments.pop("action")
            unexpected = sorted(set(arguments) - {"limit"})
            if unexpected:
                raise SwarmStoreError("invalid_arguments", field=unexpected[0])
            notes: list[str] = []
            limit = _clamped_limit(arguments, notes)
            prepared = await self._store().prepare_inbox_delivery(
                binding.group_id, binding.participant_id, limit=limit
            )
            if prepared.get("receipt_id"):
                context.record_delivery_receipt(
                    prepared["receipt_id"], prepared["content_hash"], prepared["effect_kind"]
                )
            if not prepared["entries"]:
                return tool_success(with_notes({"content": EMPTY_INBOX}, notes))
            data: Json = {}
            if prepared["pending_remaining"]:
                again = f" with {call_text(arguments)}" if arguments else ""
                data["more"] = INBOX_MORE.format(
                    count=prepared["pending_remaining"], arguments=again
                )
            roster = Roster.of(swarm, binding.participant_id)
            data["content"] = messages_text(
                prepared["entries"], roster, swarm["main_discussion_id"]
            )
            return tool_success(with_notes(data, notes))
        except SwarmStoreError as error:
            return _failure(error, arguments, INBOX_PARAMETERS)

    async def state(self, context: ToolContext, arguments: Json) -> Json:
        try:
            binding, swarm, arguments = await self._bound_arguments(
                context, "swarm_state", arguments
            )
            self._pacing.read_only(binding.group_id, binding.participant_id, context.tool_call_id)
            if arguments.get("action") == "status":
                arguments.pop("action")
            _validate_state(arguments)
            notes: list[str] = []
            # One page holds the whole roster of a Swarm up to the maximum page size.
            limit = _clamped_limit(arguments, notes, default=STATE_LIMIT)
            status = await self._store().participant_status(
                binding.group_id,
                binding.participant_id,
                cursor=arguments.get("cursor"),
                limit=limit,
            )
            data = status_data(
                status,
                Roster.of(swarm, binding.participant_id),
                inbox=_inbox_available(binding),
            )
            if status["has_more"]:
                continuation = {**arguments, "cursor": status["cursor"]}
                data["more"] = STATE_MORE.format(call=call_text(continuation))
            return tool_success(with_notes(data, notes))
        except SwarmStoreError as error:
            return _failure(error, arguments, STATE_PARAMETERS)

    async def wiki(self, context: ToolContext, arguments: Json) -> Json:
        try:
            binding, swarm, arguments = await self._bound_arguments(
                context, "swarm_wiki", arguments
            )
            if arguments.get("action") in _WIKI_READS:
                self._pacing.read_only(
                    binding.group_id, binding.participant_id, context.tool_call_id
                )
            notes: list[str] = []
            call = WikiCall(
                self._store(),
                context,
                binding.group_id,
                binding.participant_id,
                swarm["epoch"],
                notes,
            )
            data, changed = await call.run(arguments)
            if changed:
                self._changed("wiki", binding.group_id, swarm["settings_revision"])
            return tool_success(with_notes(data, notes))
        except AgentCallError as error:
            return tool_failure(error.code, error.message)
        except SwarmStoreError as error:
            return _failure(error, arguments, WIKI_PARAMETERS)

    # The open page reloads only what shows a change, so each change names what
    # changed. ``profiles`` names a profile id; ``discussion_posts`` names a
    # Swarm/discussion pair; the other resources name a Swarm
    # id: ``swarms`` its entry in the Swarm list (created, deleted, state, title,
    # settings), ``participants`` their Runs, states and pending messages,
    # ``discussions`` discussions and their members,
    # ``wiki`` its Wiki pages. ``ChangePacing`` publishes each resource and id at
    # most once a second, the first change at once.
    def _changed(self, resource: str, record_id: str, revision: int = 0) -> None:
        if self.host is not None and self.host.publish_change is not None:
            self._changes.changed(resource, record_id, revision)

    def _publish_change(self, resource: str, record_id: str, revision: int) -> None:
        host = self.host
        if host is None or host.publish_change is None:
            return
        try:
            host.publish_change(resource, [record_id], revision)
        except ValueError:
            # The registration retired for a reload or disable, which
            # invalidates every Extension surface itself. The change is
            # already stored, so the call that made it still succeeded.
            _LOGGER.debug("Swarm change not published: registration retired")

    def _participants_changed(self, swarm_id: str, result: Json, revision: int = 0) -> None:
        """Publish a participant change and, when it changed the Swarm's state, its entry."""
        self._changed("participants", swarm_id, revision)
        if result.get("swarm_state_changed"):
            self._changed("swarms", swarm_id, revision)

    async def command(
        self, context: ExtensionCommandContext, argument: str | None
    ) -> CommandOutcome:
        if argument is None:
            return CommandOutcome(
                command="swarm",
                feedback=CommandFeedback(
                    kind="notice", text="Open Swarms to choose a profile and goal."
                ),
                navigation=CommandNavigation(
                    kind="open_extension_page", extension="swarm", page="swarms", route=""
                ),
            )
        profile_id, prompt = _swarm_command_argument(argument)
        profile_id = await self._profile_id_for_slug(profile_id)
        result = await self.management.operation(
            "swarms.start",
            {"profile_id": profile_id, "prompt": prompt, "request_id": new_id("req")},
        )
        return CommandOutcome(
            command="swarm",
            feedback=CommandFeedback(kind="notice", text="Swarm started."),
            facts={"swarm_id": result["swarm_id"]},
            navigation=CommandNavigation(
                kind="open_extension_page",
                extension="swarm",
                page="swarms",
                route=f"swarms/{result['swarm_id']}",
            ),
        )

    async def _profile_id_for_slug(self, slug: str) -> str:
        cursor = None
        while True:
            page = await self._store().list_profiles(cursor=cursor, limit=100)
            for profile in page.entries:
                if profile.get("slug") == slug:
                    return str(profile["id"])
            if not page.has_more:
                break
            cursor = page.cursor
        raise SwarmStoreError("profile_not_found")

    async def _schedule_wakes(self, swarm_id: str) -> None:
        """Admit at most one continuation Run for each durable wake intent."""

        self._wake_dirty.add(swarm_id)
        current = self._wake_tasks.get(swarm_id)
        if current is None or current.done():
            current = asyncio.create_task(
                self._run_background(swarm_id, self._drain_wakes(swarm_id)),
                name=f"swarm-wakes:{swarm_id}",
            )
            self._wake_tasks[swarm_id] = current
            current.add_done_callback(
                lambda task: (
                    self._wake_tasks.pop(swarm_id, None)
                    if self._wake_tasks.get(swarm_id) is task
                    else None
                )
            )
        await asyncio.shield(current)

    def _enqueue_wakes(self, swarm_id: str) -> None:
        task = asyncio.create_task(
            self._schedule_wakes(swarm_id), name=f"swarm-wake-schedule:{swarm_id}"
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._observe_cleanup_task)

    def _enqueue_title(self, swarm_id: str, prompt: str, revision: int) -> None:
        """Name a started Run from its goal without delaying the Start reply."""
        task = asyncio.create_task(
            self._title_swarm(swarm_id, prompt, revision), name=f"swarm-title:{swarm_id}"
        )
        self._title_tasks.add(task)
        task.add_done_callback(self._observe_title_task)

    async def _title_swarm(self, swarm_id: str, prompt: str, revision: int) -> None:
        host = self.host
        if host is None or host.temporary_agents is None:
            return
        await host.temporary_agents.title_group(swarm_id, prompt)
        self._changed("swarms", swarm_id, revision)

    def _observe_title_task(self, task: asyncio.Task[None]) -> None:
        self._title_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.api.logger.warning("Swarm Run title failed (error=%s)", type(error).__name__)

    def _observe_cleanup_task(self, task: asyncio.Task[None]) -> None:
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.api.logger.error(
                    "Swarm background cleanup failed (error=%s)", type(error).__name__
                )

    async def _run_background(self, swarm_id: str, operation: Awaitable[None]) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except SwarmStoreError as error:
            if error.code in {"stale_epoch", "swarm_closed", "swarm_not_found"}:
                return
            self.api.logger.warning(
                "Swarm background operation failed (swarm_id=%s, code=%s)", swarm_id, error.code
            )
            await self._wake_failed(swarm_id)
        except Exception as error:
            self.api.logger.error(
                "Swarm background operation failed (swarm_id=%s, error=%s)",
                swarm_id,
                type(error).__name__,
            )
            await self._wake_failed(swarm_id)

    async def _wake_failed(self, swarm_id: str) -> None:
        try:
            swarm = await self._store().get_swarm(swarm_id)
            if swarm["state"] in {"running", "idle", "needs_attention"}:
                await self._store().set_swarm_state(swarm_id, "needs_attention")
                self._changed("swarms", swarm_id, swarm["settings_revision"])
        except SwarmStoreError as error:
            if error.code not in {"swarm_closed", "swarm_not_found", "stale_epoch"}:
                raise

    async def _drain_wakes(self, swarm_id: str) -> None:
        async with self.management.control(swarm_id):
            await self._drain_wakes_locked(swarm_id)

    async def _drain_wakes_locked(self, swarm_id: str) -> None:
        """Coalesce changes before claiming the Store's durable wake work."""

        host = self.host
        if host is None or host.temporary_agents is None:
            return
        while swarm_id in self._wake_dirty:
            self._wake_dirty.discard(swarm_id)
            swarm = await self._store().get_swarm(swarm_id)
            if swarm["state"] not in {"running", "idle", "needs_attention"}:
                return
            await asyncio.sleep(swarm["delivery"]["coalesce_ms"] / 1000)
            swarm = await self._store().get_swarm(swarm_id)
            if swarm["state"] not in {"running", "idle", "needs_attention"}:
                return
            handle = await host.temporary_agents.open_group(swarm_id)
            for participant in swarm["participants"]:
                await self._store().prepare_wake(
                    swarm_id,
                    participant["id"],
                    expected_epoch=swarm["epoch"],
                    wake_routes=self._pacing.wake_routes(swarm_id, participant["id"]),
                )
            while True:
                page = await self._store().list_wake_intents(swarm_id, limit=100)
                if not page.entries:
                    break
                claimed_count = 0
                for wake in page.entries:
                    claim = await self._store().claim_wake(
                        swarm_id, wake["participant_id"], expected_epoch=swarm["epoch"]
                    )
                    if not claim["pending"]:
                        continue
                    claimed_count += 1
                    participant = next(
                        item
                        for item in swarm["participants"]
                        if item["id"] == wake["participant_id"]
                    )
                    if participant.get("lifecycle_run_id"):
                        prior = await host.temporary_agents.owned_run(
                            swarm_id, participant["lifecycle_run_id"]
                        )
                        if prior.run is not None:
                            await prior.run.wait()
                    admission = await host.temporary_agents.start(
                        handle,
                        wake["participant_id"],
                        TemporaryRunInput(
                            "continuation",
                            "",
                            f"wake:{swarm['epoch']}:{wake['participant_id']}:{claim['receipt_id']}",
                        ),
                    )
                    admitted = await self._store().mark_wake_admitted(
                        swarm_id,
                        wake["participant_id"],
                        expected_epoch=swarm["epoch"],
                        run_id=admission.run_id,
                        boundary=claim["boundary"],
                    )
                    self._lifecycle_runs[(swarm_id, wake["participant_id"])] = admission.run_id
                    self._participants_changed(swarm_id, admitted, swarm["settings_revision"])
                if claimed_count == 0 or len(page.entries) < 100:
                    break

    async def _start_swarm(
        self,
        profile_id: str,
        prompt: str,
        request_id: str,
        *,
        expected_profile_revision: int | None = None,
        working_directory: str | None = None,
    ) -> Json:
        """Prepare every bound participant before admitting any initial Run.

        This is intentionally private until the complete private Tool grant set is
        registered. Keeping it here makes the eventual operation use the same
        all-or-stop path as Resume rather than a UI-specific shortcut.
        """

        host = self.host
        if host is None or host.temporary_agents is None or host.catalog is None:
            raise SwarmStoreError("swarm_closed")
        replay = await self._store().replay_start(
            profile_id,
            prompt,
            request_id=request_id,
            expected_profile_revision=expected_profile_revision,
            working_directory=working_directory,
        )
        if replay is not None:
            return replay
        profile = await self._store().get_profile(profile_id)
        if (
            expected_profile_revision is not None
            and profile["revision"] != expected_profile_revision
        ):
            raise SwarmStoreError("revision_conflict")
        if working_directory is not None:
            profile = {
                **profile,
                "working_directory": {"kind": "directory", "path": working_directory},
            }
        catalog = await host.catalog()
        cwd, project_id = await _profile_cwd(profile, catalog)
        _validate_profile_catalog(profile, catalog)
        effective = {"cwd": str(cwd), "project_id": project_id}
        swarm = await self._store().create_swarm(
            profile_id,
            prompt,
            effective,
            request_id=request_id,
            expected_profile_revision=expected_profile_revision or profile["revision"],
        )
        if swarm.get("replayed"):
            return swarm
        async with self.management.control(swarm["swarm_id"]):
            snapshot = await self._store().get_swarm(swarm["swarm_id"])
            if snapshot["state"] != "preparing":
                raise SwarmStoreError("swarm_closed")
            group = host.temporary_agents
            bindings: dict[str, TemporarySessionBinding] = {}
            try:
                for participant in snapshot["participants"]:
                    binding = await group.create(
                        snapshot["id"],
                        participant["id"],
                        _participant_config(profile, participant, cwd),
                        project_id=project_id,
                    )
                    bindings[participant["id"]] = binding
                    await self._store().bind_participant_session(binding)
                handle = await group.open_group(snapshot["id"])
                await self._store().bind_execution_epoch(
                    snapshot["id"], expected_epoch=snapshot["epoch"], execution_epoch=handle.epoch
                )
                await self._store().set_swarm_state(snapshot["id"], "running")
                admissions = await self._admit_initial(snapshot, handle, request_id, bindings)
            except BaseException:
                await group.close_group(snapshot["id"])
                await self._store().fail_startup(snapshot["id"], expected_epoch=snapshot["epoch"])
                raise
            self._changed("swarms", snapshot["id"], snapshot["settings_revision"])
            result = await self._store().finish_admission(
                snapshot["id"], request_id=request_id, kind="start", runs=admissions
            )
            self._enqueue_title(snapshot["id"], prompt, snapshot["settings_revision"])
            _LOGGER.info(
                "Swarm started (swarm=%s profile=%s participants=%d runs=%d)",
                snapshot["id"],
                profile_id,
                len(snapshot["participants"]),
                len(admissions),
            )
            return result

    async def _resume_swarm(
        self,
        swarm_id: str,
        request_id: str,
        *,
        participant_id: str | None = None,
        reason: str,
    ) -> Json:
        host = self.host
        if host is None or host.temporary_agents is None:
            raise SwarmStoreError("swarm_closed")
        resumed = await self._store().begin_resume(
            swarm_id, request_id=request_id, actor="user", participant_id=participant_id
        )
        if resumed.get("replayed"):
            return resumed
        snapshot = await self._store().get_swarm(swarm_id)
        group = host.temporary_agents
        existing: dict[str, TemporarySessionBinding] = {}
        try:
            after = ""
            while True:
                page = await group.list(swarm_id, after=after)
                existing.update((binding.participant_id, binding) for binding in page)
                if len(page) < 100:
                    break
                after = page[-1].participant_id
            profile = snapshot["profile_snapshot"]
            cwd = Path(snapshot["effective_configuration"]["cwd"])
            project_id = snapshot["effective_configuration"].get("project_id")
            for participant in snapshot["participants"]:
                binding = existing.get(participant["id"])
                if binding is None:
                    binding = await group.create(
                        swarm_id,
                        participant["id"],
                        _participant_config(profile, participant, cwd),
                        project_id=project_id,
                    )
                    existing[participant["id"]] = binding
                # Recording is idempotent; it also records a binding whose Session a
                # failed or interrupted Start created without recording it.
                await self._store().bind_participant_session(binding)
            handle = await group.open_group(swarm_id)
            await self._store().bind_execution_epoch(
                swarm_id, expected_epoch=snapshot["epoch"], execution_epoch=handle.epoch
            )
            await self._store().set_swarm_state(swarm_id, "running")
        except BaseException:
            if not resumed.get("reused_epoch"):
                await group.close_group(swarm_id)
                await self._store().fail_startup(swarm_id, expected_epoch=snapshot["epoch"])
            raise
        bindings = existing
        admissions: list[Json] = []
        for participant_id in resumed["participant_ids"]:
            try:
                binding = bindings[participant_id]
                await self._store().prepare_wake(
                    swarm_id, participant_id, expected_epoch=snapshot["epoch"], announce=False
                )
                initial = await group.delivery_receipt(
                    binding.address,
                    binding.generation_id,
                    f"initial:{binding.generation_id}",
                )
                input = (
                    TemporaryRunInput(
                        "initial",
                        _initial_message(snapshot, binding),
                        request_id,
                    )
                    if initial is None
                    else TemporaryRunInput(
                        "continuation", _reminder(snapshot, "resume"), request_id
                    )
                )
                admission = await group.start(
                    handle,
                    participant_id,
                    input,
                )
                await self._record_run_started(
                    swarm_id, participant_id, admission.run_id, snapshot["epoch"]
                )
                admissions.append({"participant_id": participant_id, "run_id": admission.run_id})
            except Exception as error:
                _LOGGER.warning(
                    "Swarm participant resume failed (swarm=%s participant=%s)",
                    swarm_id,
                    participant_id,
                    exc_info=True,
                )
                await self._store().set_participant_state(swarm_id, participant_id, "failed")
                admissions.append({"participant_id": participant_id, "error": type(error).__name__})
        self._changed("swarms", swarm_id, snapshot["settings_revision"])
        failed = sum("error" in admission for admission in admissions)
        _LOGGER.log(
            logging.WARNING if failed else logging.INFO,
            "Swarm resumed (swarm=%s reason=%s runs=%d failed=%d)",
            swarm_id,
            reason,
            len(admissions) - failed,
            failed,
        )
        return await self._store().finish_admission(
            swarm_id, request_id=request_id, kind="resume", runs=admissions
        )

    async def _admit_initial(
        self,
        swarm: Json,
        handle: Any,
        request_id: str,
        bindings: dict[str, TemporarySessionBinding],
    ) -> list[Json]:
        assert self.host is not None and self.host.temporary_agents is not None
        admissions: list[Json] = []
        for participant in swarm["participants"]:
            admission = await self.host.temporary_agents.start(
                handle,
                participant["id"],
                TemporaryRunInput(
                    "initial",
                    _initial_message(swarm, bindings[participant["id"]]),
                    request_id,
                ),
            )
            await self._record_run_started(
                swarm["id"], participant["id"], admission.run_id, swarm["epoch"]
            )
            admissions.append({"participant_id": participant["id"], "run_id": admission.run_id})
        return admissions

    async def _record_run_started(
        self, swarm_id: str, participant_id: str, run_id: str, epoch: int
    ) -> Json:
        started = await self._store().record_run_started(
            swarm_id, participant_id, run_id=run_id, expected_epoch=epoch
        )
        self._lifecycle_runs[(swarm_id, participant_id)] = run_id
        return started

    async def _before_request(
        self, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        binding = context.binding
        owner = context.execution_owner
        if owner is None or owner.extension != "swarm" or binding.group_id != owner.group_id:
            raise SwarmStoreError("participant_inactive")
        swarm = await self._store().get_swarm(binding.group_id)
        # Every Model request of a Run lands here; only its first one can record
        # it. The epoch check of a skipped recording is the delivery's own.
        if self._lifecycle_runs.get((binding.group_id, binding.participant_id)) != context.run_id:
            started = await self._record_run_started(
                binding.group_id, binding.participant_id, context.run_id, swarm["epoch"]
            )
            # Start, resume and wake admission record their Runs themselves; a Run
            # recorded here (another Run, or a wake Run whose first request beats its
            # admission) changes what the open page shows.
            if started["recorded"]:
                self._participants_changed(binding.group_id, started, swarm["settings_revision"])
        prepared = await self._store().prepare_automatic_delivery(
            binding.group_id,
            binding.participant_id,
            expected_epoch=swarm["epoch"],
        )
        if not prepared.get("entries"):
            return None
        entry = delivery_text(
            _reminder(swarm, "delivery"),
            prepared["entries"],
            prepared["pending_remaining"],
            Roster.of(swarm, binding.participant_id),
            swarm["main_discussion_id"],
            inbox=_inbox_available(binding),
            automatic=automatic_delivery(swarm["delivery"]),
            skipped=prepared.get("skipped", ()),
        )
        return PreparedSessionDelivery(
            prepared["receipt_id"],
            prepared["content_hash"],
            (entry,),
            str(prepared["settings_revision"]),
            prepared["effect_kind"],
        )

    async def _acknowledge_delivery(
        self, context: SessionRequestContext, delivery: PreparedSessionDelivery
    ) -> None:
        if not await self._store().reconcile_delivery(delivery.delivery_id):
            raise SwarmStoreError("delivery_unacknowledged")
        self._changed("participants", context.binding.group_id)

    async def _reconcile_tool_batch(
        self,
        context: SessionRequestContext,
        *,
        receipts: tuple[tuple[str, str, str, str], ...],
        persisted_call_ids: tuple[str, ...],
        turn_end_requested: bool,
        **_ignored: Any,
    ) -> ToolBatchDecision:
        owner = context.execution_owner
        if owner is None:
            return ToolBatchDecision(end=turn_end_requested)
        binding = context.binding
        self._pacing.tools_used(
            binding.group_id, binding.participant_id, context.run_id, persisted_call_ids
        )
        for _, receipt_id, _, _ in receipts:
            if not await self._store().reconcile_delivery(receipt_id):
                raise SwarmStoreError("delivery_unacknowledged")
        if receipts:
            self._changed("participants", context.binding.group_id)
        return ToolBatchDecision(end=turn_end_requested)

    async def _run_finished(self, context: SessionRequestContext, *, outcome: str) -> None:
        binding = context.binding
        owner = context.execution_owner
        if owner is None:
            return
        snapshot = await self._store().get_swarm(binding.group_id)
        terminal_outcome = {"success": "completed", "error": "failed"}.get(outcome, outcome)
        try:
            finished = await self._store().reconcile_run_finished(
                binding.group_id,
                binding.participant_id,
                run_id=context.run_id,
                expected_epoch=snapshot["epoch"],
                outcome=terminal_outcome,
            )
        except SwarmStoreError as error:
            # Stop and newer Runs already own the retained outcome. Their late
            # completion callbacks must neither overwrite it nor fail cleanup.
            if error.code in {"swarm_closed", "stale_epoch", "stale_run"}:
                return
            raise
        self._pacing.run_finished(
            binding.group_id,
            binding.participant_id,
            context.run_id,
            completed=terminal_outcome == "completed",
        )
        if terminal_outcome == "completed":
            self._enqueue_wakes(binding.group_id)
        self._participants_changed(binding.group_id, finished, snapshot["settings_revision"])
