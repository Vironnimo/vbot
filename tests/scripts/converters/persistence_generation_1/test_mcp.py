"""Tests for the Generation 1 conversion of saved MCP results into Tool result payloads."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.store import SessionStore
from scripts.converters.persistence_generation_1 import mcp, sessions
from scripts.converters.persistence_generation_1._context import ConversionContext
from tests.scripts.converters.persistence_generation_1.legacy_sessions_support import (
    LegacySessionStore,
)

BASE = SessionAddress(project_id=None, agent_id="main", session_id="base")
BRANCH = SessionAddress(project_id=None, agent_id="main", session_id="branch")
SAVED = "res_000000000001"
FAILED = "res_000000000002"
UNCLAIMED = "res_000000000003"
AMBIGUOUS = "res_000000000004"
BROKEN = "res_000000000005"
REJECTED = "mcp/content/mcp_0001.png"


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


@contextmanager
def _opened(context: ConversionContext) -> Iterator[ChatSessionManager]:
    store = SessionStore(context.staging / "sessions.db", _offline=True)
    manager = ChatSessionManager(context.staging, store=store)
    try:
        yield manager
    finally:
        manager.close()
        store.close()


def _rows(context: ConversionContext, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(context.staging / "sessions.db")) as connection:
        return [tuple(row) for row in connection.execute(sql)]


def _skips(context: ConversionContext) -> list[tuple[str, str]]:
    return sorted(
        (skip.item, skip.reason) for skip in context.report.skipped if skip.area == mcp.AREA
    )


def _receipt(result_id: str) -> dict[str, Any]:
    """The receipt the MCP Extension returned before Generation 1."""
    return {
        "result_id": result_id,
        "result_file": f"C:/vbot/data/mcp/content/results/{result_id}.json",
        "complete": False,
        "preview": {"items": 1},
        "read": {"action": "read", "result_id": result_id},
    }


def _success(data: dict[str, Any]) -> str:
    return json.dumps({"ok": True, "error": None, "data": data, "artifacts": []})


def _remote_failure(receipt: dict[str, Any]) -> str:
    """A remote Tool error: the receipt travels as the error message's JSON text."""
    error = {"code": "mcp_tool_error", "message": json.dumps(receipt), "retryable": False}
    return json.dumps({"ok": False, "error": error, "data": None, "artifacts": []})


def _save(context: ConversionContext, result_id: str, payload: Any) -> None:
    path = context.source / "mcp" / "content" / "results" / f"{result_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "owner": {"agent_id": "main", "project_id": None},
        "connection": "docs",
        "source": "search",
        "payload": payload,
    }
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")


def _call(legacy: LegacySessionStore, key: int, run_id: str, *call_ids: str, minute: float) -> None:
    legacy.assistant(
        key,
        None,
        minute=minute,
        run_id=run_id,
        tool_calls=[{"id": call_id, "name": "mcp_docs", "arguments": {}} for call_id in call_ids],
    )


def _retired(context: ConversionContext) -> list[str]:
    return sorted(path.as_posix() for path in context.retired)


def _files(context: ConversionContext) -> list[str]:
    content = context.source / "mcp" / "content"
    return sorted(
        PurePosixPath(path.relative_to(context.source).as_posix()).as_posix()
        for path in content.rglob("*")
        if path.is_file()
    )


@pytest.mark.asyncio
async def test_saved_results_become_payloads_of_the_tool_calls_that_returned_them(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        key = legacy.session("base", minute=0)
        legacy.start_run(key, "run_1", minute=1)
        legacy.user(key, "look it up", minute=1, run_id="run_1")
        _call(legacy, key, "run_1", "call_saved", "call_failed", minute=2)
        legacy.tool_result(key, "call_saved", _success(_receipt(SAVED)), minute=3)
        legacy.tool_result(key, "call_failed", _remote_failure(_receipt(FAILED)), minute=3)
        _call(legacy, key, "run_1", "call_read", "call_twin_a", "call_twin_b", minute=4)
        # A later read page names the saved result too, but returns no receipt.
        page = {"result_id": SAVED, "type": "object", "complete": True, "value": {"a": 1}}
        legacy.tool_result(key, "call_read", _success(page), minute=5)
        legacy.tool_result(key, "call_twin_a", _success(_receipt(AMBIGUOUS)), minute=5)
        legacy.tool_result(key, "call_twin_b", _success(_receipt(AMBIGUOUS)), minute=5)
        legacy.finish_run(key, "run_1", minute=6)
    _save(context, SAVED, {"structuredContent": {"rows": [1, 2, 3]}})
    rejected = {
        "type": "image",
        "mimeType": "image/png",
        "path": f"C:/vbot/data/{REJECTED}",
        "media_delivery_error": "Attachment is too large",
    }
    _save(context, FAILED, {"isError": True, "content": [rejected]})
    _save(context, UNCLAIMED, {"content": []})
    _save(context, AMBIGUOUS, {"content": []})
    broken = context.source_path(f"mcp/content/results/{BROKEN}.json")
    broken.write_text("{not json", encoding="utf-8", newline="\n")
    context.source_path(REJECTED).write_bytes(b"\x89PNG")
    files = _files(context)

    sessions.convert(context)
    mcp.convert(context)

    assert _rows(
        context,
        "SELECT c.call_id, p.payload_id, p.owner_name FROM tool_result_payloads AS p "
        "JOIN tool_calls AS c ON c.call_key = p.call_key ORDER BY p.payload_id",
    ) == [("call_saved", SAVED, "mcp"), ("call_failed", FAILED, "mcp")]
    with _opened(context) as manager:
        assert await manager.tool_result_payload_async(BASE, SAVED, owner_name="mcp") == {
            "connection": "docs",
            "source": "search",
            "payload": {"structuredContent": {"rows": [1, 2, 3]}},
        }
        failed: Any = await manager.tool_result_payload_async(BASE, FAILED, owner_name="mcp")
        assert failed["payload"]["content"] == [
            {
                "type": "image",
                "mimeType": "image/png",
                "media_delivery_error": "Attachment is too large",
                "content_omitted": True,
            }
        ]
        assert await manager.tool_result_payload_async(BASE, SAVED, owner_name="swarm") is None
    results = "mcp/content/results"
    skips = dict(_skips(context))
    assert skips.pop(f"{results}/{BROKEN}.json").startswith("dropped: unreadable saved result")
    assert skips == {
        f"{results}/{UNCLAIMED}.json": "dropped: no Tool Result returned this saved result",
        f"{results}/{AMBIGUOUS}.json": "dropped: several Tool Results name this saved result",
    }
    assert context.report.counts[mcp.AREA] == {
        "rejected_media_omitted": 1,
        "results_attached": 2,
        "files_retired": len(files),
    }
    assert _retired(context) == files
    assert REJECTED in files
    assert _rows(context, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_a_fork_that_kept_its_copy_of_the_tool_result_gets_its_own_payload(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        key = legacy.session("base", minute=0)
        legacy.start_run(key, "run_1", minute=1)
        legacy.user(key, "look it up", minute=1, run_id="run_1")
        _call(legacy, key, "run_1", "call_saved", minute=2)
        legacy.tool_result(key, "call_saved", _success(_receipt(SAVED)), minute=3)
        legacy.finish_run(key, "run_1", minute=4)
        # A fork made while the next Run was running keeps its own copies.
        legacy.start_run(key, "run_2", minute=5)
        legacy.user(key, "and more", minute=5, run_id="run_2")
        legacy.fork(key, "branch", minute=6)
        legacy.finish_run(key, "run_2", minute=7)
    _save(context, SAVED, {"structuredContent": {"rows": [1]}})

    sessions.convert(context)
    mcp.convert(context)

    assert context.report.counts[sessions.AREA]["forks_self_contained"] == 1
    assert _rows(
        context,
        "SELECT COUNT(DISTINCT p.call_key), COUNT(*) FROM tool_result_payloads AS p "
        f"WHERE p.payload_id = '{SAVED}'",
    ) == [(2, 2)]
    assert context.report.counts[mcp.AREA]["results_attached"] == 1
    assert context.report.counts[mcp.AREA]["fork_copies_attached"] == 1
    with _opened(context) as manager:
        for address in (BASE, BRANCH):
            payload: Any = await manager.tool_result_payload_async(address, SAVED, owner_name="mcp")
            assert payload["payload"] == {"structuredContent": {"rows": [1]}}
        manager.delete(BASE)
        payload = await manager.tool_result_payload_async(BRANCH, SAVED, owner_name="mcp")
        assert payload["payload"] == {"structuredContent": {"rows": [1]}}


def test_saved_results_stay_without_a_converted_session_database(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _save(context, SAVED, {"content": []})

    mcp.convert(context)

    assert context.retired == []
    assert context.report.counts[mcp.AREA] == {"files_kept_without_staged_sessions": 1}
    assert _skips(context) == [
        ("mcp/content", "kept: no converted sessions.db to attach saved results to")
    ]


def test_nothing_to_convert_without_saved_results(tmp_path: Path) -> None:
    context = _context(tmp_path)

    mcp.convert(context)

    assert context.retired == []
    assert context.report.to_dict() == {"counts": {}, "skipped": []}
