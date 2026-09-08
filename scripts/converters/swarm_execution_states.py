"""Copy an offline Swarm database to the execution-only participant format."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def convert(source: Path, output: Path) -> None:
    """Retain the input unchanged; remove the output if conversion or validation fails."""
    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().absolute()
    if source == output.resolve() or output.exists():
        raise ValueError("Output must be a new file separate from the source")
    with output.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(output)) as writer,
        ):
            reader.backup(writer)
            writer.execute("PRAGMA journal_mode=DELETE")
            _convert_copy(writer)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _convert_copy(connection: sqlite3.Connection) -> None:
    participants = {"idle", "running", "failed", "cancelled", "interrupted"}
    retired = {"prepared", "starting", "waiting", "blocked", "finishing", "done"}
    states = {row[0] for row in connection.execute("SELECT DISTINCT state FROM participants")}
    if states - participants - retired:
        raise ValueError("Unrecognized participant state; source was not changed")
    with connection:
        for table, column in (("profiles", "payload"), ("swarms", "profile_snapshot")):
            for row_id, raw in connection.execute(f"SELECT id,{column} FROM {table}").fetchall():
                profile = json.loads(raw)
                reminders = profile.get("reminders")
                if reminders is not None:
                    if not isinstance(reminders, dict) or set(reminders) not in (
                        {"delivery", "wake", "resume", "completion"},
                        {"delivery", "wake", "resume"},
                        {"delivery", "resume"},
                    ):
                        raise ValueError("Unrecognized reminder settings; source was not changed")
                    reminders.pop("completion", None)
                    reminders.pop("wake", None)
                    connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE id=?",
                        (json.dumps(profile, ensure_ascii=False), row_id),
                    )
        connection.execute(
            "UPDATE participants SET state=CASE "
            "WHEN state IN ('running','starting','finishing') THEN 'interrupted' "
            "WHEN state IN ('prepared','waiting','blocked','done') THEN 'idle' ELSE state END,"
            "wake_pending=0"
        )
        connection.execute("UPDATE swarms SET state='stopped'")
        connection.execute("UPDATE swarm_epochs SET is_open=0")
        connection.execute("DELETE FROM swarm_execution_epochs")
        connection.execute("DROP TABLE IF EXISTS lifecycle_intents")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(participants)")}
        for name in ("wait_reason", "summary_json", "artifacts_json", "completion_call_id"):
            if name in columns:
                connection.execute(f"ALTER TABLE participants DROP COLUMN {name}")
        # Prior control requests refer to an execution attempt intentionally closed here.
        connection.execute(
            "DELETE FROM requests WHERE scope LIKE 'stop:%' OR scope LIKE 'stop-finish:%' "
            "OR scope LIKE 'resume:%'"
        )
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Converted database failed integrity validation")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Converted database failed reference validation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Offline Swarm database")
    parser.add_argument("--output", type=Path, required=True, help="New converted database file")
    args = parser.parse_args()
    convert(args.source, args.output)
    print(f"Converted copy: {args.output}. Original retained: {args.source}")


if __name__ == "__main__":
    main()
