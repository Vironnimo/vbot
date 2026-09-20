"""Bounded, Session-owned snapshots and context-sized views for web_fetch."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from core.storage.temp_files import TEMPORARY_FILE_RETENTION, TemporaryFileManager
from core.tools.tools import ToolContext, tool_failure, tool_success
from core.utils.tokens import estimate_tokens

DEFAULT_MAX_CHARS = 12_000
MAX_CHARS = 30_000
MAX_CONTENT_TOKENS = 4_000
MAX_SAVED_CHARS = 2_000_000
_REF = re.compile(r"(tmp_[0-9a-hjkmnp-tv-z]{12})(?:\.([mp])\.([0-9]{1,7}))?")
_CATEGORY = "web_fetch"


def _decode_ref(ref: str) -> tuple[str, str, int]:
    match = _REF.fullmatch(ref)
    if not match or int(match[3] or 0) > MAX_SAVED_CHARS:
        raise ValueError("Invalid ref. Use a ref returned by web_fetch, or fetch a URL.")
    return match[1], "page" if match[2] == "p" else "main", int(match[3] or 0)


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
            if "Saved content is partial (2,000,000-character limit per view)." not in warnings:
                warnings.append("Saved content is partial (2,000,000-character limit per view).")
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


def load_page(context: ToolContext, manager: TemporaryFileManager, ref: str) -> dict[str, Any]:
    """Resolve only this Agent's snapshots in this Session, including expiry."""
    identifier, _, _ = _decode_ref(ref)
    path = manager.root / _CATEGORY / f"{identifier}.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_SAVED_CHARS * 16:
            raise ValueError("Invalid saved page.")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            "Saved page unavailable or expired. Fetch the original URL again."
        ) from error
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fetched_at"), (int, float)):
        raise ValueError("Saved page is invalid. Fetch the original URL again.")
    if not all(isinstance(snapshot.get(key), str) for key in ("content", "page", "ref", "url")):
        raise ValueError("Saved page is invalid. Fetch the original URL again.")
    if (
        snapshot.get("owner") != [context.session_id, context.agent_id]
        or time.time() - snapshot.get("fetched_at", 0)
        > TEMPORARY_FILE_RETENTION[_CATEGORY].total_seconds()
    ):
        raise ValueError("Saved page unavailable or expired. Fetch the original URL again.")
    return snapshot


def read_page(snapshot: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a contiguous excerpt or literal search hits with reusable next args."""
    find = arguments.get("find")
    _, view, cursor = _decode_ref(arguments.get("ref", snapshot["ref"]))
    scope = arguments.get("scope", "page" if find else view)
    text = snapshot["page" if scope == "page" else "content"]
    offset = arguments.get("offset", cursor if not find or view == "page" else 0)
    if offset > len(text):
        return tool_failure(
            "validation_error",
            "The requested position exceeds the saved page. "
            "Use its original ref to start again, or fetch the URL again.",
            retryable=False,
        )
    max_chars = arguments.get("max_chars", DEFAULT_MAX_CHARS)
    data: dict[str, Any] = {
        key: snapshot[key]
        for key in (
            "ref",
            "url",
            "title",
            "description",
            "author",
            "published",
            "source",
            "warnings",
        )
        if key in snapshot
    }
    data.update({"offset": offset, "total_chars": len(text)})
    if find:
        snippets: list[str] = []
        next_offset = None
        last_end = -1
        for match in re.finditer(re.escape(find), text[offset:], re.IGNORECASE):
            position = offset + match.start()
            if position < last_end:
                continue
            start = max(offset, position - 180)
            end = min(len(text), position + len(find) + 350)
            match_ref = _continuation(snapshot["ref"], scope, start)
            snippet = f"[ref {match_ref}]\n{text[start:end]}"
            trial = "\n\n".join([*snippets, snippet])
            if len(snippets) >= 12 or (snippets and _bounded(trial, max_chars) != trial):
                next_offset = position
                break
            snippets.append(snippet)
            last_end = end
        data["content"] = (
            _bounded("\n\n".join(snippets), max_chars)
            if snippets
            else "No matches in the saved content."
        )
        data["find"] = find
        if next_offset is not None:
            data["next"] = {
                "ref": _continuation(snapshot["ref"], scope, next_offset),
                "find": find,
            }
    else:
        content = _bounded(text[offset:], max_chars)
        data["content"] = content
        data["returned_chars"] = len(content)
        if offset + len(content) < len(text):
            data["next"] = {"ref": _continuation(snapshot["ref"], scope, offset + len(content))}
    if "next" in data:
        data["hint"] = (
            "More matches are available. Call web_fetch with next to continue this search."
            if find
            else "More page text is available. Call web_fetch with next to continue reading."
        )
    return tool_success(data)
