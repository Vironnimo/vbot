"""swarm_wiki actions as an Agent calls them.

``WikiCall`` checks a normalized call, repairs what it can identify with
certainty, runs the action against the Store with a request ID derived from the
Tool Call, corrects read-only page references it can identify uniquely, explains
every other failure with the next call, and renders the result as readable text.
"""

from __future__ import annotations

import re
from difflib import get_close_matches, unified_diff
from typing import Any

from core.tools import ToolContext
from core.utils.timestamps import parse_timestamp

from . import wiki_text as text
from ._board_view import call_text, spoken_list
from ._extension_values import AgentCallError, Json, _call_request_id
from ._store_values import wiki_number, wiki_ref
from ._store_wiki import _FIELDS, MUTATIONS
from .agent_text import FIELD_IGNORED, LIMIT_CLAMPED, REPLAYED
from .store import SwarmStore, SwarmStoreError

_READ_ONLY = frozenset({"list", "read", "history"})
_MAX_LIMIT = {"read": 20000, "list": 100, "history": 100}
# Preconditions request nothing an action that changes nothing could check.
_PRECONDITIONS = ("expected_revision",)
# A page link ends in "#wiki/w3"; a quoted stored page ID starts with "wpg_".
_LINKED_PAGE = re.compile(r"#wiki/([A-Za-z0-9_]+)")
_PAGE_ID = re.compile(r"wpg_[A-Za-z0-9]+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_CLOSE_MATCH = 0.85
_EXCERPT_CHARS = 200
_DIFF_LINES = 60
_DIFF_CHARS = 3000
_AMBIGUOUS_LINES = 10


# A read with a query names at most this many matching lines, each cut to this length.
_FOUND_LINES = 5
_FOUND_LINE_CHARS = 200


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
        # Text a read call asked to find in its page.
        self.find: str | None = None

    async def run(self, arguments: Json) -> tuple[Json, bool]:
        """Run the call; return its rendered result and whether the Wiki changed."""

        action = self._prepare(arguments)
        if action == "update" and not arguments.get("page_id"):
            arguments["page_id"] = await self._page_holding(arguments["old_text"])
        elif action == "create" and "page_id" in arguments:
            await self._drop_create_page_id(arguments)
        elif "page_id" in arguments:
            await self._name_by_number(arguments)
        # The Store accepts a whole-content update without a base only on the caller's revision.
        unbased = "content" in arguments and "expected_revision" not in arguments
        if action in MUTATIONS:
            arguments["request_id"] = _call_request_id(self.context)
        data = await self._execute(action, arguments)
        if data is None:
            return await self._past_end(arguments), False
        if data.get("replayed"):
            self.notes.append(REPLAYED)
        changed = action in MUTATIONS and not data.get("replayed") and not data.get("unchanged")
        if action == "update" and unbased and changed:
            self.notes.append(text.WIKI_OWN_REVISION.format(revision=data["revision"] - 1))
        if action == "read":
            rendered = self._read(data)
            if self.find:
                content = rendered.pop("content", None)
                rendered["found"] = await self._find(data, self.find)
                if content is not None:
                    rendered["content"] = content
            return rendered, changed
        if action == "list":
            rendered = self._list(data, arguments)
            unfiltered = not {"query", "include_deleted", "cursor"} & arguments.keys()
            if not data["entries"] and unfiltered and await self.store.wiki_pages(self.sid):
                rendered["pages"] = text.WIKI_LIST_ONLY_DELETED.format(
                    call=call_text({"action": "list", "include_deleted": True})
                )
            return rendered, changed
        if action == "history":
            return self._history(data, arguments), changed
        return self._mutation(action, data, arguments), changed

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
            for field in ("revision", "offset", "limit"):
                arguments.pop(field, None)
        if action in _READ_ONLY:
            for field in _PRECONDITIONS:
                if field in arguments:
                    del arguments[field]
                    self.notes.append(FIELD_IGNORED.format(field=field, action=action))
        if action == "read" and "query" in arguments:
            # A query on one page asks where its text occurs there.
            query = arguments.pop("query")
            self.find = query if isinstance(query, str) and query.strip() else None
        # create decides about a page_id once it knows whether that page exists.
        accepted = _FIELDS[action] | {"action", "request_id"}
        if action == "create":
            accepted = accepted | {"page_id"}
        for field in sorted(arguments.keys() - accepted):
            users = [name for name, fields in _FIELDS.items() if field in fields]
            raise AgentCallError(
                "invalid_arguments",
                text.WIKI_FIELD_ELSEWHERE.format(
                    action=action, field=field, users=spoken_list(users)
                )
                if users
                else text.WIKI_FIELD_UNKNOWN.format(field=field),
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

    def _require(self, action: str, arguments: Json) -> None:
        def missing(key: str, **values: Any) -> AgentCallError:
            return AgentCallError(
                "invalid_arguments",
                f"{text.WIKI_NEEDS[key].format(**values)} {text.NOTHING_CHANGED}",
            )

        if action not in {"list", "create"} and not arguments.get("page_id"):
            old_text = arguments.get("old_text")
            # run() finds the page that holds the text an update replaces.
            if action != "update" or not isinstance(old_text, str) or not old_text.strip():
                message = text.WIKI_NEEDS["page_id"].format(action=action)
                if action in MUTATIONS:
                    message += f" {text.NOTHING_CHANGED}"
                raise AgentCallError("invalid_arguments", message)
        if action == "update":
            if "new_text" in arguments and "old_text" not in arguments:
                raise missing("old_text")
            if "old_text" in arguments and "new_text" not in arguments:
                raise missing("new_text")
            if "old_text" in arguments and "content" in arguments:
                raise missing("one_change")
            if not {"old_text", "content", "title"} & arguments.keys():
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
                call=call_text({"action": "history", "page_id": arguments["page_id"]}),
            )

    async def _drop_create_page_id(self, arguments: Json) -> None:
        value = arguments.pop("page_id")
        existing = _named_page(value, await self.store.wiki_pages(self.sid))
        if existing is not None:
            raise AgentCallError(
                "invalid_arguments",
                text.WIKI_CREATE_EXISTING.format(
                    title=existing["title"], page_id=wiki_ref(existing["number"])
                )
                + f" {text.NOTHING_CHANGED}",
            )
        self.notes.append(text.WIKI_CREATE_IGNORED_ID.format(value=value))

    async def _page_holding(self, old_text: str) -> str:
        """Return the one live page whose content holds ``old_text``; else name the choices.

        An update without page_id replaces a passage whose exact text identifies its
        page; among several pages the call never picks one itself.
        """

        pages = await self.store.wiki_pages(self.sid, containing=old_text)
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
        raise AgentCallError("invalid_arguments", f"{message} {text.NOTHING_CHANGED}")

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
            # A failed change says so in its first line, before any evidence lines.
            if action not in MUTATIONS:
                return message
            head, separator, evidence = message.partition("\n")
            return f"{head} {text.NOTHING_CHANGED}{separator}{evidence}"

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
                        call=call_text({"action": "history", "page_id": page_id}),
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
                        current=details["current_revision"], call=call_text(restore)
                    )
                ),
            )
        if "passages" in details:
            return AgentCallError(error.code, closing(_miss_message(details, page_id)))
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

    async def _conflict_message(self, details: Json, arguments: Json) -> str:
        expected, current = details["expected_revision"], details["current_revision"]
        template = text.WIKI_CONFLICT_FUTURE if expected > current else text.WIKI_CONFLICT
        parts = [template.format(expected=expected, current=current)]
        if "content" not in arguments:
            parts.append(text.WIKI_CONFLICT_RETRY.format(current=current))
            return " ".join(parts)
        contents = await self.store.wiki_contents(
            self.sid, arguments["page_id"], [expected, current]
        )
        if expected in contents and current in contents:
            parts.append(
                "\n".join(
                    (
                        text.WIKI_CONFLICT_DIFF.format(expected=expected),
                        _bounded_diff(contents[expected], contents[current]),
                    )
                )
            )
        parts.append(text.WIKI_CONFLICT_CONTENT.format(current=current))
        return "\n".join(parts)

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
        rendered = self._read({**data, "content": "", "offset": 0, "total_chars": 0})
        rendered.pop("content", None)
        rendered.pop("more", None)
        rendered["shown"] = text.WIKI_READ_PAST_END.format(
            offset=arguments.get("offset"), total=data["total_chars"]
        )
        return rendered

    async def _find(self, data: Json, query: str) -> str:
        """Name the lines of the read revision that contain ``query``, ignoring case."""

        content = data["content"]
        if data["offset"] or len(content) < data["total_chars"]:
            contents = await self.store.wiki_contents(self.sid, data["page_id"], [data["revision"]])
            content = contents.get(data["revision"], "")
        wanted = query.casefold()
        hits: list[str] = []
        offset = 0
        for number, line in enumerate(content.split("\n"), 1):
            if wanted in line.casefold():
                clipped = (
                    line if len(line) <= _FOUND_LINE_CHARS else line[:_FOUND_LINE_CHARS] + "..."
                )
                hits.append(
                    text.WIKI_READ_FOUND_LINE.format(line=number, offset=offset, text=clipped)
                )
            offset += len(line) + 1
        if not hits:
            return text.WIKI_READ_FOUND_NONE.format(
                query=query, call=call_text({"action": "list", "query": query})
            )
        shown = [text.WIKI_READ_FOUND.format(query=query), *hits[:_FOUND_LINES]]
        if len(hits) > _FOUND_LINES:
            shown.append(text.WIKI_READ_FOUND_MORE.format(count=len(hits) - _FOUND_LINES))
        return "\n".join(shown)

    def _read(self, data: Json) -> Json:
        author = data["author"]["name"]
        revision = (
            text.WIKI_REVISION_CURRENT.format(revision=data["revision"], author=author)
            if data["revision"] == data["current_revision"]
            else text.WIKI_REVISION_OLDER.format(
                revision=data["revision"], current=data["current_revision"], author=author
            )
        )
        reference = wiki_ref(data["number"])
        rendered: Json = {
            "page_id": reference,
            "title": data["title"],
            "revision": revision,
            "link": data["link"],
        }
        if data["deleted"]:
            rendered["deleted"] = text.WIKI_READ_DELETED.format(
                call=call_text(
                    {"action": "restore", "page_id": reference, "revision": data["revision"]}
                )
            )
        start, content, total = data["offset"], data["content"], data["total_chars"]
        if not total:
            rendered["shown"] = text.WIKI_READ_EMPTY
        elif start or start + len(content) < total:
            rendered["shown"] = text.WIKI_READ_SHOWN.format(
                start=start, end=start + len(content), total=total
            )
        if data.get("next_call"):
            rendered["more"] = text.WIKI_READ_MORE.format(
                call=call_text(data["next_call"]["arguments"])
            )
        if content:
            rendered["content"] = content
        return rendered

    def _list(self, data: Json, arguments: Json) -> Json:
        entries, query = data["entries"], arguments.get("query")
        if not entries:
            header = text.WIKI_LIST_NO_MATCH.format(query=query) if query else text.WIKI_LIST_NONE
        elif query:
            header = text.WIKI_LIST_MATCHES.format(query=query, count=len(entries))
        else:
            header = text.WIKI_LIST_PAGES.format(count=len(entries))
        rendered: Json = {"pages": header}
        if data.get("next_call"):
            rendered["more"] = text.WIKI_LIST_MORE.format(
                call=call_text(data["next_call"]["arguments"])
            )
        blocks = []
        for entry in entries:
            block = text.WIKI_LIST_ENTRY.format(
                title=entry["title"],
                page_id=wiki_ref(entry["number"]),
                revision=entry["revision"],
                author=entry["author"]["name"],
                deleted=text.WIKI_ENTRY_DELETED if entry["deleted"] else "",
            )
            excerpt = _excerpt(entry.get("excerpt", ""))
            blocks.append(f"{block}\n  {excerpt}" if excerpt else block)
        if blocks:
            rendered["content"] = "\n".join(blocks)
        return rendered

    def _history(self, data: Json, arguments: Json) -> Json:
        entries = data["entries"]
        title = entries[0]["title"] if entries else ""
        rendered: Json = {
            "history": text.WIKI_HISTORY.format(
                title=title, page_id=arguments["page_id"], count=len(entries)
            )
        }
        if data.get("next_call"):
            rendered["more"] = text.WIKI_HISTORY_MORE.format(
                call=call_text(data["next_call"]["arguments"])
            )
        rendered["content"] = "\n".join(
            text.WIKI_HISTORY_ENTRY.format(
                revision=entry["revision"],
                time=_time(entry["updated_at"]),
                author=entry["author"]["name"],
                title=text.WIKI_HISTORY_TITLE.format(title=entry["title"])
                if entry["title"] != title
                else "",
                deleted=text.WIKI_HISTORY_DELETED if entry["deleted"] else "",
            )
            for entry in entries
        )
        return rendered

    def _mutation(self, action: str, data: Json, arguments: Json) -> Json:
        reference = wiki_ref(data["number"])
        rendered: Json = {
            "page_id": reference,
            "title": data["title"],
            "revision": data["revision"],
        }
        if not data["deleted"]:
            rendered["link"] = data["link"]
        status = text.WIKI_STATUS
        if data.get("unchanged"):
            rendered["status"] = status.get(f"unchanged_{action}", status["unchanged"])
        elif action == "create":
            rendered["status"] = status["created"]
        elif action == "delete":
            rendered["status"] = status["deleted"].format(
                call=call_text(
                    {"action": "restore", "page_id": reference, "revision": data["revision"]}
                )
            )
        elif action == "restore":
            rendered["status"] = status["restored"].format(revision=arguments["revision"])
        else:
            parts = []
            if "old_text" in arguments:
                parts.append(status["passage"].format(line=data.get("line", 1)))
                self.notes.extend(data.get("notes", ()))
            elif "content" in arguments:
                parts.append(status["content"])
            if "title" in arguments:
                parts.append(status["title"])
            rendered["status"] = " ".join(parts)
        excerpt = data.get("content_excerpt")
        if excerpt is not None:
            end = excerpt["offset"] + len(excerpt["content"])
            rendered["shown"] = text.WIKI_SAVED_SHOWN.format(
                revision=data["revision"],
                line=excerpt["line"],
                start=excerpt["offset"],
                end=end,
                total=excerpt["total_chars"],
            )
            rendered["content"] = excerpt["content"]
            if end < excerpt["total_chars"]:
                rendered["more"] = text.WIKI_READ_MORE.format(
                    call=call_text(
                        {
                            "action": "read",
                            "page_id": reference,
                            "revision": data["revision"],
                            "offset": end,
                        }
                    )
                )
        return rendered


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


def _miss_message(details: Json, page_id: str) -> str:
    """Explain an old_text that identifies no single passage, with line hints."""

    passages = details["passages"]
    current = details["current_revision"]
    if details["occurrences"] > 1:
        starts = [str(line) for line in details["lines"]]
        if len(starts) > _AMBIGUOUS_LINES:
            starts[_AMBIGUOUS_LINES:] = [
                text.WIKI_MORE_LINES.format(count=len(starts) - _AMBIGUOUS_LINES)
            ]
        where = (
            text.WIKI_IN_LINE.format(line=starts[0])
            if len(starts) == 1
            else text.WIKI_AT_LINES.format(lines=spoken_list(starts))
        )
        ambiguous = text.WIKI_AMBIGUOUS_SIMILAR if details.get("similar") else text.WIKI_AMBIGUOUS
        parts = [ambiguous.format(count=details["occurrences"], where=where)]
        parts.extend(
            f"{text.WIKI_PASSAGE_LINE.format(line=passage['line'])}\n{_passage(passage)}"
            for passage in passages
        )
        return "\n".join(parts)
    expected = details.get("expected_revision")
    parts = [
        text.WIKI_NOT_FOUND_STALE.format(expected=expected, current=current)
        if expected is not None and expected < current
        else text.WIKI_NOT_FOUND.format(current=current)
    ]
    # Closest passages often overlap by all but one line; show each line once.
    distinct: list[Json] = []
    for passage in passages:
        if not any(set(_lines(passage)) & set(_lines(shown)) for shown in distinct):
            distinct.append(passage)
    if len(distinct) == 1:
        parts.append(
            f"{text.WIKI_CLOSEST.format(line=distinct[0]['line'])}\n{_passage(distinct[0])}"
        )
    elif distinct:
        parts.append(text.WIKI_CLOSEST_SEVERAL)
        parts.extend(
            f"{text.WIKI_PASSAGE_LINE.format(line=passage['line'])}\n{_passage(passage)}"
            for passage in distinct[:2]
        )
    difference = details.get("difference")
    if difference:
        cut = difference["truncated"]
        template = text.WIKI_FIRST_DIFFERENCE_CUT if cut else text.WIKI_FIRST_DIFFERENCE
        page, copy = repr(difference["file"]), repr(difference["copy"])
        parts.append(template.format(line=difference["line"], page=page, copy=copy))
    parts.append(
        text.WIKI_COPY_EXACTLY.format(call=call_text({"action": "read", "page_id": page_id}))
    )
    return "\n".join(parts)


def _lines(passage: Json) -> range:
    """Return the page lines a passage shows."""
    return range(passage["line"], passage["line"] + passage["text"].count("\n") + 1)


def _passage(passage: Json) -> str:
    return str(passage["text"]) + (f"\n{text.WIKI_TRUNCATED}" if passage["truncated"] else "")


def _bounded_diff(before: str, after: str) -> str:
    lines = list(unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))[2:]
    shown: list[str] = []
    size = 0
    for line in lines:
        if len(shown) >= _DIFF_LINES or size + len(line) > _DIFF_CHARS:
            shown.append(text.WIKI_CONFLICT_DIFF_CUT)
            break
        shown.append(line)
        size += len(line) + 1
    return "\n".join(shown)


def _excerpt(value: str) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= _EXCERPT_CHARS else flat[: _EXCERPT_CHARS - 3].rstrip() + "..."


def _time(value: str) -> str:
    return parse_timestamp(value).strftime("%Y-%m-%d %H:%M UTC")


__all__ = ["WikiCall"]
