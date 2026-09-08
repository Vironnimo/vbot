"""Swarm coordination through owner-bound Extension capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from core.agents.temporary import TemporaryAgentConfig, TemporaryRunInput
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

from .agent_text import (
    BOARD_DESCRIPTION,
    BOARD_PARAMETERS,
    COMPLETION_RACE_REMINDER,
    DELIVERY_PREFIX,
    EMPTY_INBOX,
    ERRORS,
    INBOX_DESCRIPTION,
    INBOX_PARAMETERS,
    POST_SAVED,
    REPLAYED,
    RESUME_REMINDER,
)
from .store import Page, SwarmStore, SwarmStoreError

Json = dict[str, Any]


class SwarmExtension:
    """Own registration state, bound participants, and complete Board operations."""

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.store: SwarmStore | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def start(self, host: ExtensionHost) -> None:
        if host.state_dir is None or host.temporary_agents is None:
            raise RuntimeError("Swarm requires the owner-bound Extension host")
        self.host = host
        groups = host.temporary_agents

        async def receipt(address: SessionAddress, generation: str, owner: str, receipt_id: str):
            if owner != "swarm":
                return None
            return await groups.delivery_receipt(address, generation, receipt_id)

        async def terminal(swarm_id: str, run_id: str):
            try:
                return (await groups.owned_run(swarm_id, run_id)).record
            except Exception:
                return None

        async def descendants(swarm_id: str, participant_id: str, run_id: str, _epoch: int) -> bool:
            snapshot = await self._store().get_swarm(swarm_id)
            execution_epoch = snapshot["execution_epoch"]
            return bool(
                execution_epoch
                and groups.has_descendants(swarm_id, participant_id, run_id, execution_epoch)
            )

        self.store = SwarmStore(
            host.state_dir / "swarm.db",
            lookup_delivery_receipt=receipt,
            lookup_terminal_proof=terminal,
            has_owned_descendants=descendants,
        )
        await self.store.open()
        await self.store.recover_interrupted()

    async def close(self) -> None:
        if self.host is not None and self.host.temporary_agents is not None:
            await self.host.temporary_agents.quiesce()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        if self.store is not None:
            await self.store.close()
            self.store = None
        self.host = None

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
            return tool_success(data)
        except SwarmStoreError as error:
            return _failure(error)

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
            if set(arguments) - {"limit"}:
                raise SwarmStoreError("invalid_arguments")
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
            return _failure(error)

    def _changed(self, swarm_id: str, revision: int) -> None:
        if self.host is not None and self.host.publish_change is not None:
            self.host.publish_change("swarms", [swarm_id], revision)

    async def operation(self, name: str, arguments: Json) -> Json:
        """Run a management operation without exposing runtime services to the page."""

        try:
            handlers: dict[str, Callable[[Json], Awaitable[Json]]] = {
                "catalog": self._catalog,
                "profiles.list": self._profiles_list,
                "profiles.get": self._profiles_get,
                "profiles.save": self._profiles_save,
                "profiles.delete": self._profiles_delete,
                "swarms.list": self._swarms_list,
                "swarms.get": self._swarms_get,
                "swarms.events": self._swarms_events,
                "swarms.settings": self._swarms_settings,
                "swarms.stop": self._swarms_stop,
                "board.list": self._board_list,
                "board.read": self._board_read,
                "board.post": self._board_post,
            }
            return await handlers[name](arguments)
        except SwarmStoreError as error:
            raise ValueError(error.code) from error

    async def _catalog(self, _arguments: Json) -> Json:
        host = self.host
        if host is None or host.catalog is None:
            return {"catalog": {}}
        return {"catalog": await host.catalog()}

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
        return {
            "swarm": _swarm_projection(
                await self._store().get_swarm(_string(arguments, "swarm_id"))
            )
        }

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
        self._changed(swarm_id, result["settings_revision"])
        return result

    async def _swarms_stop(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "request_id"})
        result = await self._store().begin_stop(
            _string(arguments, "swarm_id"),
            request_id=_string(arguments, "request_id"),
            actor="user",
        )
        # A stop needs group draining, which only exists after full participant
        # lifecycle registration. Keep partial Swarms impossible to start.
        self._changed(result["swarm_id"], 0)
        return result

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
        return result

    async def _start_swarm(self, profile_id: str, prompt: str, request_id: str) -> Json:
        """Prepare every bound participant before admitting any initial Run.

        This is intentionally private until the complete three-Tool grant set is
        registered. Keeping it here makes the eventual operation use the same
        all-or-stop path as Resume rather than a UI-specific shortcut.
        """

        host = self.host
        if host is None or host.temporary_agents is None or host.catalog is None:
            raise SwarmStoreError("swarm_closed")
        profile = await self._store().get_profile(profile_id)
        catalog = await host.catalog()
        cwd, project_id = await _profile_cwd(profile, catalog)
        _validate_profile_catalog(profile, catalog)
        effective = {"cwd": str(cwd), "project_id": project_id}
        swarm = await self._store().create_swarm(
            profile_id,
            prompt,
            effective,
            request_id=request_id,
            expected_profile_revision=profile["revision"],
        )
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
            await self._store().set_swarm_state(snapshot["id"], "needs_attention")
            raise
        self._changed(snapshot["id"], snapshot["settings_revision"])
        return {**swarm, "runs": admissions}

    async def _resume_swarm(self, swarm_id: str, request_id: str) -> Json:
        host = self.host
        if host is None or host.temporary_agents is None:
            raise SwarmStoreError("swarm_closed")
        resumed = await self._store().begin_resume(swarm_id, request_id=request_id, actor="user")
        snapshot = await self._store().get_swarm(swarm_id)
        group = host.temporary_agents
        handle = await group.open_group(swarm_id)
        await self._store().bind_execution_epoch(
            swarm_id, expected_epoch=snapshot["epoch"], execution_epoch=handle.epoch
        )
        bindings = {binding.participant_id: binding for binding in await group.list(swarm_id)}
        admissions: list[Json] = []
        for participant_id in resumed["participant_ids"]:
            try:
                binding = bindings[participant_id]
                initial = await group.delivery_receipt(
                    binding.address,
                    binding.generation_id,
                    f"initial:{binding.generation_id}",
                )
                input = (
                    TemporaryRunInput("initial", snapshot["prompt"], request_id)
                    if initial is None
                    else TemporaryRunInput("continuation", RESUME_REMINDER, request_id)
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
        return {**resumed, "runs": admissions}

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
                DELIVERY_PREFIX,
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
        binding = context.binding
        owner = context.execution_owner
        if owner is None:
            return ToolBatchDecision(end=turn_end_requested)
        for _, receipt_id, _, _ in receipts:
            if not await self._store().reconcile_delivery(receipt_id):
                raise SwarmStoreError("delivery_unacknowledged")
        result = await self._store().reconcile_tool_batch(
            binding.group_id,
            binding.participant_id,
            run_id=context.run_id,
            expected_epoch=(await self._store().get_swarm(binding.group_id))["epoch"],
            persisted_call_ids=persisted_call_ids,
        )
        if result["continuation_required"]:
            continuation_id = hashlib.sha256(
                "\0".join(sorted(persisted_call_ids)).encode()
            ).hexdigest()
            return ToolBatchDecision(
                end=False,
                continuation=PreparedSessionDelivery(
                    f"completion:{continuation_id}",
                    continuation_id,
                    (COMPLETION_RACE_REMINDER,),
                    "0",
                    "swarm_completion_race",
                ),
            )
        return ToolBatchDecision(end=bool(result["end_run"]) or turn_end_requested)

    async def _run_finished(self, context: SessionRequestContext, *, outcome: str) -> None:
        binding = context.binding
        owner = context.execution_owner
        if owner is None:
            return
        snapshot = await self._store().get_swarm(binding.group_id)
        await self._store().reconcile_run_finished(
            binding.group_id,
            binding.participant_id,
            run_id=context.run_id,
            expected_epoch=snapshot["epoch"],
            outcome=outcome,
        )
        if outcome == "completed":
            finalized = await self._store().finalize_participant(
                binding.group_id,
                binding.participant_id,
                run_id=context.run_id,
                expected_epoch=snapshot["epoch"],
            )
            if finalized["state"] == "done":
                task = asyncio.create_task(self._close_completed_group(binding.group_id))
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)

    async def _close_completed_group(self, swarm_id: str) -> None:
        """Drain after the terminal callback returns; never await the current Run here."""

        await asyncio.sleep(0)
        swarm = await self._store().get_swarm(swarm_id)
        if all(participant["state"] == "done" for participant in swarm["participants"]):
            assert self.host is not None and self.host.temporary_agents is not None
            report = await self.host.temporary_agents.close_group(swarm_id)
            await self._store().finish_group(
                swarm_id, expected_epoch=swarm["epoch"], drain_report=report
            )


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
    result["participants"] = [
        {
            key: value
            for key, value in participant.items()
            if key not in {"summary_json", "artifacts_json"}
        }
        for participant in swarm["participants"]
    ]
    return result


def _validate_profile_catalog(profile: Json, catalog: Json) -> None:
    known_models = {item.get("id") for item in catalog.get("models", [])}
    if any(item["model"] not in known_models for item in profile["participants"]):
        raise SwarmStoreError("invalid_arguments", field="participants")
    known_tools = {item.get("name") for item in catalog.get("tools", [])}
    allowed = profile["tool_access"].get("allowed", [])
    if any(item not in known_tools for item in allowed):
        raise SwarmStoreError("invalid_arguments", field="tool_access")
    known_skills = {item.get("name") for item in catalog.get("skills", [])}
    if any(item != "*" and item not in known_skills for item in profile["allowed_skills"]):
        raise SwarmStoreError("invalid_arguments", field="allowed_skills")


def _participant_config(profile: Json, participant: Json, cwd: Path) -> TemporaryAgentConfig:
    formation = next(
        item for item in profile["participants"] if item["model"] == participant["model"]
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
    )


def _failure(error: SwarmStoreError) -> Json:
    code = error.code
    if code in {"stale_epoch", "swarm_not_found"}:
        code = "swarm_closed"
    elif code == "participant_not_found":
        code = "participant_inactive"
    guidance = ERRORS.get(code, ERRORS["invalid_arguments"])
    if error.field:
        guidance = f"{error.field}: {ERRORS['invalid_value']}"
    return tool_failure(code, guidance)


def _validate_board(arguments: Json) -> str:
    fields = {
        "list": {"cursor", "limit"},
        "read": {"discussion_id", "message_id", "cursor", "limit"},
        "post": {"discussion_id", "text", "reply_to", "recipients", "request_id"},
        "create": {"title", "text", "request_id"},
        "join": {"discussion_id"},
        "leave": {"discussion_id"},
    }
    action = arguments.get("action")
    if (
        not isinstance(action, str)
        or action not in fields
        or set(arguments) - {"action", *fields[action]}
    ):
        raise SwarmStoreError("invalid_arguments")
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
        raise SwarmStoreError("invalid_arguments", field="message_id")
    return action


def register(api: ExtensionAPI) -> None:
    service = SwarmExtension(api)
    api.operations.startup.append(service.start)
    api.on_shutdown(service.close)
    api.register_session_tool("swarm_board", BOARD_DESCRIPTION, BOARD_PARAMETERS, service.board)
    api.register_session_tool("swarm_inbox", INBOX_DESCRIPTION, INBOX_PARAMETERS, service.inbox)
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
    "swarms.stop": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "request_id"],
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
