"""swarm_wiki actions as an Agent calls them.

``WikiCall`` checks a normalized call, repairs what it can identify with
certainty, runs the action against the Store with a request ID derived from the
Tool Call, corrects read-only page references it can identify uniquely, explains
every other failure with the next call, and renders the result as readable text.
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Any

from core.tools import ToolContext

from . import wiki_text as text
from ._board_view import spoken_list
from ._extension_values import AgentCallError, Json, _call_request_id
from ._store_values import wiki_number, wiki_ref
from ._store_wiki import _FIELDS, _MAX_EDITS, _TEXT_CHARS, MUTATIONS
from ._wiki_presentation import (
    _bounded_diff,
    _call,
    _edit_numbers,
    _every_edit,
    _found,
    _history,
    _link_prose,
    _list,
    _metadata,
    _miss_message,
    _mutation,
    _nothing_changed,
    _read,
    _shown,
)
from .agent_text import FIELD_IGNORED, LIMIT_CLAMPED, REPLAYED
from .store import SwarmStore, SwarmStoreError

_READ_ONLY = frozenset({"list", "read", "history"})
# Read options the Tool resolves itself around the Store's read.
_READ_OPTIONS = frozenset({"query", "since_revision"})
_ACTION_FIELDS = {**_FIELDS, "read": _FIELDS["read"] | _READ_OPTIONS}
_MAX_LIMIT = {"read": 20000, "list": 100, "history": 100}
# Preconditions request nothing an action that changes nothing could check.
_PRECONDITIONS = ("expected_revision",)
# A page link ends in "#wiki/w3"; a quoted stored page ID starts with "wpg_".
_LINKED_PAGE = re.compile(r"#wiki/([A-Za-z0-9_]+)")
_PAGE_ID = re.compile(r"wpg_[A-Za-z0-9]+")
# A wiki-style "[[target]]" link, or a "#wiki/<page>" link target; only page numbers and
# stored page IDs open a page.
_LINK_TARGET = re.compile(r"\[\[([^\[\]\n]+)\]\]|#wiki/([^\s)\]>\"'`]+)")
_UNKNOWN_LINKS = 5
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_CLOSE_MATCH = 0.85


# A read with a query names at most this many matching lines, each cut to this length.
# Without offset, it shows limit characters, or this many, from a little before the first.
_AROUND_CHARS = 2000
_AROUND_LINES_BEFORE = 2
_AROUND_CHARS_BEFORE = 400


class WikiCall:
    """One swarm_wiki action for a bound participant."""

    def __init__(
        self,
        store: SwarmStore,
        context: ToolContext,
        swarm_id: str,
        participant_id: str,
        epoch: int,
        notes: list[str],
    ) -> None:
        self.store = store
        self.context = context
        self.sid = swarm_id
        self.pid = participant_id
        self.epoch = epoch
        self.notes = notes
        # Text a read call asked to find in its page, and whether to show the text around it.
        self.find: str | None = None
        self.around = False
        # The revision a read compares the page with instead of showing its content.
        self.since: int | None = None
        # A negative read offset, counted back from the end of the page.
        self.from_end: int | None = None

    async def run(self, arguments: Json) -> tuple[Json, bool]:
        """Run the call; return its rendered result and whether the Wiki changed."""

        action = self._prepare(arguments)
        if action == "update" and not arguments.get("page_id"):
            arguments["page_id"] = await self._page_holding(_old_texts(arguments))
        elif action == "create" and "page_id" in arguments:
            await self._drop_create_page_id(arguments)
        elif "page_id" in arguments:
            await self._name_by_number(arguments)
        # The Store accepts a whole-content update without a base only on the caller's revision.
        unbased = "content" in arguments and "expected_revision" not in arguments
        if action == "read":
            return await self._read_call(arguments), False
        if action in MUTATIONS:
            arguments["request_id"] = _call_request_id(self.context)
        data = await self._execute(action, arguments)
        assert data is not None, "only a read can start past the end"
        if data.get("replayed"):
            self.notes.append(REPLAYED)
        changed = action in MUTATIONS and not data.get("replayed") and not data.get("unchanged")
        if action == "update" and unbased and changed:
            self.notes.append(text.WIKI_OWN_REVISION.format(revision=data["revision"] - 1))
        if action in {"create", "update"} and changed:
            await self._note_unknown_links(_written(arguments), data["link"])
        if action == "list":
            rendered = _list(data, arguments)
            unfiltered = not {"query", "include_deleted", "cursor"} & arguments.keys()
            if not data["entries"] and unfiltered and await self.store.wiki_pages(self.sid):
                rendered["pages"] = text.WIKI_LIST_ONLY_DELETED.format(
                    call=_call({"action": "list", "include_deleted": True})
                )
            return rendered, changed
        if action == "history":
            return _history(data, arguments), changed
        if (
            not data.get("unchanged")
            and action not in {"create", "delete", "restore"}
            and ("old_text" in arguments or "edits" in arguments)
        ):
            self.notes.extend(data.get("notes", ()))
        return _mutation(action, data, arguments), changed

    async def _note_unknown_links(self, written: str, link: str) -> None:
        """Note links in the text a change wrote that open no page, such as "[[slug]]"."""

        targets = {
            match.group(0): match.group(1) or match.group(2)
            for match in _LINK_TARGET.finditer(_link_prose(written))
        }
        if not targets:
            return
        pages = await self.store.wiki_pages(self.sid)
        unknown = [
            shown
            for shown, target in targets.items()
            if _named_page(target.strip().rstrip(".,;:!?"), pages) is None
        ][:_UNKNOWN_LINKS]
        if unknown:
            self.notes.append(
                text.WIKI_LINKS_UNKNOWN["one" if len(unknown) == 1 else "many"].format(
                    links=spoken_list(unknown), link=link
                )
            )

    def _prepare(self, arguments: Json) -> str:
        """Check the call in place and return its action."""

        action = arguments.get("action")
        if not isinstance(action, str) or action not in _FIELDS:
            raise SwarmStoreError("invalid_arguments", field="action")
        page_id = arguments.get("page_id")
        if isinstance(page_id, str):
            # A pasted link or quoted ID still names exactly one page.
            found = set(_LINKED_PAGE.findall(page_id)) or set(_PAGE_ID.findall(page_id))
            arguments["page_id"] = (
                found.pop() if len(found) == 1 else page_id.strip().strip("\"'`[]<>").strip()
            )
        if action == "read" and "page_id" not in arguments:
            action = arguments["action"] = "list"
            self.notes.append(text.WIKI_LISTED_INSTEAD)
            for field in ("revision", "since_revision", "offset", "limit"):
                arguments.pop(field, None)
        if action in _READ_ONLY:
            for field in _PRECONDITIONS:
                if field in arguments:
                    del arguments[field]
                    self.notes.append(FIELD_IGNORED.format(field=field, action=action))
        if action == "read":
            self._read_options(arguments)
        # create decides about a page_id once it knows whether that page exists.
        accepted = _FIELDS[action] | {"action", "request_id"}
        if action == "create":
            accepted = accepted | {"page_id"}
        for field in sorted(arguments.keys() - accepted):
            users = [name for name, fields in _ACTION_FIELDS.items() if field in fields]
            message = (
                text.WIKI_FIELD_ELSEWHERE.format(
                    action=action, field=field, users=spoken_list(users)
                )
                if users
                else text.WIKI_FIELD_UNKNOWN.format(field=field)
            )
            raise AgentCallError(
                "invalid_arguments",
                _nothing_changed(message) if action in MUTATIONS else message,
            )
        arguments.pop("request_id", None)
        self._require(action, arguments)
        limit = arguments.get("limit")
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise SwarmStoreError("invalid_arguments", field="limit")
            maximum = _MAX_LIMIT[action]
            if limit > maximum:
                arguments["limit"] = maximum
                self.notes.append(LIMIT_CLAMPED.format(requested=limit, maximum=maximum))
        return action

    def _read_options(self, arguments: Json) -> None:
        """Take the read options the Store does not know out of a read call."""

        # A query on one page asks where its text occurs there.
        query = arguments.pop("query", None)
        self.find = query if isinstance(query, str) and query.strip() else None
        if "since_revision" in arguments:
            since = arguments.pop("since_revision")
            if type(since) is not int or since < 1:
                raise SwarmStoreError("invalid_arguments", field="since_revision")
            self.since = since
            for field in ("offset", "limit"):
                if field in arguments:
                    del arguments[field]
                    self.notes.append(text.WIKI_SINCE_IGNORES.format(field=field))
        offset = arguments.get("offset")
        if type(offset) is int and offset < 0:
            self.from_end = arguments.pop("offset")
        # limit bounds how much it shows; only an explicit offset moves where it starts.
        self.around = (
            self.find is not None
            and self.since is None
            and "offset" not in arguments
            and self.from_end is None
        )

    def _require(self, action: str, arguments: Json) -> None:
        def missing(key: str, **values: Any) -> AgentCallError:
            return AgentCallError(
                "invalid_arguments",
                _nothing_changed(text.WIKI_NEEDS[key].format(**values)),
            )

        for field in ("content", "old_text", "new_text"):
            _check_text_limit(field, arguments.get(field))
        if action == "update" and "edits" in arguments:
            _check_edits(arguments)
        # run() finds the page that holds the text an update replaces.
        if (
            action not in {"list", "create"}
            and not arguments.get("page_id")
            and (action != "update" or not any(old.strip() for old in _old_texts(arguments)))
        ):
            message = text.WIKI_NEEDS["page_id"].format(action=action)
            if action in MUTATIONS:
                message = _nothing_changed(message)
            raise AgentCallError("invalid_arguments", message)
        if action == "update":
            passage = [field for field in ("old_text", "new_text") if field in arguments]
            if "content" in arguments and passage:
                raise missing("one_change", fields=" and ".join(passage))
            if "new_text" in arguments and "old_text" not in arguments:
                raise missing("old_text")
            if "old_text" in arguments and "new_text" not in arguments:
                raise missing("new_text")
            if not {"old_text", "edits", "content", "title"} & arguments.keys():
                raise missing("change")
        if action == "create":
            if "content" not in arguments:
                raise missing("content")
            if not isinstance(arguments.get("title"), str) or not arguments["title"].strip():
                heading = (
                    _HEADING.search(arguments["content"])
                    if isinstance(arguments["content"], str)
                    else None
                )
                if heading is None:
                    raise missing("title")
                arguments["title"] = heading.group(1)[:240]
                self.notes.append(text.WIKI_TITLE_FROM_HEADING.format(title=arguments["title"]))
        if action == "restore" and "revision" not in arguments:
            raise missing(
                "revision",
                call=_call({"action": "history", "page_id": arguments["page_id"]}),
            )

    async def _drop_create_page_id(self, arguments: Json) -> None:
        value = arguments.pop("page_id")
        existing = _named_page(value, await self.store.wiki_pages(self.sid))
        if existing is not None:
            raise AgentCallError(
                "invalid_arguments",
                _nothing_changed(
                    text.WIKI_CREATE_EXISTING.format(
                        title=existing["title"], page_id=wiki_ref(existing["number"])
                    )
                ),
            )
        self.notes.append(text.WIKI_CREATE_IGNORED_ID.format(value=value))

    async def _page_holding(self, texts: list[str]) -> str:
        """Return the one live page whose content holds any of ``texts``; else name the choices.

        An update without page_id replaces passages whose exact text identifies their
        page; among several pages the call never picks one itself.
        """

        found: dict[str, Json] = {}
        for old_text in filter(str.strip, texts):
            for page in await self.store.wiki_pages(self.sid, containing=old_text):
                found.setdefault(page["page_id"], page)
        pages = list(found.values())
        if len(pages) == 1:
            reference = wiki_ref(pages[0]["number"])
            self.notes.append(
                text.WIKI_PAGE_FROM_TEXT.format(page_id=reference, title=pages[0]["title"])
            )
            return reference
        if pages:
            named = [f'{wiki_ref(page["number"])} ("{page["title"]}")' for page in pages[:3]]
            if len(pages) > len(named):
                named.append(text.WIKI_MORE_PAGES.format(count=len(pages) - len(named)))
            message = text.WIKI_PAGES_HOLD_TEXT.format(pages=spoken_list(named))
        else:
            message = text.WIKI_NEEDS["page_id"].format(action="update")
        raise AgentCallError("invalid_arguments", _nothing_changed(message))

    async def _name_by_number(self, arguments: Json) -> None:
        """Refer to a page the call names exactly by its number, as results show it."""

        page = _named_page(arguments["page_id"], await self.store.wiki_pages(self.sid))
        if page is not None:
            arguments["page_id"] = wiki_ref(page["number"])

    async def _execute(self, action: str, arguments: Json) -> Json | None:
        # One correction replaces the page reference; a second failure is explained.
        for attempt in range(2):
            try:
                return await self.store.wiki(
                    self.sid, self.pid, arguments, expected_epoch=self.epoch
                )
            except SwarmStoreError as error:
                if (
                    action == "read"
                    and error.code == "invalid_arguments"
                    and error.field == "offset"
                ):
                    return None
                if attempt or not await self._correct(error, action, arguments):
                    raise await self._explain(error, action, arguments) from error
        raise AssertionError("unreachable")

    async def _correct(self, error: SwarmStoreError, action: str, arguments: Json) -> bool:
        """Replace a read-only page reference that names one page; report whether it did."""

        if error.code != "wiki_page_not_found" or action not in _READ_ONLY:
            return False
        value = arguments["page_id"]
        match = _page_match(value, await self.store.wiki_pages(self.sid))
        if match is None:
            return False
        page, by_title = match
        reference = wiki_ref(page["number"])
        self.notes.append(
            text.WIKI_PAGE_TITLE.format(value=value, page_id=reference)
            if by_title
            else text.WIKI_PAGE_CLOSE.format(value=value, page_id=reference, title=page["title"])
        )
        arguments["page_id"] = reference
        return True

    async def _explain(self, error: SwarmStoreError, action: str, arguments: Json) -> Exception:
        """Return the Agent-facing explanation of a failed call."""

        def closing(message: str) -> str:
            # A failed change says so first.
            return _nothing_changed(message) if action in MUTATIONS else message

        details = error.details
        page_id = arguments.get("page_id", "")
        if error.code == "wiki_page_not_found":
            match = _page_match(page_id, await self.store.wiki_pages(self.sid))
            parts = [text.WIKI_PAGE_NOT_FOUND.format(value=page_id)]
            if match is not None:
                page = match[0]
                reference = wiki_ref(page["number"])
                parts.append(
                    text.WIKI_PAGE_SUGGESTION.format(title=page["title"], page_id=reference)
                )
                parts.append(text.WIKI_PAGE_RETRY.format(page_id=reference))
            else:
                parts.append(text.WIKI_PAGE_FIND)
            return AgentCallError("wiki_page_not_found", closing(" ".join(parts)))
        if error.code == "wiki_revision_not_found":
            current = (await self._read_current(page_id, action))["current_revision"]
            return AgentCallError(
                "wiki_revision_not_found",
                closing(
                    text.WIKI_REVISION_MISSING.format(
                        revision=arguments.get("revision"),
                        current=current,
                        call=_call({"action": "history", "page_id": page_id}),
                    )
                ),
            )
        if error.code == "wiki_deleted":
            restore = {"action": "restore", "page_id": page_id}
            restore["revision"] = details["current_revision"]
            return AgentCallError(
                "wiki_deleted",
                closing(
                    text.WIKI_DELETED.format(
                        current=details["current_revision"], call=_call(restore)
                    )
                ),
            )
        if "edits" in details:
            return AgentCallError(error.code, await self._edits_message(details, arguments))
        if "passages" in details:
            expected, current = details.get("expected_revision"), details["current_revision"]
            changes = (
                await self._changes(page_id, expected, current)
                if expected is not None and expected < current
                else None
            )
            # The recent revision that last held old_text, and who changed that passage.
            found = details.get("origin")
            origin = (found["revision"], found["author"]) if found else None
            old = arguments.get("old_text")
            return AgentCallError(
                error.code,
                closing(
                    _miss_message(
                        details, page_id, changes, origin, old if isinstance(old, str) else ""
                    )
                ),
            )
        if error.code == "wiki_revision_conflict":
            return AgentCallError(
                "wiki_revision_conflict", closing(await self._conflict_message(details, arguments))
            )
        if error.code == "invalid_cursor":
            return AgentCallError("invalid_cursor", text.WIKI_CURSOR)
        if error.field == "expected_revision" and "current_revision" in details:
            return AgentCallError(
                "invalid_arguments",
                closing(
                    text.WIKI_NEEDS["expected_revision"].format(current=details["current_revision"])
                ),
            )
        return error

    async def _edits_message(self, details: Json, arguments: Json) -> str:
        """Explain each of an update's edits that could not be applied, in input order."""

        page_id = arguments["page_id"]
        expected, current = details.get("expected_revision"), details["current_revision"]
        changes = (
            await self._changes(page_id, expected, current)
            if expected is not None and expected < current
            else None
        )
        total = details["total"]
        failed = _edit_numbers([item["edit"] for item in details["edits"]])
        parts = [text.WIKI_EDITS_NONE.format(every=_every_edit(total), failed=failed)]
        if changes is not None:
            # Shown once; each edit's explanation refers to them.
            parts.append("\n".join(part for part in changes if part))
        for item in details["edits"]:
            found = item.get("origin")
            origin = (found["revision"], found["author"]) if found else None
            old = arguments["edits"][item["edit"] - 1]["old_text"]
            state = {**item, "current_revision": current, "expected_revision": expected}
            explanation = _miss_message(state, page_id, changes, origin, old, show_changes=False)
            parts.append(text.WIKI_EDIT_MISS.format(number=item["edit"], explanation=explanation))
        parts.append(text.WIKI_EDITS_RETRY.format(every=_every_edit(total), failed=failed))
        return "\n".join(parts)

    async def _conflict_message(self, details: Json, arguments: Json) -> str:
        expected, current = details["expected_revision"], details["current_revision"]
        template = text.WIKI_CONFLICT_FUTURE if expected > current else text.WIKI_CONFLICT
        parts = [template.format(expected=expected, current=current)]
        # What changed since, and who changed it, decides whether the call still applies.
        changes = (
            await self._changes(arguments["page_id"], expected, current)
            if expected < current
            else None
        )
        if changes is not None:
            parts.append("\n".join(part for part in changes if part))
        parts.append(
            text.WIKI_CONFLICT_CONTENT.format(current=current)
            if "content" in arguments
            else text.WIKI_CONFLICT_RETRY.format(current=current)
        )
        return "\n".join(parts) if changes is not None else " ".join(parts)

    async def _read_current(self, page_id: str, action: str) -> Json:
        try:
            return await self.store.wiki(
                self.sid, self.pid, {"action": "read", "page_id": page_id, "limit": 1}
            )
        except SwarmStoreError as error:
            raise await self._explain(error, action, {"page_id": page_id}) from error

    async def _past_end(self, arguments: Json) -> Json:
        query = {"action": "read", "page_id": arguments["page_id"], "limit": 1}
        if "revision" in arguments:
            query["revision"] = arguments["revision"]
        data = await self.store.wiki(self.sid, self.pid, query)
        rendered = _metadata(data)
        rendered["shown"] = text.WIKI_READ_PAST_END.format(
            offset=arguments.get("offset"), total=data["total_chars"]
        )
        return rendered

    async def _read_call(self, arguments: Json) -> Json:
        """Read a page: its content, the changes since a revision, or the text around a query."""

        if self.since is not None or self.around or self.from_end is not None:
            # Learn the revision and its size first; what to show depends on them.
            probe = {**arguments, "limit": 1}
            probe.pop("offset", None)
            data = await self._execute("read", probe)
            assert data is not None, "a read from the start is never past the end"
            arguments["page_id"] = probe["page_id"]
            if self.since is not None:
                return await self._read_changes(data, arguments)
            if self.around:
                return await self._read_around(data, arguments)
            assert self.from_end is not None
            arguments["offset"] = max(0, data["total_chars"] + self.from_end)
            arguments.setdefault("revision", data["revision"])
            self.notes.append(text.WIKI_OFFSET_FROM_END.format(value=self.from_end))
        data = await self._execute("read", arguments)
        if data is None:
            return await self._past_end(arguments)
        rendered = _read(data)
        if self.find:
            content = rendered.pop("content", None)
            found, _ = _found(await self._full_content(data), self.find)
            rendered["found"] = found
            if content is not None:
                rendered["content"] = content
        return rendered

    async def _read_changes(self, data: Json, arguments: Json) -> Json:
        since, revision = self.since, data["revision"]
        assert since is not None
        if since > revision:
            if revision == data["current_revision"]:
                message = text.WIKI_CONFLICT_FUTURE.format(expected=since, current=revision)
            else:
                message = text.WIKI_SINCE_AFTER.format(since=since, revision=revision)
            raise AgentCallError("invalid_arguments", message)
        rendered = _metadata(data)
        if self.find:
            rendered["found"] = _found(await self._full_content(data), self.find)[0]
        if since == revision:
            rendered["changes"] = text.WIKI_CHANGES_NONE.format(since=since)
            return rendered
        changes = await self._changes(arguments["page_id"], since, revision)
        if changes is None:
            raise AgentCallError(
                "wiki_revision_not_found",
                text.WIKI_REVISION_MISSING.format(
                    revision=since,
                    current=data["current_revision"],
                    call=_call({"action": "history", "page_id": arguments["page_id"]}),
                ),
            )
        rendered["changes"], diff = changes
        if diff:
            rendered["content"] = diff
        return rendered

    async def _read_around(self, data: Json, arguments: Json) -> Json:
        """Show the lines that contain the query and the text around the first of them."""

        assert self.find is not None
        content = await self._full_content(data)
        found, first = _found(content, self.find)
        if first is None:
            rendered = _metadata(data)
            rendered["found"] = found
            return rendered
        line, position = first
        starts = [0, *(index + 1 for index, char in enumerate(content) if char == "\n")]
        limit = arguments.get("limit", _AROUND_CHARS)
        # The lead before the match stays a small part of the window, so it shows the match.
        lead = min(_AROUND_CHARS_BEFORE, limit // 5)
        start = max(starts[max(0, line - 1 - _AROUND_LINES_BEFORE)], position - lead)
        end = min(len(content), start + limit)
        next_call = {**arguments, "revision": data["revision"], "offset": end}
        rendered = _read(
            {
                **data,
                "offset": start,
                "content": content[start:end],
                "next_call": {"arguments": next_call} if end < len(content) else None,
            }
        )
        rendered["shown"] = text.WIKI_READ_AROUND.format(
            start=start, end=end, total=len(content), line=line
        )
        shown = rendered.pop("content", None)
        rendered["found"] = found
        if shown is not None:
            rendered["content"] = shown
        return rendered

    async def _full_content(self, data: Json) -> str:
        """Return the complete content of the revision a read returned."""

        if not data["offset"] and len(data["content"]) == data["total_chars"]:
            return str(data["content"])
        contents = await self.store.wiki_contents(self.sid, data["page_id"], [data["revision"]])
        return contents.get(data["revision"], "")

    async def _changes(self, page_id: str, since: int, until: int) -> tuple[str, str] | None:
        """Return a header naming who changed the page since ``since``, and the bounded diff."""

        changes = await self.store.wiki_changes(self.sid, page_id, since, until)
        if changes is None:
            return None
        authors = spoken_list(changes["authors"])
        diff = _bounded_diff(changes["before"], changes["after"])
        if not diff:
            return text.WIKI_CHANGES_SAME.format(since=since, authors=authors), ""
        return text.WIKI_CHANGES.format(since=since, authors=authors), diff


def _old_texts(arguments: Json) -> list[str]:
    """Return the passages an update replaces: its old_text, or those of its edits."""

    if "edits" in arguments:
        return [
            item["old_text"]
            for item in arguments["edits"]
            if isinstance(item, dict) and isinstance(item.get("old_text"), str)
        ]
    old_text = arguments.get("old_text")
    return [old_text] if isinstance(old_text, str) else []


def _written(arguments: Json) -> str:
    """Return the text a create or update writes: its content or its new passages."""

    parts = [arguments.get("content"), arguments.get("new_text")]
    parts.extend(item.get("new_text") for item in arguments.get("edits", ()))
    return "\n".join(part for part in parts if isinstance(part, str))


def _check_edits(arguments: Json) -> None:
    """Refuse an update's edits unless each is an old_text with its new_text."""

    edits = arguments["edits"]
    if edits == []:
        del arguments["edits"]  # No edits requests nothing.
        return
    problem: tuple[str, dict[str, Any]] | None
    together = [field for field in ("old_text", "new_text", "content") if field in arguments]
    if together:
        problem = "edits_with", {"fields": spoken_list(together)}
    elif not isinstance(edits, list):
        problem = "edit", {"number": 1, "received": _shown(edits)}
    elif len(edits) > _MAX_EDITS:
        problem = "edits_many", {"count": len(edits), "maximum": _MAX_EDITS}
    else:
        problem = next(
            (
                ("edit", {"number": number, "received": received})
                for number, received in enumerate(map(_edit_problem, edits), 1)
                if received is not None
            ),
            None,
        )
    if problem is not None:
        key, values = problem
        raise AgentCallError(
            "invalid_arguments", _nothing_changed(text.WIKI_NEEDS[key].format(**values))
        )
    for number, item in enumerate(edits, 1):
        for field, value in item.items():
            _check_text_limit(f"edit {number} {field}", value)


def _check_text_limit(field: str, value: Any) -> None:
    if isinstance(value, str) and len(value) > _TEXT_CHARS:
        raise AgentCallError(
            "invalid_arguments",
            _nothing_changed(
                text.WIKI_NEEDS["text_long"].format(
                    field=field, count=len(value), maximum=_TEXT_CHARS
                )
            ),
        )


def _edit_problem(item: Any) -> str | None:
    """Return what one of an update's edits holds instead of old_text and new_text."""

    if not isinstance(item, dict):
        return _shown(item)
    if item.keys() != {"old_text", "new_text"}:
        return ", ".join(item) or "{}"
    wrong = next((field for field, value in item.items() if not isinstance(value, str)), None)
    if wrong is not None:
        return f"{wrong} {_shown(item[wrong])}"
    return 'old_text ""' if not item["old_text"] else None


def _named_page(value: str, pages: list[Json]) -> Json | None:
    """Return the page ``value`` names exactly, by its number or its stored ID."""

    number = wiki_number(value)
    return next(
        (page for page in pages if page["page_id"] == value or page["number"] == number), None
    )


def _page_match(value: str, pages: list[Json]) -> tuple[Json, bool] | None:
    """Return the one page a mistyped ID or a title names, and whether it was the title."""

    wanted = " ".join(value.split()).casefold()
    titled = [page for page in pages if " ".join(page["title"].split()).casefold() == wanted]
    if len(titled) == 1:
        return titled[0], True
    close = get_close_matches(value, [page["page_id"] for page in pages], n=2, cutoff=_CLOSE_MATCH)
    if len(close) == 1:
        return next(page for page in pages if page["page_id"] == close[0]), False
    return None


__all__ = ["WikiCall"]
