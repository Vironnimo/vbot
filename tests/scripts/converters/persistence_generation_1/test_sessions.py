"""Tests for the Generation 1 conversion of ``sessions.db``: Sessions, history and Runs."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from core.chat.messages import COMPACTION_SUMMARY_NOTE_PREFIX
from core.database import open_offline_database
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions._store_schema import session_database_spec
from core.sessions.schema import APPLICATION_ID
from core.sessions.store import SessionStore
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1.sessions import AREA, convert
from tests.scripts.converters.persistence_generation_1.legacy_sessions_support import (
    RELEASED_IDENTITY,
    LegacySessionStore,
    at,
    canonical,
)

MAIN = SessionAddress(project_id=None, agent_id="main", session_id="s1")
_DENIED = '{"ok": false, "error": {"code": "denied", "message": "No access", "retryable": false}}'


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


def _legacy(context: ConversionContext, **options: Any) -> LegacySessionStore:
    return LegacySessionStore(context.source / "sessions.db", **options)


@contextmanager
def _opened(context: ConversionContext) -> Iterator[ChatSessionManager]:
    store = SessionStore(context.staging / "sessions.db", _offline=True)
    manager = ChatSessionManager(context.staging, store=store)
    try:
        yield manager
    finally:
        manager.close()
        store.close()


def _rows(
    context: ConversionContext, sql: str, parameters: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(context.staging / "sessions.db")) as connection:
        return [tuple(row) for row in connection.execute(sql, parameters)]


def _skips(context: ConversionContext) -> list[tuple[str, str]]:
    return [(skip.item, skip.reason) for skip in context.report.skipped if skip.area == AREA]


def _conversation(legacy: LegacySessionStore) -> dict[str, str]:
    """One Session with every kind of history record the old store wrote."""
    key = legacy.session("s1", minute=0, metadata={"title": "Planning"})
    legacy.start_run(key, "run_1", minute=1, work_id="work_1")
    ids = {
        "user": legacy.user(
            key,
            "Please update b.txt, zebra",
            minute=1,
            run_id="run_1",
            sender={"id": "u-1", "display_name": "Ada", "role": "admin"},
        ),
        "assistant": legacy.assistant(
            key,
            "Reading first.",
            minute=2,
            run_id="run_1",
            tool_calls=[
                {"id": "call_read", "name": "read_file", "arguments": {"path": "a.txt"}},
                {"id": "call_write", "name": "write_file", "arguments": {"path": "b.txt"}},
            ],
            reasoning="Look before writing.",
            reasoning_timing={"started_at": at(1.5), "completed_at": at(2), "duration_ms": 30000},
            usage={"input_tokens": 120, "output_tokens": 30, "provider_cost": 0.01},
        ),
        "read": legacy.tool_result(
            key,
            "call_read",
            '{"ok": true, "data": {"text": "hello"}}',
            minute=3,
            display={"title": "Read a.txt"},
        ),
        "write": legacy.tool_result(
            key,
            "call_write",
            _DENIED,
            minute=3,
        ),
        "answer": legacy.assistant(
            key,
            "b.txt is updated.",
            minute=4,
            run_id="run_1",
            output_files=[{"path": "b.txt", "line_index": 0}],
        ),
    }
    ids["summary"] = legacy.finish_run(
        key,
        "run_1",
        minute=5,
        iteration_count=2,
        change_stats={"files": 1, "added": 3, "removed": 1, "paths": ["b.txt"], "tool": "edit"},
    )
    ids["note"] = legacy.note(key, "Remember the deadline", minute=6)
    ids["error"] = legacy.error(key, "provider_error", "The Provider failed", minute=7)
    return ids


def test_a_session_keeps_its_history_side_rows_and_run(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        ids = _conversation(legacy)
        generation = legacy.generation(1)

    convert(context)

    with _opened(context) as manager:
        session = manager.get(MAIN)
        history = session.load_active()
        assert [(message.id, message.role) for message in history] == [
            (ids["user"], "user"),
            (ids["assistant"], "assistant"),
            (ids["read"], "tool"),
            (ids["write"], "tool"),
            (ids["answer"], "assistant"),
            (ids["summary"], "run_summary"),
            (ids["note"], "note"),
            (ids["error"], "error"),
        ]
        user, assistant, read, write, answer, summary, note, error = history
        assert user.content == "Please update b.txt, zebra"
        assert user.timestamp == canonical(1)
        assert user.sender is not None and user.sender.display_name == "Ada"
        assert assistant.reasoning == "Look before writing."
        assert assistant.reasoning_timing == {
            "started_at": canonical(1.5),
            "completed_at": canonical(2),
            "duration_ms": 30000,
        }
        assert assistant.usage == {"input_tokens": 120, "output_tokens": 30, "provider_cost": 0.01}
        assert [(call.id, call.name, call.arguments) for call in assistant.tool_calls or ()] == [
            ("call_read", "read_file", {"path": "a.txt"}),
            ("call_write", "write_file", {"path": "b.txt"}),
        ]
        assert (read.tool_call_id, read.name, read.tool_display) == (
            "call_read",
            "read_file",
            {"title": "Read a.txt"},
        )
        assert read.timing == {
            "started_at": canonical(3),
            "completed_at": canonical(3),
            "duration_ms": 250,
        }
        assert write.content == _DENIED
        # The old line-only reference gets the span of the whole line it named.
        assert [reference.to_dict() for reference in answer.output_files or ()] == [
            {"line_index": 0, "path": "b.txt", "start_index": 0, "end_index": 17}
        ]
        assert (note.content, error.error_kind) == ("Remember the deadline", "provider_error")
        assert session.find_run_summary(run_id="run_1") == summary
        assert (summary.status, summary.work_id, summary.iteration_count) == (
            "completed",
            "work_1",
            2,
        )
        assert summary.timing == {
            "started_at": canonical(1),
            "completed_at": canonical(5),
            "duration_ms": 240000,
        }
        assert summary.change_stats == {
            "tool": "edit",
            "files": 1,
            "added": 3,
            "removed": 1,
            "paths": ["b.txt"],
        }
        assert session.load() == history
        assert manager.get_metadata(MAIN) == {"title": "Planning", "run_kinds": ["user"]}

    assert _rows(
        context,
        "SELECT session_key, generation_id, state, created_at, next_seq, cursor_floor_seq, "
        "last_activity_at, last_entry_id, latest_completion_run_id, latest_completion_status, "
        "latest_completion_at, fork_parent_key, forked_at, fork_point_seq FROM sessions",
    ) == [
        (
            1,
            generation,
            "live",
            canonical(0),
            8,
            0,
            canonical(7),
            ids["error"],
            "run_1",
            "completed",
            canonical(5),
            None,
            None,
            None,
        )
    ]
    assert _rows(
        context,
        "SELECT r.run_id, r.status, r.inherited, r.contributes_to_activity, r.start_seq, "
        "r.timing_started_at, r.changed_files, e.entry_id, e.seq FROM runs AS r "
        "JOIN entries AS e ON e.entry_key = r.end_entry_key",
    ) == [("run_1", "completed", 0, 1, 0, canonical(1), 1, ids["summary"], 5)]
    assert _rows(context, "SELECT path FROM run_change_paths") == [("b.txt",)]
    assert _rows(context, "SELECT call_id, status FROM tool_calls ORDER BY call_key") == [
        ("call_read", "completed"),
        ("call_write", "failed"),
    ]
    assert _rows(context, "SELECT seq FROM entries ORDER BY entry_key") == [
        (seq,) for seq in range(8)
    ]
    assert _rows(context, "PRAGMA foreign_key_check") == []
    assert _rows(
        context, "SELECT path, line_index, start_index, end_index FROM assistant_output_files"
    ) == [("b.txt", 0, 0, 17)]
    counts = context.report.counts[AREA]
    assert {
        key: counts[key] for key in ("sessions", "entries", "runs", "tool_calls", "tool_results")
    } == {
        "sessions": 1,
        "entries": 8,
        "runs": 1,
        "tool_calls": 2,
        "tool_results": 2,
    }
    assert counts["output_file_spans_derived"] == 1
    assert "usage_provenance_derived" not in counts
    assert counts["search_index_healthy"] == 1
    assert _skips(context) == []


def test_edits_supersede_the_replaced_tail_at_the_edit(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        first = legacy.user(key, "first question", minute=1)
        first_answer = legacy.assistant(key, "first answer", minute=2)
        replaced = legacy.user(key, "second question", minute=3)
        legacy.assistant(key, "second answer", minute=4)
        legacy.edit(key, replaced, minute=5)
        rewritten = legacy.user(key, "second question, rewritten", minute=6)
        rewritten_answer = legacy.assistant(key, "better answer", minute=7)

    convert(context)

    with _opened(context) as manager:
        session = manager.get(MAIN)
        assert [message.id for message in session.load_active()] == [
            first,
            first_answer,
            rewritten,
            rewritten_answer,
        ]
        assert len(session.load()) == 7
    assert _rows(context, "SELECT seq, role, superseded_at_seq FROM entries ORDER BY seq") == [
        (0, "user", None),
        (1, "assistant", None),
        (2, "user", 4),
        (3, "assistant", 4),
        (4, "history_edit", 4),
        (5, "user", None),
        (6, "assistant", None),
    ]
    assert _rows(context, "SELECT cursor_floor_seq FROM sessions") == [(5,)]
    assert context.report.counts[AREA]["history_edits"] == 1
    assert _skips(context) == []


def test_inactive_rows_no_edit_explains_are_hidden_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        kept = legacy.user(key, "kept", minute=1)
        hidden = legacy.assistant(key, "cleared without an edit", minute=2)
        legacy.execute("UPDATE messages SET active = 0 WHERE message_id = ?", (hidden,))

    convert(context)

    with _opened(context) as manager:
        assert [message.id for message in manager.get(MAIN).load_active()] == [kept]
    assert _rows(context, "SELECT seq, superseded_at_seq FROM entries ORDER BY seq") == [
        (0, None),
        (1, 1),
    ]
    assert _skips(context) == [
        (
            "session -/main/s1 (gen_0001)",
            "an inactive record no edit explains is hidden at its own seq",
        )
    ]


# Older Compactions placed this note between the summary and the retained tail.
_TAIL_GUIDANCE = (
    "The messages below are the most recent verbatim Session activity retained after this "
    "Compaction checkpoint. They chronologically follow the summary above."
)


def test_checkpoints_get_a_projection_and_a_policy(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        legacy.user(key, "old question", minute=1)
        legacy.assistant(key, "old answer", minute=2)
        tail = legacy.user(key, "recent question", minute=3)
        legacy.assistant(key, "recent answer", minute=4)
        boundary = legacy.checkpoint(key, "Earlier work", minute=5, tail_boundary_id=tail)
        legacy.user(key, "after the checkpoint", minute=6)
        projected = legacy.checkpoint(
            key,
            "Later work",
            minute=7,
            projection=[
                {"id": "p1", "role": "note", "timestamp": at(7), "content": "carried"},
                {"id": "p2", "role": "note", "timestamp": at(7), "content": _TAIL_GUIDANCE},
            ],
            policy="auto",
        )

    convert(context)

    with _opened(context) as manager:
        checkpoints = {
            message.id: message
            for message in manager.get(MAIN).load_active()
            if message.role == "compaction_checkpoint"
        }
    rebuilt = checkpoints[boundary]
    assert [(entry["role"], entry["content"]) for entry in rebuilt.projection or ()] == [
        ("note", f"{COMPACTION_SUMMARY_NOTE_PREFIX}Earlier work"),
        ("user", "recent question"),
        ("assistant", "recent answer"),
    ]
    assert (rebuilt.compaction_policy, rebuilt.compaction_strategy) == (
        "summary_tail",
        "summary_tail",
    )
    assert rebuilt.usage == {"compacted_token_count": 900}
    kept = checkpoints[projected]
    assert kept.projection == [
        {"id": "p1", "role": "note", "timestamp": at(7), "content": "carried"}
    ]
    assert (kept.compaction_policy, kept.compaction_strategy) == ("auto", "auto")
    counts = context.report.counts[AREA]
    assert (counts["checkpoints"], counts["checkpoints_materialized"]) == (2, 1)
    assert counts["tail_guidance_notes_dropped"] == 1
    assert _skips(context) == [
        (
            "session -/main/s1 (gen_0001)",
            f"compaction checkpoint {projected} policy or strategy filled in",
        )
    ]


_WHOLE_TURN_ESTIMATE = {"input_tokens": 40, "output_tokens": 5, "estimated": True}
_FIELD_ESTIMATES = {
    "input_tokens": 40,
    "output_tokens": 5,
    "input_tokens_estimated": True,
    "output_tokens_estimated": True,
    "estimated": True,
}


def test_whole_turn_estimates_get_field_level_provenance(tmp_path: Path) -> None:
    context = _context(tmp_path)
    partial = {
        "input_tokens": 40,
        "output_tokens": 5,
        "input_tokens_estimated": True,
        "estimated": True,
    }
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        whole_turn = legacy.assistant(key, "guessed", minute=1, usage=_WHOLE_TURN_ESTIMATE)
        measured = legacy.assistant(
            key,
            "measured",
            minute=2,
            usage={"input_tokens": 40, "output_tokens": 5, "estimated": False},
        )
        field_level = legacy.assistant(key, "half guessed", minute=3, usage=partial)

    convert(context)

    with _opened(context) as manager:
        usages = {message.id: message.usage for message in manager.get(MAIN).load_active()}
    assert usages == {
        whole_turn: _FIELD_ESTIMATES,
        measured: {"input_tokens": 40, "output_tokens": 5},
        field_level: partial,
    }
    assert _rows(
        context,
        "SELECT input_tokens_estimated, output_tokens_estimated FROM assistant_entries "
        "ORDER BY entry_key",
    ) == [(1, 1), (None, None), (1, None)]
    assert context.report.counts[AREA]["usage_provenance_derived"] == 1
    assert _skips(context) == []


def test_line_only_file_references_get_a_span_or_are_dropped(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        answer = legacy.assistant(
            key,
            "Files:\n  report.md\n\nchart.png table.csv\nSee file:notes.md",
            minute=1,
            output_files=[
                {"path": "report.md", "line_index": 1},
                {"path": "blank.md", "line_index": 2},
                {"path": "chart.png", "line_index": 3},
                {"path": "table.csv", "line_index": 3},
                {"path": "gone.md", "line_index": 9},
                {"path": "notes.md", "line_index": 4, "start_index": 4, "end_index": 17},
            ],
        )
        emptied = legacy.assistant(
            key, "", minute=2, output_files=[{"path": "empty.md", "line_index": 0}]
        )
        label = f"session -/main/s1 ({legacy.generation(key)})"

    convert(context)

    with _opened(context) as manager:
        messages = {message.id: message for message in manager.get(MAIN).load_active()}
    # The old server replaced the whole line body, leading whitespace included.
    assert [reference.to_dict() for reference in messages[answer].output_files or ()] == [
        {"line_index": 1, "path": "report.md", "start_index": 0, "end_index": 11},
        {"line_index": 4, "path": "notes.md", "start_index": 4, "end_index": 17},
    ]
    assert messages[emptied].output_files is None
    assert context.report.counts[AREA]["output_file_spans_derived"] == 1
    dropped = f"assistant {answer} line-only file reference"
    assert _skips(context) == [
        (label, f"{dropped} blank.md dropped: it names a missing or empty line"),
        (label, f"{dropped} chart.png dropped: it shares its line with another reference"),
        (label, f"{dropped} table.csv dropped: it shares its line with another reference"),
        (label, f"{dropped} gone.md dropped: it names a missing or empty line"),
        (
            label,
            f"assistant {emptied} line-only file reference empty.md dropped: "
            "it names a missing or empty line",
        ),
    ]


def test_stored_checkpoint_projections_get_the_current_assistant_shapes(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    projected = {
        "id": "p1",
        "role": "assistant",
        "timestamp": at(2),
        "model": "test-model",
        "content": "Chart:\nchart.png",
    }
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        legacy.user(key, "draw it", minute=1)
        checkpoint = legacy.checkpoint(
            key,
            "Drew a chart",
            minute=3,
            projection=[
                {
                    **projected,
                    "usage": _WHOLE_TURN_ESTIMATE,
                    "output_files": [{"path": "chart.png", "line_index": 1}],
                }
            ],
            policy="auto",
            strategy="auto",
        )

    convert(context)

    with _opened(context) as manager:
        converted = next(
            message for message in manager.get(MAIN).load_active() if message.id == checkpoint
        )
    assert converted.projection == [
        {
            **projected,
            "usage": _FIELD_ESTIMATES,
            "output_files": [
                {"line_index": 1, "path": "chart.png", "start_index": 0, "end_index": 9}
            ],
        }
    ]
    counts = context.report.counts[AREA]
    assert (counts["usage_provenance_derived"], counts["output_file_spans_derived"]) == (1, 1)
    assert _skips(context) == []


def test_a_running_run_settles_like_after_a_crash(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        live = legacy.session("s1")
        legacy.start_run(live, "run_live", minute=1)
        legacy.user(live, "go", minute=1, run_id="run_live")
        legacy.assistant(
            live,
            None,
            minute=2,
            run_id="run_live",
            tool_calls=[{"id": "call_live", "name": "shell", "arguments": {}}],
        )
        old = legacy.session("s2", minute=0)
        legacy.start_run(old, "run_old", minute=1)
        legacy.assistant(
            old,
            None,
            minute=2,
            run_id="run_old",
            tool_calls=[{"id": "call_old", "name": "shell", "arguments": {}}],
        )
        legacy.archive(old, minute=9)
        old_label = f"session -/main/s2 ({legacy.generation(old)})"

    convert(context)

    assert _rows(
        context, "SELECT run_id, status, completed_at, completion_reason FROM runs ORDER BY run_key"
    ) == [
        ("run_live", "running", None, None),
        ("run_old", "interrupted", canonical(9), "process_restart"),
    ]
    assert _rows(
        context, "SELECT call_id, status, completed_at FROM tool_calls ORDER BY call_key"
    ) == [
        ("call_live", "pending", None),
        ("call_old", "interrupted", canonical(9)),
    ]
    with _opened(context) as manager:
        manager.recover_interrupted_runs()
        summary = manager.get(MAIN).find_run_summary(run_id="run_live")
        assert summary is not None and summary.status == "interrupted"
    assert _rows(
        context, "SELECT status, completion_reason FROM runs WHERE run_id = 'run_live'"
    ) == [("interrupted", "process_restart")]
    assert _rows(context, "SELECT status FROM tool_calls WHERE call_id = 'call_live'") == [
        ("interrupted",)
    ]
    assert context.report.counts[AREA]["runs_running"] == 1
    assert _skips(context) == [
        (old_label, "Run run_old was still running in an archived Session; it ends interrupted"),
        (old_label, "1 Tool calls without a result end interrupted or cancelled"),
    ]


def test_metadata_moves_into_columns_and_relations(tmp_path: Path) -> None:
    context = _context(tmp_path)
    affinity = "ab" * 16
    with _legacy(context) as legacy:
        legacy.session(
            "s1",
            metadata={
                "title": "Title",
                "auto_title": "Auto",
                "auto_title_initialized": True,
                "source_channel_id": "telegram-main",
                "platform": "telegram",
                "platform_conv_id": "42",
                "compaction_policy": {"threshold": 0.8},
                "run_kinds": ["user", "cron", "retired_kind"],
                "seen_skills": ["writing", "coding", "writing"],
                "prompt_cache_affinity_id": affinity,
                "pinned_system_context": {"text": "pinned"},
                "active_session_id": "s0",
                "conversation_kind": "group",
                "participants": ["u-1", "u-2"],
                "last_reply_target": {"chat_id": 42},
                "custom": {"kept": True},
            },
        )

    convert(context)

    with _opened(context) as manager:
        assert manager.get_metadata(MAIN) == {
            "title": "Title",
            "auto_title": "Auto",
            "auto_title_initialized": True,
            "source_channel_id": "telegram-main",
            "platform": "telegram",
            "platform_conv_id": "42",
            "compaction_policy": {"threshold": 0.8},
            "run_kinds": ["cron", "user"],
            "last_reply_target": {"chat_id": 42},
            "custom": {"kept": True},
        }
        assert manager.seen_skills(MAIN) == frozenset({"coding", "writing"})
        assert manager.prompt_cache_affinity_id(MAIN) == affinity
        assert manager.prompt_pin(MAIN, "pinned_system_context") == {"text": "pinned"}
    assert _rows(context, "SELECT run_kind FROM session_run_kinds ORDER BY run_kind") == [
        ("cron",),
        ("user",),
    ]
    counts = context.report.counts[AREA]
    assert {
        key: counts[key]
        for key in (
            "retired_metadata_keys",
            "seen_skill_sets",
            "prompt_pins",
            "prompt_cache_affinities",
        )
    } == {
        "retired_metadata_keys": 3,
        "seen_skill_sets": 1,
        "prompt_pins": 1,
        "prompt_cache_affinities": 1,
    }
    assert _skips(context) == [
        (
            "session -/main/s1 (gen_0001)",
            "metadata run_kinds entries dropped: unknown Run kinds ['retired_kind']",
        )
    ]


def test_invalid_metadata_values_are_dropped_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        legacy.session(
            "s1",
            metadata={
                "auto_title_initialized": "yes",
                "prompt_cache_affinity_id": "not-an-affinity",
                "pinned_system_context": "not an object",
                "subagent_parent": {"agent_id": "boss", "tool_call_index": -1},
                "seen_skills": "coding",
            },
        )

    convert(context)

    with _opened(context) as manager:
        metadata = manager.get_metadata(MAIN)
        assert metadata["subagent_parent"]["agent_id"] == "boss"
        assert metadata["subagent_parent"]["tool_call_index"] is None
        assert "auto_title_initialized" not in metadata
        assert manager.seen_skills(MAIN) is None
        assert manager.prompt_pin(MAIN, "pinned_system_context") is None
    reasons = sorted(reason for _item, reason in _skips(context))
    assert reasons == [
        "metadata auto_title_initialized dropped: not a boolean",
        "metadata pinned_system_context dropped: a prompt pin must be a JSON object",
        "metadata prompt_cache_affinity_id dropped: not a prompt-cache affinity id",
        "metadata seen_skills dropped: not a list",
        "metadata subagent_parent.tool_call_index dropped: not a supported Sub-Agent parent field",
    ]


def test_owner_managed_rows_keep_their_order(tmp_path: Path) -> None:
    context = _context(tmp_path)
    group = SessionAddress(project_id=None, agent_id="main", session_id="member")
    with _legacy(context) as legacy:
        key = legacy.session("member")
        legacy.bind(key, owner="swarm", group="g1", participant="p1", config={"model": "m"})
        legacy.group_title(owner="swarm", group="g1", title="Research group")
        legacy.receipt(key, owner="swarm", receipt_id="rcpt_1", carrier_sequence=0)
        for run_id, record_key in (("run_b", 9), ("run_a", 4)):
            legacy.start_run(key, run_id, minute=1)
            legacy.execution_owner(
                key, run_id, record_key=record_key, owner="swarm", group="g1", participant="p1"
            )
            legacy.finish_run(key, run_id, minute=2)
        legacy.execution_owner(
            key, "run_gone", record_key=12, owner="swarm", group="g1", participant="p1"
        )
        stale = legacy.session("stale")
        legacy.receipt(stale, owner="swarm", receipt_id="rcpt_stale", carrier_sequence=0)
        legacy.execute(
            "UPDATE session_delivery_receipts SET generation_id = 'gone' "
            "WHERE receipt_id = 'rcpt_stale'"
        )

    convert(context)

    with _opened(context) as manager:
        binding = manager.temporary_binding(group)
        assert binding is not None
        assert (binding.owner_name, binding.group_id, binding.participant_id, binding.config) == (
            "swarm",
            "g1",
            "p1",
            {"model": "m"},
        )
        owned = manager.owned_runs(owner_name="swarm", group_id="g1")
        assert [(record.record_key, record.run_id, record.terminal_status) for record in owned] == [
            (4, "run_a", "completed"),
            (9, "run_b", "completed"),
        ]
    assert _rows(context, "SELECT title FROM temporary_group_titles") == [("Research group",)]
    assert _rows(context, "SELECT receipt_id, carrier_sequence FROM session_delivery_receipts") == [
        ("rcpt_1", 0)
    ]
    counts = context.report.counts[AREA]
    assert {
        key: counts[key]
        for key in (
            "temporary_session_bindings",
            "temporary_group_titles",
            "session_delivery_receipts",
            "run_execution_owners",
        )
    } == {
        "temporary_session_bindings": 1,
        "temporary_group_titles": 1,
        "session_delivery_receipts": 1,
        "run_execution_owners": 2,
    }
    assert sorted(_skips(context)) == [
        ("Run execution owner swarm/g1 of Run run_gone", "dropped: its Run does not exist"),
        ("delivery receipt swarm/rcpt_stale", "dropped: its Session generation no longer exists"),
    ]


def test_search_finds_converted_messages(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        ids = _conversation(legacy)

    convert(context)

    with _opened(context) as manager:
        assert manager.fts_health().state == "healthy"
        result = manager.search_messages("zebra", project_id=None, agent_id="main")
        assert [(hit.address, hit.message_id) for hit in result.hits] == [(MAIN, ids["user"])]
        assert result.method == "fts"


def test_timestamps_become_canonical_and_unreadable_ones_are_replaced(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        key = legacy.session("s1", minute=0)
        first = legacy.user(key, "first", minute=1)
        naive = legacy.user(key, "naive", minute=2)
        broken = legacy.user(key, "broken", minute=3)
        legacy.execute("UPDATE sessions SET created_at = '2026-03-01T12:00:00+02:00'")
        legacy.execute(
            "UPDATE messages SET timestamp = '2026-03-01T10:02:00' WHERE message_id = ?", (naive,)
        )
        legacy.execute(
            "UPDATE messages SET timestamp = 'yesterday' WHERE message_id = ?", (broken,)
        )

    convert(context)

    with _opened(context) as manager:
        history = manager.get(MAIN).load_active()
    assert [(message.id, message.timestamp) for message in history] == [
        (first, canonical(1)),
        (naive, canonical(2)),
        (broken, canonical(2)),
    ]
    assert _rows(context, "SELECT created_at FROM sessions") == [(canonical(0),)]
    assert {reason for _item, reason in _skips(context)} == {
        f"user {naive} time '2026-03-01T10:02:00' read as UTC",
        f"user {broken} time 'yesterday' is not an ISO 8601 timestamp; "
        "the nearest known time is used",
    }


def test_continuations_are_dropped_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        key = legacy.session("s1")
        legacy.start_run(key, "run_1", minute=1)
        checkpoint = legacy.user(key, "continue me", minute=1, run_id="run_1")
        legacy.finish_run(key, "run_1", minute=2, status="interrupted")
        legacy.continuation(key, checkpoint_id=checkpoint, run_id="run_1")

    convert(context)

    assert _rows(context, "SELECT COUNT(*) FROM continuations") == [(0,)]
    assert context.report.counts[AREA]["continuations_dropped"] == 1
    assert _skips(context) == [
        (
            "session -/main/s1 (gen_0001)",
            "Continuation dropped: Generation 1 does not resume old Continuations",
        )
    ]


@pytest.mark.parametrize(
    "identity",
    [(0, 0), (0, 1), RELEASED_IDENTITY, (0x56424F54, 0), (APPLICATION_ID, 1)],
    ids=["unmarked", "unmarked-v1", "released", "released-v0", "interim"],
)
def test_every_identity_of_the_old_table_shape_converts(
    tmp_path: Path, identity: tuple[int, int]
) -> None:
    context = _context(tmp_path)
    with _legacy(context, identity=identity) as legacy:
        legacy.user(legacy.session("s1"), "hello", minute=1)

    convert(context)

    with _opened(context) as manager:
        assert [message.content for message in manager.get(MAIN).load_active()] == ["hello"]


def test_a_foreign_identity_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _legacy(context, identity=(0x1234, 1)).close()

    with pytest.raises(
        ConversionError,
        match=r"is not a pre-Generation-1 sessions database "
        r"\(application_id=4660, user_version=1\)$",
    ):
        convert(context)
    assert not (context.staging / "sessions.db").exists()


def test_a_database_without_the_old_history_tables_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with closing(sqlite3.connect(context.source / "sessions.db")) as connection:
        connection.execute("CREATE TABLE notes (text TEXT)")

    with pytest.raises(
        ConversionError,
        match=r"is not a pre-Generation-1 sessions database \(application_id=0, user_version=0\)$",
    ):
        convert(context)
    assert not (context.staging / "sessions.db").exists()


def test_an_already_converted_source_is_left_alone(tmp_path: Path) -> None:
    context = _context(tmp_path)
    source = context.source / "sessions.db"
    open_offline_database(session_database_spec(source)).close()
    Path(f"{source}-wal").write_bytes(b"")

    convert(context)

    assert context.report.counts[AREA] == {"already_current": 1}
    assert context.report.skipped == []
    assert context.retired == []
    assert not (context.staging / "sessions.db").exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("DROP TABLE history_edits", "table history_edits is missing"),
        (
            "ALTER TABLE runs DROP COLUMN change_stats_extra_json",
            "table runs lacks change_stats_extra_json",
        ),
    ],
)
def test_an_older_table_shape_is_refused(tmp_path: Path, change: str, message: str) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        legacy.execute(change)

    with pytest.raises(ConversionError, match=f"{message}. Start the latest vBot release"):
        convert(context)


def test_a_missing_source_stages_nothing(tmp_path: Path) -> None:
    context = _context(tmp_path)

    convert(context)

    assert context.report.counts[AREA] == {"source_missing": 1}
    assert not (context.staging / "sessions.db").exists()


def test_a_repeated_run_replaces_its_staged_database(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        legacy.user(legacy.session("s1"), "hello", minute=1)
    convert(context)
    staged = context.staging / "sessions.db"
    Path(f"{staged}-wal").write_bytes(b"left over")

    rerun = ConversionContext(source=context.source, staging=context.staging)
    convert(rerun)

    assert not Path(f"{staged}-wal").exists()
    assert rerun.report.counts[AREA]["entries"] == 1
    with _opened(context) as manager:
        assert [message.content for message in manager.get(MAIN).load_active()] == ["hello"]


def test_the_source_stays_untouched(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context, journal_mode="wal") as legacy:
        _conversation(legacy)
    source = context.source / "sessions.db"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    listing = sorted(path.name for path in context.source.iterdir())

    convert(context)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert sorted(path.name for path in context.source.iterdir()) == listing
    assert context.retired == []


def test_leftover_source_journal_files_are_retired_unread(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with _legacy(context) as legacy:
        legacy.user(legacy.session("s1"), "hello", minute=1)
    for suffix in ("-wal", "-shm"):
        (context.source / f"sessions.db{suffix}").write_bytes(b"")

    convert(context)

    assert context.retired == [PurePosixPath("sessions.db-wal"), PurePosixPath("sessions.db-shm")]
    assert (context.source / "sessions.db-shm").read_bytes() == b""
    assert not (context.staging / "sessions.db-wal").exists()


def test_a_pending_source_journal_is_read_and_retired(tmp_path: Path) -> None:
    context = _context(tmp_path)
    writer = tmp_path / "writer" / "sessions.db"
    with LegacySessionStore(writer, journal_mode="wal") as legacy:
        legacy.execute("PRAGMA wal_autocheckpoint = 0")
        legacy.user(legacy.session("s1"), "only in the journal", minute=1)
        # Copied while the writer still holds the journal, as a crash leaves it.
        for suffix in ("", "-wal"):
            shutil.copyfile(f"{writer}{suffix}", context.source / f"sessions.db{suffix}")

    convert(context)

    with _opened(context) as manager:
        assert [message.content for message in manager.get(MAIN).load_active()] == [
            "only in the journal"
        ]
    retired = set(context.retired)
    assert PurePosixPath("sessions.db-wal") in retired
    assert retired <= {PurePosixPath("sessions.db-wal"), PurePosixPath("sessions.db-shm")}
