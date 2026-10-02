"""The aggregate tier: Run rows and hourly usage and Tool cubes maintained at reconcile."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.database import DatabaseUnavailableError
from core.models.pricing import TokenPricing
from core.runs import Run, RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress
from core.statistics._projection import datetime_instant
from core.statistics.index import IndexView, StatisticsIndex, StatisticsScope
from core.tools import tool_failure, tool_success
from core.usage import UsageRecorder
from tests.core.sessions.history_fixtures import seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    _admit,
    _assistant,
    _call,
    _compaction,
    _run_summary,
    _tool,
)

Row = dict[str, Any]
Rows = dict[str, list[Row]]
Prices = dict[str, TokenPricing | None]

_PARTICIPANT = SessionAddress(None, "temporary", "participant")
# Each aggregate table's unit key and the address columns that replace it.
_SESSION_ADDRESS = ("session_key", "stat_sessions", ("project_id", "agent_id", "session_id"))
_ADDRESSES = {
    **dict.fromkeys(
        (
            "agg_runs",
            "agg_run_models",
            "agg_tools",
            "agg_tool_latency",
            "agg_records",
            "agg_cache",
            "agg_cache_breaks",
        ),
        _SESSION_ADDRESS,
    ),
    "agg_usage": (
        "unit_key",
        "stat_usage_units",
        ("project_id", "agent_id", "session_id", "owner_name", "group_id"),
    ),
}


def _at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def _scopes(manager: ChatSessionManager) -> tuple[StatisticsScope, ...]:
    """The ``main`` Agent's Sessions and every Extension-owned Session, as reports list them."""
    return (
        StatisticsScope(None, "main", "main", tuple(manager.list_summaries("main"))),
        *(
            StatisticsScope(
                owned.address.project_id,
                owned.address.agent_id,
                f"extension:{owned.owner_name}",
                (owned.summary,),
                owner_name=owned.owner_name,
            )
            for owned in manager.list_owned_session_summaries()
        ),
    )


def _dump(view: IndexView) -> Rows:
    """Every aggregate row by column, its unit key replaced by the unit's address."""
    connection = view.connection
    connection.row_factory = sqlite3.Row
    rows: Rows = {}
    for table, (key, units, address) in _ADDRESSES.items():
        columns = [
            f"a.{row[1]}"
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row[1] != key
        ]
        selected = connection.execute(
            f"SELECT {', '.join(f'u.{column}' for column in address)}, {', '.join(columns)} "
            f"FROM {table} a LEFT JOIN {units} u ON u.session_key = a.{key}"
        )
        rows[table] = sorted(
            (
                {"address": tuple(row[: len(address)])}
                | {column: row[column] for column in row.keys()[len(address) :]}
                for row in selected
            ),
            key=repr,
        )
    return rows


def _rollups(
    index: StatisticsIndex, manager: ChatSessionManager, ledger: UsageRecorder, prices: Prices
) -> Rows:
    return index.read(
        manager, _scopes(manager), _dump, usage_recorder=ledger, pricing_lookup=prices.get
    )


def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
    raise DatabaseUnavailableError("statistics: the index directory is not writable")


def _runs(rows: Rows) -> dict[tuple[str, str], Row]:
    return {(row["address"][2], row["run_id"]): row for row in rows["agg_runs"]}


def _move_call(ledger: UsageRecorder, call_id: str, address: SessionAddress, run_id: str) -> None:
    """Re-attribute a recorded call under a new ledger revision."""

    def move(connection: sqlite3.Connection) -> None:
        revision = connection.execute(
            "UPDATE usage_revision SET revision = revision + 1 RETURNING revision"
        ).fetchone()[0]
        connection.execute(
            "UPDATE usage_calls SET agent_id = ?, session_id = ?, run_id = ?, revision = ? "
            "WHERE id = ?",
            (address.agent_id, address.session_id, run_id, revision, call_id),
        )

    ledger.database.write(move)


async def _participant_run(manager: ChatSessionManager, at: datetime) -> None:
    """Run ``owned`` for the ``swarm`` Extension in its participant Session."""
    binding = manager.create_bound_temporary_session(
        _PARTICIPANT, owner_name="swarm", group_id="group", participant_id="peer", config={}
    )
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "epoch")
    await manager.start_run(
        Run(run_id="owned", agent_id="temporary", session_id="participant", execution_owner=owner)
    )
    manager.get(_PARTICIPANT).for_run("owned").append(
        _assistant(model="chat/m", at=at, usage={"input_tokens": 9, "output_tokens": 1})
    )


@pytest.mark.asyncio
async def test_incremental_maintenance_equals_a_full_rebuild(
    tmp_path: Path,
    manager: ChatSessionManager,
    index: StatisticsIndex,
    ledger: UsageRecorder,
) -> None:
    prices: Prices = {"chat/m": TokenPricing.from_cost({"input": 1, "output": 2}, source="t")}
    checks = 0

    def check() -> Rows:
        """The incremental index matches a rebuilt file index and a transient one."""
        nonlocal checks
        checks += 1
        incremental = _rollups(index, manager, ledger, prices)
        rebuilt = StatisticsIndex(tmp_path / f"rebuilt-{checks}")
        transient = StatisticsIndex(tmp_path / f"transient-{checks}")
        transient._read_file = _unavailable  # type: ignore[method-assign]
        try:
            assert incremental == _rollups(rebuilt, manager, ledger, prices)
            assert incremental == _rollups(transient, manager, ledger, prices)
        finally:
            rebuilt.close()
            transient.close()
        return incremental

    alpha = manager.create("main", session_id="alpha")
    question = ChatMessage.user("question", timestamp=_at(1))
    seed_history(
        alpha,
        [
            question,
            _assistant(
                model="chat/m",
                at=_at(2),
                content=None,
                usage={"input_tokens": 4000, "output_tokens": 10, "cache_read_tokens": 0},
            ),
            _tool(name="read", at=_at(3), envelope=tool_success({}), duration_ms=250),
            # A cache read far below the previous prompt: a suspected cache break.
            _assistant(
                model="chat/m",
                at=_at(4),
                usage={"input_tokens": 4100, "output_tokens": 5, "cache_read_tokens": 100},
            ),
            _run_summary(status="completed", at=_at(5), duration_ms=4000, run_id="a1"),
        ],
    )
    usage = {"input_tokens": 100, "output_tokens": 10}
    await _call(ledger, usage, address=alpha.address, run_id="a1")
    await _call(ledger, {"input_tokens": 7, "output_tokens": 1}, address=alpha.address)
    await _call(ledger, {"input_tokens": 3}, kind="text_embedding", model="embed/m")
    beta = manager.create("main", session_id="beta")
    manager.set_metadata(beta.address, {"is_subagent_session": True})
    seed_history(
        beta,
        [
            _assistant(model="chat/m", at=_at(10), usage={"input_tokens": 40, "output_tokens": 4}),
            _tool(name="write", at=_at(11), envelope=tool_failure("denied", "no"), duration_ms=9),
            _run_summary(status="failed", at=_at(12), duration_ms=2000, run_id="b1"),
        ],
    )
    await _participant_run(manager, _at(20))
    await _call(
        ledger,
        {"input_tokens": 9, "output_tokens": 1, "reported_cost_usd": 0.5},
        address=_PARTICIPANT,
        run_id="owned",
        owner_name="swarm",
    )
    check()

    # Appends to a Session, a new Session whose requests were recorded before
    # it was indexed, and a call that starts now and finishes later.
    _admit(manager, alpha.address, "a2", "cron")
    seed_history(
        manager.get(alpha.address),
        [
            _assistant(model="chat/m", at=_at(30), usage={"input_tokens": 50, "output_tokens": 2}),
            _tool(name="read", at=_at(31), envelope=tool_success({}), duration_ms=40_000),
            _compaction(at=_at(32), before=900, after=100),
            _run_summary(status="completed", at=_at(33), duration_ms=3000, run_id="a2"),
        ],
    )
    gamma = SessionAddress(None, "main", "gamma")
    await _call(ledger, {"input_tokens": 5, "output_tokens": 5}, address=gamma, run_id="g1")
    check()
    _admit(manager, gamma, "g1", "channel")
    seed_history(
        manager.get(gamma),
        [_run_summary(status="completed", at=_at(40), duration_ms=10, run_id="g1")],
    )
    pending = await ledger.start(
        model="chat/m", kind="chat", agent_id="main", session_id="alpha", run_id="a2"
    )
    check()
    await ledger.finish(pending, {"input_tokens": 50, "output_tokens": 2, "reported_cost_usd": 0.1})
    check()

    # A call that moves to another Session and Run.
    _move_call(ledger, pending, beta.address, "b1")
    check()

    # A history edit rebuilds a Session's facts from its start.
    manager.get(alpha.address).apply_edit(
        question.id, [ChatMessage.user("edited", timestamp=_at(1))]
    )
    check()

    # Changed catalog pricing reprices retrospective calls.
    prices["chat/m"] = TokenPricing.from_cost({"input": 3, "output": 5}, source="t")
    check()

    # A changed Session flag and a deleted Session.
    manager.set_metadata(alpha.address, {"is_subagent_session": True})
    check()
    manager.delete(beta.address)
    final = check()

    assert all(final.values())
    assert {row["origin"] for row in final["agg_usage"]} == {
        "background",
        "channel",
        "extension",
        "subagent",
        "user",
    }


@pytest.mark.asyncio
async def test_every_run_tool_call_and_request_gets_one_origin(
    manager: ChatSessionManager, index: StatisticsIndex, ledger: UsageRecorder
) -> None:
    kinds = {
        "user": "user",
        "cron": "automation",
        "calendar": "automation",
        "channel": "channel",
        "reflection": "reflection",
        "memory_reflection": "reflection",
        "skill_reflection": "reflection",
        "system": "system",
        "subagent": "subagent",
    }
    plain = manager.create("main", session_id="plain")
    for offset, kind in enumerate(kinds):
        _admit(manager, plain.address, kind, kind)
        seed_history(
            manager.get(plain.address),
            [
                _tool(name=kind, at=_at(offset), envelope=tool_success({}), duration_ms=1),
                _run_summary(status="completed", at=_at(offset), duration_ms=1, run_id=kind),
            ],
        )
        await _call(
            ledger, {"input_tokens": 1}, model=f"run/{kind}", address=plain.address, run_id=kind
        )
    # Without a Run, a Tool call or a chat or Compaction request takes its
    # Session's origin; any other request is background work unless an
    # Extension or Sub-Agent Session owns it.
    declaring = ChatMessage.assistant(
        model="fixture", content=None, tool_calls=[ToolCall(id="loose", name="loose")]
    )
    plain.append(declaring)
    plain.assistant_message_id = declaring.id
    plain.append(
        ChatMessage.tool(tool_call_id="loose", name="loose", content=json.dumps(tool_success({})))
    )
    for kind in ("chat", "compaction", "session_title"):
        await _call(
            ledger, {"input_tokens": 1}, model=f"plain/{kind}", kind=kind, address=plain.address
        )
    await _call(
        ledger, {"input_tokens": 1}, model="plain/gone", address=plain.address, run_id="gone"
    )
    await _call(
        ledger, {"input_tokens": 1}, model="plain/owned", address=plain.address, owner_name="swarm"
    )
    await _call(ledger, {"input_tokens": 1}, model="nowhere/chat")
    await _call(ledger, {"input_tokens": 1}, model="nowhere/embedding", kind="text_embedding")
    helper = manager.create("main", session_id="helper")
    manager.set_metadata(helper.address, {"is_subagent_session": True})
    _admit(manager, helper.address, "helped")
    seed_history(
        manager.get(helper.address),
        [_run_summary(status="completed", at=_at(60), duration_ms=1, run_id="helped")],
    )
    await _call(ledger, {"input_tokens": 1}, model="helper/chat", address=helper.address)
    await _call(
        ledger,
        {"input_tokens": 1},
        model="helper/title",
        kind="session_title",
        address=helper.address,
    )
    await _participant_run(manager, _at(70))
    await _call(
        ledger,
        {"input_tokens": 1},
        model="participant/title",
        kind="session_title",
        address=_PARTICIPANT,
    )

    rows = _rollups(index, manager, ledger, {})

    assert {key: row["origin"] for key, row in _runs(rows).items()} == {
        **{("plain", kind): origin for kind, origin in kinds.items()},
        ("helper", "helped"): "subagent",
        ("participant", "owned"): "extension",
    }
    assert {row["name"]: row["origin"] for row in rows["agg_tools"]} == {
        **kinds,
        "loose": "user",
    }
    assert {row["model_key"]: row["origin"] for row in rows["agg_usage"]} == {
        **{f"run/{kind}": origin for kind, origin in kinds.items()},
        "plain/chat": "user",
        "plain/compaction": "user",
        "plain/session_title": "background",
        "plain/gone": "user",
        "plain/owned": "extension",
        "nowhere/chat": "user",
        "nowhere/embedding": "background",
        "helper/chat": "subagent",
        "helper/title": "subagent",
        "participant/title": "extension",
    }


@pytest.mark.parametrize("source", ["saved", "ledger"])
@pytest.mark.asyncio
async def test_a_run_row_sums_its_steps_tools_and_requests(
    manager: ChatSessionManager, index: StatisticsIndex, ledger: UsageRecorder, source: str
) -> None:
    session = manager.create("main", session_id="session")
    _admit(manager, session.address, "run")
    seed_history(
        session,
        [
            ChatMessage.user("question", timestamp=_at(1)),
            _assistant(
                model="b/model",
                at=_at(2),
                content=None,
                usage={"input_tokens": 100, "output_tokens": 10},
            ),
            _tool(name="edit", at=_at(3), envelope=tool_failure("denied", "no"), duration_ms=250),
            _tool(name="read", at=_at(3), envelope=tool_success({}), duration_ms=50),
            _assistant(
                model="a/model",
                at=_at(4.5),
                usage={
                    "input_tokens": 30,
                    "output_tokens": 20,
                    "cache_read_tokens": 6,
                    "reported_cost_usd": 0.25,
                },
            ),
            _compaction(at=_at(5), before=900, after=100),
            ChatMessage.error("rate_limit", "provider failed", timestamp=_at(6)),
            ChatMessage.run_summary(
                run_id="run",
                status="completed",
                timing={
                    "started_at": BASE.isoformat(),
                    "completed_at": _at(7).isoformat(),
                    "duration_ms": 7000,
                },
                iteration_count=3,
                timestamp=_at(7),
                change_stats={"files": 2, "added": 10, "removed": 4, "paths": ["a", "b"]},
            ),
        ],
    )
    if source == "ledger":
        # Ledger requests replace the saved usage: every attempt, failed ones too.
        await _call(
            ledger,
            {"input_tokens": 500, "output_tokens": 5},
            model="c/model",
            address=session.address,
            run_id="run",
            status="failed",
        )
        await _call(
            ledger,
            {"input_tokens": 200, "output_tokens": 50, "reported_cost_usd": 1.5},
            kind="session_title",
            model="d/model",
            address=session.address,
            run_id="run",
        )
        # An in-flight request counts as a request, never as a failed one.
        await ledger.start(
            model="d/model",
            kind="chat",
            agent_id="main",
            session_id="session",
            run_id="run",
        )
    prices: Prices = {"b/model": TokenPricing.from_cost({"input": 1, "output": 1}, source="t")}

    run = _runs(_rollups(index, manager, ledger, prices))[("session", "run")]

    start = datetime_instant(BASE)
    assert {key: run[key] for key in list(run)[1:20]} == {
        "run_id": "run",
        "origin": "user",
        "run_kind": "user",
        "status": "completed",
        "completion_reason": None,
        "start_instant": start,
        "end_instant": start + 7_000_000,
        "duration_ms": 7000,
        "iterations": 3,
        "model_steps": 2,
        "visible_messages": 1,
        "user_messages": 1,
        "first_visible_ms": 4500,
        "tool_calls": 2,
        "tool_rejected": 1,
        "tool_ms": 300,
        "compactions": 1,
        "errors": 1,
        "calls": 2 if source == "saved" else 3,
    }
    assert (run["changed_files"], run["lines_added"], run["lines_removed"]) == (2, 10, 4)
    requests = {key: run[key] for key in list(run)[21:]}
    del requests["changed_files"], requests["lines_added"], requests["lines_removed"]
    if source == "saved":
        assert requests == {
            "input_tokens": 130,
            "estimated_input_tokens": 0,
            "output_tokens": 30,
            "estimated_output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 6,
            "cache_write_tokens": 0,
            "cache_input_tokens": 30,
            "cache_calls": 1,
            "reported_nusd": 250_000_000,
            # b/model's 110 tokens at $1 per million.
            "estimated_nusd": 110_000,
            "unpriced_calls": 0,
            "primary_model": "b/model",
            "models": '["b/model","a/model"]',
            "kinds": '["chat"]',
        }
        assert run["failed_calls"] == 0
    else:
        assert requests == {
            "input_tokens": 700,
            "estimated_input_tokens": 0,
            "output_tokens": 55,
            "estimated_output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_input_tokens": 0,
            "cache_calls": 0,
            "reported_nusd": 1_500_000_000,
            "estimated_nusd": 0,
            "unpriced_calls": 2,
            "primary_model": "c/model",
            "models": '["c/model","d/model"]',
            "kinds": '["chat","session_title"]',
        }
        assert run["failed_calls"] == 1


@pytest.mark.asyncio
async def test_money_sums_are_exact_nano_usd(
    manager: ChatSessionManager, index: StatisticsIndex, ledger: UsageRecorder
) -> None:
    session = manager.create("main", session_id="session")
    _admit(manager, session.address, "run")
    seed_history(
        session, [_run_summary(status="completed", at=_at(1), duration_ms=1, run_id="run")]
    )
    amounts = (0.1, 0.2, 0.3, 1.4e-9, 1.6e-9)
    for amount in amounts:
        await _call(
            ledger,
            {"input_tokens": 1, "output_tokens": 1, "reported_cost_usd": amount},
            address=session.address,
            run_id="run",
        )

    rows = _rollups(index, manager, ledger, {})

    # Each amount rounds to whole nano-USD before summing.
    (usage,) = rows["agg_usage"]
    assert usage["reported_nusd"] == 600_000_003
    assert usage["reported_calls"] == 5
    assert _runs(rows)[("session", "run")]["reported_nusd"] == 600_000_003
