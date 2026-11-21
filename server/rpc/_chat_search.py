"""Bounded snippets for the displayed Chat timeline."""

from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate
from typing import Any

from core.sessions.search_content import compact_text
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_bool,
    _reject_unsupported,
    _required_session_address,
    _required_string,
    rpc_request_fields,
)

_RESULT_LIMIT = 200
_SNIPPET_LENGTH = 168


def _snippet(text: str, terms: tuple[str, ...]) -> tuple[str, list[tuple[int, int]]]:
    """Return half-open Unicode code point ranges in a short single-line excerpt."""
    text = compact_text(text)
    folded = text.casefold()
    first = min(offset for term in terms if (offset := folded.find(term)) >= 0)
    # Locate the match in the source before clipping it. Even a term
    # longer than the excerpt must retain its visible highlight.
    first_source = first
    if len(folded) != len(text):
        folded_offset = 0
        for index, char in enumerate(text):
            folded_offset += len(char.casefold())
            if folded_offset > first:
                first_source = index
                break
    start = max(0, first_source - 32)
    end = min(len(text), start + _SNIPPET_LENGTH)
    prefix = int(start > 0)
    excerpt = ("…" if prefix else "") + text[start:end] + ("…" if end < len(text) else "")
    boundaries = list(accumulate((len(c.casefold()) for c in text[start:end]), initial=0))
    window = len(text[:start].casefold()) if len(text) != len(folded) else start
    stop = window + boundaries[-1]
    marked: set[int] = set()
    for term in terms:
        offset = folded.find(term, max(0, window - len(term) + 1), stop + len(term) - 1)
        while 0 <= offset < stop:
            left = max(0, offset - window)
            right = min(stop, offset + len(term)) - window
            marked.update(
                range(
                    prefix + bisect_right(boundaries, left) - 1,
                    prefix + bisect_right(boundaries, right - 1),
                )
            )
            offset = folded.find(term, offset + 1, stop + len(term) - 1)
    ranges: list[tuple[int, int]] = []
    for index in sorted(marked):
        if ranges and ranges[-1][1] == index:
            ranges[-1] = (ranges[-1][0], index + 1)
        else:
            ranges.append((index, index + 1))
    return excerpt, ranges


async def chat_search(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(params, rpc_request_fields("chat.search"), "chat.search")
    address = _required_session_address(params)
    query = _required_string(params, "query")
    include_inherited = _optional_bool(params, "include_inherited", default=True)
    if len(query) > 256:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "query must contain at most 256 characters")
    sessions = state.runtime.chat_sessions

    def read() -> dict[str, Any]:
        sessions.get(address)
        result = sessions.search_messages(
            query,
            project_id=address.project_id,
            agent_id=address.agent_id,
            session_id=address.session_id,
            timeline_only=True,
            include_inherited=include_inherited,
            roles=("user", "assistant"),
            order="newest",
            limit=_RESULT_LIMIT + 1,
        )
        terms = tuple(dict.fromkeys(query.casefold().split()))
        hits = []
        for hit in result.hits[:_RESULT_LIMIT]:
            snippet, ranges = _snippet(hit.text, terms)
            hits.append(
                {
                    "message_id": hit.message_id,
                    "sequence": hit.message_index,
                    "run_id": hit.run_id,
                    "role": hit.role,
                    "timestamp": hit.timestamp,
                    "snippet": snippet,
                    "ranges": ranges,
                }
            )
        return {"hits": hits, "complete": result.complete and len(result.hits) <= _RESULT_LIMIT}

    try:
        response: dict[str, Any] = await sessions.run_async(read)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return response
