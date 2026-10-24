"""Entry rows: per-role storage, set-wise decoding and materialized copies.

Every history item is one ``entries`` row; large or role-specific values live
in 1:1 side tables. Reads select entry rows first and then fetch only the side
tables their roles need, set-wise by ``entry_key``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_attachments, _store_fts, _store_tool_invocations, _store_values
from core.sessions._types import JsonObject, ToolResultFacts, ToolResultPayload
from core.sessions.errors import SessionStoreCorruptError
from core.utils.ids import is_safe_id
from core.utils.surrogates import replace_lone_surrogates, without_lone_surrogates

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage

# ruff: noqa: E501


# The entry columns every read selects; ``e`` names ``entries``.
ENTRY_COLUMNS = (
    "e.entry_key, e.session_key, e.seq, e.role, e.entry_id, e.created_at, e.run_key, "
    "e.model, e.superseded_at_seq, e.subagent_reply_target, "
    "e.skill_package_source, e.skill_package_name, e.skill_package_location"
)
# Roles a plain append may write. Run footers belong to ``finish_run`` and edit
# markers to ``apply_edit``, which keep their relations consistent.
APPENDABLE_ROLES = frozenset(
    {
        "system",
        "user",
        "assistant",
        "tool",
        "note",
        "error",
        "compaction_checkpoint",
        "agent_takeover",
    }
)
TOOL_RESULT_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Roles without an ``entry_text`` row.
_TEXTLESS_ROLES = frozenset({"run_summary", "history_edit"})


def _split_structured_fields(
    payload: JsonObject | None,
    validators: Mapping[str, Callable[[Any], bool]],
) -> tuple[dict[str, Any], str | None, bool]:
    """Promote stable fields while retaining unknown/future fields losslessly."""
    if payload is None:
        return {}, None, False
    remaining = dict(payload)
    promoted: dict[str, Any] = {}
    for key, validator in validators.items():
        if key in remaining and validator(remaining[key]):
            promoted[key] = remaining.pop(key)
    return promoted, _store_values._optional_json(remaining or None, "structured extras"), True


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _timing_fields(
    payload: JsonObject | None,
) -> tuple[dict[str, Any], str | None, bool]:
    return _split_structured_fields(
        payload,
        {
            "started_at": _is_string,
            "completed_at": _is_string,
            "duration_ms": _is_non_negative_int,
        },
    )


_USAGE_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "input_tokens": _is_non_negative_int,
    "output_tokens": _is_non_negative_int,
    "cache_read_tokens": _is_non_negative_int,
    "cache_write_tokens": _is_non_negative_int,
    "reasoning_tokens": _is_non_negative_int,
    "input_tokens_estimated": _is_bool,
    "output_tokens_estimated": _is_bool,
}
_CHECKPOINT_USAGE_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "compacted_token_count": _is_non_negative_int,
    "context_tokens_before": _is_non_negative_int,
    "context_tokens_after": _is_non_negative_int,
    "compaction_duration_ms": _is_non_negative_int,
}
_USAGE_FLAG_COLUMNS = ("input_tokens_estimated", "output_tokens_estimated")
# The whole-turn ``estimated`` summarizes the field-level flags (Message
# validation keeps them in step), so it is derived on read instead of stored.
_USAGE_SUMMARY_KEY = "estimated"
_USAGE_COUNT_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def validate_appendable(message: ChatMessage) -> None:
    """Validate one Message a plain append may persist."""
    message.validate()
    if message.role not in APPENDABLE_ROLES:
        raise ChatSessionError(f"{message.role} entries are written by their Session operation")


def storable(message: ChatMessage) -> ChatMessage:
    """Return *message* as SQLite can store it: every lone surrogate replaced with U+FFFD.

    UTF-8 cannot encode a lone surrogate, so one anywhere in a Message would
    fail its whole write, and the Run that writes it. Chat replaces them where
    Model output and Tool Results enter a Run; this guards every other path.
    """
    data = message.to_dict()
    replaced = without_lone_surrogates(data)
    if replaced is data:
        return message
    from core.chat.messages import ChatMessage

    return ChatMessage.from_dict(replaced)


def validate_tool_results(
    messages: Sequence[ChatMessage], tool_results: Mapping[str, ToolResultFacts]
) -> None:
    """Every fact must describe one Tool result of the same batch."""
    result_ids = {message.tool_call_id for message in messages if message.role == "tool"}
    for call_id, facts in tool_results.items():
        if call_id not in result_ids:
            raise ChatSessionError(f"Tool result facts have no Tool result: {call_id}")
        if not isinstance(facts, ToolResultFacts) or facts.status not in TOOL_RESULT_STATUSES:
            raise ChatSessionError(f"Tool result facts are invalid: {call_id}")
        if (
            (facts.ok is not None and not isinstance(facts.ok, bool))
            or (facts.error_code is not None and not isinstance(facts.error_code, str))
            or (facts.error_retryable is not None and not isinstance(facts.error_retryable, bool))
            or (facts.error_attempts is not None and not _is_non_negative_int(facts.error_attempts))
            or not _valid_payloads(facts.payloads)
        ):
            raise ChatSessionError(f"Tool result facts are invalid: {call_id}")


def _valid_payloads(payloads: Any) -> bool:
    """Payloads are distinct safe ids, each with an owner and JSON text."""
    if not isinstance(payloads, tuple) or not all(
        isinstance(payload, ToolResultPayload)
        and is_safe_id(payload.payload_id)
        and isinstance(payload.owner_name, str)
        and payload.owner_name
        and isinstance(payload.payload_json, str)
        for payload in payloads
    ):
        return False
    return len({payload.payload_id for payload in payloads}) == len(payloads)


def _text_row(message: ChatMessage) -> tuple[str | None, str | None, str | None] | None:
    if message.role in _TEXTLESS_ROLES or message.content is None:
        return None
    if isinstance(message.content, str):
        return message.content, None, None
    from core.chat.content_blocks import content_block_to_dict
    from core.sessions.search_content import content_to_text

    blocks = [content_block_to_dict(block) for block in message.content]
    return None, _store_values._optional_json(blocks, "content"), content_to_text(message.content)


def insert_entry(
    connection: sqlite3.Connection,
    session_key: int,
    seq: int,
    message: ChatMessage,
    *,
    run_key: int | None,
    superseded_at_seq: int | None = None,
) -> int:
    """Write one validated Message as an entry with its side rows; return its key.

    Tool results are linked to their call separately (:func:`link_tool_result`).
    """
    cursor = connection.execute(
        "INSERT INTO entries (session_key, seq, role, entry_id, created_at, run_key, model, "
        "searchable, superseded_at_seq, subagent_reply_target, skill_package_source, skill_package_name, skill_package_location) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_key,
            seq,
            message.role,
            message.id,
            _store_values._timestamp(message.timestamp, "Message timestamp"),
            run_key,
            message.model,
            int(_store_fts._message_is_searchable(message)),
            superseded_at_seq,
            message.subagent_reply_target,
            message.skill_package.source if message.skill_package else None,
            message.skill_package.name if message.skill_package else None,
            message.skill_package.location if message.skill_package else None,
        ),
    )
    if cursor.lastrowid is None:
        raise SessionStoreCorruptError("SQLite did not return an entry key")
    entry_key = int(cursor.lastrowid)
    text = _text_row(message)
    if text is not None:
        connection.execute(
            "INSERT INTO entry_text (entry_key, content, blocks_json, search_text) VALUES (?, ?, ?, ?)",
            (entry_key, *text),
        )
    match message.role:
        case "assistant":
            _insert_assistant(connection, entry_key, message)
        case "user":
            _insert_user(connection, entry_key, message)
        case "error":
            connection.execute(
                "INSERT INTO error_entries (entry_key, error_kind) VALUES (?, ?)",
                (entry_key, message.error_kind),
            )
        case "history_edit":
            connection.execute(
                "INSERT INTO history_edit_entries (entry_key, target_entry_id) VALUES (?, ?)",
                (entry_key, message.target_message_id),
            )
        case "note" if message.model_fallback is not None:
            connection.execute(
                "INSERT INTO note_model_fallbacks (entry_key, from_model, to_model, retry_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    entry_key,
                    message.model_fallback.from_model,
                    message.model_fallback.to_model,
                    message.model_fallback.retry_at,
                ),
            )
        case "note" if message.goal_event is not None:
            goal = message.goal_event
            connection.execute(
                "INSERT INTO note_goal_events "
                "(entry_key, event, reason, objective, turn, max_turns) VALUES (?, ?, ?, ?, ?, ?)",
                (entry_key, goal.event, goal.reason, goal.objective, goal.turn, goal.max_turns),
            )
        case "compaction_checkpoint":
            _insert_checkpoint(connection, entry_key, message)
    if message.role == "note" and message.subagent_messages:
        connection.executemany(
            "INSERT INTO note_subagent_messages (entry_key, ordinal, subagent_id, title, content, "
            "delivery_id, agent_id, session_id, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    entry_key,
                    ordinal,
                    item.subagent_id,
                    item.title,
                    item.content,
                    item.delivery_id,
                    item.agent_id,
                    item.session_id,
                    item.project_id,
                )
                for ordinal, item in enumerate(message.subagent_messages)
            ],
        )
    if message.role == "note" and message.widget_states:
        connection.executemany(
            "INSERT INTO note_widget_states (entry_key, ordinal, widget_id, title, state_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    entry_key,
                    ordinal,
                    item.widget_id,
                    item.title,
                    json.dumps(item.state, ensure_ascii=False, separators=(",", ":")),
                )
                for ordinal, item in enumerate(message.widget_states)
            ],
        )
    _store_attachments.record(connection, entry_key, message.to_dict())
    return entry_key


def _insert_user(connection: sqlite3.Connection, entry_key: int, message: ChatMessage) -> None:
    if message.sender is not None:
        connection.execute(
            "INSERT INTO user_entry_senders (entry_key, sender_id, display_name, sender_role) "
            "VALUES (?, ?, ?, ?)",
            (entry_key, message.sender.id, message.sender.display_name, message.sender.role),
        )
    if message.input_origin is not None:
        connection.execute(
            "INSERT INTO user_entry_origins (entry_key, input_origin) VALUES (?, ?)",
            (entry_key, message.input_origin),
        )


def _insert_assistant(connection: sqlite3.Connection, entry_key: int, message: ChatMessage) -> None:
    stored_usage = (
        None
        if message.usage is None
        else {key: value for key, value in message.usage.items() if key != _USAGE_SUMMARY_KEY}
    )
    usage, usage_extra, usage_present = _split_structured_fields(stored_usage, _USAGE_VALIDATORS)
    timing, timing_extra, _timing_present = _timing_fields(message.reasoning_timing)
    connection.execute(
        "INSERT INTO assistant_entries (entry_key, phase, reasoning_scope, has_tool_calls, "
        "interrupted, interruption_cause, usage_present, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, reasoning_tokens, input_tokens_estimated, "
        "output_tokens_estimated, reasoning_started_at, reasoning_completed_at, "
        "reasoning_duration_ms, usage_extra_json, reasoning_timing_extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry_key,
            message.phase,
            message.reasoning_scope,
            int(message.tool_calls is not None),
            int(message.interrupted),
            message.interruption_cause,
            int(usage_present),
            *(usage.get(column) for column in _USAGE_COUNT_COLUMNS),
            *(None if key not in usage else int(usage[key]) for key in _USAGE_FLAG_COLUMNS),
            _store_values._optional_timestamp(timing.get("started_at"), "reasoning timing"),
            _store_values._optional_timestamp(timing.get("completed_at"), "reasoning timing"),
            timing.get("duration_ms"),
            usage_extra,
            timing_extra,
        ),
    )
    if (
        message.reasoning is not None
        or message.reasoning_summary is not None
        or message.reasoning_meta is not None
    ):
        connection.execute(
            "INSERT INTO assistant_reasoning (entry_key, reasoning, summary_json, meta_json) "
            "VALUES (?, ?, ?, ?)",
            (
                entry_key,
                message.reasoning,
                _store_values._optional_json(message.reasoning_summary, "reasoning_summary"),
                _store_values._optional_json(message.reasoning_meta, "reasoning_meta"),
            ),
        )
    for ordinal, tool_call in enumerate(message.tool_calls or ()):
        rejection = tool_call.rejection
        cursor = connection.execute(
            "INSERT INTO tool_calls (entry_key, ordinal, call_id, name, status, rejection_code, "
            "rejection_fingerprint, argument_sequence_index, argument_sequence_length) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (
                entry_key,
                ordinal,
                tool_call.id,
                tool_call.name,
                None if rejection is None else rejection.code,
                None if rejection is None else rejection.fingerprint,
                tool_call.argument_sequence_index,
                tool_call.argument_sequence_length,
            ),
        )
        connection.execute(
            "INSERT INTO tool_call_payloads (call_key, arguments_json, rejection_message) "
            "VALUES (?, ?, ?)",
            (
                cursor.lastrowid,
                _store_values._json_object(tool_call.arguments, "tool call arguments"),
                None if rejection is None else rejection.message,
            ),
        )
    connection.executemany(
        "INSERT INTO assistant_output_files (entry_key, ordinal, path, line_index, start_index, "
        "end_index) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                entry_key,
                ordinal,
                reference.path,
                reference.line_index,
                reference.start_index,
                reference.end_index,
            )
            for ordinal, reference in enumerate(message.output_files or ())
        ],
    )


def _insert_checkpoint(
    connection: sqlite3.Connection, entry_key: int, message: ChatMessage
) -> None:
    usage, usage_extra, usage_present = _split_structured_fields(
        message.usage, _CHECKPOINT_USAGE_VALIDATORS
    )
    connection.execute(
        "INSERT INTO checkpoint_entries (entry_key, policy, strategy, compacted_token_count, "
        "context_tokens_before, context_tokens_after, duration_ms, usage_present, "
        "usage_extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry_key,
            message.compaction_policy,
            message.compaction_strategy,
            usage.get("compacted_token_count"),
            usage.get("context_tokens_before"),
            usage.get("context_tokens_after"),
            usage.get("compaction_duration_ms"),
            int(usage_present),
            usage_extra,
        ),
    )
    connection.execute(
        "INSERT INTO checkpoint_projections (entry_key, projection_json) VALUES (?, ?)",
        (entry_key, _store_values._optional_json(message.projection, "projection")),
    )


def link_tool_result(
    connection: sqlite3.Connection,
    session_key: int,
    entry_key: int,
    message: ChatMessage,
    *,
    run_key: int | None,
    assistant_message_id: str | None,
    facts: ToolResultFacts | None,
) -> None:
    """Attach one stored Tool result to the single open call it answers."""
    clauses = ["c.call_id = ?", "e.session_key = ?"]
    values: list[Any] = [message.tool_call_id, session_key]
    if run_key is not None:
        clauses.append("e.run_key = ?")
        values.append(run_key)
    if assistant_message_id is not None:
        clauses.append("e.entry_id = ?")
        values.append(assistant_message_id)
    rows = connection.execute(
        "SELECT c.call_key, c.result_entry_key, c.name, e.run_key, "
        "(e.seq >= COALESCE(s.fork_point_seq, 0) AND COALESCE(r.inherited, 0) = 0) AS own_call "
        "FROM tool_calls AS c JOIN entries AS e ON e.entry_key = c.entry_key "
        "JOIN sessions AS s ON s.session_key = e.session_key "
        "LEFT JOIN runs AS r ON r.run_key = e.run_key WHERE " + " AND ".join(clauses),
        values,
    ).fetchall()
    if len(rows) != 1:
        raise ChatSessionError("Tool result must identify exactly one stored invocation")
    if rows[0]["result_entry_key"] is not None:
        raise ChatSessionError("Tool invocation already has a result")
    call_key = int(rows[0]["call_key"])
    timing, timing_extra, _present = _timing_fields(message.timing)
    connection.execute(
        "UPDATE tool_calls SET result_entry_key = ?, status = ?, result_ok = ?, error_code = ?, "
        "error_retryable = ?, error_attempts = ?, started_at = ?, completed_at = ?, "
        "duration_ms = ? WHERE call_key = ?",
        (
            entry_key,
            "completed" if facts is None else facts.status,
            None if facts is None or facts.ok is None else int(facts.ok),
            None if facts is None else facts.error_code,
            None if facts is None or facts.error_retryable is None else int(facts.error_retryable),
            None if facts is None else facts.error_attempts,
            _store_values._optional_timestamp(timing.get("started_at"), "Tool timing"),
            _store_values._optional_timestamp(timing.get("completed_at"), "Tool timing"),
            timing.get("duration_ms"),
            call_key,
        ),
    )
    connection.execute(
        "UPDATE tool_call_payloads SET display_json = ?, timing_extra_json = ?, media_json = ? "
        "WHERE call_key = ?",
        (
            _store_values._optional_json(message.tool_display, "tool_display"),
            timing_extra,
            _store_values._optional_json(message.tool_media, "tool_media"),
            call_key,
        ),
    )
    _store_tool_invocations.link_result(
        connection,
        session_key=session_key,
        run_key=rows[0]["run_key"],
        result_entry_key=entry_key,
        call_id=str(message.tool_call_id),
        name=rows[0]["name"],
        timestamp=_store_values._timestamp(message.timestamp, "Message timestamp"),
        started_at=_store_values._optional_timestamp(timing.get("started_at"), "Tool timing"),
        status="completed" if facts is None else facts.status,
        result_ok=None if facts is None else facts.ok,
        error_code=None if facts is None else facts.error_code,
        duration_ms=timing.get("duration_ms"),
        invocation_key=None if facts is None else facts.invocation_key,
        content=message.content if isinstance(message.content, str) else None,
        own_call=bool(rows[0]["own_call"]),
    )
    if facts is not None and facts.payloads:
        created_at = _store_values._timestamp(message.timestamp, "Message timestamp")
        payloads = [
            (payload.payload_id, payload.owner_name, replace_lone_surrogates(payload.payload_json))
            for payload in facts.payloads
        ]
        connection.executemany(
            "INSERT INTO tool_result_payloads (payload_id, call_key, owner_name, created_at, "
            "payload_json) VALUES (?, ?, ?, ?, ?)",
            [
                (payload_id, call_key, owner_name, created_at, payload_json)
                for payload_id, owner_name, payload_json in payloads
            ],
        )
        for _payload_id, _owner_name, payload_json in payloads:
            _store_attachments.record(connection, entry_key, json.loads(payload_json))


@dataclass
class EntryBatch:
    """Entry rows with their side rows, decoded to Messages after the read.

    Selecting runs inside the read transaction; :meth:`messages` builds the
    ChatMessages afterwards, once per entry key.
    """

    rows: list[sqlite3.Row]
    text: dict[int, sqlite3.Row] = field(default_factory=dict)
    assistants: dict[int, sqlite3.Row] = field(default_factory=dict)
    reasoning: dict[int, sqlite3.Row] = field(default_factory=dict)
    output_files: dict[int, list[sqlite3.Row]] = field(default_factory=dict)
    tool_calls: dict[int, list[sqlite3.Row]] = field(default_factory=dict)
    tool_results: dict[int, sqlite3.Row] = field(default_factory=dict)
    senders: dict[int, sqlite3.Row] = field(default_factory=dict)
    origins: dict[int, sqlite3.Row] = field(default_factory=dict)
    errors: dict[int, sqlite3.Row] = field(default_factory=dict)
    edits: dict[int, sqlite3.Row] = field(default_factory=dict)
    model_fallbacks: dict[int, sqlite3.Row] = field(default_factory=dict)
    goal_events: dict[int, sqlite3.Row] = field(default_factory=dict)
    subagent_messages: dict[int, list[sqlite3.Row]] = field(default_factory=dict)
    widget_states: dict[int, list[sqlite3.Row]] = field(default_factory=dict)
    checkpoints: dict[int, sqlite3.Row] = field(default_factory=dict)
    run_ids: dict[int, str] = field(default_factory=dict)
    run_summaries: dict[int, sqlite3.Row] = field(default_factory=dict)
    change_paths: dict[int, list[sqlite3.Row]] = field(default_factory=dict)
    _decoded: dict[int, ChatMessage] = field(default_factory=dict)

    def messages(self) -> list[ChatMessage]:
        return [self.message(row) for row in self.rows]

    def message(self, row: sqlite3.Row) -> ChatMessage:
        key = int(row["entry_key"])
        decoded = self._decoded.get(key)
        if decoded is None:
            decoded = self._decode(row)
            self._decoded[key] = decoded
        return decoded

    def _decode(self, row: sqlite3.Row) -> ChatMessage:
        from core.chat.messages import ChatMessage

        key = int(row["entry_key"])
        role = str(row["role"])
        try:
            data: JsonObject = {"id": str(row["entry_id"]), "role": role}
            data["timestamp"] = str(row["created_at"])
            if row["subagent_reply_target"] is not None:
                data["subagent_reply_target"] = str(row["subagent_reply_target"])
            if row["skill_package_source"] is not None:
                data["skill_package"] = {
                    "source": str(row["skill_package_source"]),
                    "name": str(row["skill_package_name"]),
                    "location": row["skill_package_location"],
                }
            if row["model"] is not None:
                data["model"] = str(row["model"])
            if row["run_key"] is not None:
                data["run_id"] = self.run_ids[int(row["run_key"])]
            text = self.text.get(key)
            if text is not None:
                if text["blocks_json"] is not None:
                    data["content"] = json.loads(str(text["blocks_json"]))
                elif text["content"] is not None:
                    data["content"] = str(text["content"])
            match role:
                case "assistant":
                    self._decode_assistant(key, data)
                case "tool":
                    self._decode_tool_result(key, data)
                case "user":
                    sender = self.senders.get(key)
                    if sender is not None:
                        data["sender"] = {
                            "id": sender["sender_id"],
                            "display_name": sender["display_name"],
                            "role": sender["sender_role"],
                        }
                    origin = self.origins.get(key)
                    if origin is not None:
                        data["input_origin"] = str(origin["input_origin"])
                case "error":
                    data["error_kind"] = str(self.errors[key]["error_kind"])
                case "history_edit":
                    data["target_message_id"] = str(self.edits[key]["target_entry_id"])
                case "note" if key in self.model_fallbacks:
                    fallback = self.model_fallbacks[key]
                    data["model_fallback"] = {
                        "from_model": str(fallback["from_model"]),
                        "to_model": str(fallback["to_model"]),
                    }
                    if fallback["retry_at"] is not None:
                        data["model_fallback"]["retry_at"] = str(fallback["retry_at"])
                case "note" if key in self.goal_events:
                    goal = self.goal_events[key]
                    data["goal_event"] = {
                        field_name: goal[field_name]
                        for field_name in ("event", "reason", "objective", "turn", "max_turns")
                        if goal[field_name] is not None
                    }
                case "compaction_checkpoint":
                    self._decode_checkpoint(key, data)
                case "run_summary":
                    self._decode_run_summary(key, data)
            subagent_messages = self.subagent_messages.get(key)
            if subagent_messages:
                data["subagent_messages"] = [
                    {
                        field_name: row[field_name]
                        for field_name in _SUBAGENT_MESSAGE_FIELDS
                        if row[field_name] is not None
                    }
                    for row in subagent_messages
                ]
            widget_states = self.widget_states.get(key)
            if widget_states:
                data["widget_states"] = [
                    {
                        "widget_id": str(row["widget_id"]),
                        "title": str(row["title"]),
                        "state": json.loads(row["state_json"]),
                    }
                    for row in widget_states
                ]
            return ChatMessage.from_dict(data)
        except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise SessionStoreCorruptError(f"invalid stored Session entry: {key}") from exc

    def _decode_assistant(self, key: int, data: JsonObject) -> None:
        assistant = self.assistants[key]
        for column in ("phase", "reasoning_scope", "interruption_cause"):
            if assistant[column] is not None:
                data[column] = assistant[column]
        if assistant["interrupted"]:
            data["interrupted"] = True
        reasoning = self.reasoning.get(key)
        if reasoning is not None:
            if reasoning["reasoning"] is not None:
                data["reasoning"] = reasoning["reasoning"]
            if reasoning["summary_json"] is not None:
                data["reasoning_summary"] = json.loads(str(reasoning["summary_json"]))
            if reasoning["meta_json"] is not None:
                data["reasoning_meta"] = json.loads(str(reasoning["meta_json"]))
        timing = _timing_value(
            assistant["reasoning_started_at"],
            assistant["reasoning_completed_at"],
            assistant["reasoning_duration_ms"],
            assistant["reasoning_timing_extra_json"],
        )
        if timing is not None:
            data["reasoning_timing"] = timing
        if assistant["usage_present"]:
            usage = _json_extras(assistant["usage_extra_json"])
            for column in _USAGE_COUNT_COLUMNS:
                if assistant[column] is not None:
                    usage[column] = assistant[column]
            for column in _USAGE_FLAG_COLUMNS:
                if assistant[column] is not None:
                    usage[column] = bool(assistant[column])
            if any(assistant[column] == 1 for column in _USAGE_FLAG_COLUMNS):
                usage[_USAGE_SUMMARY_KEY] = True
            data["usage"] = usage
        if assistant["has_tool_calls"]:
            calls: list[JsonObject] = []
            for call in self.tool_calls.get(key, ()):
                value: JsonObject = {
                    "id": call["call_id"],
                    "name": call["name"],
                    "arguments": json.loads(str(call["arguments_json"])),
                }
                if call["rejection_code"] is not None:
                    value["rejection"] = {
                        "code": call["rejection_code"],
                        "message": call["rejection_message"],
                        "fingerprint": call["rejection_fingerprint"],
                    }
                if call["argument_sequence_index"] is not None:
                    value["argument_sequence_index"] = call["argument_sequence_index"]
                    value["argument_sequence_length"] = call["argument_sequence_length"]
                calls.append(value)
            data["tool_calls"] = calls
        files = self.output_files.get(key)
        if files:
            data["output_files"] = [
                {
                    "path": file["path"],
                    "line_index": file["line_index"],
                    "start_index": file["start_index"],
                    "end_index": file["end_index"],
                }
                for file in files
            ]

    def _decode_tool_result(self, key: int, data: JsonObject) -> None:
        call = self.tool_results[key]
        data["tool_call_id"] = call["call_id"]
        data["name"] = call["name"]
        timing = _timing_value(
            call["started_at"], call["completed_at"], call["duration_ms"], call["timing_extra_json"]
        )
        if timing is not None:
            data["timing"] = timing
        if call["display_json"] is not None:
            data["tool_display"] = json.loads(str(call["display_json"]))
        if call["media_json"] is not None:
            data["tool_media"] = json.loads(str(call["media_json"]))

    def _decode_checkpoint(self, key: int, data: JsonObject) -> None:
        checkpoint = self.checkpoints[key]
        data["compaction_policy"] = checkpoint["policy"]
        data["compaction_strategy"] = checkpoint["strategy"]
        data["projection"] = json.loads(str(checkpoint["projection_json"]))
        if checkpoint["usage_present"]:
            usage = _json_extras(checkpoint["usage_extra_json"])
            for usage_key, column in (
                ("compacted_token_count", "compacted_token_count"),
                ("context_tokens_before", "context_tokens_before"),
                ("context_tokens_after", "context_tokens_after"),
                ("compaction_duration_ms", "duration_ms"),
            ):
                if checkpoint[column] is not None:
                    usage[usage_key] = checkpoint[column]
            data["usage"] = usage

    def _decode_run_summary(self, key: int, data: JsonObject) -> None:
        run = self.run_summaries[key]
        data["run_id"] = run["run_id"]
        data["status"] = run["status"]
        if run["work_id"] is not None:
            data["work_id"] = run["work_id"]
        if run["iteration_count"] is not None:
            data["iteration_count"] = run["iteration_count"]
        if run["completion_reason"] and run["status"] in {"cancelled", "interrupted"}:
            data["completion_reason"] = run["completion_reason"]
        timing = _timing_value(
            run["timing_started_at"],
            run["completed_at"],
            run["duration_ms"],
            run["timing_extra_json"],
        )
        data["timing"] = {} if timing is None else timing
        if run["changed_files"] is not None:
            changes = _json_extras(run["change_stats_extra_json"])
            path_rows = self.change_paths.get(int(run["run_key"]), [])
            changes.update(
                {
                    "files": run["changed_files"],
                    "added": run["lines_added"],
                    "removed": run["lines_removed"],
                    "paths": [str(row["path"]) for row in path_rows],
                }
            )
            # Runs recorded before per-file counts existed have none to report.
            if all(
                row["lines_added"] is not None and row["lines_removed"] is not None
                for row in path_rows
            ):
                changes["file_stats"] = [
                    {
                        "path": str(row["path"]),
                        "added": row["lines_added"],
                        "removed": row["lines_removed"],
                    }
                    for row in path_rows
                ]
            data["change_stats"] = changes


def _json_extras(value: Any) -> JsonObject:
    if value is None:
        return {}
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ValueError("stored extras must be an object")
    return decoded


def _timing_value(
    started_at: Any, completed_at: Any, duration_ms: Any, extra: Any
) -> JsonObject | None:
    if started_at is None and completed_at is None and duration_ms is None and extra is None:
        return None
    timing = _json_extras(extra)
    for timing_key, value in (
        ("started_at", started_at),
        ("completed_at", completed_at),
        ("duration_ms", duration_ms),
    ):
        if value is not None:
            timing[timing_key] = value
    return timing


def _rows_by_key(
    connection: sqlite3.Connection, sql: str, keys: Sequence[int]
) -> dict[int, sqlite3.Row]:
    if not keys:
        return {}
    return {int(row[0]): row for row in connection.execute(sql, (_store_values._key_list(keys),))}


def _grouped_rows(
    connection: sqlite3.Connection, sql: str, keys: Sequence[int]
) -> dict[int, list[sqlite3.Row]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    if keys:
        for row in connection.execute(sql, (_store_values._key_list(keys),)):
            grouped.setdefault(int(row[0]), []).append(row)
    return grouped


_KEYS = "IN (SELECT value FROM json_each(?))"
_SUBAGENT_MESSAGE_FIELDS = (
    "subagent_id",
    "title",
    "content",
    "delivery_id",
    "agent_id",
    "session_id",
    "project_id",
)
_ASSISTANT_COLUMNS = (
    "entry_key, phase, reasoning_scope, has_tool_calls, interrupted, interruption_cause, "
    "usage_present, "
    + ", ".join(_USAGE_COUNT_COLUMNS)
    + ", "
    + ", ".join(_USAGE_FLAG_COLUMNS)
    + ", reasoning_started_at, reasoning_completed_at, reasoning_duration_ms, "
    "usage_extra_json, reasoning_timing_extra_json"
)
_CHECKPOINT_COLUMNS = (
    "c.entry_key, c.policy, c.strategy, c.compacted_token_count, c.context_tokens_before, "
    "c.context_tokens_after, c.duration_ms, c.usage_present, c.usage_extra_json"
)
_RUN_SUMMARY_COLUMNS = (
    "end_entry_key, run_key, run_id, work_id, status, completion_reason, iteration_count, "
    "timing_started_at, completed_at, duration_ms, timing_extra_json, changed_files, "
    "lines_added, lines_removed, change_stats_extra_json"
)


def select_batch(connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> EntryBatch:
    """Fetch the side rows *rows* need, querying only tables their roles use."""
    batch = EntryBatch(list(rows))
    by_role: dict[str, list[int]] = {}
    for row in rows:
        by_role.setdefault(str(row["role"]), []).append(int(row["entry_key"]))
    texted = [int(row["entry_key"]) for row in rows if str(row["role"]) not in _TEXTLESS_ROLES]
    batch.text = _rows_by_key(
        connection,
        f"SELECT entry_key, content, blocks_json FROM entry_text WHERE entry_key {_KEYS}",
        texted,
    )
    assistants = by_role.get("assistant", [])
    batch.assistants = _rows_by_key(
        connection,
        f"SELECT {_ASSISTANT_COLUMNS} FROM assistant_entries WHERE entry_key {_KEYS}",
        assistants,
    )
    batch.reasoning = _rows_by_key(
        connection,
        "SELECT entry_key, reasoning, summary_json, meta_json FROM assistant_reasoning "
        f"WHERE entry_key {_KEYS}",
        assistants,
    )
    batch.output_files = _grouped_rows(
        connection,
        "SELECT entry_key, path, line_index, start_index, end_index FROM assistant_output_files "
        f"WHERE entry_key {_KEYS} ORDER BY entry_key, ordinal",
        assistants,
    )
    batch.tool_calls = _grouped_rows(
        connection,
        "SELECT c.entry_key, c.call_id, c.name, c.rejection_code, c.rejection_fingerprint, "
        "c.argument_sequence_index, c.argument_sequence_length, p.arguments_json, "
        "p.rejection_message FROM tool_calls AS c JOIN tool_call_payloads AS p "
        f"ON p.call_key = c.call_key WHERE c.entry_key {_KEYS} ORDER BY c.entry_key, c.ordinal",
        assistants,
    )
    batch.tool_results = _rows_by_key(
        connection,
        "SELECT c.result_entry_key, c.call_id, c.name, c.started_at, c.completed_at, "
        "c.duration_ms, p.display_json, p.timing_extra_json, p.media_json FROM tool_calls AS c "
        f"JOIN tool_call_payloads AS p ON p.call_key = c.call_key WHERE c.result_entry_key {_KEYS}",
        by_role.get("tool", []),
    )
    batch.senders = _rows_by_key(
        connection,
        "SELECT entry_key, sender_id, display_name, sender_role FROM user_entry_senders "
        f"WHERE entry_key {_KEYS}",
        by_role.get("user", []),
    )
    batch.origins = _rows_by_key(
        connection,
        f"SELECT entry_key, input_origin FROM user_entry_origins WHERE entry_key {_KEYS}",
        by_role.get("user", []),
    )
    batch.errors = _rows_by_key(
        connection,
        f"SELECT entry_key, error_kind FROM error_entries WHERE entry_key {_KEYS}",
        by_role.get("error", []),
    )
    batch.edits = _rows_by_key(
        connection,
        f"SELECT entry_key, target_entry_id FROM history_edit_entries WHERE entry_key {_KEYS}",
        by_role.get("history_edit", []),
    )
    batch.model_fallbacks = _rows_by_key(
        connection,
        f"SELECT entry_key, from_model, to_model, retry_at FROM note_model_fallbacks WHERE entry_key {_KEYS}",
        by_role.get("note", []),
    )
    batch.goal_events = _rows_by_key(
        connection,
        "SELECT entry_key, event, reason, objective, turn, max_turns FROM note_goal_events "
        f"WHERE entry_key {_KEYS}",
        by_role.get("note", []),
    )
    batch.subagent_messages = _grouped_rows(
        connection,
        f"SELECT entry_key, {', '.join(_SUBAGENT_MESSAGE_FIELDS)} FROM note_subagent_messages "
        f"WHERE entry_key {_KEYS} ORDER BY entry_key, ordinal",
        by_role.get("note", []),
    )
    batch.widget_states = _grouped_rows(
        connection,
        "SELECT entry_key, widget_id, title, state_json FROM note_widget_states "
        f"WHERE entry_key {_KEYS} ORDER BY entry_key, ordinal",
        by_role.get("note", []),
    )
    batch.checkpoints = _rows_by_key(
        connection,
        f"SELECT {_CHECKPOINT_COLUMNS}, p.projection_json FROM checkpoint_entries AS c "
        f"JOIN checkpoint_projections AS p ON p.entry_key = c.entry_key WHERE c.entry_key {_KEYS}",
        by_role.get("compaction_checkpoint", []),
    )
    run_keys = sorted({int(row["run_key"]) for row in rows if row["run_key"] is not None})
    if run_keys:
        batch.run_ids = {
            int(row[0]): str(row[1])
            for row in connection.execute(
                f"SELECT run_key, run_id FROM runs WHERE run_key {_KEYS}",
                (_store_values._key_list(run_keys),),
            )
        }
    summaries = by_role.get("run_summary", [])
    batch.run_summaries = _rows_by_key(
        connection,
        f"SELECT {_RUN_SUMMARY_COLUMNS} FROM runs WHERE end_entry_key {_KEYS}",
        summaries,
    )
    summary_runs = [int(row["run_key"]) for row in batch.run_summaries.values()]
    batch.change_paths = _grouped_rows(
        connection,
        "SELECT run_key, path, lines_added, lines_removed FROM run_change_paths "
        f"WHERE run_key {_KEYS} ORDER BY run_key, ordinal",
        summary_runs,
    )
    return batch


def select_entries(
    connection: sqlite3.Connection, where: str, params: Sequence[Any], *, tail: str = ""
) -> EntryBatch:
    """Select entries by one ``e``-aliased predicate, then their side rows."""
    rows = connection.execute(
        f"SELECT {ENTRY_COLUMNS} FROM entries AS e WHERE {where} {tail}", params
    ).fetchall()
    return select_batch(connection, rows)
