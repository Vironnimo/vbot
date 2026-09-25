"""swarm_board actions as an Agent calls them.

``prepare_board_call`` checks a normalized call and drops harmless extras.
``BoardCall`` runs one action against the Store, corrects read-only references
it can identify uniquely, explains any other failure with the exact next call,
and renders the result as readable text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.sessions import TemporarySessionBinding
from core.tools import ToolContext

from . import agent_text as text
from ._board_view import (
    Roster,
    call_text,
    close_discussion,
    discussion_choices,
    discussion_label,
    discussions_text,
    post_block,
    post_suggestion,
    posts_text,
    queued_text,
)
from ._extension_values import AgentCallError, Json, _call_request_id
from .store import Page, SwarmStoreError

if TYPE_CHECKING:
    from .extension import SwarmExtension

_FIELDS = {
    "list": {"cursor", "limit"},
    "read": {"discussion_id", "message_id", "before", "cursor", "limit"},
    "post": {"discussion_id", "text", "reply_to", "recipients"},
    "create": {"title", "text", "recipients"},
    "join": {"discussion_id"},
    "leave": {"discussion_id"},
}
_REQUIRED = {
    "post": ("text",),
    "create": ("title", "text"),
    "join": ("discussion_id",),
    "leave": ("discussion_id",),
}
# Paging fields request nothing an action without pages could do.
_PAGING = frozenset({"cursor", "limit", "before"})
_REFERENCES = ("discussion_id", "message_id", "reply_to", "before")
_MAX_LIMIT = 100
_MAX_TEXT = {"text": 16000, "title": 120}


def prepare_board_call(arguments: Json) -> tuple[str, list[str]]:
    """Check a normalized call in place; return its action and notes on dropped fields."""

    action = arguments.get("action")
    if not isinstance(action, str) or action not in _FIELDS:
        raise SwarmStoreError("invalid_arguments", field="action")
    notes: list[str] = []
    for field in _REFERENCES:
        if isinstance(arguments.get(field), str):
            arguments[field] = arguments[field].strip().strip("\"'`[]<>").strip()
    if action == "post" and "message_id" in arguments:
        message_id = arguments.pop("message_id")
        if "reply_to" not in arguments:
            raise AgentCallError(
                "invalid_arguments", text.POST_WITH_MESSAGE_ID.format(post_id=message_id)
            )
        if arguments["reply_to"] != message_id:
            raise AgentCallError("invalid_arguments", text.POST_WITH_TWO_TARGETS)
    if action == "read" and "message_id" in arguments:
        if {"before", "cursor"} & arguments.keys():
            raise AgentCallError("invalid_arguments", text.MESSAGE_WITH_PAGE)
        # One exact post needs no page size.
        arguments.pop("limit", None)
    # create resolves a discussion_id against the group before deciding.
    accepted = _FIELDS[action] | ({"discussion_id"} if action == "create" else set())
    for field in sorted(arguments.keys() - {"action", *accepted}):
        if field in _PAGING or (action == "list" and field == "discussion_id"):
            del arguments[field]
            notes.append(text.FIELD_IGNORED.format(field=field, action=action))
            continue
        users = [name for name, fields in _FIELDS.items() if field in fields]
        raise AgentCallError(
            "invalid_arguments",
            text.FIELD_FOR_OTHER_ACTION.format(
                action=action, field=field, actions=" or ".join(users) or "no action"
            ),
        )
    for field in _REQUIRED.get(action, ()):
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AgentCallError(
                "invalid_arguments", text.BOARD_REQUIRED[field].format(action=action)
            )
    for field, value in arguments.items():
        if field == "limit":
            if type(value) is not int or value < 1:
                raise SwarmStoreError("invalid_arguments", field=field)
            if value > _MAX_LIMIT:
                arguments[field] = _MAX_LIMIT
                notes.append(text.LIMIT_CLAMPED.format(requested=value, maximum=_MAX_LIMIT))
        elif field == "recipients":
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise SwarmStoreError("invalid_arguments", field=field)
        elif field != "action" and (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > _MAX_TEXT.get(field, len(value))
        ):
            raise SwarmStoreError("invalid_arguments", field=field)
    return action, notes


class BoardCall:
    """One swarm_board action for a bound participant."""

    def __init__(
        self,
        service: SwarmExtension,
        context: ToolContext,
        binding: TemporarySessionBinding,
        swarm: Json,
        notes: list[str],
    ) -> None:
        self.service = service
        self.store = service._store()
        self.context = context
        self.binding = binding
        self.sid = binding.group_id
        self.pid = binding.participant_id
        self.swarm = swarm
        self.main = swarm["main_discussion_id"]
        self.roster = Roster.of(swarm, binding.participant_id)
        self.notes = notes
        self._discussion_rows: list[Json] | None = None

    async def list(self, arguments: Json) -> Json:
        try:
            page = await self.store.list_discussions(
                self.sid, self.pid, cursor=arguments.get("cursor"), limit=arguments.get("limit", 20)
            )
        except SwarmStoreError as error:
            if error.code == "invalid_cursor":
                raise AgentCallError("invalid_cursor", text.LIST_CURSOR_INVALID) from error
            raise
        data: Json = {"user_request": self._user_request()}
        if page.has_more:
            data["more"] = text.BOARD_MORE_DISCUSSIONS.format(
                call=call_text({**arguments, "cursor": page.cursor})
            )
        data["content"] = discussions_text(page.entries, self.main)
        return data

    async def read(self, arguments: Json) -> Json:
        query = {key: value for key, value in arguments.items() if key != "action"}
        cursor = query.get("cursor")
        if isinstance(cursor, str) and cursor.startswith("pst_"):
            query["before"] = query.pop("cursor")
            self.notes.append(text.CURSOR_AS_BEFORE.format(value=cursor))
        stated = query.pop("discussion_id", None) if "message_id" in query else None
        page = await self._read_page(query)
        await self.service._record_read(self.context, self.binding, page.entries)
        if "message_id" in query:
            post = page.entries[0]
            if stated is not None and stated != post["discussion_id"]:
                self.notes.append(text.MESSAGE_DISCUSSION_IGNORED.format(post_id=post["id"]))
            return {"content": post_block(post, self.roster, self.main, with_discussion=True)}
        if page.entries:
            discussion_id = page.entries[0]["discussion_id"]
        elif "before" in query:
            discussion_id = (await self._post(query["before"]))["discussion_id"]
        else:
            discussion_id = query.get("discussion_id") or self.main
        label = discussion_label(
            discussion_id,
            page.entries[0].get("discussion_title")
            if page.entries
            else await self._title(discussion_id),
            self.main,
        )
        count = len(page.entries)
        if "before" in query:
            summary = (
                text.BOARD_PAGE_BEFORE.format(count=count, before=query["before"], discussion=label)
                if count
                else text.BOARD_PAGE_EMPTY_BEFORE.format(before=query["before"], discussion=label)
            )
        elif "cursor" in query:
            summary = text.BOARD_PAGE_OLDER.format(count=count, discussion=label)
        else:
            summary = (
                text.BOARD_PAGE_NEWEST.format(count=count, discussion=label)
                if count
                else text.BOARD_PAGE_EMPTY.format(discussion=label)
            )
        data: Json = {"page": summary}
        if discussion_id == self.main and not page.has_more:
            # Discussion pages omit the pinned request, which precedes the main discussion.
            data["user_request"] = self._user_request()
        if page.has_more and page.entries:
            continuation: Json = {
                "action": "read",
                "discussion_id": discussion_id,
                "before": page.entries[0]["id"],
            }
            if "limit" in query:
                continuation["limit"] = query["limit"]
            data["older"] = text.BOARD_OLDER.format(call=call_text(continuation))
        if page.entries:
            data["content"] = posts_text(page.entries, self.roster, self.main)
        return data

    async def post(self, arguments: Json) -> Json:
        values = {key: value for key, value in arguments.items() if key != "action"}
        try:
            data = await self.store.post(
                self.sid,
                self.pid,
                request_id=_call_request_id(self.context),
                expected_epoch=self.swarm["epoch"],
                **values,
            )
        except SwarmStoreError as error:
            raise await self._write_error(error, values) from error
        result: Json = {
            "post_id": data["post_id"],
            "discussion": await self._label(data["discussion_id"]),
            "delivery": queued_text(data["routes"]),
        }
        if data.get("replayed"):
            result["replayed"] = text.REPLAYED
        return result

    async def create(self, arguments: Json) -> Json:
        values = {key: value for key, value in arguments.items() if key != "action"}
        discussion_id = values.pop("discussion_id", None)
        if discussion_id is not None and discussion_id != self.main:
            if any(row["id"] == discussion_id for row in await self._discussions()):
                raise AgentCallError(
                    "invalid_arguments",
                    text.CREATE_IN_DISCUSSION.format(discussion_id=discussion_id),
                )
            self.notes.append(text.CREATE_IGNORES_ID.format(value=discussion_id))
        data = await self.store.create_discussion(
            self.sid,
            self.pid,
            request_id=_call_request_id(self.context),
            expected_epoch=self.swarm["epoch"],
            **values,
        )
        result: Json = {
            "discussion_id": data["discussion_id"],
            "opening_post_id": data["opening_post_id"],
            "status": text.BOARD_CREATED,
        }
        if data.get("replayed"):
            result["replayed"] = text.REPLAYED
        return result

    async def join(self, arguments: Json) -> Json:
        try:
            data = await self.store.join_discussion(
                self.sid, self.pid, arguments["discussion_id"], expected_epoch=self.swarm["epoch"]
            )
        except SwarmStoreError as error:
            raise await self._write_error(error, arguments) from error
        recent = data["recent"]
        await self.service._record_read(self.context, self.binding, recent["entries"])
        result: Json = {
            "discussion": discussion_label(data["discussion_id"], data["title"], self.main),
            "status": text.BOARD_ALREADY_JOINED if data["already"] else text.BOARD_JOINED,
        }
        if recent["has_more"] and recent["entries"]:
            result["older"] = text.BOARD_OLDER.format(
                call=call_text(
                    {
                        "action": "read",
                        "discussion_id": data["discussion_id"],
                        "before": recent["entries"][0]["id"],
                    }
                )
            )
        if recent["entries"]:
            result["content"] = posts_text(recent["entries"], self.roster, self.main)
        return result

    async def leave(self, arguments: Json) -> Json:
        try:
            data = await self.store.leave_discussion(
                self.sid, self.pid, arguments["discussion_id"], expected_epoch=self.swarm["epoch"]
            )
        except SwarmStoreError as error:
            raise await self._write_error(error, arguments) from error
        return {
            "discussion": discussion_label(data["discussion_id"], data["title"], self.main),
            "status": text.BOARD_ALREADY_LEFT if data["already"] else text.BOARD_LEFT,
        }

    async def _read_page(self, query: Json) -> Page:
        # Each correction replaces one reference; three attempts cover all of them.
        for _ in range(3):
            try:
                return await self.store.read_posts(self.sid, self.pid, **query)
            except SwarmStoreError as error:
                await self._correct_read(error, query)
        return await self.store.read_posts(self.sid, self.pid, **query)

    async def _correct_read(self, error: SwarmStoreError, query: Json) -> None:
        """Replace a unique read-only reference match in ``query``, or raise the explanation."""

        if error.code == "message_not_found" and error.field in {"message_id", "before"}:
            value = query[error.field]
            candidates = await self.store.post_suggestions(self.sid, value)
            if len(candidates) == 1:
                candidate = candidates[0]
                template = text.POST_BY_NUMBER if candidate["by_number"] else text.POST_CLOSE_MATCH
                self.notes.append(
                    template.format(field=error.field, value=value, post_id=candidate["id"])
                )
                query[error.field] = candidate["id"]
                return
            parts = [text.MESSAGE_NOT_FOUND.format(field=error.field, value=value)]
            parts.extend(post_suggestion(candidate, self.main) for candidate in candidates)
            raise AgentCallError("message_not_found", " ".join(parts)) from error
        if error.code == "discussion_not_found" and "discussion_id" in query:
            value = query["discussion_id"]
            discussions = await self._discussions()
            close = close_discussion(value, discussions)
            if close is not None:
                self.notes.append(
                    text.DISCUSSION_CLOSE_MATCH.format(
                        value=value,
                        discussion=discussion_label(close["id"], close["title"], self.main),
                    )
                )
                query["discussion_id"] = close["id"]
                return
            raise AgentCallError(
                "discussion_not_found",
                " ".join(
                    (
                        text.DISCUSSION_NOT_FOUND.format(value=value),
                        discussion_choices(discussions, self.main),
                        text.DISCUSSION_CHOOSE,
                    )
                ),
            ) from error
        if error.code == "before_discussion_mismatch":
            anchor = await self._post(query["before"])
            raise AgentCallError(
                "invalid_arguments",
                text.BEFORE_DISCUSSION_CONFLICT.format(
                    post_id=query["before"],
                    before_discussion=await self._label(anchor["discussion_id"]),
                    discussion=await self._label(query["discussion_id"]),
                ),
            ) from error
        if error.code == "invalid_cursor":
            raise AgentCallError("invalid_cursor", text.READ_CURSOR_INVALID) from error
        raise error

    async def _write_error(self, error: SwarmStoreError, values: Json) -> Exception:
        """Return the explanation of a failed write; never correct a write's target."""

        if error.code == "message_not_found" and error.field == "reply_to":
            value = values["reply_to"]
            candidates = await self.store.post_suggestions(self.sid, value)
            parts = [text.REPLY_NOT_FOUND.format(value=value)]
            parts.extend(post_suggestion(candidate, self.main) for candidate in candidates)
            parts.append(
                text.REPLY_RETRY.format(post_id=candidates[0]["id"])
                if len(candidates) == 1
                else text.REPLY_CHOOSE
            )
            return AgentCallError("message_not_found", " ".join(parts))
        if error.code == "discussion_not_found" and "discussion_id" in values:
            value = values["discussion_id"]
            discussions = await self._discussions()
            close = close_discussion(value, discussions)
            return AgentCallError(
                "discussion_not_found",
                " ".join(
                    (
                        text.DISCUSSION_NOT_FOUND.format(value=value),
                        discussion_choices(discussions, self.main),
                        text.DISCUSSION_RETRY.format(discussion_id=close["id"])
                        if close is not None
                        else text.DISCUSSION_CHOOSE,
                        text.NOTHING_CHANGED,
                    )
                ),
            )
        if error.code == "reply_discussion_mismatch":
            target = await self._post(values["reply_to"])
            return AgentCallError(
                "reply_discussion_mismatch",
                text.REPLY_DISCUSSION_CONFLICT.format(
                    post_id=values["reply_to"],
                    reply_discussion=await self._label(target["discussion_id"]),
                    discussion=await self._label(values["discussion_id"]),
                ),
            )
        return error

    def _user_request(self) -> str:
        goal = self.swarm["goal_post_id"]
        return text.BOARD_USER_REQUEST.format(
            post_id=goal, call=call_text({"action": "read", "message_id": goal})
        )

    async def _discussions(self) -> Sequence[Json]:
        if self._discussion_rows is None:
            page = await self.store.list_discussions(self.sid, self.pid, limit=_MAX_LIMIT)
            self._discussion_rows = list(page.entries)
        return self._discussion_rows

    async def _title(self, discussion_id: str) -> str | None:
        if discussion_id == self.main:
            return None
        return next(
            (row["title"] for row in await self._discussions() if row["id"] == discussion_id),
            None,
        )

    async def _label(self, discussion_id: str) -> str:
        return discussion_label(discussion_id, await self._title(discussion_id), self.main)

    async def _post(self, post_id: str) -> Any:
        return (await self.store.read_posts(self.sid, self.pid, message_id=post_id)).entries[0]


__all__ = ["BoardCall", "prepare_board_call"]
