"""Builders for Skills, Extensions, and settings in direct Runtime tests."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from inspect import isawaitable
from pathlib import Path
from typing import Any

from core.chat import CommandExecutionContext, ReplySurface
from core.runtime.runtime import Runtime
from core.tools import ToolContext

CAPABILITY_EXT_SOURCE = (
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


def command_extension_source(reply: str) -> str:
    """Source of an Extension whose ``/workflow`` command answers with *reply*."""
    return (
        "from core.chat import CommandFeedback, CommandOutcome\n"
        "def _workflow(context, argument):\n"
        f"    return CommandOutcome(command='workflow', "
        f"feedback=CommandFeedback(kind='notice', text={reply!r}))\n"
        "def register(api):\n"
        "    api.register_command('workflow', 'Run the workflow.', _workflow)\n"
    )


def tool_extension_source(tool_name: str, version: str | None = None) -> str:
    """Source of an Extension registering *tool_name*, answering ``{'version': version}``."""
    result = "{}" if version is None else f"{{'version': {version!r}}}"
    return (
        "from core.tools import tool_success\n"
        "def _echo(context, arguments):\n"
        f"    return tool_success({result})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _echo)\n"
    )


def lifecycle_extension_source(marker: Path) -> str:
    """Source of an Extension appending ``startup``/``shutdown`` lines to *marker*."""
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def register(api):\n"
        "    api.on_startup(lambda: _write('startup'))\n"
        "    api.on_shutdown(lambda: _write('shutdown'))\n"
    )


def dispatch_workflow_command(runtime: Runtime) -> str:
    """Run the ``/workflow`` Extension command for ``main`` and return its notice text."""
    prepared = runtime.command_dispatcher.prepare("/workflow")
    assert prepared is not None
    outcome = asyncio.run(
        runtime.command_dispatcher.execute(
            prepared,
            CommandExecutionContext(
                agent_id="main",
                session_id=runtime.agents.get("main").current_session_id,
                project_id=None,
                reply_surface=ReplySurface.webui(),
            ),
        )
    )
    assert outcome.feedback is not None
    return outcome.feedback.text


def write_skill(skill_root: Path, name: str, description: str) -> Path:
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )
    return skill_dir


def write_project_skill(repo: Path, name: str, description: str) -> Path:
    """Write a project-owned Skill under ``<repo>/.opencode/skills/<name>/``."""
    return write_skill(repo / ".opencode" / "skills", name, description)


def write_agent_skill(data_dir: Path, agent_id: str, name: str, description: str) -> Path:
    """Write an Agent-private Skill under ``<data_dir>/agents/<id>/skills/<name>/``."""
    return write_skill(data_dir / "agents" / agent_id / "skills", name, description)


def write_extension(data_dir: Path, name: str, source: str) -> Path:
    extensions_dir = data_dir / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    path = extensions_dir / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def write_extension_with_skill(
    data_dir: Path, ext_name: str, skill_name: str, description: str
) -> Path:
    """Write a package Extension bundling one Skill under ``<ext>/skills/<name>/``."""
    ext_dir = data_dir / "extensions" / ext_name
    ext_dir.mkdir(parents=True)
    ext_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
    write_skill(ext_dir / "skills", skill_name, description)
    return ext_dir


def write_settings(data_dir: Path, settings: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(
        json.dumps({"format_version": 1, **settings}), encoding="utf-8"
    )


def rewrite_source(path: Path, source: str) -> None:
    """Rewrite an Extension source file and bump its mtime past the cached bytecode.

    A test rewrites v1 to v2 within the same wall-clock second and often at the
    same byte length; CPython's timestamp-based ``.pyc`` invalidation would then
    treat the bytecode written on the first import as current and re-execute the
    stale code. Pushing the source mtime forward makes the reload recompile.
    """
    path.write_text(source, encoding="utf-8")
    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))


def marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def extension_record(runtime: Runtime, name: str) -> Any:
    assert runtime.extensions is not None
    return next(record for record in runtime.extensions.records() if record.name == name)


def extension_record_names(runtime: Runtime) -> list[str]:
    assert runtime.extensions is not None
    return [record.name for record in runtime.extensions.records()]


def tool_names(runtime: Runtime) -> list[str]:
    return [tool.name for tool in runtime.tools.list_tools()]


def tool_context(tool_name: str, data_dir: Path, *, agent_id: str = "a") -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id="s",
        run_id="r",
        tool_call_id="c1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=data_dir,
        vbot_root=data_dir,
        data_root=data_dir,
    )


def dispatch_tool(
    runtime: Runtime,
    tool_name: str,
    data_dir: Path,
    arguments: dict[str, Any] | None = None,
    *,
    agent_id: str = "a",
) -> dict[str, Any]:
    """Dispatch *tool_name* through the Runtime Tool registry from synchronous code."""
    context = tool_context(tool_name, data_dir, agent_id=agent_id)
    return asyncio.run(runtime.tools.dispatch(context, arguments or {}))


async def call_rpc(
    handlers: Mapping[str, Callable[[Any, dict[str, Any]], Any]],
    method: str,
    state: object,
    params: dict[str, Any],
) -> Any:
    """Call one RPC method handler directly, awaiting it when it is asynchronous."""
    result = handlers[method](state, params)
    return await result if isawaitable(result) else result
