"""Swarm management, profile editing and human Board operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from core.utils.logging import get_logger

from ._extension_values import (
    Json,
    _exact,
    _integer,
    _management_page,
    _page_arguments,
    _pagination_arguments,
    _participant_config,
    _string,
    _swarm_projection,
    _validate_profile_catalog,
)
from ._registration import session_tool_catalog
from ._store_values import _validate_profile
from ._store_wiki import MUTATIONS
from .agent_text import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_PROMPT_BLOCKS,
    DEFAULT_REMINDERS,
    REMINDER_TEXTS,
)
from .store import SwarmStoreError
from .wiki_text import WIKI_ERRORS

if TYPE_CHECKING:
    from .extension import SwarmExtension

_LOGGER = get_logger("extensions.swarm")


def _changed_profile_fields(previous: Json | None, saved: Json, expected_revision: int) -> str:
    """Names of the profile fields an update changed, for its log line."""
    if previous is None or previous.get("revision") != expected_revision:
        return "unknown"  # Another save landed between the read and this one.
    names = sorted(
        name
        for name in set(previous) | set(saved)
        if name != "revision" and previous.get(name) != saved.get(name)
    )
    return ",".join(names) or "none"


class SwarmManagement:
    """Serialize operator mutations while execution remains with the Extension."""

    def __init__(self, extension: SwarmExtension) -> None:
        self._extension = extension
        self._control_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            WeakValueDictionary()
        )

    def _control(self, kind: str, identity: str) -> asyncio.Lock:
        key = (kind, identity)
        lock = self._control_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._control_locks[key] = lock
        return lock

    def control(self, swarm_id: str) -> asyncio.Lock:
        """Serialize one Swarm's lifecycle and wakes, without blocking other Swarms."""
        return self._control("swarm", swarm_id)

    async def _wiki_operation(self, arguments: Json) -> Json:
        sid = _string(arguments, "swarm_id")
        values = {key: value for key, value in arguments.items() if key != "swarm_id"}
        try:
            data = await self._extension._store().wiki(sid, None, values)
        except SwarmStoreError as error:
            if error.code in WIKI_ERRORS:
                raise ValueError(WIKI_ERRORS[error.code]) from error
            raise
        if values["action"] in MUTATIONS and not data.get("replayed") and not data.get("unchanged"):
            swarm = await self._extension._store().get_swarm(sid)
            self._extension._changed("wiki", sid, swarm["settings_revision"])
        return data

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
                "wiki": self._wiki_operation,
            }
            if name in {
                "swarms.start",
                "swarms.stop",
                "swarms.resume",
                "swarms.delete",
                "board.post",
            }:
                lock = (
                    self._control("start", _string(arguments, "request_id"))
                    if name == "swarms.start"
                    else self.control(_string(arguments, "swarm_id"))
                )
                async with lock:
                    return await handlers[name](arguments)
            return await handlers[name](arguments)
        except SwarmStoreError as error:
            # The page words the code; the log keeps the field it concerns.
            _LOGGER.warning(
                "Swarm operation rejected (operation=%s code=%s field=%s)",
                name,
                error.code,
                error.field or "-",
            )
            raise ValueError(error.code) from error

    async def _catalog(self, _arguments: Json) -> Json:
        host = self._extension.host
        catalog = await host.catalog() if host is not None and host.catalog is not None else {}
        catalog["prompt_defaults"] = {
            "instructions": DEFAULT_INSTRUCTIONS,
            "prompt_blocks": DEFAULT_PROMPT_BLOCKS,
            "reminders": DEFAULT_REMINDERS,
        }
        catalog["reminder_texts"] = REMINDER_TEXTS
        catalog["tools"] = [*catalog.get("tools", []), *session_tool_catalog()]
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
        host = self._extension.host
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
        page = await self._extension._store().list_profiles(**_page_arguments(arguments))
        return _management_page(page)

    async def _profiles_get(self, arguments: Json) -> Json:
        _exact(arguments, {"profile_id"})
        return {
            "profile": await self._extension._store().get_profile(_string(arguments, "profile_id"))
        }

    async def _profiles_save(self, arguments: Json) -> Json:
        _exact(arguments, {"profile", "expected_revision"})
        profile = arguments.get("profile")
        if not isinstance(profile, dict):
            raise SwarmStoreError("invalid_arguments", field="profile")
        expected = arguments.get("expected_revision")
        if expected is not None and type(expected) is not int:
            raise SwarmStoreError("invalid_arguments", field="expected_revision")
        previous = None
        if expected is not None and isinstance(profile.get("id"), str):
            try:
                previous = await self._extension._store().get_profile(profile["id"])
            except SwarmStoreError:
                previous = None  # The save reports the missing profile.
        saved = await self._extension._store().save_profile(profile, expected_revision=expected)
        self._extension._changed("profiles", saved["id"], saved["revision"])
        if expected is None:
            _LOGGER.info("Swarm profile created (profile=%s)", saved["id"])
        else:
            _LOGGER.info(
                "Swarm profile updated (profile=%s revision=%s fields=%s)",
                saved["id"],
                saved["revision"],
                _changed_profile_fields(previous, saved, expected),
            )
        return {"profile": saved}

    async def _profiles_delete(self, arguments: Json) -> Json:
        _exact(arguments, {"profile_id", "expected_revision"})
        profile_id = _string(arguments, "profile_id")
        revision = _integer(arguments, "expected_revision", minimum=1)
        await self._extension._store().delete_profile(profile_id, expected_revision=revision)
        self._extension._changed("profiles", profile_id, revision)
        _LOGGER.info("Swarm profile deleted (profile=%s)", profile_id)
        return {"profile_id": profile_id, "deleted": True}

    async def _swarms_list(self, arguments: Json) -> Json:
        page = await self._extension._store().list_swarms(**_page_arguments(arguments))
        result = _management_page(page)
        host = self._extension.host
        if host is not None and host.temporary_agents is not None and result["entries"]:
            # Generated Run titles live with the canonical execution group; Runs
            # started before titles existed keep their first goal line.
            titles = await host.temporary_agents.group_titles(
                [entry["id"] for entry in result["entries"]]
            )
            result["entries"] = [
                {**entry, "title": titles.get(entry["id"], entry["title"])}
                for entry in result["entries"]
            ]
        return result

    async def _swarms_get(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "include_snapshot"}, required={"swarm_id"})
        include_snapshot = arguments.get("include_snapshot", True)
        if type(include_snapshot) is not bool:
            raise SwarmStoreError("invalid_arguments", field="include_snapshot")
        swarm = await self._extension._store().get_swarm(
            _string(arguments, "swarm_id"), include_snapshot=include_snapshot
        )
        host = self._extension.host
        groups = None if host is None else host.temporary_agents
        run_ids = [
            participant["lifecycle_run_id"]
            for participant in swarm["participants"]
            if participant.get("lifecycle_run_id")
        ]
        inspections = (
            await groups.owned_runs(swarm["id"], run_ids) if groups is not None and run_ids else {}
        )
        for participant in swarm["participants"]:
            inspection = inspections.get(participant.get("lifecycle_run_id") or "")
            participant["run_active"] = (
                inspection is not None
                and inspection.run is not None
                and inspection.run.status.value == "running"
            )
        # The page links "#46" and "w7" in Markdown only when they name a post or page.
        swarm["newest_post_sequence"] = await self._extension._store().newest_post_number(
            swarm["id"]
        )
        swarm["newest_wiki_page_number"] = await self._extension._store().newest_wiki_page_number(
            swarm["id"]
        )
        return {"swarm": _swarm_projection(swarm)}

    async def _swarms_events(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "cursor", "limit"}, required={"swarm_id"})
        page = await self._extension._store().list_events(
            _string(arguments, "swarm_id"), **_pagination_arguments(arguments)
        )
        return _management_page(page)

    async def _swarms_settings(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "delivery", "expected_revision", "request_id"})
        swarm_id = _string(arguments, "swarm_id")
        delivery = arguments.get("delivery")
        if not isinstance(delivery, dict):
            raise SwarmStoreError("invalid_arguments", field="delivery")
        result = await self._extension._store().apply_delivery_settings(
            swarm_id,
            delivery,
            expected_revision=_integer(arguments, "expected_revision", minimum=1),
            request_id=_string(arguments, "request_id"),
            actor="user",
        )
        self._extension._changed("swarms", swarm_id, result["revision"])
        self._extension._enqueue_wakes(swarm_id)
        routes = sorted(
            route
            for route in set(result["old"]) | set(result["new"])
            if result["old"].get(route) != result["new"].get(route)
        )
        if routes and not result.get("replayed"):
            _LOGGER.info(
                "Swarm delivery settings updated (swarm=%s revision=%s routes=%s)",
                swarm_id,
                result["revision"],
                ",".join(routes),
            )
        return result

    async def _swarms_delete(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id"})
        swarm_id = _string(arguments, "swarm_id")
        if await self._extension._store().begin_delete(swarm_id):
            self._extension._changed("swarms", swarm_id)
            task = self._extension._wake_tasks.get(swarm_id)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._extension._wake_dirty.discard(swarm_id)
            self._extension._pacing.forget(swarm_id)
            host = self._extension.host
            if host is None or host.temporary_agents is None:
                raise SwarmStoreError("swarm_closed")
            await host.temporary_agents.delete_group(swarm_id)
            await self._extension._store().delete_swarm(swarm_id)
            _LOGGER.info("Swarm deleted (swarm=%s)", swarm_id)
        self._extension._changed("swarms", swarm_id)
        return {"swarm_id": swarm_id, "deleted": True}

    async def _swarms_stop(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "request_id"})
        result = await self._extension._store().begin_stop(
            _string(arguments, "swarm_id"),
            request_id=_string(arguments, "request_id"),
            actor="user",
        )
        snapshot = await self._extension._store().get_swarm(result["swarm_id"])
        if (
            result["state"] != "stopping"
            or snapshot["state"] != "stopping"
            or result["epoch"] != snapshot["epoch"]
        ):
            return result
        host = self._extension.host
        if host is None or host.temporary_agents is None:
            raise SwarmStoreError("swarm_closed")
        task = self._extension._wake_tasks.get(result["swarm_id"])
        if task is not None:
            task.cancel()
        self._extension._pacing.forget(result["swarm_id"])
        report = await host.temporary_agents.close_group(result["swarm_id"], reason="swarm_stop")
        finished = await self._extension._store().finish_stop(
            result["swarm_id"],
            request_id=_string(arguments, "request_id"),
            actor="user",
            drain_report=report,
        )
        self._extension._changed("swarms", result["swarm_id"])
        _LOGGER.info("Swarm stopped (swarm=%s state=%s)", result["swarm_id"], finished["state"])
        return finished

    async def _swarms_start(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {
                "profile_id",
                "prompt",
                "request_id",
                "expected_profile_revision",
                "working_directory",
            },
            required={"profile_id", "prompt", "request_id"},
        )
        return await self._extension._start_swarm(
            _string(arguments, "profile_id"),
            _string(arguments, "prompt"),
            _string(arguments, "request_id"),
            expected_profile_revision=(
                _integer(arguments, "expected_profile_revision", minimum=1)
                if "expected_profile_revision" in arguments
                else None
            ),
            working_directory=(
                _string(arguments, "working_directory")
                if "working_directory" in arguments
                else None
            ),
        )

    async def _swarms_resume(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"swarm_id", "request_id", "participant_id"},
            required={"swarm_id", "request_id"},
        )
        return await self._extension._resume_swarm(
            _string(arguments, "swarm_id"),
            _string(arguments, "request_id"),
            participant_id=_string(arguments, "participant_id")
            if "participant_id" in arguments
            else None,
            reason="request",
        )

    async def _swarms_usage(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "participant_id"}, required={"swarm_id"})
        host = self._extension.host
        if host is None or host.temporary_agents is None:
            raise SwarmStoreError("swarm_closed")
        query = {key: arguments[key] for key in {"participant_id"} if key in arguments}
        return {"usage": await host.temporary_agents.usage(_string(arguments, "swarm_id"), query)}

    async def _board_list(self, arguments: Json) -> Json:
        _exact(arguments, {"swarm_id", "cursor", "limit"}, required={"swarm_id"})
        page = await self._extension._store().list_human_discussions(
            _string(arguments, "swarm_id"), **_pagination_arguments(arguments)
        )
        return _management_page(page)

    async def _board_read(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"swarm_id", "discussion_id", "message_id", "cursor", "limit", "after", "before"},
            required={"swarm_id"},
        )
        kwargs = {key: value for key, value in arguments.items() if key != "swarm_id"}
        page = await self._extension._store().read_human_posts(
            _string(arguments, "swarm_id"), **kwargs
        )
        return _management_page(page)

    async def _board_post(self, arguments: Json) -> Json:
        _exact(
            arguments,
            {"swarm_id", "discussion_id", "text", "reply_to", "recipients", "request_id"},
            required={"swarm_id", "text", "request_id"},
        )
        swarm_id = _string(arguments, "swarm_id")
        result = await self._extension._store().post_human(
            swarm_id,
            **{key: value for key, value in arguments.items() if key != "swarm_id"},
        )
        snapshot = await self._extension._store().get_swarm(swarm_id)
        if not result.get("replayed") and snapshot["state"] in {
            "stopped",
            "cancelled",
            "interrupted",
            "failed",
        }:
            try:
                resumed = await self._extension._resume_swarm(
                    swarm_id, f"post:{result['post_id']}", reason="post"
                )
                result["runs"] = resumed.get("runs", [])
            except Exception:
                _LOGGER.warning(
                    "Swarm message saved but resume failed (swarm=%s)", swarm_id, exc_info=True
                )
                result["resume_failed"] = True
        if not result.get("replayed"):
            self._extension._changed(
                "discussion_posts",
                f"{swarm_id}/{result['discussion_id']}",
                snapshot["settings_revision"],
            )
        self._extension._enqueue_wakes(swarm_id)
        return result
