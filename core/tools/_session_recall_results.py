"""Bounded search excerpts, conversation context and Session descriptors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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


SESSION_SEARCH_EXCERPT_MAX_CHARS = 1800


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
    conversations: dict[str, list[JsonObject]]


def _render_search_page(
    page: RecallSearchPage,
    targets: list[JsonObject],
    session_contexts: dict[str, _SearchSessionContext],
    *,
    project_id: str | None = None,
) -> JsonObject:
    hits = list(page.hits)
    if len(targets) != len(hits):
        raise _SessionSearchError(
            "session_search_error", "Search targets do not match the result page."
        )
    count = len(hits)
    while count > 0:
        items = [
            _hit_item(
                hit,
                index + 1,
                excerpt_chars=0,
                target=targets[index],
                context=session_contexts[hit.session_id].conversations.get(hit.message_id, []),
            )
            for index, hit in enumerate(hits[:count])
        ]
        has_more = count < len(hits) or page.has_more
        data = _search_data(
            page,
            items,
            _session_descriptors_for_hits(hits[:count], session_contexts),
            has_more=has_more,
            project_id=project_id,
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
        return _search_data(page, [], [], has_more=False, project_id=project_id)

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
                target=targets[index],
                context=session_contexts[hit.session_id].conversations.get(hit.message_id, []),
            )
            for index, hit in enumerate(selected)
        ]
        has_more = count < len(hits) or page.has_more
        candidate = _search_data(
            page,
            items,
            _session_descriptors_for_hits(selected, session_contexts),
            has_more=has_more,
            project_id=project_id,
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
    project_id: str | None,
) -> JsonObject:
    data: JsonObject = {
        "result_type": page.result_type,
        "project_id": project_id,
        "items": items,
        "sessions": session_descriptors,
        "has_more": has_more,
        "searched_sessions": page.total_candidate_sessions,
    }
    if has_more:
        data["guidance"] = "Narrow query, period or session_id to find additional matches."
    if page.degraded:
        data["degraded"] = True
        data["degradation_reason"] = page.degradation_reason
    return data


def _hit_item(
    hit: RecallSearchHit,
    rank: int,
    *,
    excerpt_chars: int,
    target: JsonObject,
    context: list[JsonObject],
) -> JsonObject:
    start, end = _excerpt_bounds(hit, excerpt_chars)
    item: JsonObject = {
        "rank": rank,
        "agent_id": target["agent_id"],
        "session_id": hit.session_id,
        "message_id": hit.message_id,
        "role": hit.role,
        "timestamp": hit.timestamp,
        "context": context,
        "context_is_partial": True,
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
    if "include_subagents" in target:
        item["include_subagents"] = True
    if hit.role == "compaction_checkpoint":
        item["content_kind"] = "compaction_summary"
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
        target: JsonObject = {
            "agent_id": agent_id,
            "session_id": hit.session_id,
            "message_id": hit.message_id,
        }
        if (
            include_subagents
            and loaded[hit.session_id].descriptor.get("is_subagent_session") is True
        ):
            target["include_subagents"] = True
        if sessions is not None:
            loaded[hit.session_id].conversations[hit.message_id] = sessions.recall_context(
                addresses[hit.session_id], hit.message_id
            )
        refs.append(target)
    return refs, loaded


def _search_hit_session_context(
    agent_id: str,
    session_id: str,
    source: SessionDescriptorSource | None,
) -> _SearchSessionContext:
    if source is None:
        return _SearchSessionContext(
            descriptor=_session_descriptor(agent_id, session_id, {}, None),
            conversations={},
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
        conversations={},
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


def _serialized_result_bytes(data: JsonObject) -> int:
    return len(
        json.dumps(tool_success(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
