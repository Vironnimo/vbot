"""Shared fixtures and fakes for runtime extensions behavior tests."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.runtime.runtime import Runtime
from core.sessions.format import write_bootstrap_marker


def _authorize_session_store(data_dir: Path) -> None:
    if not (data_dir / "sessions.db").is_file():
        write_bootstrap_marker(data_dir)


_CAPABILITY_EXT_SOURCE = (
    "from core.tools import tool_success\n"
    "def _echo(context, arguments):\n"
    "    return tool_success({'value': arguments.get('value')})\n"
    "class ExtBackend:\n"
    "    def __init__(self, context):\n"
    "        self.context = context\n"
    "    def search_capabilities(self):\n"
    "        from core.recall import RecallSearchCapabilities\n"
    "        return RecallSearchCapabilities(result_type='message', guidance='Extension search.')\n"
    "    async def search_page(self, request):\n"
    "        from core.recall import RecallSearchPage\n"
    "        return RecallSearchPage((), 'message', 'extension', 'snapshot', False, 0)\n"
    "def register(api):\n"
    "    api.register_tool('ext_echo', 'desc', {'type': 'object'}, _echo)\n"
    "    api.register_recall_backend('ext_recall', ExtBackend)\n"
)


def _command_extension_source(reply: str) -> str:
    return (
        "from core.chat import CommandFeedback, CommandOutcome\n"
        "def _workflow(context, argument):\n"
        f"    return CommandOutcome(command='workflow', "
        f"feedback=CommandFeedback(kind='notice', text={reply!r}))\n"
        "def register(api):\n"
        "    api.register_command('workflow', 'Run the workflow.', _workflow)\n"
    )


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _write_extension(data_dir: Path, name: str, source: str) -> None:
    extensions_dir = data_dir / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(data_dir)
    (extensions_dir / f"{name}.py").write_text(source, encoding="utf-8")


def _write_settings(data_dir: Path, settings: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(data_dir)
    (data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def _extension_record(runtime: Runtime, name: str):
    assert runtime.extensions is not None
    return next(record for record in runtime.extensions.records() if record.name == name)


def _rewrite_source(path: Path, source: str) -> None:
    """Rewrite an extension source file and bump its mtime past the cached bytecode.

    A test rewrites v1→v2 within the same wall-clock second and often at the same
    byte length; CPython's timestamp-based ``.pyc`` invalidation would then treat
    the bytecode written on the first import as still current and re-execute the
    stale code. Pushing the source mtime forward guarantees the reload recompiles
    from the new source — the same thing real elapsed time does in production.
    """
    path.write_text(source, encoding="utf-8")
    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))
