"""Pure Swarm Wiki result, failure, query, link and diff presentation."""

from __future__ import annotations

import json
import re
from difflib import unified_diff
from typing import Any

from core.utils.timestamps import parse_timestamp

from . import wiki_text as text
from ._board_view import call_text, spoken_list
from ._extension_values import Json
from ._store_values import wiki_ref
from ._store_wiki import query_words
from ._wiki_emphasis import _FENCE, _code_spans

_ESCAPED_LINK_MARKER = re.compile(r"\\[\\\[\]#]")

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

_EXCERPT_CHARS = 200

_DIFF_LINES = 60

_DIFF_CHARS = 3000

_AMBIGUOUS_LINES = 10

_FOUND_LINES = 5

_FOUND_LINE_CHARS = 200


def _call(arguments: Json) -> str:
    """Return a copyable swarm_wiki call, named so it is not sent to another Tool."""

    return f"swarm_wiki {call_text(arguments)}"


def _shown(value: Any) -> str:
    shown = json.dumps(value, ensure_ascii=False, default=str)
    return shown if len(shown) <= 40 else shown[:37] + "..."


def _edit_numbers(numbers: list[int]) -> str:
    """Name edits by number: "edit 2", "edits 2 and 4"."""

    named = spoken_list([str(number) for number in numbers])
    return f"edit {named}" if len(numbers) == 1 else f"edits {named}"


def _every_edit(total: int) -> str:
    return text.WIKI_EDITS_BOTH if total == 2 else text.WIKI_EDITS_ALL.format(total=total)


def _edits_status(outcomes: list[Json]) -> str:
    """Say which of an update's edits changed the page, each with its line."""

    held = [outcome["edit"] for outcome in outcomes if not outcome["applied"]]
    applied = [outcome["edit"] for outcome in outcomes if outcome["applied"]]
    status = text.WIKI_STATUS
    head = (
        status["edits_held"].format(applied=_edit_numbers(applied), held=_edit_numbers(held))
        if held
        else status["edits"].format(every=_every_edit(len(outcomes)))
    )
    lines = [
        (text.WIKI_EDIT_APPLIED if outcome["applied"] else text.WIKI_EDIT_HELD).format(
            number=outcome["edit"], line=outcome["line"]
        )
        for outcome in outcomes
    ]
    return "\n".join([head, *lines])


def _found(content: str, query: str) -> tuple[str, tuple[int, int] | None]:
    """Name the lines of ``content`` that contain ``query``, ignoring case.

    When no line holds a query of several words as written, name the lines that
    hold all of its words, as list does for pages. Also return the line number
    and character position of the first match, if any.
    """

    hits, first = _lines_with(content, [query.casefold()])
    header = text.WIKI_READ_FOUND
    words = query_words(query)
    if not hits and len(words) > 1:
        hits, first = _lines_with(content, words)
        header = text.WIKI_READ_FOUND_WORDS
    if not hits:
        none = text.WIKI_READ_FOUND_NONE_WORDS if len(words) > 1 else text.WIKI_READ_FOUND_NONE
        return none.format(query=query, call=_call({"action": "list", "query": query})), None
    shown = [header.format(query=query), *hits[:_FOUND_LINES]]
    if len(hits) > _FOUND_LINES:
        shown.append(text.WIKI_READ_FOUND_MORE.format(count=len(hits) - _FOUND_LINES))
    return "\n".join(shown), first


def _lines_with(content: str, terms: list[str]) -> tuple[list[str], tuple[int, int] | None]:
    """Return the lines holding every casefolded term, and where the first match starts."""

    hits: list[str] = []
    first: tuple[int, int] | None = None
    offset = 0
    for number, line in enumerate(content.split("\n"), 1):
        folded = line.casefold()
        positions = [folded.find(term) for term in terms]
        if all(position >= 0 for position in positions):
            first = first or (number, offset + min(min(positions), len(line)))
            clipped = line if len(line) <= _FOUND_LINE_CHARS else line[:_FOUND_LINE_CHARS] + "..."
            hits.append(text.WIKI_READ_FOUND_LINE.format(line=number, offset=offset, text=clipped))
        offset += len(line) + 1
    return hits, first


def _link_prose(written: str) -> str:
    """Hide Markdown code and escaped link markers from the link advisory only.

    Reuse the Wiki editor's fence and code-span recognition. Keep newlines so
    separate paragraphs cannot turn unmatched backticks into a code span.
    """

    lines = []
    fence = ""
    for raw in written.splitlines(keepends=True):
        marker = _FENCE.match(raw.rstrip("\r\n"))
        hidden = bool(fence or marker or raw.startswith(("    ", "\t")))
        if marker:
            found = marker[1]
            if not fence:
                fence = found
            elif found[0] == fence[0] and len(found) >= len(fence) and not marker[2].strip():
                fence = ""
        lines.append(re.sub(r"[^\r\n]", " ", raw) if hidden else raw)
    prose = "".join(lines)
    parts: list[str] = []
    start = 0
    for boundary in [*(_PARAGRAPH_BREAK.finditer(prose)), None]:
        end = boundary.end() if boundary is not None else len(prose)
        paragraph = prose[start:end]
        offset = 0
        for begin, finish in _code_spans(paragraph):
            parts.extend((paragraph[offset:begin], " " * (finish - begin)))
            offset = finish
        parts.append(paragraph[offset:])
        start = end
    return _ESCAPED_LINK_MARKER.sub("  ", "".join(parts))


def _nothing_changed(message: str) -> str:
    """Open the explanation of a failed change with what it applied: nothing."""

    return f"{text.NOTHING_CHANGED} {message}"


def _miss_message(
    details: Json,
    page_id: str,
    changes: tuple[str, str] | None,
    origin: tuple[int, str] | None,
    old: str,
    *,
    show_changes: bool = True,
) -> str:
    """Explain an old_text that identifies no single passage, with line hints.

    ``origin`` names the newest recent revision that held ``old`` and who saved
    the next one. When the changes shown since expected_revision removed it, they
    explain the miss on their own; otherwise the closest passage follows, and the
    origin, when known, replaces the advice to copy old_text from the page.
    Without ``show_changes``, the caller shows the changes once for several edits.
    """

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
    if changes is not None:
        if show_changes:
            parts.append("\n".join(part for part in changes if part))
        if origin is not None and _removes(changes[1], old):
            # A passage shown elsewhere would only distract from the change that explains it.
            parts.append(text.WIKI_CHANGED_ABOVE.format(revision=origin[0] + 1, author=origin[1]))
            return "\n".join(parts)
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
    differences = details.get("differences") or []
    if differences:
        shown = [text.WIKI_DIFFERENCES, *map(_difference, differences)]
        more = details.get("different_lines", 0) - len(differences)
        if more > 0:
            shown.append(text.WIKI_DIFFERENCES_MORE.format(count=more))
        parts.append("\n".join(shown))
    if origin is not None:
        revision, author = origin
        since = {"action": "read", "page_id": page_id, "since_revision": revision}
        parts.append(
            text.WIKI_CHANGED_SINCE.format(
                revision=revision, changed=revision + 1, author=author, call=_call(since)
            )
        )
        return "\n".join(parts)
    # A read of the closest passage shows exactly the text to copy.
    read: Json = {"action": "read", "page_id": page_id}
    span = details.get("span")
    if span:
        read.update(offset=span[0], limit=span[1])
    parts.append(text.WIKI_COPY_EXACTLY.format(call=_call(read)))
    return "\n".join(parts)


def _removes(diff: str, old: str) -> bool:
    """Whether ``diff`` removes a line of ``old``, comparing runs of whitespace as one."""

    wanted = {" ".join(line.split()) for line in old.splitlines() if line.strip()}
    return any(
        line.startswith("-") and " ".join(line[1:].split()) in wanted for line in diff.splitlines()
    )


def _difference(item: Json) -> str:
    """Describe one line where old_text differs from the closest passage."""

    if item["line"] is None:
        return text.WIKI_DIFFERENCE_COPY_ONLY.format(
            line=item["copy_line"], copy=repr(item["copy"])
        )
    if item["copy"] is None:
        return text.WIKI_DIFFERENCE_PAGE_ONLY.format(line=item["line"], page=repr(item["file"]))
    if item.get("spacing"):
        return text.WIKI_DIFFERENCE_SPACING.format(
            line=item["line"],
            page=repr(item["file"]),
            copy=repr(item["copy"]),
            side=item["spacing"],
        )
    template = text.WIKI_DIFFERENCE_CUT if item["truncated"] else text.WIKI_DIFFERENCE
    return template.format(line=item["line"], page=repr(item["file"]), copy=repr(item["copy"]))


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


def _read(data: Json) -> Json:
    rendered = _metadata(data)
    start, content, total = data["offset"], data["content"], data["total_chars"]
    if not total:
        rendered["shown"] = text.WIKI_READ_EMPTY
    elif start or start + len(content) < total:
        rendered["shown"] = text.WIKI_READ_SHOWN.format(
            start=start, end=start + len(content), total=total
        )
    if data.get("next_call"):
        rendered["more"] = text.WIKI_READ_MORE.format(call=_call(data["next_call"]["arguments"]))
    if content:
        rendered["content"] = content
    return rendered


def _metadata(data: Json) -> Json:
    """Return what every read result says about the page and the revision read."""

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
            call=_call({"action": "restore", "page_id": reference, "revision": data["revision"]})
        )
    return rendered


def _list(data: Json, arguments: Json) -> Json:
    entries, query = data["entries"], arguments.get("query")
    if data.get("matched") == "words":
        header = (
            text.WIKI_LIST_WORD_MATCHES.format(query=query, count=len(entries))
            if entries
            else text.WIKI_LIST_NO_WORD_MATCH.format(query=query)
        )
    elif not entries:
        header = text.WIKI_LIST_NO_MATCH.format(query=query) if query else text.WIKI_LIST_NONE
    elif query:
        header = text.WIKI_LIST_MATCHES.format(query=query, count=len(entries))
    else:
        header = text.WIKI_LIST_PAGES.format(count=len(entries))
    rendered: Json = {"pages": header}
    if data.get("next_call"):
        rendered["more"] = text.WIKI_LIST_MORE.format(call=_call(data["next_call"]["arguments"]))
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


def _history(data: Json, arguments: Json) -> Json:
    entries = data["entries"]
    title = entries[0]["title"] if entries else ""
    rendered: Json = {
        "history": text.WIKI_HISTORY.format(
            title=title, page_id=arguments["page_id"], count=len(entries)
        )
    }
    if data.get("next_call"):
        rendered["more"] = text.WIKI_HISTORY_MORE.format(call=_call(data["next_call"]["arguments"]))
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


def _mutation(action: str, data: Json, arguments: Json) -> Json:
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
        key = "unchanged_edits" if "edits" in arguments else f"unchanged_{action}"
        if "old_text" in arguments and "line" in data:
            rendered["status"] = status["unchanged_passage"].format(line=data["line"])
        else:
            rendered["status"] = status.get(key, status["unchanged"])
    elif action == "create":
        rendered["status"] = _sized(status["created"], data)
    elif action == "delete":
        rendered["status"] = status["deleted"].format(
            call=_call({"action": "restore", "page_id": reference, "revision": data["revision"]})
        )
    elif action == "restore":
        rendered["status"] = status["restored"].format(revision=arguments["revision"])
    else:
        parts = []
        if "old_text" in arguments:
            parts.append(_replaced(data))
        elif "edits" in arguments:
            parts.append(_edits_status(data["edits"]))
        elif "content" in arguments:
            parts.append(_sized(status["content"], data))
        if "title" in arguments:
            parts.append(status["title"])
        rendered["status"] = " ".join(parts)
    seam = data.get("seam")
    if seam is not None:
        rendered.update(_seam(seam, reference, data["revision"]))
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
                call=_call(
                    {
                        "action": "read",
                        "page_id": reference,
                        "revision": data["revision"],
                        "offset": end,
                    }
                )
            )
    return rendered


def _line_span(first: int, last: int) -> str:
    """Name lines: "line 3", "lines 3–5"."""

    return text.WIKI_LINES["one" if first == last else "many"].format(first=first, last=last)


def _count(forms: dict[str, str], count: int) -> str:
    return forms["one" if count == 1 else "many"].format(count=count)


def _sized(status: str, data: Json) -> str:
    """Add to a create's or whole-content update's status that the page holds the text as sent.

    The text is the caller's own, so its size is the evidence; receipts saved
    before sizes were recorded have none.
    """

    size = data.get("size")
    if size is None:
        return status
    if not size["chars"]:
        return f"{status} {text.WIKI_SAVED_EMPTY}"
    saved = text.WIKI_SAVED_SIZE.format(
        lines=_count(text.WIKI_COUNT_LINES, size["lines"]),
        chars=_count(text.WIKI_COUNT_CHARS, size["chars"]),
    )
    return f"{status} {saved}"


def _replaced(data: Json) -> str:
    """Name the lines old_text covered and the lines its replacement covers now."""

    status = text.WIKI_STATUS
    replaced = data.get("replaced")
    if replaced is None:
        return status["passage"].format(line=data.get("line", 1))
    old = _line_span(*replaced["old"])
    first, last = replaced["new"]
    if last < first:
        return status["removed"].format(old=old)
    if replaced["new"] == replaced["old"]:
        return status["replaced_same"].format(old=old)
    return status["replaced"].format(old=old, new=_line_span(first, last))


def _seam(seam: Json, reference: str, revision: int) -> Json:
    """Render where a passage edit's text meets the saved page."""

    rendered: Json = {
        "shown": text.WIKI_SAVED_SEAM.format(
            lines=_line_span(seam["first"], seam["last"]), total=seam["lines"]
        ),
        "content": "\n".join(map(_seam_part, seam["parts"])),
    }
    if "offset" in seam:
        read = {"action": "read", "page_id": reference, "revision": revision}
        rendered["more"] = text.WIKI_READ_MORE.format(
            call=_call({**read, "offset": seam["offset"]})
        )
    return rendered


def _seam_part(part: Json) -> str:
    """Render one seam part: a page line, a long line's excerpts, or a range of lines."""

    if "lines" in part:
        return text.WIKI_SEAM_LINES[part["kind"]].format(lines=_line_span(*part["lines"]))
    if "text" in part:
        return str(part["text"])
    shown: list[str] = []
    end = 0
    for start, piece in part["pieces"]:
        if start > end:
            shown.append(text.WIKI_SEAM_CLIPPED.format(count=start - end))
        shown.append(piece)
        end = start + len(piece)
    if end < part["chars"]:
        shown.append(text.WIKI_SEAM_CLIPPED.format(count=part["chars"] - end))
    return "".join(shown)
