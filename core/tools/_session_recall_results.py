"""Internal Session result projection, source references and bounded read continuations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from core.debug.redaction import redact_json_body
from core.recall import (
    RecallSearchHit,
    RecallSearchPage,
)
from core.recall.canonical import (
    compact_text,
    message_search_text,
)
from core.runs import RunKind
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SESSION_RUN_KINDS_META_KEY,
    ChatSessionManager,
    SessionAddress,
    SessionDescriptorSource,
)
from core.tools.tools import (
    JsonObject,
    tool_success,
)

SESSION_SEARCH_DEFAULT_LIMIT = 10


SESSION_SEARCH_RESULT_MAX_BYTES = 50 * 1024


SESSION_SEARCH_EXCERPT_MAX_CHARS = 800


SESSION_READ_INLINE_TOOL_RESULT_MAX_BYTES = 4 * 1024


SESSION_READ_TOOL_RESULT_PREVIEW_CHARS = 800


SESSION_READ_USER_ANCHOR_EXCERPT_MAX_CHARS = 160


SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS = 240


SESSION_DESCRIPTOR_TITLE_MAX_CHARS = 200


SESSION_DESCRIPTOR_PLATFORM_MAX_CHARS = 64


SESSION_DESCRIPTOR_AGENT_ID_MAX_CHARS = 64


SESSION_DESCRIPTOR_SESSION_ID_MAX_CHARS = 128


SESSION_DESCRIPTOR_PROJECT_ID_MAX_CHARS = 128


SESSION_DESCRIPTOR_TIMESTAMP_MAX_CHARS = 64


SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"


SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"


CHANNEL_PLATFORM_METADATA_KEY = "platform"


_VALID_RUN_KINDS = frozenset(kind.value for kind in RunKind)


_REFLECTION_RUN_KINDS = frozenset(
    {
        RunKind.REFLECTION.value,
        RunKind.MEMORY_REFLECTION.value,
        RunKind.SKILL_REFLECTION.value,
    }
)


_USER_FACING_RUN_KINDS = frozenset(
    {
        RunKind.USER.value,
        RunKind.CHANNEL.value,
        RunKind.CRON.value,
    }
)


class _SessionSearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _SearchSessionContext:
    descriptor: JsonObject


def _render_search_page(
    page: RecallSearchPage,
    read_refs: list[JsonObject],
    session_contexts: dict[str, _SearchSessionContext],
) -> JsonObject:
    hits = list(page.hits)
    if len(read_refs) != len(hits):
        raise _SessionSearchError(
            "session_search_error", "Search read references do not match the result page."
        )
    count = len(hits)
    while count > 0:
        items = [
            _hit_item(
                hit,
                index + 1,
                excerpt_chars=0,
                read_ref=read_refs[index],
            )
            for index, hit in enumerate(hits[:count])
        ]
        has_more = count < len(hits) or page.has_more
        data = _search_data(
            page,
            items,
            _session_descriptors_for_hits(hits[:count], session_contexts),
            has_more=has_more,
        )
        if _serialized_result_bytes(data) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            break
        count -= 1
    if count == 0 and hits:
        raise _SessionSearchError(
            "session_search_error", "Search result metadata exceeds the result safety limit."
        )
    selected = hits[:count]
    if not selected:
        return _search_data(page, [], [], has_more=False)

    maximum = max(len(hit.text) for hit in selected)
    low = 1
    high = min(maximum, SESSION_SEARCH_EXCERPT_MAX_CHARS)
    best: JsonObject | None = None
    while low <= high:
        excerpt_chars = (low + high) // 2
        items = [
            _hit_item(
                hit,
                index + 1,
                excerpt_chars=excerpt_chars,
                read_ref=read_refs[index],
            )
            for index, hit in enumerate(selected)
        ]
        has_more = count < len(hits) or page.has_more
        candidate = _search_data(
            page,
            items,
            _session_descriptors_for_hits(selected, session_contexts),
            has_more=has_more,
        )
        if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            best = candidate
            low = excerpt_chars + 1
        else:
            high = excerpt_chars - 1
    if best is None:
        raise _SessionSearchError(
            "session_search_error", "Search excerpts exceed the result safety limit."
        )
    return best


def _search_data(
    page: RecallSearchPage,
    items: list[JsonObject],
    session_descriptors: list[JsonObject],
    *,
    has_more: bool,
) -> JsonObject:
    data: JsonObject = {
        "result_type": page.result_type,
        "items": items,
        "sessions": session_descriptors,
        "has_more": has_more,
        "searched_sessions": page.total_candidate_sessions,
    }
    if page.degraded:
        data["degraded"] = True
        data["degradation_reason"] = page.degradation_reason
    return data


def _hit_item(
    hit: RecallSearchHit,
    rank: int,
    *,
    excerpt_chars: int,
    read_ref: JsonObject,
) -> JsonObject:
    start, end = _excerpt_bounds(hit, excerpt_chars)
    item: JsonObject = {
        "rank": rank,
        "agent_id": read_ref["agent_id"],
        "session_id": hit.session_id,
        "message_id": hit.message_id,
        "role": hit.role,
        "timestamp": hit.timestamp,
        "excerpt": {
            "text": hit.text[start:end],
            "source_start": start,
            "source_end": end,
            "leading_truncated": start > 0,
            "trailing_truncated": end < len(hit.text),
        },
    }
    if hit.result_type == "passage":
        item["passage_id"] = hit.passage_id
        item["end_timestamp"] = hit.end_timestamp
    item["read_ref"] = read_ref
    if hit.sources:
        item["sources"] = list(hit.sources)
    return item


def _search_context_for_hits(
    hits: list[RecallSearchHit],
    *,
    agent_id: str,
    project_id: str | None,
    sessions: ChatSessionManager | None,
    include_subagents: bool,
) -> tuple[list[JsonObject], dict[str, _SearchSessionContext]]:
    loaded: dict[str, _SearchSessionContext] = {}
    refs: list[JsonObject] = []
    addresses = {
        hit.session_id: SessionAddress(
            project_id=project_id,
            agent_id=agent_id,
            session_id=hit.session_id,
        )
        for hit in hits
    }
    try:
        sources = {} if sessions is None else sessions.descriptor_sources(tuple(addresses.values()))
    except Exception:
        sources = {}
    for hit in hits:
        if hit.session_id not in loaded:
            source = sources.get(addresses[hit.session_id])
            loaded[hit.session_id] = _search_hit_session_context(
                agent_id,
                hit.session_id,
                source,
            )
        read_ref: JsonObject = {
            "agent_id": agent_id,
            "session_id": hit.session_id,
            "message_id": hit.message_id,
        }
        if (
            include_subagents
            and loaded[hit.session_id].descriptor.get("is_subagent_session") is True
        ):
            read_ref["include_subagents"] = True
        refs.append(read_ref)
    return refs, loaded


def _search_hit_session_context(
    agent_id: str,
    session_id: str,
    source: SessionDescriptorSource | None,
) -> _SearchSessionContext:
    if source is None:
        return _SearchSessionContext(
            descriptor=_session_descriptor(agent_id, session_id, {}, None),
        )
    return _SearchSessionContext(
        descriptor=_session_descriptor(
            agent_id,
            session_id,
            source.metadata,
            None,
            message_count=source.message_count,
            first_user_message=source.first_user_message,
        ),
    )


def _session_descriptors_for_hits(
    hits: list[RecallSearchHit],
    contexts: dict[str, _SearchSessionContext],
) -> list[JsonObject]:
    seen: set[str] = set()
    descriptors: list[JsonObject] = []
    for hit in hits:
        if hit.session_id in seen:
            continue
        seen.add(hit.session_id)
        context = contexts.get(hit.session_id)
        if context is not None:
            descriptors.append(context.descriptor)
    return descriptors


def _excerpt_bounds(hit: RecallSearchHit, excerpt_chars: int) -> tuple[int, int]:
    if excerpt_chars <= 0 or not hit.text:
        return 0, 0
    if len(hit.text) <= excerpt_chars:
        return 0, len(hit.text)
    anchor = hit.match_start if hit.match_start is not None else len(hit.text) // 2
    start = max(anchor - excerpt_chars // 3, 0)
    end = min(start + excerpt_chars, len(hit.text))
    start = max(end - excerpt_chars, 0)
    return start, end


def _project_read_items(
    messages: list[Any],
    first: int,
    last: int,
    *,
    exact_tool_result: bool,
    agent_id: str,
    session_id: str,
    current_agent_id: str,
    include_subagents: bool,
) -> list[JsonObject]:
    items: list[JsonObject] = []
    for message_index in range(first, last + 1):
        message = messages[message_index].to_dict()
        item: JsonObject = {"message_index": message_index, "message": message}
        if not exact_tool_result and _replace_large_tool_result(message):
            read_ref: JsonObject = {
                "session_id": session_id,
                "message_id": str(message["id"]),
            }
            if agent_id != current_agent_id:
                read_ref["agent_id"] = agent_id
            if include_subagents:
                read_ref["include_subagents"] = True
            item["read_ref"] = read_ref
        items.append(item)
    return items


def _user_anchor_index(messages: list[Any]) -> list[JsonObject]:
    anchors: list[JsonObject] = []
    for message_index, message in enumerate(messages):
        if str(message.role) != "user":
            continue
        text = compact_text(message_search_text(message))
        end = min(len(text), SESSION_READ_USER_ANCHOR_EXCERPT_MAX_CHARS)
        anchors.append(
            {
                "message_index": message_index,
                "message_id": str(message.id),
                "timestamp": str(message.timestamp),
                "excerpt": {
                    "text": text[:end],
                    "trailing_truncated": end < len(text),
                },
            }
        )
    return anchors


def _replace_large_tool_result(message: JsonObject) -> bool:
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return False
    content = str(message["content"])
    original_bytes = len(content.encode("utf-8"))
    if original_bytes <= SESSION_READ_INLINE_TOOL_RESULT_MAX_BYTES:
        return False
    try:
        value = redact_json_body(json.loads(content))
        preview_source = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        preview_source = content
    marker = {
        "_vbot_referenced_tool_result": True,
        "original_bytes": original_bytes,
        "preview": _bounded_preview(preview_source, SESSION_READ_TOOL_RESULT_PREVIEW_CHARS),
    }
    message["content"] = json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
    return True


def _bounded_preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"...[{len(value)} chars omitted]..."
    remaining = max(limit - len(marker), 0)
    head = (remaining * 2) // 3
    tail = remaining - head
    return f"{value[:head]}{marker}{value[-tail:] if tail else ''}"


def _render_read_selection(
    items: list[JsonObject],
    continuation: str | None,
    session_id: str,
    session_details: JsonObject,
    *,
    selection_key: str,
    selection_details: JsonObject,
    user_anchors: list[JsonObject] | None = None,
) -> JsonObject:
    complete = _read_data(
        session_id,
        session_details,
        items,
        has_more=False,
        selection_details=selection_details,
        user_anchors=user_anchors,
    )
    if _serialized_result_bytes(complete) <= SESSION_SEARCH_RESULT_MAX_BYTES:
        if continuation is not None:
            raise _SessionSearchError(
                "invalid_continuation",
                "Read continuation token is invalid for this selection.",
            )
        return complete

    selection: JsonObject | list[JsonObject]
    selection = items if user_anchors is None else {"user_anchors": user_anchors, "items": items}
    selection_json = json.dumps(selection, ensure_ascii=False, separators=(",", ":"))
    offset = (
        0
        if continuation is None
        else _read_continuation_offset(continuation, selection_key, selection_json)
    )
    if not selection_json:
        if continuation is not None:
            raise _SessionSearchError(
                "invalid_continuation",
                "Read continuation token is invalid for this selection.",
            )
        return complete
    if offset >= len(selection_json):
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        )

    low = offset + 1
    high = len(selection_json)
    best: JsonObject | None = None
    while low <= high:
        end = (low + high) // 2
        has_more = end < len(selection_json)
        candidate = _read_data(
            session_id,
            session_details,
            [
                {
                    "segment": {
                        "start": offset,
                        "end": end,
                        "complete": not has_more,
                        "selection_json": selection_json[offset:end],
                    }
                }
            ],
            has_more=has_more,
            selection_details=selection_details,
        )
        if has_more:
            candidate["next_continuation"] = _read_continuation_token(
                end, selection_key, selection_json
            )
        if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            best = candidate
            low = end + 1
        else:
            high = end - 1
    if best is None:
        raise _SessionSearchError(
            "session_read_error", "Read metadata exceeds the result safety limit."
        )
    return best


def _read_continuation_token(offset: int, selection_key: str, selection_json: str) -> str:
    digest = _read_selection_digest(selection_key, selection_json)
    return f"r1:{offset}:{digest}"


def _read_continuation_offset(token: str, selection_key: str, selection_json: str) -> int:
    parts = token.split(":")
    expected_digest = _read_selection_digest(selection_key, selection_json)
    if len(parts) != 3 or parts[0] != "r1" or parts[2] != expected_digest:
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        )
    try:
        offset = int(parts[1])
    except ValueError as error:
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        ) from error
    if offset < 0:
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        )
    return offset


def _read_selection_digest(selection_key: str, selection_json: str) -> str:
    value = f"{selection_key}\0{selection_json}".encode()
    return hashlib.sha256(value).hexdigest()


def _read_data(
    session_id: str,
    session_details: JsonObject,
    items: list[JsonObject],
    *,
    has_more: bool,
    selection_details: JsonObject,
    user_anchors: list[JsonObject] | None = None,
) -> JsonObject:
    data: JsonObject = {
        "session_id": session_id,
        "session": session_details,
        "selection": selection_details,
        "items": items,
        "has_more": has_more,
    }
    if user_anchors is not None:
        data["user_anchors"] = user_anchors
    return data


def _page_data(items: list[JsonObject], *, has_more: bool) -> JsonObject:
    return {"result_type": "session", "items": items, "has_more": has_more}


def _render_list_page(
    items: list[JsonObject],
    *,
    total_count: int,
) -> JsonObject:
    count = len(items)
    while count > 0:
        has_more = count < total_count
        data = _page_data(items[:count], has_more=has_more)
        if _serialized_result_bytes(data) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            return data
        count -= 1
    if items:
        raise _SessionSearchError(
            "session_search_error", "Session metadata exceeds the result safety limit."
        )
    return _page_data([], has_more=False)


def _session_summary_items(
    sessions: ChatSessionManager,
    agent_id: str,
    project_id: str | None,
    summaries: list[JsonObject],
) -> list[JsonObject]:
    items: list[JsonObject] = []
    addresses = {
        str(summary.get("id") or ""): SessionAddress(
            project_id=project_id,
            agent_id=agent_id,
            session_id=str(summary.get("id") or ""),
        )
        for summary in summaries
    }
    try:
        sources = sessions.descriptor_sources(tuple(addresses.values()))
    except Exception:
        sources = {}
    for summary in summaries:
        session_id = str(summary.get("id") or "")
        message_count: int | None = None
        first_user_message: Any | None = None
        source = sources.get(addresses[session_id])
        if source is not None:
            message_count = source.message_count
            first_user_message = source.first_user_message
        items.append(
            _session_summary(
                agent_id,
                summary,
                None,
                message_count=message_count,
                first_user_message=first_user_message,
            )
        )
    return items


def _session_summary(
    agent_id: str,
    summary: JsonObject,
    messages: list[Any] | None,
    *,
    message_count: int | None = None,
    first_user_message: Any | None = None,
) -> JsonObject:
    session_id = str(summary.get("id") or "")
    item = _session_descriptor(
        agent_id,
        session_id,
        summary,
        messages,
        message_count=message_count,
        first_user_message=first_user_message,
    )
    item.update(
        {
            "created_at": summary.get("created_at"),
            "last_active_at": summary.get("last_active_at"),
        }
    )
    return item


def _session_descriptor(
    agent_id: str,
    session_id: str,
    metadata: JsonObject,
    messages: list[Any] | None,
    *,
    message_count: int | None = None,
    first_user_message: Any | None = None,
) -> JsonObject:
    if messages is not None:
        message_count = len(messages)
        first_user_message = next(
            (message for message in messages if str(message.role) == "user"),
            None,
        )
    run_kinds = _session_run_kinds(metadata)
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "title": _descriptor_text(
            metadata.get("title") or metadata.get("auto_title"),
            SESSION_DESCRIPTOR_TITLE_MAX_CHARS,
        ),
        "run_kinds": run_kinds,
        "is_subagent_session": _is_subagent_session(metadata, run_kinds),
        "subagent_parent": _session_address(metadata.get(SUBAGENT_PARENT_METADATA_KEY)),
        "platform": _descriptor_text(
            metadata.get(CHANNEL_PLATFORM_METADATA_KEY),
            SESSION_DESCRIPTOR_PLATFORM_MAX_CHARS,
        ),
        "fork_source": _fork_source(metadata.get(FORK_SOURCE_META_KEY)),
        "message_count": message_count,
        "first_user_excerpt": _user_message_excerpt(first_user_message),
    }


def _session_run_kinds(metadata: JsonObject) -> list[str] | None:
    raw = metadata.get(SESSION_RUN_KINDS_META_KEY)
    if not isinstance(raw, list) or not raw:
        return None
    if any(not isinstance(value, str) or value not in _VALID_RUN_KINDS for value in raw):
        return None
    return list(dict.fromkeys(raw))


def _is_subagent_session(metadata: JsonObject, run_kinds: list[str] | None) -> bool | None:
    if run_kinds is not None and RunKind.SUBAGENT.value in run_kinds:
        return True
    explicit = metadata.get(SUBAGENT_SESSION_METADATA_FLAG)
    if isinstance(explicit, bool):
        return explicit
    if run_kinds is not None:
        return False
    return None


def _session_address(value: Any) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    agent_id = _descriptor_identifier(value.get("agent_id"), SESSION_DESCRIPTOR_AGENT_ID_MAX_CHARS)
    session_id = _descriptor_identifier(
        value.get("session_id"), SESSION_DESCRIPTOR_SESSION_ID_MAX_CHARS
    )
    if agent_id is None or session_id is None:
        return None
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "project_id": _descriptor_identifier(
            value.get("project_id"), SESSION_DESCRIPTOR_PROJECT_ID_MAX_CHARS
        ),
    }


def _fork_source(value: Any) -> JsonObject | None:
    address = _session_address(value)
    if address is None:
        return None
    assert isinstance(value, dict)
    address["forked_at"] = _descriptor_text(
        value.get("forked_at"), SESSION_DESCRIPTOR_TIMESTAMP_MAX_CHARS
    )
    return address


def _descriptor_identifier(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars:
        return None
    return normalized


def _descriptor_text(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = compact_text(value)
    if not normalized:
        return None
    return normalized[:max_chars]


def _user_message_excerpt(message: Any | None) -> JsonObject | None:
    if message is None:
        return None
    text = compact_text(message_search_text(message))
    if not text:
        return None
    end = min(len(text), SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS)
    return {
        "text": text[:end],
        "trailing_truncated": end < len(text),
    }


def _message_ref(message: Any) -> JsonObject:
    return {
        "message_id": str(message.id),
        "timestamp": str(message.timestamp),
        "role": str(message.role),
    }


def _session_details(
    agent_id: str,
    session_id: str,
    metadata: JsonObject,
    messages: list[Any],
) -> JsonObject:
    details = _session_descriptor(agent_id, session_id, metadata, messages)
    details.update(
        {
            "role_counts": dict(Counter(str(message.role) for message in messages)),
            "first_message": _message_ref(messages[0]) if messages else None,
            "last_message": _message_ref(messages[-1]) if messages else None,
        }
    )
    return details


def _serialized_result_bytes(data: JsonObject) -> int:
    return len(
        json.dumps(tool_success(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
