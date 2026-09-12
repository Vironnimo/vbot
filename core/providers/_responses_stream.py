"""Responses stream."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.providers._responses_output import (
    _assistant_phase_from_output,
    _extract_output_text_parts,
    _extract_reasoning_meta,
    _extract_reasoning_parts,
    _extract_responses_usage,
)
from core.providers._responses_values import (
    REASONING_SUMMARY_DELTA_EVENTS,
    RESPONSES_DONE_MARKER,
    RESPONSES_ERROR_EVENTS,
    RESPONSES_INCOMPLETE_EVENTS,
    _function_call_arguments,
    _function_call_id,
    _function_call_name,
    _joined_or_none,
    _non_empty_string_or_none,
    _response_output_from_meta,
    _response_output_items,
)
from core.providers.adapter import (
    TERMINAL_OUTCOME_CONTENT_FILTERED,
    TERMINAL_OUTCOME_ERROR,
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    TerminalOutcome,
    normalize_tool_call_candidates,
)
from core.providers.errors import (
    IN_BAND_AUTH_ERROR_CODES,
    IN_BAND_RATE_LIMIT_ERROR_CODES,
    IN_BAND_RETRYABLE_NUMERIC_CODES,
    IN_BAND_TIMEOUT_ERROR_CODES,
    IN_BAND_TRANSIENT_ERROR_CODES,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    classify_in_band_provider_error,
)
from core.providers.reasoning import merge_reasoning_meta


@dataclass
class ResponsesStreamState:
    """State needed to normalize one Responses SSE stream."""

    lenient_unknown_errors: bool = False
    tool_call_ids_by_output_index: dict[int, str] = field(default_factory=dict)
    item_id_to_call_id: dict[str, str] = field(default_factory=dict)
    tool_call_order: list[str] = field(default_factory=list)
    emitted_tool_names: dict[str, str] = field(default_factory=dict)
    emitted_tool_arguments: dict[str, str] = field(default_factory=dict)
    emitted_output_text: str = ""
    emitted_reasoning_text: str = ""
    reasoning_meta: dict[str, Any] | None = None
    output_items_by_index: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    completed_response: dict[str, Any] | None = None

    def normalized_response(self) -> dict[str, Any]:
        """Return canonical assistant fields accumulated across the SSE stream."""
        tool_calls: list[dict[str, Any]] = []
        known_tool_ids = [
            *self.tool_call_order,
            *self.emitted_tool_names,
            *self.emitted_tool_arguments,
        ]
        for position, tool_call_id in enumerate(dict.fromkeys(known_tool_ids)):
            tool_calls.extend(
                normalize_tool_call_candidates(
                    tool_call_id=tool_call_id,
                    name=self.emitted_tool_names.get(tool_call_id),
                    arguments=self.emitted_tool_arguments.get(tool_call_id),
                    fallback_id=f"tool_call_{position}",
                )
            )
        result: dict[str, Any] = {
            "role": "assistant",
            "content": self.emitted_output_text or None,
            "reasoning": self.emitted_reasoning_text or None,
            "reasoning_meta": (
                dict(self.reasoning_meta) if self.reasoning_meta is not None else None
            ),
            "tool_calls": tool_calls or None,
        }
        phase = _assistant_phase_from_output(_response_output_from_meta(self.reasoning_meta))
        if phase is not None:
            result["phase"] = phase
        if self.usage is not None:
            result["usage"] = dict(self.usage)
        if self.completed_response is not None:
            result["terminal_outcome"] = _responses_finish_reason(
                self.completed_response,
                self,
            )
        return result


def iter_responses_sse_deltas_with_state(
    lines: Iterable[str],
    state: ResponsesStreamState,
) -> Iterator[dict[str, Any]]:
    """Parse Responses SSE lines using caller-owned stream state."""

    for event_name, event_data in _iter_sse_events(lines):
        yield from normalize_responses_stream_event(event_name, event_data, state)


def normalize_responses_stream_event(
    event_name: str,
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    """Normalize one parsed Responses stream event.

    Unknown event names are ignored so Copilot can add new non-critical events
    without leaking raw provider chunks to the chat layer.
    """

    event_type = _event_type(event_name, event_data)
    if event_type in RESPONSES_ERROR_EVENTS:
        raise _classify_responses_stream_error(
            event_data, lenient_unknown=state.lenient_unknown_errors
        )
    if event_type in RESPONSES_INCOMPLETE_EVENTS:
        return _completed_event_deltas(event_data, state, implied_status="incomplete")
    if event_type == "response.output_text.delta":
        return _output_text_delta(event_data, state)
    if event_type in REASONING_SUMMARY_DELTA_EVENTS:
        return _reasoning_delta(event_data, state)
    if event_type == "response.function_call_arguments.delta":
        return _function_arguments_delta(event_data, state)
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        return _output_item_event_deltas(event_data, state)
    if event_type in {"response.completed", "response.done"}:
        return _completed_event_deltas(event_data, state, implied_status="completed")
    return []


def _iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    event_name = ""
    data_parts: list[str] = []
    for raw_chunk in lines:
        for raw_line in raw_chunk.splitlines():
            line = raw_line.rstrip("\r\n")
            if not line:
                yield from _flush_sse_event(event_name, data_parts)
                event_name = ""
                data_parts = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                data_parts.append(line[len("data:") :].strip())
    yield from _flush_sse_event(event_name, data_parts)


def _flush_sse_event(
    event_name: str,
    data_parts: list[str],
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if not data_parts:
        return
    data = "\n".join(data_parts).strip()
    if not data or data == RESPONSES_DONE_MARKER:
        return
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Responses provider sent malformed JSON in stream ({exc.msg}): {data}",
            retryable=False,
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProviderError(
            f"Responses provider sent non-object JSON in stream: {data}",
            retryable=False,
        )
    yield event_name, parsed


def _event_type(event_name: str, event_data: Mapping[str, Any]) -> str:
    event_type = event_data.get("type")
    if isinstance(event_type, str) and event_type:
        return event_type
    return event_name


def _text_delta(event_data: Mapping[str, Any], delta_type: str) -> list[dict[str, Any]]:
    delta = event_data.get("delta")
    if not isinstance(delta, str) or not delta:
        return []
    return [{"type": delta_type, "text": delta}]


def _output_text_delta(
    event_data: Mapping[str, Any], state: ResponsesStreamState
) -> list[dict[str, Any]]:
    deltas = _text_delta(event_data, "content_delta")
    if deltas:
        state.emitted_output_text += deltas[0]["text"]
    return deltas


def _reasoning_delta(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    deltas = _text_delta(event_data, "reasoning_delta")
    if deltas:
        state.emitted_reasoning_text += deltas[0]["text"]
    return deltas


def _function_arguments_delta(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    tool_call_id = _stream_tool_call_id(event_data, state)
    delta = event_data.get("delta")
    if not isinstance(delta, str) or not delta:
        return []
    arguments_delta = _record_tool_argument_delta(tool_call_id, delta, state)
    if arguments_delta is None:
        return []
    return [
        {
            "type": "tool_call_delta",
            "id": tool_call_id,
            "name_delta": "",
            "arguments_delta": arguments_delta,
        }
    ]


def _output_item_event_deltas(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    item = event_data.get("item")
    if not isinstance(item, Mapping):
        return []
    _record_stream_output_item(event_data, item, state)
    if item.get("type") == "reasoning":
        reasoning_deltas: list[dict[str, Any]] = []
        reasoning = _joined_or_none(_extract_reasoning_parts([item]))
        reasoning_delta = _reasoning_backfill_delta(reasoning, state)
        if reasoning_delta is not None:
            reasoning_deltas.append({"type": "reasoning_delta", "text": reasoning_delta})
        reasoning_deltas.append(
            {"type": "reasoning_meta", "reasoning_meta": {"reasoning_items": [dict(item)]}}
        )
        _record_reasoning_meta({"reasoning_items": [dict(item)]}, state)
        return reasoning_deltas
    if item.get("type") != "function_call":
        return []
    tool_call_id = _function_call_id(item)
    _remember_stream_tool_call_id(event_data, tool_call_id, state)
    item_own_id = _non_empty_string_or_none(item.get("id"))
    if item_own_id is not None and item_own_id != tool_call_id:
        state.item_id_to_call_id[item_own_id] = tool_call_id
    deltas: list[dict[str, Any]] = []
    name = _function_call_name(item)
    arguments = item.get("arguments")
    name_delta = ""
    arguments_delta: str | None = None
    if isinstance(name, str) and name and tool_call_id not in state.emitted_tool_names:
        name_delta = name
        state.emitted_tool_names[tool_call_id] = name
    if isinstance(arguments, str) and arguments:
        arguments_delta = _record_tool_argument_delta(tool_call_id, arguments, state)
    nested_arguments = _function_call_arguments(item)
    if arguments_delta is None and isinstance(nested_arguments, str) and nested_arguments:
        arguments_delta = _record_tool_argument_delta(tool_call_id, nested_arguments, state)
    if name_delta or arguments_delta:
        deltas.append(
            {
                "type": "tool_call_delta",
                "id": tool_call_id,
                "name_delta": name_delta,
                "arguments_delta": arguments_delta or "",
            }
        )
    return deltas


def _record_tool_argument_delta(
    tool_call_id: str,
    delta: str,
    state: ResponsesStreamState,
) -> str | None:
    emitted_arguments = state.emitted_tool_arguments.get(tool_call_id, "")
    if not emitted_arguments:
        state.emitted_tool_arguments[tool_call_id] = delta
        return delta
    if delta == emitted_arguments:
        return None
    if delta.startswith(emitted_arguments):
        suffix = delta[len(emitted_arguments) :]
        state.emitted_tool_arguments[tool_call_id] = delta
        return suffix or None

    state.emitted_tool_arguments[tool_call_id] = emitted_arguments + delta
    return delta


def _completed_event_deltas(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
    *,
    implied_status: str | None = None,
) -> list[dict[str, Any]]:
    response = event_data.get("response")
    if not isinstance(response, Mapping):
        response = event_data
    if implied_status is not None and not isinstance(response.get("status"), str):
        response = {**response, "status": implied_status}
    state.completed_response = dict(response)
    deltas: list[dict[str, Any]] = []
    completed_output_items = _response_output_items(response.get("output"))
    if completed_output_items:
        state.output_items_by_index = {
            output_index: dict(item) for output_index, item in enumerate(completed_output_items)
        }
    output_items = completed_output_items or _ordered_stream_output_items(state)
    content = _joined_or_none(_extract_output_text_parts(output_items))
    content_backfill = _text_backfill_delta(content, state.emitted_output_text)
    if content_backfill is not None:
        state.emitted_output_text += content_backfill
        deltas.append({"type": "content_delta", "text": content_backfill})
    reasoning = _joined_or_none(_extract_reasoning_parts(output_items))
    reasoning_backfill = _reasoning_backfill_delta(reasoning, state)
    if reasoning_backfill is not None:
        deltas.append({"type": "reasoning_delta", "text": reasoning_backfill})
    reasoning_meta = _extract_reasoning_meta(response, output_items)
    if reasoning_meta is not None:
        _record_reasoning_meta(reasoning_meta, state)
        deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})
    usage = _extract_responses_usage(response.get("usage"))
    if usage is not None:
        state.usage = dict(usage)
        deltas.append({"type": "usage", **usage})
    for output_index, item in enumerate(output_items):
        if item.get("type") == "function_call":
            _output_item_event_deltas(
                {"item": item, "output_index": output_index},
                state,
            )
    deltas.append({"type": "finish", "reason": _responses_finish_reason(response, state)})
    return deltas


def _record_stream_output_item(
    event_data: Mapping[str, Any],
    item: Mapping[str, Any],
    state: ResponsesStreamState,
) -> None:
    output_index = event_data.get("output_index")
    if isinstance(output_index, int) and output_index >= 0:
        state.output_items_by_index[output_index] = dict(item)
        return
    stable_id = item.get("id") or item.get("call_id")
    if isinstance(stable_id, str) and stable_id:
        for existing_index, existing_item in state.output_items_by_index.items():
            existing_id = existing_item.get("id") or existing_item.get("call_id")
            if existing_id == stable_id:
                state.output_items_by_index[existing_index] = dict(item)
                return
    next_index = max(state.output_items_by_index, default=-1) + 1
    state.output_items_by_index[next_index] = dict(item)


def _ordered_stream_output_items(state: ResponsesStreamState) -> list[Mapping[str, Any]]:
    return [state.output_items_by_index[index] for index in sorted(state.output_items_by_index)]


def _record_reasoning_meta(meta: Mapping[str, Any], state: ResponsesStreamState) -> None:
    state.reasoning_meta = merge_reasoning_meta(state.reasoning_meta, meta)


def _text_backfill_delta(text: str | None, emitted_text: str) -> str | None:
    if text is None or text == emitted_text or emitted_text.endswith(text):
        return None
    if not emitted_text:
        return text
    if text.startswith(emitted_text):
        return text[len(emitted_text) :] or None
    overlap = _suffix_prefix_overlap(emitted_text, text)
    if overlap <= 0:
        return None
    return text[overlap:] or None


def _reasoning_backfill_delta(
    reasoning: str | None,
    state: ResponsesStreamState,
) -> str | None:
    if reasoning is None:
        return None
    emitted_reasoning = state.emitted_reasoning_text
    if not emitted_reasoning:
        state.emitted_reasoning_text = reasoning
        return reasoning
    if reasoning == emitted_reasoning or emitted_reasoning.endswith(reasoning):
        return None
    if reasoning.startswith(emitted_reasoning):
        backfill = reasoning[len(emitted_reasoning) :]
        state.emitted_reasoning_text = reasoning
        return backfill or None

    overlap = _suffix_prefix_overlap(emitted_reasoning, reasoning)
    if overlap > 0:
        backfill = reasoning[overlap:]
        state.emitted_reasoning_text += backfill
        return backfill or None
    return None


def _suffix_prefix_overlap(left: str, right: str) -> int:
    max_overlap = min(len(left), len(right))
    for overlap in range(max_overlap, 0, -1):
        if left.endswith(right[:overlap]):
            return overlap
    return 0


def _responses_finish_reason(
    response: Mapping[str, Any],
    state: ResponsesStreamState | None = None,
) -> TerminalOutcome:
    status = response.get("status")
    if status == "failed":
        return TERMINAL_OUTCOME_ERROR
    if status == "incomplete":
        incomplete_details = response.get("incomplete_details")
        reason = (
            incomplete_details.get("reason") if isinstance(incomplete_details, Mapping) else None
        )
        if reason == "max_output_tokens":
            return TERMINAL_OUTCOME_OUTPUT_TRUNCATED
        if reason in {"content_filter", "content_policy_violation"}:
            return TERMINAL_OUTCOME_CONTENT_FILTERED
        return TERMINAL_OUTCOME_UNKNOWN
    if status != "completed":
        return TERMINAL_OUTCOME_UNKNOWN

    output_items = _response_output_items(response.get("output"))
    if any(item.get("type") == "function_call" for item in output_items):
        return TERMINAL_OUTCOME_TOOL_CALLS
    if state is not None and _stream_has_tool_calls(state):
        return TERMINAL_OUTCOME_TOOL_CALLS
    return TERMINAL_OUTCOME_STOP


def _stream_has_tool_calls(state: ResponsesStreamState) -> bool:
    return bool(
        state.tool_call_ids_by_output_index
        or state.emitted_tool_names
        or state.emitted_tool_arguments
    )


def _responses_error_message(event_data: Mapping[str, Any]) -> str:
    payload = _responses_error_payload(event_data)
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    return "Responses request failed"


def _classify_responses_stream_error(
    event_data: Mapping[str, Any], *, lenient_unknown: bool = False
) -> ProviderError:
    """Map exact Responses error facts into vBot's shared recovery taxonomy."""

    payload = _responses_error_payload(event_data)
    error = payload.get("error")
    error_mapping = error if isinstance(error, Mapping) else {}
    error_type = _non_empty_string_or_none(payload.get("error_type"))
    if error_type is None:
        error_type = _non_empty_string_or_none(error_mapping.get("error_type"))
    code: Any = error_mapping.get("code")
    if code is None:
        code = payload.get("code")
    classifier = error_type or (_non_empty_string_or_none(code) if isinstance(code, str) else None)
    numeric_code = code if isinstance(code, int) and not isinstance(code, bool) else None
    message = _responses_error_message(event_data)

    if lenient_unknown:
        router_error = {**payload, **error_mapping}
        if error_type is not None:
            metadata = router_error.get("metadata")
            router_error["metadata"] = {
                **(metadata if isinstance(metadata, Mapping) else {}),
                "error_type": error_type,
            }
        return classify_in_band_provider_error(router_error, lenient_unknown=True)

    if classifier in IN_BAND_AUTH_ERROR_CODES or numeric_code in {401, 403}:
        return ProviderAuthError(message)
    if classifier in IN_BAND_RATE_LIMIT_ERROR_CODES or numeric_code == 429:
        return ProviderRateLimitError(message)
    if classifier in IN_BAND_TIMEOUT_ERROR_CODES or numeric_code == 504:
        return ProviderTimeoutError(message)
    if (
        classifier in IN_BAND_TRANSIENT_ERROR_CODES
        or numeric_code in IN_BAND_RETRYABLE_NUMERIC_CODES
    ):
        return ProviderError(message, retryable=True)
    return ProviderError(message, retryable=False)


def _responses_error_payload(event_data: Mapping[str, Any]) -> Mapping[str, Any]:
    response = event_data.get("response")
    return response if isinstance(response, Mapping) else event_data


def _stream_tool_call_id(event_data: Mapping[str, Any], state: ResponsesStreamState) -> str:
    call_id = _non_empty_string_or_none(event_data.get("call_id"))
    if call_id is not None:
        _record_tool_call_order(call_id, state)
        return call_id
    item_id = _non_empty_string_or_none(event_data.get("item_id"))
    if item_id is not None:
        canonical_id = state.item_id_to_call_id.get(item_id)
        if canonical_id:
            return canonical_id
    output_index = event_data.get("output_index")
    if isinstance(output_index, int):
        existing_id = state.tool_call_ids_by_output_index.get(output_index)
        if existing_id:
            _record_tool_call_order(existing_id, state)
            return existing_id
        generated_id = f"tool_call_{output_index}"
        state.tool_call_ids_by_output_index[output_index] = generated_id
        _record_tool_call_order(generated_id, state)
        return generated_id
    if item_id is not None:
        _record_tool_call_order(item_id, state)
        return item_id
    _record_tool_call_order("tool_call_0", state)
    return "tool_call_0"


def _remember_stream_tool_call_id(
    event_data: Mapping[str, Any],
    tool_call_id: str,
    state: ResponsesStreamState,
) -> None:
    output_index = event_data.get("output_index")
    if isinstance(output_index, int):
        state.tool_call_ids_by_output_index[output_index] = tool_call_id
    _record_tool_call_order(tool_call_id, state)


def _record_tool_call_order(tool_call_id: str, state: ResponsesStreamState) -> None:
    if tool_call_id not in state.tool_call_order:
        state.tool_call_order.append(tool_call_id)
