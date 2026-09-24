"""Session store benchmarks: durable appends and active-history loads.

Both go through the public ``ChatSessionManager`` / ``ChatSession`` API on a
fresh store in the suite's temporary work directory. Commits run with
``PRAGMA synchronous=FULL``, so ``sessions.append`` is dominated by the fsync
latency of the disk holding that directory (choose it with ``--tmp-dir``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from core.chat.messages import ChatMessage
from core.database import required_journal_mode
from scripts.perf_bench_suite.fixtures import (
    AGENT_ID,
    AGENT_MODEL,
    HISTORY_SHAPES,
    HistoryShape,
    TextFactory,
    loaded_history,
    session_store,
)
from scripts.perf_bench_suite.runner import BenchContext, Benchmark, Prepared

APPEND_MESSAGE_CHARS = 1024


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
)
