"""Bounded, Session-owned snapshots and context-sized views for web_fetch."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from core.storage.temp_files import TEMPORARY_FILE_RETENTION, TemporaryFileManager
from core.tools._web_fetch_find import find_spans
from core.tools.tools import ToolContext, tool_failure, tool_success
from core.utils.edit_distance import edit_distance
from core.utils.tokens import estimate_tokens

DEFAULT_MAX_CHARS = 12_000
MIN_CHARS = 200
MAX_CHARS = 30_000
MAX_CONTENT_TOKENS = 4_000
MAX_SAVED_CHARS = 2_000_000
_REF_BODY = r"tmp_[0-9a-hjkmnp-tv-z]{12}"
_REF = re.compile(rf"({_REF_BODY})(?:\.([mp])\.([0-9]{{1,7}}))?")
# A ref inside copied text, such as a find passage's read-on call.
REF_IN_TEXT = re.compile(rf"{_REF_BODY}(?:\.[mp]\.[0-9]{{1,7}})?")
_CATEGORY = "web_fetch"
_RETENTION_HOURS = int(TEMPORARY_FILE_RETENTION[_CATEGORY].total_seconds() // 3600)
_PARTIAL_WARNING = "Saved content is partial (2,000,000-character limit per view)."
# Snapshot limitations worth repeating when a later part is read.
_LASTING_WARNINGS = ("partial", "Service unavailable")
# Characters a find passage shows before and after its match: a paragraph of
# prose, or about eight lines before and twenty-five after in plain text and code,
# where a match is mostly a name whose definition follows.
_PROSE_PASSAGE = (180, 350)
_CODE_PASSAGE = (300, 900)
# Passages this close together are shown as one, so no text appears twice.
_ADJACENT_CHARS = 120
_MAX_PASSAGES = 12
# Snapshot sources whose text is not a Markdown rendering: plain text and code
# fetched as is, and text extracted from documents.
_PLAIN_SOURCES = frozenset({"direct", "document"})


class SavedPageError(ValueError):
    """A ref cannot be read.

    ``state`` is what the store can tell: ``malformed``, ``unknown`` (no such saved
    page), ``foreign`` (another conversation's page), ``expired`` or ``unreadable``.
    The message is ``situation`` followed by ``next_step``, which a caller knowing
    more of the call can replace. ``reason`` says the state in one clause for a
    result that fetched the call's url instead; ``url`` is an expired page's address.
    """

    def __init__(
        self,
        code: str,
        state: str,
        situation: str,
        reason: str,
        *,
        next_step: str = "Fetch the page again with url.",
        url: str | None = None,
    ) -> None:
        self.code = code
        self.state = state
        self.situation = situation
        self.reason = reason
        self.url = url
        super().__init__(f"{situation} {next_step}")


def _decode_ref(ref: str) -> tuple[str, str, int]:
    match = _REF.fullmatch(ref)
    if not match or int(match[3] or 0) > MAX_SAVED_CHARS:
        raise SavedPageError(
            "invalid_ref",
            "malformed",
            f'"{ref}" is not a web_fetch ref. Refs come from earlier web_fetch results and '
            "look like tmp_7k2m9x4q1b3c or tmp_7k2m9x4q1b3c.m.12000.",
            "it is not a web_fetch ref",
            next_step="To fetch a page, pass its address as url.",
        )
    return match[1], "page" if match[2] == "p" else "main", int(match[3] or 0)


def _unknown(identifier: str) -> SavedPageError:
    return SavedPageError(
        "invalid_ref",
        "unknown",
        f"No saved page has ref {identifier}: the ref is mistyped, or the page was saved more "
        f"than {_RETENTION_HOURS} hours ago and has been removed.",
        "no saved page has that ref",
    )


def _unreadable(identifier: str) -> SavedPageError:
    return SavedPageError(
        "storage_error",
        "unreadable",
        f"Saved page {identifier} could not be read.",
        "the saved page could not be read",
    )


def _continuation(ref: str, scope: str, offset: int) -> str:
    return f"{ref}.{'p' if scope == 'page' else 'm'}.{offset}"


def _bounded(text: str, max_chars: int) -> str:
    candidate = text[:max_chars]
    if estimate_tokens(candidate)[0] <= MAX_CONTENT_TOKENS:
        return candidate
    low, high = 0, len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(candidate[:middle])[0] <= MAX_CONTENT_TOKENS:
            low = middle
        else:
            high = middle - 1
    return candidate[:low]


def save_page(
    context: ToolContext, manager: TemporaryFileManager, data: dict[str, Any]
) -> dict[str, Any]:
    """Store both views once; subsequent calls never fetch or bill a provider."""
    snapshot = dict(data)
    snapshot["page"] = snapshot.get("page", snapshot["content"])
    warnings = list(snapshot.get("warnings", []))
    for key in ("content", "page"):
        if len(snapshot[key]) > MAX_SAVED_CHARS:
            snapshot[key] = snapshot[key][:MAX_SAVED_CHARS]
            if _PARTIAL_WARNING not in warnings:
                warnings.append(_PARTIAL_WARNING)
    if warnings:
        snapshot["warnings"] = warnings
    snapshot["owner"] = [context.session_id, context.agent_id]
    snapshot["fetched_at"] = time.time()
    lease = manager.create(_CATEGORY, ".json")
    try:
        snapshot["ref"] = lease.path.stem
        lease.path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    finally:
        lease.finish()
    return snapshot


def _snapshot(manager: TemporaryFileManager, identifier: str) -> dict[str, Any]:
    """Read one saved page as stored, or say why it cannot be read."""
    path = manager.root / _CATEGORY / f"{identifier}.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_SAVED_CHARS * 16:
            raise ValueError("Invalid saved page.")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise _unknown(identifier) from error
    except (OSError, ValueError) as error:
        raise _unreadable(identifier) from error
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fetched_at"), (int, float)):
        raise _unreadable(identifier)
    if not all(isinstance(snapshot.get(key), str) for key in ("content", "page", "ref", "url")):
        raise _unreadable(identifier)
    return snapshot


def load_page(context: ToolContext, manager: TemporaryFileManager, ref: str) -> dict[str, Any]:
    """Resolve only this Agent's snapshots in this Session, including expiry."""
    identifier, _, _ = _decode_ref(ref)
    snapshot = _snapshot(manager, identifier)
    if snapshot.get("owner") != [context.session_id, context.agent_id]:
        raise SavedPageError(
            "invalid_ref",
            "foreign",
            f"Saved page {identifier} belongs to another conversation; a saved page can be "
            "read only in the conversation that fetched it.",
            "it belongs to another conversation",
            next_step="Fetch the page with url.",
        )
    if time.time() - snapshot["fetched_at"] > TEMPORARY_FILE_RETENTION[_CATEGORY].total_seconds():
        raise SavedPageError(
            "page_expired",
            "expired",
            f"Saved page {identifier} of {snapshot['url']} expired: saved pages last "
            f"{_RETENTION_HOURS} hours.",
            "it expired",
            next_step=f"Fetch it again with {_call({'url': snapshot['url']})}.",
            url=snapshot["url"],
        )
    return snapshot


# Edits that turn a mistyped ref into one of the conversation's refs: a copy slip
# such as two swapped characters. Refs are random, so another conversation's ref
# or an unrelated one of this conversation is never this close.
_REF_TYPOS = 2
_REF_SHAPE = re.compile(r"tmp_([0-9a-z]{10,14})(?:\.[mp]\.[0-9]{1,7})?")


def similar_refs(
    context: ToolContext, manager: TemporaryFileManager, ref: str
) -> list[tuple[str, str]]:
    """Return this conversation's readable saved pages whose ref differs from ``ref`` by a typo.

    Each entry is the ref and the page's address. Only files whose name is that
    close are read, so the lookup stays cheap however many pages are saved.
    """
    shape = _REF_SHAPE.fullmatch(ref.strip().lower())
    if shape is None:
        return []
    try:
        names = [path.stem for path in (manager.root / _CATEGORY).glob("tmp_*.json")]
    except OSError:
        return []
    found = []
    for name in sorted(names):
        if name[4:] == shape[1] or edit_distance(shape[1], name[4:], _REF_TYPOS) > _REF_TYPOS:
            continue
        try:
            snapshot = load_page(context, manager, name)
        except SavedPageError:
            continue
        found.append((name, snapshot["url"]))
    return found


def page_matches_url(snapshot: dict[str, Any], url: str) -> bool:
    """Return whether a url names the saved page: its final or requested address."""
    return url in {snapshot["url"], snapshot.get("requested_url")}


def _call(arguments: Any) -> str:
    return json.dumps(arguments, ensure_ascii=False)


def _note(snapshot: dict[str, Any], *, first_read: bool) -> str:
    """Fold the snapshot's limitations into one sentence group for this view."""
    notes = []
    for warning in snapshot.get("warnings", []):
        if not isinstance(warning, str):
            continue
        if warning.startswith("Main content shown"):
            if first_read:
                notes.append(
                    "Only the main content is shown; find also searches navigation, "
                    "sidebars and footers."
                )
        elif warning.startswith("Embedded or interactive"):
            if first_read:
                notes.append("Embedded or interactive content appears as labels or links.")
        elif first_read or any(marker in warning for marker in _LASTING_WARNINGS):
            notes.append(warning)
    return " ".join(notes)


def _read_text(
    snapshot: dict[str, Any], text: str, scope: str, offset: int, max_chars: int
) -> dict[str, Any]:
    ref = snapshot["ref"]
    content = _bounded(text[offset:], max_chars)
    end = offset + len(content)
    data: dict[str, Any] = {}
    if offset > 0 or end < len(text) or scope == "page":
        view = " of the whole page" if scope == "page" else ""
        data["shown"] = f"characters {offset + 1}-{end} of {len(text)}{view}"
    if end < len(text):
        data["more"] = (
            f"{len(text) - end} more characters. Continue with "
            f"{_call({'ref': _continuation(ref, scope, end)})}, or search the page with "
            f"{_call({'ref': ref, 'find': '...'})}."
        )
    data["content"] = content
    return data


def _passage_window(
    text: str, offset: int, start: int, end: int, size: tuple[int, int]
) -> tuple[int, int]:
    """Return the passage around text[start:end], cut at line ends where lines are short."""
    first = max(offset, start - size[0])
    last = min(len(text), end + size[1])
    if first > offset and text[first - 1] != "\n":
        line_start = text.find("\n", first, start)
        if line_start != -1:
            first = line_start + 1
    if last < len(text) and text[last - 1] != "\n":
        line_end = text.rfind("\n", end, last)
        if line_end != -1:
            last = line_end + 1
    return first, last


def _find_text(
    snapshot: dict[str, Any], text: str, scope: str, offset: int, find: str, max_chars: int
) -> dict[str, Any]:
    ref = snapshot["ref"]
    before, after = _CODE_PASSAGE if snapshot.get("source") == "direct" else _PROSE_PASSAGE
    # A small max_chars from older history still shows the match and its read-on call.
    size = (min(before, max_chars // 5), min(after, max_chars // 2))
    spans: list[list[int]] = []
    next_offset = None

    def render(passages: list[list[int]]) -> str:
        parts = []
        for first, last in passages:
            passage = text[first:last].rstrip("\n")
            if last < len(text):
                passage += f"\nRead on with {_call({'ref': _continuation(ref, scope, last)})}"
            parts.append(passage)
        return "\n\n".join(parts)

    # Plain text and code, and document text, carry no Markdown syntax to read past.
    markdown = snapshot.get("source") not in _PLAIN_SOURCES
    found = find_spans(text, find, markdown=markdown, offset=offset)
    for position, match_end in found.spans:
        first, last = _passage_window(text, offset, position, match_end, size)
        if spans and first <= spans[-1][1] + _ADJACENT_CHARS:
            if last <= spans[-1][1]:
                continue
            trial = [*spans[:-1], [spans[-1][0], last]]
        elif len(spans) >= _MAX_PASSAGES:
            next_offset = position
            break
        else:
            trial = [*spans, [first, last]]
        rendered = render(trial)
        if spans and _bounded(rendered, max_chars) != rendered:
            # A match next to the last passage continues the search where it ends.
            next_offset = spans[-1][1] if len(trial) == len(spans) else position
            break
        spans = trial
    searched = f"after character {offset}" if offset else "in the whole page"
    if not spans:
        sections = (
            ", including navigation and footers"
            if scope == "page" and snapshot["page"] != snapshot["content"]
            else ""
        )
        narrower = (
            "To find differing wording, search for one distinctive word or number from it."
            if len(find.split()) > 1
            else "Try another word or spelling."
        )
        return {
            "content": (
                f"No word-for-word match for {_call(find)} {searched} ({len(text)} "
                f"characters{sections}); formatting is ignored. {narrower}"
            )
        }
    count = f"{len(spans)} passage{'s' if len(spans) != 1 else ''}"
    # Matches only inside longer words are not the text searched for, as "used by"
    # in "caused by" is not.
    words = ", ".join(_call(word) for word in found.inside_words)
    words = f", only inside longer words such as {words}" if words else ""
    data: dict[str, Any] = {"shown": f"{count} matching {_call(find)} {searched}{words}"}
    if next_offset is not None:
        data["more"] = (
            "More matches. Continue with "
            f"{_call({'ref': _continuation(ref, scope, next_offset), 'find': find})}."
        )
    data["content"] = _bounded(render(spans), max_chars)
    return data


def read_page(snapshot: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a contiguous part or literal search hits with copyable follow-up calls."""
    find = arguments.get("find")
    _, view, cursor = _decode_ref(arguments.get("ref", snapshot["ref"]))
    scope = arguments.get("scope", "page" if find else view)
    text = snapshot["page" if scope == "page" else "content"]
    offset = arguments.get("offset", cursor if not find or view == "page" else 0)
    if offset > len(text):
        return tool_failure(
            "invalid_arguments",
            f"Position {offset} is past the end of the saved page ({len(text)} characters). "
            f"Read it from the start with {_call({'ref': snapshot['ref']})}.",
            retryable=False,
        )
    max_chars = arguments.get("max_chars", DEFAULT_MAX_CHARS)
    data: dict[str, Any] = {
        key: snapshot[key]
        for key in ("url", "title", "author", "published")
        if isinstance(snapshot.get(key), str) and snapshot[key]
    }
    data["ref"] = snapshot["ref"]
    if find:
        data.update(_find_text(snapshot, text, scope, offset, find, max_chars))
    else:
        data.update(_read_text(snapshot, text, scope, offset, max_chars))
    note = _note(snapshot, first_read=offset == 0 and not find)
    if note:
        data["note"] = note
    return tool_success(data)
