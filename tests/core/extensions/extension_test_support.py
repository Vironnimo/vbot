"""Write, load and observe filesystem Extensions in tests.

Tests declare Extensions as real source files and load them through
``ExtensionRegistry.load``/``aload``, so the whole declare -> apply path runs.
Hooks report through marker files or module globals instead of registry internals.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from core.extensions import ExtensionRegistry, HookContext, InteractionEvent
from core.extensions.extensions import ExtensionRecord
from core.tools import ToolContext


def write_extension(root: Path, name: str, source: str) -> Path:
    """Write the single-file Extension ``<root>/<name>.py`` and return its entry."""
    root.mkdir(parents=True, exist_ok=True)
    entry = root / f"{name}.py"
    entry.write_text(source, encoding="utf-8")
    return entry


def write_package(
    root: Path,
    name: str,
    source: str,
    *,
    manifest: dict[str, Any] | str | None = None,
    entry: str = "__init__.py",
) -> Path:
    """Write a directory Extension with *source* as *entry* and an optional manifest."""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / entry).write_text(source, encoding="utf-8")
    if manifest is not None:
        content = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (package / "extension.json").write_text(content, encoding="utf-8")
    return package


def record(registry: ExtensionRegistry, name: str) -> ExtensionRecord:
    return next(item for item in registry.records() if item.name == name)


def marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def marker_source(
    marker: Path, tag: str, *, event: str = "run_start", asynchronous: bool = False
) -> str:
    """Extension whose *event* hook appends *tag* to *marker* each time it fires."""
    register = "async def register(api):" if asynchronous else "def register(api):"
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _handler(ctx, **payload):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        f"        fh.write({tag!r} + '\\n')\n"
        f"{register}\n"
        f"    api.on({event!r}, _handler)\n"
    )


def tool_source(tool_name: str, marker: str = "from-extension") -> str:
    """Extension registering one echo Tool that returns *marker* and ``value``."""
    return (
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        f"    return tool_success({{'marker': {marker!r}, 'value': arguments.get('value')}})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _handler)\n"
    )


def command_source(command_name: Any, marker: str = "from-extension") -> str:
    """Extension registering one Command whose feedback is ``<marker>:<argument>``."""
    return (
        "from core.chat import CommandFeedback, CommandOutcome\n"
        "def _handler(context, argument):\n"
        f"    return CommandOutcome(command={command_name!r}, "
        f"feedback=CommandFeedback(kind='notice', text={marker!r} + ':' + str(argument)))\n"
        "def register(api):\n"
        f"    api.register_command({command_name!r}, 'desc', _handler)\n"
    )


def interaction_source(prefix: str, answer: str = "answered") -> str:
    """Extension whose interaction handler for *prefix* answers with *answer*."""
    return (
        "async def _handler(event, responder):\n"
        f"    await responder.answer({answer!r})\n"
        "def register(api):\n"
        f"    api.register_interaction_handler({prefix!r}, _handler)\n"
    )


def hook_context(**kwargs: Any) -> HookContext:
    return HookContext(session_id="s", agent_id="a", run_id="r", **kwargs)


def fire_run_start(registry: ExtensionRegistry) -> None:
    """Dispatch ``run_start`` from a thread without a running event loop."""
    asyncio.run(registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a"))


def tool_context(tool_name: str, root: Path) -> ToolContext:
    return ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=root,
        vbot_root=root,
        data_root=root,
    )


def tap(data: str) -> InteractionEvent:
    """A channel button tap carrying *data*."""
    return InteractionEvent(
        platform="telegram",
        channel_id="chan",
        chat_id="1",
        user_id="2",
        message_id="3",
        data=data,
        buttons=(),
    )


class RecordingResponder:
    """``InteractionResponder`` that records every answer text."""

    def __init__(self) -> None:
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answers.append(text)

    async def edit(self, *, text: str | None = None, buttons: object = None) -> None:
        return None
