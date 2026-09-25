"""Generation 1 conversion of the MCP Extension's saved results.

Before Generation 1 the MCP Extension saved every result it presented as
``mcp/content/results/<result_id>.json`` (``owner``, ``connection``, ``source``
and ``payload``), and kept a binary block the Attachment store rejected as an
unmanaged file under ``mcp/content/``. Generation 1 keeps a large result as a
payload of the Tool call that returned it, inside ``sessions.db``, and keeps no
rejected bytes.

This area runs after the sessions area and writes into its staged
``sessions.db``:

- A saved result is attached to the Tool call whose Tool Result returned it:
  the one whose receipt carries the result id with the matching ``read``
  request. Later ``read`` pages name the id too but return no receipt, so they
  never count. The result keeps its id as the payload id and belongs to the
  MCP Extension. A fork that kept its own copy of that Tool Result (the same
  Message) gets its own copy of the payload, as materialization gives one.
- A result that no Tool Result returned, that several different Tool Results
  name, or whose file is unreadable is dropped and reported.
- A rejected binary block inside a kept payload becomes an omission marker: its
  ``path`` goes, and ``content_omitted`` says the bytes were not kept.
- Every file under ``mcp/content/`` retires.

Without a staged ``sessions.db`` (the source has none, or it is already
current) nothing is attached or retired, and the kept files are reported.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.database import open_offline_database
from core.sessions._store_schema import session_database_spec
from core.utils.ids import is_safe_id
from scripts.converters.persistence_generation_1._context import ConversionContext
from scripts.converters.persistence_generation_1._legacy_sqlite import Tally

AREA = "mcp"
OWNER = "mcp"
DATABASE = "sessions.db"
CONTENT = "mcp/content"
RESULTS = f"{CONTENT}/results"


@dataclass(frozen=True, slots=True)
class _Result:
    """One saved result file, decoded."""

    result_id: str
    relative: str
    payload_json: str


@dataclass(frozen=True, slots=True)
class _Receipt:
    """One Tool Result that returned a saved result."""

    call_key: int
    entry_id: str
    created_at: str


def convert(context: ConversionContext) -> None:
    """Attach saved MCP results to their Tool calls in the staged ``sessions.db``."""
    content = context.source_path(CONTENT)
    files = sorted(path for path in content.rglob("*") if path.is_file())
    if not files:
        return
    staged = context.staging / DATABASE
    if not staged.is_file():
        context.report.count(AREA, "files_kept_without_staged_sessions", len(files))
        context.report.skip(
            AREA,
            CONTENT,
            "kept: no converted sessions.db to attach saved results to",
        )
        return
    tally = Tally()
    results = [_read_result(context, path, tally) for path in files if _is_result(context, path)]
    decoded = [result for result in results if result is not None]
    database = open_offline_database(session_database_spec(staged))
    try:
        attempt = database.write(lambda connection: _attach(connection, decoded))
    finally:
        database.close()
    tally.publish(context, AREA)
    attempt.publish(context, AREA)
    for path in files:
        context.retire(path.relative_to(context.source).as_posix())
    context.report.count(AREA, "files_retired", len(files))


def _is_result(context: ConversionContext, path: Path) -> bool:
    relative = path.relative_to(context.source).as_posix()
    return relative.startswith(f"{RESULTS}/res_") and path.suffix == ".json"


def _read_result(context: ConversionContext, path: Path, tally: Tally) -> _Result | None:
    relative = path.relative_to(context.source).as_posix()
    result_id = path.stem
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        tally.skip(relative, f"dropped: unreadable saved result ({error})")
        return None
    if (
        not is_safe_id(result_id)
        or not isinstance(document, dict)
        or not isinstance(document.get("connection"), str)
        or "payload" not in document
    ):
        tally.skip(relative, "dropped: not a saved MCP result")
        return None
    source = document.get("source")
    payload = {
        "connection": document["connection"],
        "source": source if isinstance(source, str) else None,
        "payload": _omit_rejected_media(document["payload"], tally),
    }
    return _Result(
        result_id=result_id,
        relative=relative,
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def _omit_rejected_media(value: Any, tally: Tally) -> Any:
    """Replace the unmanaged file of every rejected binary block with the omission marker."""
    if isinstance(value, list):
        return [_omit_rejected_media(item, tally) for item in value]
    if not isinstance(value, dict):
        return value
    converted = {key: _omit_rejected_media(item, tally) for key, item in value.items()}
    if "media_delivery_error" in converted and "path" in converted:
        del converted["path"]
        converted["content_omitted"] = True
        tally.count("rejected_media_omitted")
    return converted


def _attach(connection: sqlite3.Connection, results: list[_Result]) -> Tally:
    """Insert each result as the payload of the Tool call that returned it."""
    tally = Tally()
    receipts = _receipts(connection)
    for result in results:
        found = receipts.get(result.result_id, [])
        if not found:
            tally.skip(result.relative, "dropped: no Tool Result returned this saved result")
            continue
        if len({receipt.entry_id for receipt in found}) > 1:
            tally.skip(result.relative, "dropped: several Tool Results name this saved result")
            continue
        connection.executemany(
            "INSERT INTO tool_result_payloads (payload_id, call_key, owner_name, created_at, "
            "payload_json) VALUES (?, ?, ?, ?, ?)",
            [
                (result.result_id, receipt.call_key, OWNER, receipt.created_at, result.payload_json)
                for receipt in found
            ],
        )
        tally.count("results_attached")
        if len(found) > 1:
            tally.count("fork_copies_attached", len(found) - 1)
    return tally


def _receipts(connection: sqlite3.Connection) -> dict[str, list[_Receipt]]:
    """Index every Tool Result that returned a saved result by its result id."""
    receipts: dict[str, list[_Receipt]] = defaultdict(list)
    rows = connection.execute(
        "SELECT c.call_key, e.entry_id, e.created_at, t.content FROM tool_calls AS c "
        "JOIN entries AS e ON e.entry_key = c.result_entry_key "
        "JOIN entry_text AS t ON t.entry_key = e.entry_key "
        # A remote Tool error escapes the receipt inside its message, so the
        # filter matches the bare key and the decoder decides.
        "WHERE t.content IS NOT NULL AND instr(t.content, 'result_id') > 0"
    )
    for call_key, entry_id, created_at, content in rows:
        result_id = _receipt_result_id(content)
        if result_id is not None:
            receipts[result_id].append(_Receipt(int(call_key), str(entry_id), str(created_at)))
    return receipts


def _receipt_result_id(content: str) -> str | None:
    """Return the result id a Tool Result's receipt returned, if it returned one.

    A successful call carries the receipt as its data; a failed remote Tool
    call carries it as the JSON text of its error message.
    """
    try:
        envelope = json.loads(content)
    except ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    receipt: Any = envelope.get("data") if envelope.get("ok") is True else None
    error = envelope.get("error")
    if envelope.get("ok") is False and isinstance(error, dict):
        try:
            receipt = json.loads(str(error.get("message")))
        except ValueError:
            return None
    if not isinstance(receipt, dict):
        return None
    result_id = receipt.get("result_id")
    if not is_safe_id(result_id) or receipt.get("read") != {
        "action": "read",
        "result_id": result_id,
    }:
        return None
    return str(result_id)
