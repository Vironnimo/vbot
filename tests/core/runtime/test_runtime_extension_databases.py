"""Runtime wiring of owner-bound Extension databases across the Extension lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.database import create_data_snapshot, read_marker, read_verified_manifest
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_extensions_test_support import (
    _clean_extension_modules as _clean_extension_modules,
)
from tests.core.runtime.runtime_extensions_test_support import (
    _marker_lines,
    _write_extension,
)

DATABASE_NAME = "ext.notes_ext.notes"


def _notes_extension(marker: Path) -> str:
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "_SCHEMA = 'CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL) STRICT;'\n"
        "_STATE = {}\n"
        "async def _start(host):\n"
        "    database = await host.open_database('notes', _SCHEMA)\n"
        "    _STATE['database'] = database\n"
        "    await database.write_async(\n"
        "        lambda connection: connection.execute(\n"
        "            'INSERT INTO notes (body) VALUES (?)', ('started',)\n"
        "        )\n"
        "    )\n"
        "def _shutdown():\n"
        "    database = _STATE['database']\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(f'shutdown-open={not database.is_closed()}\\n')\n"
        "def register(api):\n"
        "    api.operations.startup.append(_start)\n"
        "    api.on_shutdown(_shutdown)\n"
    )


def _extension_database(runtime: Runtime):
    matches = [
        database for database in runtime.canonical_databases() if database.name == DATABASE_NAME
    ]
    assert len(matches) <= 1
    return matches[0] if matches else None


def _note_count(database) -> int:
    with database.read() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0])


def test_extension_database_follows_startup_reload_and_disable(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    marker = tmp_path / "lifecycle.txt"
    _write_extension(data_dir, "notes_ext", _notes_extension(marker))

    runtime = Runtime(config)
    runtime.start()
    try:
        asyncio.run(runtime.fire_extension_startup())

        first = _extension_database(runtime)
        assert first is not None
        assert first.path == data_dir / "extension-data" / "notes_ext" / "notes.db"
        assert {"sessions", "decisions", DATABASE_NAME} <= {
            database.name for database in runtime.canonical_databases()
        }
        registered = read_marker(data_dir)
        assert registered is not None and DATABASE_NAME in registered.databases
        snapshot = create_data_snapshot(
            data_dir, reason="test", databases=runtime.canonical_databases()
        )
        assert snapshot is not None
        manifest = read_verified_manifest(data_dir, snapshot)
        assert manifest is not None and DATABASE_NAME in manifest.members

        asyncio.run(runtime.reload_extensions())

        assert first.is_closed()
        second = _extension_database(runtime)
        assert second is not None and second is not first
        assert _note_count(second) == 2
        assert _marker_lines(marker) == ["shutdown-open=True"]

        asyncio.run(runtime.apply_extension_disabled_change({"notes_ext"}))

        assert second.is_closed()
        assert _extension_database(runtime) is None
        assert _marker_lines(marker) == ["shutdown-open=True", "shutdown-open=True"]
    finally:
        runtime.stop()


def test_runtime_stop_closes_extension_databases(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    _write_extension(config.data_dir, "notes_ext", _notes_extension(tmp_path / "lifecycle.txt"))

    runtime = Runtime(config)
    runtime.start()
    try:
        asyncio.run(runtime.fire_extension_startup())
        database = _extension_database(runtime)
        assert database is not None
    finally:
        runtime.stop()

    assert database.is_closed()
    assert runtime.canonical_databases() == ()
