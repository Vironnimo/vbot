"""Shared context and dispatch helpers for the search_files Tool tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.tools.search_files import register_search_files_tool
from core.tools.tools import ToolContext, ToolRegistry


def context(root: Path, **kwargs: Any) -> ToolContext:
    return ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c",
        tool_name="search_files",
        tool_call_index=0,
        workspace=root,
        vbot_root=root,
        data_root=root,
        **kwargs,
    )


def search_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_search_files_tool(registry)
    return registry


async def dispatch(root: Path, arguments: dict[str, Any], **context_fields: Any) -> dict[str, Any]:
    """Dispatch one search_files call as Chat does: normalizer, contract, then handler."""
    return await search_registry().dispatch(context(root, **context_fields), arguments)


def search(root: Path, **arguments: Any) -> dict[str, Any]:
    """Run one search written in fixture notation and return its data.

    ``action`` (content or paths), ``kind`` (all, files, directories), ``patterns``,
    ``options`` and ``paths`` become the ``args`` list; the other keywords are
    passed as fields.
    """
    action = arguments.pop("action", "content")
    kind = arguments.pop("kind", "all")
    patterns = arguments.pop("patterns", [])
    options = arguments.pop("options", [])
    paths = arguments.pop("paths", [])
    tokens = list(options)
    if "--type-list" not in tokens:
        if action == "paths":
            tokens.append({"all": "--entries", "files": "--files", "directories": "--dirs"}[kind])
            for pattern in patterns:
                tokens.extend(["-g", "./" + pattern])
        else:
            for pattern in patterns:
                tokens.extend(["-e", pattern])
    if paths:
        tokens.extend(["--", *paths])
    result = asyncio.run(dispatch(root, {"args": tokens, **arguments}))
    assert result["ok"], result
    data: dict[str, Any] = result["data"]
    return data
