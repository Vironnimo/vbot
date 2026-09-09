"""Swarm coordination through owner-bound Extension capabilities."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from core.agents.temporary import TemporaryAgentConfig, TemporaryRunInput
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
from core.tools.availability import normalize_tool_access
from core.tools.tools import run_tool_worker
from core.utils.ids import new_id

from .agent_text import (
    BOARD_DESCRIPTION,
    BOARD_PARAMETERS,
    DEFAULT_INSTRUCTIONS,
    DEFAULT_PROMPT_BLOCKS,
    DEFAULT_REMINDERS,
    EMPTY_INBOX,
    ERRORS,
    INBOX_DESCRIPTION,
    INBOX_PARAMETERS,
    POST_SAVED,
    REMINDER_TEXTS,
    REPLAYED,
    STATE_DESCRIPTION,
    STATE_PARAMETERS,
)
from .store import Page, SwarmStore, SwarmStoreError, _validate_profile

Json = dict[str, Any]


class SwarmExtension:
    """Own registration state, bound participants, and complete Board operations."""

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.store: SwarmStore | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._wake_dirty: set[str] = set()
        self._control_lock = asyncio.Lock()

    async def start(self, host: ExtensionHost) -> None:
        if host.state_dir is None or host.temporary_agents is None:
            raise RuntimeError("Swarm requires the owner-bound Extension host")
        self.host = host
        groups = host.temporary_agents

        async def receipt(address: SessionAddress, generation: str, owner: str, receipt_id: str):
            if owner != "swarm":
                return None
            return await groups.delivery_receipt(address, generation, receipt_id)

        self.store = SwarmStore(
            host.state_dir / "swarm.db",
            lookup_delivery_receipt=receipt,
        )
        await self.store.open()
        await self.store.recover_interrupted()

    async def close(self) -> None:
        if self.host is not None and self.host.temporary_agents is not None:
            await self.host.temporary_agents.quiesce()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        for task in self._wake_tasks.values():
            task.cancel()
        if self._wake_tasks:
            await asyncio.gather(*self._wake_tasks.values(), return_exceptions=True)
        if self.store is not None:
            await self.store.close()
            self.store = None
        self.host = None

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

    async def board(self, context: ToolContext, arguments: Json) -> Json:
        try:
            action = _validate_board(arguments)
            binding, swarm = await self._participant(context)
            store = self._store()
            sid, pid = binding.group_id, binding.participant_id
            if action == "list":
                page = await store.list_discussions(
                    sid, pid, cursor=arguments.get("cursor"), limit=arguments.get("limit", 20)
                )
                data = _board_page(page, arguments)
                data["main_discussion_id"] = swarm["main_discussion_id"]
            elif action == "read":
                page = await store.read_posts(
                    sid, pid, **{key: value for key, value in arguments.items() if key != "action"}
                )
                data = _board_page(page, arguments)
                await self._record_read(context, binding, page.entries)
            elif action == "post":
                data = await store.post(
                    sid,
                    pid,
                    expected_epoch=swarm["epoch"],
                    **{key: value for key, value in arguments.items() if key != "action"},
                )
                data["guidance"] = REPLAYED if data.get("replayed") else POST_SAVED
            elif action == "create":
                data = await store.create_discussion(
                    sid,
                    pid,
                    expected_epoch=swarm["epoch"],
                    **{key: value for key, value in arguments.items() if key != "action"},
                )
                if data.get("replayed"):
                    data["guidance"] = REPLAYED
            elif action == "join":
                data = await store.join_discussion(
                    sid, pid, arguments["discussion_id"], expected_epoch=swarm["epoch"]
                )
                recent = data["recent"]
                cursor = recent.pop("cursor", None)
                if recent["has_more"]:
                    recent["next_call"] = {
                        "tool": "swarm_board",
                        "arguments": {
                            "action": "read",
                            "discussion_id": arguments["discussion_id"],
                            "cursor": cursor,
                        },
                    }
                await self._record_read(context, binding, recent["entries"])
            else:
                data = await store.leave_discussion(
                    sid, pid, arguments["discussion_id"], expected_epoch=swarm["epoch"]
                )
            if action in {"post", "create", "join", "leave"}:
                self._changed(sid, swarm["settings_revision"])
            if action in {"post", "create"}:
                self._enqueue_wakes(sid)
            return tool_success(data)
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
            unexpected = sorted(set(arguments) - {"limit"})
            if unexpected:
                raise SwarmStoreError("invalid_arguments", field=unexpected[0])
            limit = arguments.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise SwarmStoreError("invalid_arguments", field="limit")
            binding, _swarm = await self._participant(context)
            prepared = await self._store().prepare_inbox_delivery(
                binding.group_id, binding.participant_id, limit=limit
            )
            if prepared.get("receipt_id"):
                context.record_delivery_receipt(
                    prepared["receipt_id"], prepared["content_hash"], prepared["effect_kind"]
                )
            data: Json = {
                "entries": prepared["entries"],
                "pending_remaining": prepared["pending_remaining"],
            }
            if not prepared["entries"]:
                data["guidance"] = EMPTY_INBOX
            elif prepared["pending_remaining"]:
                data["next_call"] = {"tool": "swarm_inbox", "arguments": dict(arguments)}
            return tool_success(data)
        except SwarmStoreError as error:
            return _failure(error, arguments, INBOX_PARAMETERS)

    async def state(self, context: ToolContext, arguments: Json) -> Json:
        try:
            _validate_state(arguments)
            binding, _ = await self._participant(context)
            data = await self._store().participant_status(
                binding.group_id,
                binding.participant_id,
                cursor=arguments.get("cursor"),
                limit=arguments.get("limit", 20),
            )
            cursor = data.pop("cursor", None)
            if data["pending_count"]:
                data["inbox_call"] = {"tool": "swarm_inbox", "arguments": {}}
            if data["has_more"]:
                data["next_call"] = {
                    "tool": "swarm_state",
                    "arguments": {**arguments, "cursor": cursor},
                }
            return tool_success(data)
        except SwarmStoreError as error:
            return _failure(error, arguments, STATE_PARAMETERS)

    def _changed(self, swarm_id: str, revision: int) -> None:
        if self.host is not None and self.host.publish_change is not None:
            self.host.publish_change("swarms", [swarm_id], revision)

    async def operation(self, name: str, arguments: Json) -> Json:
        """Run a management operation without exposing runtime services to the page."""

        try:
            handlers: dict[str, Callable[[Json], Awaitable[Json]]] = {
                "catalog": self._catalog,
                "profiles.preview": self._profiles_preview,
                "profiles.list": self._profiles_list,
                "profiles.get": self._profiles_get,
                "profiles.save": self._profiles_save,
                "profiles.delete": self._profiles_delete,
                "swarms.list": self._swarms_list,
                "swarms.get": self._swarms_get,
                "swarms.events": self._swarms_events,
                "swarms.settings": self._swarms_settings,
                "swarms.start": self._swarms_start,
                "swarms.stop": self._swarms_stop,
                "swarms.delete": self._swarms_delete,
                "swarms.resume": self._swarms_resume,
                "swarms.usage": self._swarms_usage,
                "board.list": self._board_list,
                "board.read": self._board_read,
                "board.post": self._board_post,
            }
            if name in {"swarms.start", "swarms.stop", "swarms.resume", "swarms.delete"}:
                async with self._control_lock:
                    return await handlers[name](arguments)
            return await handlers[name](arguments)
        except SwarmStoreError as error:
            raise ValueError(error.code) from error

    async def _catalog(self, _arguments: Json) -> Json:
        host = self.host
        catalog = await host.catalog() if host is not None and host.catalog is not None else {}
        catalog["prompt_defaults"] = {
            "instructions": DEFAULT_INSTRUCTIONS,
            "prompt_blocks": DEFAULT_PROMPT_BLOCKS,
            "reminders": DEFAULT_REMINDERS,
        }
        catalog["reminder_texts"] = REMINDER_TEXTS
        return {"catalog": catalog}

    async def _profiles_preview(self, arguments: Json) -> Json:
        _exact(arguments, {"profile", "formation_index"}, required={"profile"})
        value = arguments.get("profile")
        if not isinstance(value, dict):
            raise SwarmStoreError("invalid_arguments", field="profile")
        profile = _validate_profile({**value, "name": value.get("name") or "Preview"})
        index = arguments.get("formation_index", 0)
        if type(index) is not int or not 0 <= index < len(profile["participants"]):
            raise SwarmStoreError("invalid_arguments", field="formation_index")
        host = self.host
        if host is None or host.inspect_prompt is None or host.catalog is None:
            raise SwarmStoreError("swarm_closed")
        catalog = await host.catalog()
        _validate_profile_catalog(profile, catalog)
        directory = profile["working_directory"]
        project_id = directory.get("project_id")
        if project_id is not None:
            project = next((item for item in catalog["projects"] if item["id"] == project_id), None)
            if project is None:
                raise SwarmStoreError("invalid_arguments", field="working_directory")
            cwd = Path(project["cwd"])
        else:
            cwd = Path(directory["path"]).expanduser().resolve()
        participant = {
            "ordinal": 1 + sum(row["count"] for row in profile["participants"][:index]),
            "display_name": "Preview",
            "model": profile["participants"][index]["model"],
        }
        return {
            "preview": await host.inspect_prompt(
                _participant_config(profile, participant, cwd), project_id
            )
        }

    async def _profiles_list(self, arguments: Json) -> Json:
        page = await self._store().list_profiles(**_page_arguments(arguments))
        return _management_page(page)

    async def _profiles_get(self, arguments: Json) -> Json:
        _exact(arguments, {"profile_id"})
        return {"profile": await self._store().get_profile(_string(arguments, "profile_id"))}

    async def _profiles_save(self, arguments: Json) -> Json:
        _exact(arguments, {"profile", "expected_revision"})
        profile = arguments.get("profile")
        if not isinstance(profile, dict):
            raise SwarmStoreError("invalid_arguments", field="profile")
        expected = arguments.get("expected_revision")
        if expected is not None and type(expected) is not int:
            raise SwarmStoreError("invalid_arguments", field="expected_revision")
        saved = await self._store().save_profile(profile, expected_revision=expected)
        self._changed("profiles", saved["revision"])
        return {"profile": saved}

    async def _profiles_delete(self, arguments: Json) -> Json:
        _exact(arguments, {"profile_id", "expected_revision"})
        profile_id = _string(arguments, "profile_id")
        revision = _integer(arguments, "expected_revision", minimum=1)
        await self._store().delete_profile(profile_id, expected_revision=revision)
        self._changed("profiles", revision)
        return {"profile_id": profile_id, "deleted": True}

    async def _swarms_list(self, arguments: Json) -> Json:
        page = await self._store().list_swarms(**_page_arguments(arguments))
        return _management_page(page)

    async def _swarms_get(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id"})
        swarm = await self._store().get_swarm(_string(arguments, "swarm_id"))
        host = self.host
        groups = None if host is None else host.temporary_agents

        async def active(participant: Json) -> bool:
            run_id = participant.get("lifecycle_run_id")
            if groups is None or not run_id:
                return False
            try:
                inspection = await groups.owned_run(swarm["id"], run_id)
            except Exception:
                return False
            return inspection.run is not None and inspection.run.status.value == "running"

        values = await asyncio.gather(*(active(item) for item in swarm["participants"]))
        for participant, run_active in zip(swarm["participants"], values, strict=True):
            participant["run_active"] = run_active
        return {"swarm": _swarm_projection(swarm)}

    async def _swarms_events(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "cursor", "limit"}, required={"swarm_id"})
        page = await self._store().list_events(
            _string(arguments, "swarm_id"), **_pagination_arguments(arguments)
        )
        return _management_page(page)

    async def _swarms_settings(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "delivery", "expected_revision", "request_id"})
        swarm_id = _string(arguments, "swarm_id")
        delivery = arguments.get("delivery")
        if not isinstance(delivery, dict):
            raise SwarmStoreError("invalid_arguments", field="delivery")
        result = await self._store().apply_delivery_settings(
            swarm_id,
            delivery,
            expected_revision=_integer(arguments, "expected_revision", minimum=1),
            request_id=_string(arguments, "request_id"),
            actor="user",
        )
        self._changed(swarm_id, result["revision"])
        self._enqueue_wakes(swarm_id)
        return result

    async def _swarms_delete(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id"})
        swarm_id = _string(arguments, "swarm_id")
        if await self._store().begin_delete(swarm_id):
            self._changed(swarm_id, 0)
            task = self._wake_tasks.get(swarm_id)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._wake_dirty.discard(swarm_id)
            host = self.host
            if host is None or host.temporary_agents is None:
                raise SwarmStoreError("swarm_closed")
            await host.temporary_agents.delete_group(swarm_id)
            await self._store().delete_swarm(swarm_id)
        self._changed(swarm_id, 0)
        return {"swarm_id": swarm_id, "deleted": True}

    async def _swarms_stop(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "request_id"})
        result = await self._store().begin_stop(
            _string(arguments, "swarm_id"),
            request_id=_string(arguments, "request_id"),
            actor="user",
        )
        snapshot = await self._store().get_swarm(result["swarm_id"])
        if (
            result["state"] != "stopping"
            or snapshot["state"] != "stopping"
            or result["epoch"] != snapshot["epoch"]
        ):
            return result
        host = self.host
        if host is None or host.temporary_agents is None:
            raise SwarmStoreError("swarm_closed")
        task = self._wake_tasks.get(result["swarm_id"])
        if task is not None:
            task.cancel()
        report = await host.temporary_agents.close_group(result["swarm_id"], reason="swarm_stop")
        finished = await self._store().finish_stop(
            result["swarm_id"],
            request_id=_string(arguments, "request_id"),
            actor="user",
            drain_report=report,
        )
        self._changed(result["swarm_id"], 0)
        return finished

    async def _swarms_start(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"profile_id", "prompt", "request_id", "expected_profile_revision"},
            required={"profile_id", "prompt", "request_id"},
        )
        return await self._start_swarm(
            _string(arguments, "profile_id"),
            _string(arguments, "prompt"),
            _string(arguments, "request_id"),
            expected_profile_revision=(
                _integer(arguments, "expected_profile_revision", minimum=1)
                if "expected_profile_revision" in arguments
                else None
            ),
        )

    async def _swarms_resume(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"swarm_id", "request_id", "participant_id"},
            required={"swarm_id", "request_id"},
        )
        return await self._resume_swarm(
            _string(arguments, "swarm_id"),
            _string(arguments, "request_id"),
            participant_id=_string(arguments, "participant_id")
            if "participant_id" in arguments
            else None,
        )

    async def _swarms_usage(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "participant_id"}, required={"swarm_id"})
        host = self.host
        if host is None or host.temporary_agents is None:
            raise SwarmStoreError("swarm_closed")
        query = {key: arguments[key] for key in {"participant_id"} if key in arguments}
        return {"usage": await host.temporary_agents.usage(_string(arguments, "swarm_id"), query)}

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
        result = await self.operation(
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

    async def _board_list(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "cursor", "limit"}, required={"swarm_id"})
        page = await self._store().list_human_discussions(
            _string(arguments, "swarm_id"), **_pagination_arguments(arguments)
        )
        return _management_page(page)

    async def _board_read(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"swarm_id", "discussion_id", "message_id", "cursor", "limit"},
            required={"swarm_id"},
        )
        kwargs = {key: value for key, value in arguments.items() if key != "swarm_id"}
        page = await self._store().read_human_posts(_string(arguments, "swarm_id"), **kwargs)
        return _management_page(page)

    async def _board_post(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"swarm_id", "discussion_id", "text", "reply_to", "recipients", "request_id"},
            required={"swarm_id", "text", "request_id"},
        )
        swarm_id = _string(arguments, "swarm_id")
        result = await self._store().post_human(
            swarm_id,
            **{key: value for key, value in arguments.items() if key != "swarm_id"},
        )
        snapshot = await self._store().get_swarm(swarm_id)
        self._changed(swarm_id, snapshot["settings_revision"])
        self._enqueue_wakes(swarm_id)
        return result

    async def _schedule_wakes(self, swarm_id: str) -> None:
        """Admit at most one continuation Run for each durable wake intent."""

        self._wake_dirty.add(swarm_id)
        current = self._wake_tasks.get(swarm_id)
        if current is None or current.done():
            current = asyncio.create_task(
                self._run_background(swarm_id, self._drain_wakes(swarm_id))
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
        task = asyncio.create_task(self._schedule_wakes(swarm_id))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._observe_cleanup_task)

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
                self._changed(swarm_id, swarm["settings_revision"])
        except SwarmStoreError as error:
            if error.code not in {"swarm_closed", "swarm_not_found", "stale_epoch"}:
                raise

    async def _drain_wakes(self, swarm_id: str) -> None:
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
                    swarm_id, participant["id"], expected_epoch=swarm["epoch"]
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
                            f"wake:{swarm['epoch']}:{wake['participant_id']}:{wake['announced_sequence']}",
                        ),
                    )
                    await self._store().mark_wake_admitted(
                        swarm_id,
                        wake["participant_id"],
                        expected_epoch=swarm["epoch"],
                        run_id=admission.run_id,
                        boundary=claim["boundary"],
                    )
                    self._changed(swarm_id, swarm["settings_revision"])
                if claimed_count == 0 or len(page.entries) < 100:
                    break

    async def _start_swarm(
        self,
        profile_id: str,
        prompt: str,
        request_id: str,
        *,
        expected_profile_revision: int | None = None,
    ) -> Json:
        """Prepare every bound participant before admitting any initial Run.

        This is intentionally private until the complete three-Tool grant set is
        registered. Keeping it here makes the eventual operation use the same
        all-or-stop path as Resume rather than a UI-specific shortcut.
        """

        host = self.host
        if host is None or host.temporary_agents is None or host.catalog is None:
            raise SwarmStoreError("swarm_closed")
        profile = await self._store().get_profile(profile_id)
        if (
            expected_profile_revision is not None
            and profile["revision"] != expected_profile_revision
        ):
            raise SwarmStoreError("revision_conflict")
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
        snapshot = await self._store().get_swarm(swarm["swarm_id"])
        group = host.temporary_agents
        try:
            for participant in snapshot["participants"]:
                binding = await group.create(
                    snapshot["id"],
                    participant["id"],
                    _participant_config(profile, participant, cwd),
                    project_id=project_id,
                )
                await self._store().bind_participant_session(binding)
            handle = await group.open_group(snapshot["id"])
            await self._store().bind_execution_epoch(
                snapshot["id"], expected_epoch=snapshot["epoch"], execution_epoch=handle.epoch
            )
            await self._store().set_swarm_state(snapshot["id"], "running")
            admissions = await self._admit_initial(snapshot, handle, prompt, request_id)
        except BaseException:
            await group.close_group(snapshot["id"])
            await self._store().fail_startup(snapshot["id"], expected_epoch=snapshot["epoch"])
            raise
        self._changed(snapshot["id"], snapshot["settings_revision"])
        return await self._store().finish_admission(
            snapshot["id"], request_id=request_id, kind="start", runs=admissions
        )

    async def _resume_swarm(
        self, swarm_id: str, request_id: str, *, participant_id: str | None = None
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
                if participant["id"] not in existing:
                    binding = await group.create(
                        swarm_id,
                        participant["id"],
                        _participant_config(profile, participant, cwd),
                        project_id=project_id,
                    )
                    await self._store().bind_participant_session(binding)
                    existing[participant["id"]] = binding
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
                    swarm_id, participant_id, expected_epoch=snapshot["epoch"]
                )
                initial = await group.delivery_receipt(
                    binding.address,
                    binding.generation_id,
                    f"initial:{binding.generation_id}",
                )
                input = (
                    TemporaryRunInput("initial", snapshot["prompt"], request_id)
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
                await self._store().record_run_started(
                    swarm_id,
                    participant_id,
                    run_id=admission.run_id,
                    expected_epoch=snapshot["epoch"],
                )
                admissions.append({"participant_id": participant_id, "run_id": admission.run_id})
            except Exception as error:
                await self._store().set_participant_state(swarm_id, participant_id, "failed")
                admissions.append({"participant_id": participant_id, "error": type(error).__name__})
        self._changed(swarm_id, snapshot["settings_revision"])
        return await self._store().finish_admission(
            swarm_id, request_id=request_id, kind="resume", runs=admissions
        )

    async def _admit_initial(
        self, swarm: Json, handle: Any, prompt: str, request_id: str
    ) -> list[Json]:
        assert self.host is not None and self.host.temporary_agents is not None
        admissions: list[Json] = []
        for participant in swarm["participants"]:
            admission = await self.host.temporary_agents.start(
                handle,
                participant["id"],
                TemporaryRunInput("initial", prompt, request_id),
            )
            await self._store().record_run_started(
                swarm["id"],
                participant["id"],
                run_id=admission.run_id,
                expected_epoch=swarm["epoch"],
            )
            admissions.append({"participant_id": participant["id"], "run_id": admission.run_id})
        return admissions

    async def _before_request(
        self, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        binding = context.binding
        owner = context.execution_owner
        if owner is None or owner.extension != "swarm" or binding.group_id != owner.group_id:
            raise SwarmStoreError("participant_inactive")
        swarm = await self._store().get_swarm(binding.group_id)
        await self._store().record_run_started(
            binding.group_id,
            binding.participant_id,
            run_id=context.run_id,
            expected_epoch=swarm["epoch"],
        )
        prepared = await self._store().prepare_automatic_delivery(
            binding.group_id,
            binding.participant_id,
            expected_epoch=swarm["epoch"],
        )
        if not prepared.get("entries"):
            return None
        entry = "\n\n".join(
            (
                _reminder(swarm, "delivery"),
                json.dumps(
                    {
                        "entries": prepared["entries"],
                        "pending_remaining": prepared["pending_remaining"],
                        "settings_revision": prepared["settings_revision"],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        return PreparedSessionDelivery(
            prepared["receipt_id"],
            prepared["content_hash"],
            (entry,),
            str(prepared["settings_revision"]),
            prepared["effect_kind"],
        )

    async def _acknowledge_delivery(
        self, _context: SessionRequestContext, delivery: PreparedSessionDelivery
    ) -> None:
        if not await self._store().reconcile_delivery(delivery.delivery_id):
            raise SwarmStoreError("delivery_unacknowledged")

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
        for _, receipt_id, _, _ in receipts:
            if not await self._store().reconcile_delivery(receipt_id):
                raise SwarmStoreError("delivery_unacknowledged")
        return ToolBatchDecision(end=turn_end_requested)

    async def _run_finished(self, context: SessionRequestContext, *, outcome: str) -> None:
        binding = context.binding
        owner = context.execution_owner
        if owner is None:
            return
        snapshot = await self._store().get_swarm(binding.group_id)
        terminal_outcome = {"success": "completed", "error": "failed"}.get(outcome, outcome)
        await self._store().reconcile_run_finished(
            binding.group_id,
            binding.participant_id,
            run_id=context.run_id,
            expected_epoch=snapshot["epoch"],
            outcome=terminal_outcome,
        )
        if terminal_outcome == "completed":
            self._enqueue_wakes(binding.group_id)
        self._changed(binding.group_id, snapshot["settings_revision"])


def _board_page(page: Page, arguments: Json) -> Json:
    result: Json = {"entries": list(page.entries), "has_more": page.has_more}
    if page.has_more:
        result["next_call"] = {
            "tool": "swarm_board",
            "arguments": {**arguments, "cursor": page.cursor},
        }
    return result


def _management_page(page: Page) -> Json:
    result: Json = {"entries": list(page.entries), "has_more": page.has_more}
    if page.has_more:
        result["cursor"] = page.cursor
    return result


def _exact(arguments: Json, allowed: set[str], *, required: set[str] | None = None) -> None:
    if set(arguments) - allowed or not (required or set()).issubset(arguments):
        raise SwarmStoreError("invalid_arguments")


def _string(arguments: Json, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise SwarmStoreError("invalid_arguments", field=key)
    return value


def _integer(arguments: Json, key: str, *, minimum: int) -> int:
    value = arguments.get(key)
    if type(value) is not int or value < minimum:
        raise SwarmStoreError("invalid_arguments", field=key)
    return value


def _page_arguments(arguments: Json) -> Json:
    _exact(arguments, {"cursor", "limit"})
    result: Json = {}
    if "cursor" in arguments:
        result["cursor"] = _string(arguments, "cursor")
    if "limit" in arguments:
        result["limit"] = _integer(arguments, "limit", minimum=1)
    return result


def _pagination_arguments(arguments: Json) -> Json:
    return _page_arguments(
        {key: value for key, value in arguments.items() if key in {"cursor", "limit"}}
    )


async def _profile_cwd(profile: Json, catalog: Json) -> tuple[Path, str | None]:
    working = profile["working_directory"]
    if working["kind"] == "directory":
        path = Path(working["path"])
        if not path.is_absolute() or not await run_tool_worker(lambda: path.is_dir()):
            raise SwarmStoreError("invalid_arguments", field="working_directory")
        return path, None
    project = next(
        (item for item in catalog.get("projects", []) if item.get("id") == working["project_id"]),
        None,
    )
    if project is None:
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    path = Path(project["cwd"])
    if not await run_tool_worker(lambda: path.is_dir()):
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    return path, working["project_id"]


def _swarm_projection(swarm: Json) -> Json:
    result = dict(swarm)
    result.pop("execution_epoch", None)
    return result


def _validate_profile_catalog(profile: Json, catalog: Json) -> None:
    known_models = {item.get("id") for item in catalog.get("models", [])}
    if any(item["model"] not in known_models for item in profile["participants"]):
        raise SwarmStoreError("invalid_arguments", field="participants")
    known_tools = {item.get("name") for item in catalog.get("tools", [])}
    allowed = profile["tool_access"].get("allowed", [])
    if any(item not in known_tools for item in allowed):
        raise SwarmStoreError("invalid_arguments", field="tool_access")
    project = next(
        (
            item
            for item in catalog.get("projects", [])
            if item.get("id") == profile["working_directory"].get("project_id")
        ),
        None,
    )
    known_skills = {item.get("name") for item in catalog.get("skills", [])}
    known_skills.update((project or {}).get("allowed_skills", []))
    if any(item != "*" and item not in known_skills for item in profile["allowed_skills"]):
        raise SwarmStoreError("invalid_arguments", field="allowed_skills")


def _participant_config(profile: Json, participant: Json, cwd: Path) -> TemporaryAgentConfig:
    ordinal = participant["ordinal"]
    formation = next(
        item for item in profile["participants"] if (ordinal := ordinal - item["count"]) <= 0
    )
    return TemporaryAgentConfig(
        model=participant["model"],
        cwd=cwd,
        tool_access=normalize_tool_access(profile["tool_access"]),
        allowed_skills=profile["allowed_skills"],
        tools=profile["tools"],
        name=participant["display_name"],
        temperature=formation.get("temperature"),
        thinking_effort=formation.get("thinking_effort"),
        fallback_models=formation.get("fallback_models", []),
        instructions=profile["instructions"],
        prompt_blocks=["core:agent_body", *profile["prompt_blocks"]],
    )


def _reminder(swarm: Json, event: str) -> str:
    enabled = swarm["profile_snapshot"]["reminders"][event]
    return REMINDER_TEXTS[event] if enabled else ""


def _swarm_command_argument(argument: str) -> tuple[str, str]:
    profile_id, separator, remainder = argument.strip().partition(" ")
    prompt = remainder.strip()
    if not profile_id or not separator or not prompt:
        raise SwarmStoreError("invalid_arguments", field="argument")
    if prompt.startswith('"'):
        try:
            value = json.loads(prompt)
        except json.JSONDecodeError as error:
            raise SwarmStoreError("invalid_arguments", field="argument") from error
        if not isinstance(value, str) or not value.strip():
            raise SwarmStoreError("invalid_arguments", field="argument")
        prompt = value
    return profile_id, prompt


def _failure(
    error: SwarmStoreError, arguments: Json | None = None, parameters: Json | None = None
) -> Json:
    code = error.code
    if code in {"stale_epoch", "swarm_not_found"}:
        code = "swarm_closed"
    elif code == "participant_not_found":
        code = "participant_inactive"
    guidance = ERRORS.get(code, ERRORS["invalid_arguments"])
    if code == "exact_message_arguments":
        code = "invalid_arguments"
    if error.field:
        field = error.field
        properties = (parameters or {}).get("properties", {})
        if code == "inapplicable_field":
            code = "invalid_arguments"
            action = (arguments or {}).get("action")
            guidance = (
                f"Field '{field}' is not accepted for action '{action}'. "
                "Omit it. No change was applied."
            )
        elif field in properties:
            specification = properties[field]
            correction = specification["description"]
            if "enum" in specification:
                correction += " Choose one of: " + ", ".join(specification["enum"]) + "."
            guidance = f"{field}: {correction} No change was applied."
        elif parameters is not None:
            action = (arguments or {}).get("action")
            guidance = (
                f"Field '{field}' is not accepted"
                + (f" for action '{action}'" if isinstance(action, str) else "")
                + ". Omit it. No change was applied."
            )
        else:
            guidance = f"{field}: {ERRORS['invalid_value']}"
    return tool_failure(code, guidance)


def _validate_board(arguments: Json) -> str:
    fields = {
        "list": {"cursor", "limit"},
        "read": {"discussion_id", "message_id", "cursor", "limit"},
        "post": {"discussion_id", "text", "reply_to", "recipients", "request_id"},
        "create": {"title", "text", "request_id", "recipients"},
        "join": {"discussion_id"},
        "leave": {"discussion_id"},
    }
    action = arguments.get("action")
    if not isinstance(action, str) or action not in fields:
        raise SwarmStoreError("invalid_arguments", field="action")
    unexpected = sorted(set(arguments) - {"action", *fields[action]})
    if unexpected:
        raise SwarmStoreError("inapplicable_field", field=unexpected[0])
    required = {
        "post": {"text", "request_id"},
        "create": {"title", "text", "request_id"},
        "join": {"discussion_id"},
        "leave": {"discussion_id"},
    }
    for key in required.get(action, set()):
        if key not in arguments:
            raise SwarmStoreError("invalid_arguments", field=key)
    for key, value in arguments.items():
        if key == "limit":
            valid = type(value) is int and 1 <= value <= 100
        elif key == "recipients":
            valid = isinstance(value, list) and all(
                isinstance(item, str) and item for item in value
            )
        else:
            maximum = {"text": 16000, "title": 120, "request_id": 128}.get(key)
            valid = (
                isinstance(value, str)
                and bool(value.strip())
                and (maximum is None or len(value) <= maximum)
            )
        if not valid:
            raise SwarmStoreError("invalid_arguments", field=key)
    if "message_id" in arguments and {"discussion_id", "cursor", "limit"} & arguments.keys():
        raise SwarmStoreError("exact_message_arguments")
    return action


def _validate_state(arguments: Json) -> None:
    unexpected = sorted(set(arguments) - {"cursor", "limit"})
    if unexpected:
        raise SwarmStoreError("inapplicable_field", field=unexpected[0])
    for key, value in arguments.items():
        valid = (key == "limit" and type(value) is int and 1 <= value <= 100) or (
            key == "cursor" and isinstance(value, str) and bool(value.strip())
        )
        if not valid:
            raise SwarmStoreError("invalid_arguments", field=key)


def register(api: ExtensionAPI) -> None:
    service = SwarmExtension(api)
    api.operations.startup.append(service.start)
    api.on_shutdown(service.close)
    api.register_session_tool("swarm_board", BOARD_DESCRIPTION, BOARD_PARAMETERS, service.board)
    api.register_session_tool("swarm_inbox", INBOX_DESCRIPTION, INBOX_PARAMETERS, service.inbox)
    api.register_session_tool(
        "swarm_state", STATE_DESCRIPTION, STATE_PARAMETERS, service.state, parallel_safe=False
    )
    api.register_session_runtime(
        before_request=service._before_request,
        run_finished=service._run_finished,
        quiesce=service._quiesce,
        acknowledge_delivery=service._acknowledge_delivery,
        reconcile_tool_batch=service._reconcile_tool_batch,
    )
    api.register_page("swarms", "Swarms", "web/page.html")
    api.register_command(
        "swarm",
        "Start a Swarm from a profile and goal.",
        service.command,
        argument="optional",
        execution_mode="immediate",
    )
    for name, schema in _OPERATION_SCHEMAS.items():
        api.operations.register(
            name,
            "Swarm management operation.",
            schema,
            _operation_handler(service, name),
        )


def _operation_handler(service: SwarmExtension, name: str) -> Callable[[Json], Awaitable[Json]]:
    async def handler(arguments: Json) -> Json:
        return await service.operation(name, arguments)

    return handler


_PAGE = {
    "type": "object",
    "properties": {
        "cursor": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "additionalProperties": False,
}
_OPERATION_SCHEMAS: dict[str, Json] = {
    "profiles.preview": {
        "type": "object",
        "properties": {
            "profile": {"type": "object"},
            "formation_index": {"type": "integer", "minimum": 0},
        },
        "required": ["profile"],
        "additionalProperties": False,
    },
    "catalog": {"type": "object", "properties": {}, "additionalProperties": False},
    "profiles.list": _PAGE,
    "profiles.get": {
        "type": "object",
        "properties": {"profile_id": {"type": "string", "minLength": 1}},
        "required": ["profile_id"],
        "additionalProperties": False,
    },
    "profiles.save": {
        "type": "object",
        "properties": {
            "profile": {"type": "object"},
            "expected_revision": {"type": ["integer", "null"], "minimum": 1},
        },
        "required": ["profile", "expected_revision"],
        "additionalProperties": False,
    },
    "profiles.delete": {
        "type": "object",
        "properties": {
            "profile_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["profile_id", "expected_revision"],
        "additionalProperties": False,
    },
    "swarms.list": _PAGE,
    "swarms.get": {
        "type": "object",
        "properties": {"swarm_id": {"type": "string", "minLength": 1}},
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "swarms.delete": {
        "type": "object",
        "properties": {"swarm_id": {"type": "string", "minLength": 1}},
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "swarms.events": {
        "type": "object",
        "properties": {
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "swarm_id": {"type": "string", "minLength": 1},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "swarms.settings": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "delivery": {"type": "object"},
            "expected_revision": {"type": "integer", "minimum": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "delivery", "expected_revision", "request_id"],
        "additionalProperties": False,
    },
    "swarms.start": {
        "type": "object",
        "properties": {
            "profile_id": {"type": "string", "minLength": 1},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 16000},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "expected_profile_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["profile_id", "prompt", "request_id"],
        "additionalProperties": False,
    },
    "swarms.stop": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "request_id"],
        "additionalProperties": False,
    },
    "swarms.resume": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "participant_id": {"type": "string", "minLength": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "request_id"],
        "additionalProperties": False,
    },
    "swarms.usage": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "participant_id": {"type": "string", "minLength": 1},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "board.list": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "board.read": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "discussion_id": {"type": "string", "minLength": 1},
            "message_id": {"type": "string", "minLength": 1},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "board.post": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "discussion_id": {"type": "string", "minLength": 1},
            "text": {"type": "string", "minLength": 1, "maxLength": 16000},
            "reply_to": {"type": "string", "minLength": 1},
            "recipients": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "text", "request_id"],
        "additionalProperties": False,
    },
}
