"""Bounded search excerpts, conversation context and Session descriptors."""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.recall import (
    RecallSearchHit,
    RecallSearchPage,
)
from core.recall.canonical import (
    compact_text,
)
from core.runs import RunKind
from core.sessions import (
    SESSION_RUN_KINDS_META_KEY,
    ChatSessionManager,
    SessionAddress,
)
from core.tools.tools import (
    JsonObject,
    tool_success,
)

SESSION_SEARCH_DEFAULT_LIMIT = 10


SESSION_SEARCH_RESULT_MAX_BYTES = 50 * 1024


SESSION_SEARCH_EXCERPT_MAX_CHARS = 1800


SESSION_DESCRIPTOR_TITLE_MAX_CHARS = 200
SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"


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
    is_subagent: bool


def _render_search_page(
    page: RecallSearchPage,
    targets: list[JsonObject],
    session_contexts: dict[str, _SearchSessionContext],
    *,
    agent_id: str,
    project_id: str | None = None,
) -> JsonObject:
    hits = list(page.hits)
    if len(targets) != len(hits):
        raise _SessionSearchError(
            "session_search_error",
            "Search returned inconsistent results. Repeat the search; if it "
            "fails again, report the failure.",
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
            agent_id=agent_id,
        )
        if _serialized_result_bytes(data) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            break
        count -= 1
    if count == 0 and hits:
        raise _SessionSearchError(
            "session_search_error",
            "Search results exceed the output limit. Narrow the query or "
            "restrict it to one session_id.",
        )
    selected = hits[:count]
    if not selected:
        return _search_data(
            page, [], [], has_more=page.has_more, project_id=project_id, agent_id=agent_id
        )

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
            agent_id=agent_id,
        )
        if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            best = candidate
            low = excerpt_chars + 1
        else:
            high = excerpt_chars - 1
    if best is None:
        raise _SessionSearchError(
            "session_search_error",
            "Search results exceed the output limit. Narrow the query or "
            "restrict it to one session_id.",
        )
    return best


def _search_data(
    page: RecallSearchPage,
    items: list[JsonObject],
    session_descriptors: list[JsonObject],
    *,
    has_more: bool,
    project_id: str | None,
    agent_id: str,
) -> JsonObject:
    data: JsonObject = {
        "agent_id": agent_id,
        "project_id": project_id,
        "items": items,
        "sessions": session_descriptors,
        "has_more": has_more,
        "searched_sessions": page.total_candidate_sessions,
    }
    guidance: list[str] = []
    if not items:
        guidance.append(
            "No matching conversation text was returned in this scope. Try fewer or different "
            "query terms, or remove unneeded period/session_id filters. Tool "
            "Results are not searched."
        )
    else:
        guidance.append(
            "These are selected excerpts and context, not a full transcript. Use them for "
            "focused answers; inspect the saved transcript when complete wording "
            "or later revisions matter."
        )
    if has_more:
        guidance.append(
            "More matches exist. Refine query, period or session_id; there is no "
            "next-page parameter."
        )
    if any(
        item["excerpt"]["leading_truncated"] or item["excerpt"]["trailing_truncated"]
        for item in items
    ):
        guidance.append(
            "Some excerpts are cut short. A trailing_truncated excerpt does not show the "
            "ending of the source text: do not quote its last visible sentence as the "
            "Message's last sentence. Read the complete Message when its ending or full "
            "wording is needed. If that is unavailable, state what you cannot verify."
        )
    if any(item.get("content_kind") == "conversation_excerpt" for item in items):
        guidance.append(
            "Conversation excerpts can span several speakers. Read original "
            "Messages before attributing quotations."
        )
    data["guidance"] = " ".join(guidance)
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
            "leading_truncated": start > 0,
            "trailing_truncated": end < len(hit.text),
        },
    }
    if hit.result_type == "passage":
        item["end_message_id"] = hit.end_message_id
        if hit.end_message_id is not None and hit.end_message_id != hit.message_id:
            item.pop("role")
            item["content_kind"] = "conversation_excerpt"
        item["end_timestamp"] = hit.end_timestamp
    if "include_subagents" in target:
        item["include_subagents"] = True
    if hit.role == "compaction_checkpoint":
        item["content_kind"] = "compaction_summary"
    return item


def _search_context_for_hits(
    hits: list[RecallSearchHit],
    *,
    agent_id: str,
    project_id: str | None,
    sessions: ChatSessionManager | None,
    include_subagents: bool,
    summaries: dict[str, JsonObject],
) -> tuple[list[JsonObject], dict[str, _SearchSessionContext]]:
    loaded: dict[str, _SearchSessionContext] = {}
    targets: list[JsonObject] = []
    for hit in hits:
        if hit.session_id not in loaded:
            metadata = summaries.get(hit.session_id, {})
            title = metadata.get("title") or metadata.get("auto_title")
            descriptor: JsonObject = {"agent_id": agent_id, "session_id": hit.session_id}
            if isinstance(title, str) and title.strip():
                descriptor["title"] = compact_text(title)[:SESSION_DESCRIPTOR_TITLE_MAX_CHARS]
            loaded[hit.session_id] = _SearchSessionContext(
                descriptor=descriptor,
                conversations={},
                is_subagent=_is_subagent_session(metadata, _session_run_kinds(metadata)) is True,
            )
        source = loaded[hit.session_id]
        target: JsonObject = {"agent_id": agent_id}
        if include_subagents and source.is_subagent:
            target["include_subagents"] = True
        if sessions is not None and hit.message_id not in source.conversations:
            source.conversations[hit.message_id] = sessions.recall_context(
                SessionAddress(project_id=project_id, agent_id=agent_id, session_id=hit.session_id),
                hit.message_id,
            )
        targets.append(target)
    return targets, loaded


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


def _serialized_result_bytes(data: JsonObject) -> int:
    return len(
        json.dumps(tool_success(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
