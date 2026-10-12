"""Swarm Wiki pages and Board posts sent to the file Tools name the Swarm call instead."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.tools import FileReadState, ToolRegistry, register_read_tool
from core.tools.apply_patch import register_apply_patch_tool
from core.tools.edit import register_edit_tools
from core.tools.search_files import register_search_files_tool
from tests.core.tools.read_test_support import make_context

EDIT = {"path": "w3.md", "edits": [{"old_string": "draft", "new_string": "final"}]}
PATCH = {"patch": "*** Begin Patch\n*** Update File: w3.md\n-draft\n+final\n*** End Patch"}


def file_tools() -> ToolRegistry:
    state = FileReadState()
    tools = ToolRegistry()
    register_read_tool(
        tools,
        attachment_store=Mock(),
        speech_service=Mock(),
        file_state=state,
        speech_max_size_bytes=1024,
    )
    register_apply_patch_tool(tools, file_state=state)
    register_edit_tools(tools, file_state=state)
    register_search_files_tool(tools)
    return tools


async def message(root: Path, tool: str, arguments: dict, offered: set[str]) -> str:
    context = replace(make_context(root, tool), offered_tools={tool, *offered})
    result = await file_tools().dispatch(context, arguments)
    assert result["ok"] is False, result
    return str(result["error"]["message"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "call"),
    [
        # A read continuing a page carries its character offset and limit along.
        (
            "read",
            {"path": "w2", "offset": 5400, "limit": 1400},
            'swarm_wiki {"action": "read", "page_id": "w2", "offset": 5400, "limit": 1400}.',
        ),
        # Line numbers are no character positions, so the call leaves them out.
        (
            "read",
            {"path": "w2", "offset": 190, "limit": 20},
            'swarm_wiki {"action": "read", "page_id": "w2"}.',
        ),
        ("read", {"path": "W12.md"}, 'swarm_wiki {"action": "read", "page_id": "w12"}.'),
        # A search for one plain text reads the page lines that hold it.
        (
            "search_files",
            {"pattern": "retry budget", "path": "w2"},
            'swarm_wiki {"action": "read", "page_id": "w2", "query": "retry budget"}.',
        ),
        # A regular expression is no text the page could be searched for.
        (
            "search_files",
            {"pattern": "retr(y|ies)", "path": "w2"},
            'swarm_wiki {"action": "read", "page_id": "w2"}.',
        ),
        ("edit", EDIT, 'swarm_wiki: action "update", page_id "w3"'),
        ("apply_patch", PATCH, 'swarm_wiki: action "update", page_id "w3"'),
    ],
)
async def test_a_missing_wiki_page_number_names_the_offered_swarm_wiki_call(
    tmp_path: Path, tool: str, arguments: dict, call: str
) -> None:
    offered = await message(tmp_path, tool, arguments, {"swarm_wiki"})
    not_offered = await message(tmp_path, tool, arguments, set())

    assert call in offered
    assert "swarm_wiki" not in not_offered


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["docs/w2", "w2x", "web2.md"])
async def test_other_missing_paths_name_no_swarm_wiki_call(tmp_path: Path, path: str) -> None:
    (tmp_path / "docs").mkdir()

    assert "swarm_wiki" not in await message(tmp_path, "read", {"path": path}, {"swarm_wiki"})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["message_id", "messageId"])
async def test_a_board_post_sent_to_read_names_the_offered_swarm_board_call(
    tmp_path: Path, field: str
) -> None:
    offered = await message(tmp_path, "read", {field: "#452"}, {"swarm_board"})
    not_offered = await message(tmp_path, "read", {field: "#452"}, set())

    assert 'swarm_board {"action": "read", "message_id": "#452"}' in offered
    assert "swarm_board" not in not_offered
    assert "path" in not_offered
