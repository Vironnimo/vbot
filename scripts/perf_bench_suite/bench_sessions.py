"""Session store benchmarks: appends, history loads and pages, search, stream flushes.

All go through the public ``ChatSessionManager`` / ``ChatSession`` API on a
fresh store in the suite's temporary work directory. Commits run with
``PRAGMA synchronous=FULL``, so ``sessions.append`` and
``sessions.stream_flush`` are dominated by the fsync latency of the disk
holding that directory (choose it with ``--tmp-dir``) plus the pages each
commit writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from core.chat.messages import ChatMessage
from core.database import required_journal_mode
from core.tools._bash_results import BACKGROUND_STATUS_NOTE_MARKER, BACKGROUND_STATUS_TOOL_NAMES
from scripts.perf_bench_suite.fixtures import (
    AGENT_ID,
    AGENT_MODEL,
    HISTORY_SHAPES,
    HistoryShape,
    TextFactory,
    history_store,
    loaded_history,
    session_store,
)
from scripts.perf_bench_suite.runner import BenchContext, Benchmark, Prepared

APPEND_MESSAGE_CHARS = 1024
HISTORY_INITIAL_LIMIT = 100
HISTORY_OLDER_LIMIT = 50
HISTORY_OLDER_HOPS = 8
SEARCH_QUERY = "stream provider"
SEARCH_LIMIT = 21
STREAM_STEP_CHARS = 256 * 1024
STREAM_DELTA_CHARS = 512


def _append_setup(context: BenchContext) -> Prepared:
    store = session_store(context, "append-store")
    writer = store.create(AGENT_ID).start_run("run-append")
    content = TextFactory(3).words(APPEND_MESSAGE_CHARS)

    def append_one() -> None:
        writer.append(ChatMessage.assistant(model=AGENT_MODEL, content=content))

    return Prepared(
        append_one,
        params={
            "message_bytes": len(content.encode("utf-8")),
            "journal_mode": required_journal_mode(sqlite3.sqlite_version_info),
            "synchronous": "FULL",
        },
    )


def _load_active_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        history = loaded_history(context, shape)
        session = history.session

        def load() -> None:
            session.load_active()

        return Prepared(
            load,
            params={"messages": len(history.messages), "content_chars": history.content_chars},
            items=len(history.messages),
            item_unit="msg",
        )

    return setup


def _history_page_arguments(before: str | None) -> dict[str, Any]:
    """The arguments ``chat.history`` passes for a first or an older page."""
    return {
        "limit": HISTORY_INITIAL_LIMIT if before is None else HISTORY_OLDER_LIMIT,
        "before": before,
        "excluded_roles": ("note", "history_edit"),
        "complete_run_segment": True,
        "background_tool_names": BACKGROUND_STATUS_TOOL_NAMES if before is None else (),
        "background_note_marker": BACKGROUND_STATUS_NOTE_MARKER if before is None else None,
        "skip_unchanged": True,
    }


def _history_first_page_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        history = loaded_history(context, shape)
        session = history.session
        arguments = _history_page_arguments(None)

        def read() -> None:
            session.read_chat_history_snapshot(**arguments)

        return Prepared(
            read, params={"messages": len(history.messages), "limit": HISTORY_INITIAL_LIMIT}
        )

    return setup


def _history_older_page_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    """Page ``HISTORY_OLDER_HOPS`` pages back first, then time one older-page read."""

    def setup(context: BenchContext) -> Prepared:
        history = loaded_history(context, shape)
        session = history.session
        cursor = session.read_chat_history_snapshot(
            **_history_page_arguments(None)
        ).page.before_cursor
        for _ in range(HISTORY_OLDER_HOPS):
            if cursor is None:
                break
            older = session.read_chat_history_snapshot(**_history_page_arguments(cursor))
            cursor = older.page.before_cursor or cursor
        if cursor is None:
            raise RuntimeError("the synthetic history has no older page")
        arguments = _history_page_arguments(cursor)

        def read() -> None:
            session.read_chat_history_snapshot(**arguments)

        return Prepared(
            read, params={"messages": len(history.messages), "limit": HISTORY_OLDER_LIMIT}
        )

    return setup


def _search_setup(*, use_fts: bool) -> Callable[[BenchContext], Prepared]:
    """Search every synthetic history of the shared store the way Recall reads a page."""

    def setup(context: BenchContext) -> Prepared:
        messages = sum(len(loaded_history(context, shape).messages) for shape in HISTORY_SHAPES)
        manager = history_store(context)

        def search() -> None:
            manager.search_messages(
                SEARCH_QUERY,
                project_id=None,
                agent_id=AGENT_ID,
                match_mode="all_terms",
                order="relevance" if use_fts else "newest",
                limit=SEARCH_LIMIT,
                use_fts=use_fts,
            )

        return Prepared(
            search, params={"messages": messages, "limit": SEARCH_LIMIT, "use_fts": use_fts}
        )

    return setup


def _stream_record(run_id: str, content: str) -> dict[str, Any]:
    return {
        "version": 1,
        "type": "stream_delta",
        "run_id": run_id,
        "step": 1,
        "reasoning_delta": "",
        "content_delta": content,
        "timestamp": "2026-01-05T09:00:01Z",
    }


def _stream_flush_setup(context: BenchContext) -> Prepared:
    """Flush one Continuation stream delta into a step that already holds a long text."""
    session = session_store(context, "stream-store").create(AGENT_ID)
    run_id = "run-stream"
    # Continuation records belong to a Run the Session admitted.
    session.start_run(run_id)
    text = TextFactory(5)
    session.append_continuation_records(
        [
            {
                "version": 1,
                "type": "run_started",
                "run_id": run_id,
                "checkpoint_id": "checkpoint-stream",
                "origin_run_id": run_id,
                "timestamp": "2026-01-05T09:00:00Z",
            },
            _stream_record(run_id, text.words(STREAM_STEP_CHARS)),
        ]
    )
    record = _stream_record(run_id, text.words(STREAM_DELTA_CHARS))

    def flush() -> None:
        session.append_continuation_record(record)

    return Prepared(
        flush,
        params={
            "initial_step_chars": STREAM_STEP_CHARS,
            "delta_chars": STREAM_DELTA_CHARS,
            "synchronous": "FULL",
        },
    )


BENCHMARKS = (
    Benchmark(
        name="sessions.append[1kb]",
        description=(
            "ChatSession.append of one ~1 KB assistant message to a running Run "
            "(one SQLite transaction committed with synchronous=FULL)."
        ),
        setup=_append_setup,
    ),
    *(
        Benchmark(
            name=f"sessions.load_active[{shape.label}]",
            description=(
                f"ChatSession.load_active over a persisted ~{shape.messages}-message, "
                f"~{shape.tokens // 1000}k-token history."
            ),
            setup=_load_active_setup(shape),
        )
        for shape in HISTORY_SHAPES
    ),
    *(
        Benchmark(
            name=f"sessions.history_page[{shape.label}]",
            description=(
                f"First chat.history page ({HISTORY_INITIAL_LIMIT} records, complete Run "
                f"segment, background candidates) of a ~{shape.messages}-message history."
            ),
            setup=_history_first_page_setup(shape),
        )
        for shape in HISTORY_SHAPES
    ),
    Benchmark(
        name=f"sessions.history_older[{HISTORY_SHAPES[-1].label}]",
        description=(
            f"One older chat.history page ({HISTORY_OLDER_LIMIT} records) "
            f"{HISTORY_OLDER_HOPS} pages back in a ~{HISTORY_SHAPES[-1].messages}-message history."
        ),
        setup=_history_older_page_setup(HISTORY_SHAPES[-1]),
    ),
    Benchmark(
        name="sessions.search_fts",
        description=(
            f"search_messages by relevance through FTS for two common terms "
            f"({SEARCH_LIMIT} hits) over every synthetic history."
        ),
        setup=_search_setup(use_fts=True),
    ),
    Benchmark(
        name="sessions.search_scan",
        description=(
            f"search_messages newest-first without FTS for two common terms "
            f"({SEARCH_LIMIT} hits) over every synthetic history."
        ),
        setup=_search_setup(use_fts=False),
    ),
    Benchmark(
        name="sessions.stream_flush",
        description=(
            f"One ~{STREAM_DELTA_CHARS}-char Continuation stream delta flushed into a step "
            f"already holding ~{STREAM_STEP_CHARS // 1024} KB (one committed transaction)."
        ),
        setup=_stream_flush_setup,
    ),
)
