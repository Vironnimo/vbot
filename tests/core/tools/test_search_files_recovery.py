"""Search intent survives encoded lists; literal regex payloads stay literal."""

import copy
import json

import pytest

from core.tools.search_files import (
    normalize_search_arguments,
    register_search_files_tool,
    search_files_handler,
)
from core.tools.tools import ToolRegistry
from tests.core.tools.test_search_files import context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supplied",
    [
        [r"computeDamage\("],
        json.dumps([r"computeDamage\("]),
        r'["computeDamage\("]',
        r' [ "computeDamage\(" ] ',
    ],
)
async def test_encoded_pattern_list_finds_only_the_requested_calls(tmp_path, supplied):
    (tmp_path / "code.ts").write_text(
        "// ordinary comment\nexport const name = 1;\ncomputeDamage(actor, target);\n"
    )
    registry = ToolRegistry()
    register_search_files_tool(registry)
    arguments = {"action": "content", "patterns": supplied}
    before = copy.deepcopy(arguments)
    result = await registry.dispatch(context(tmp_path), arguments)
    assert arguments == before
    assert result["ok"]
    assert result["data"]["content"] == "code.ts:3:computeDamage(actor, target);"


@pytest.mark.parametrize(
    "supplied,expected",
    [
        (r'["next\(\)", "clock|now\(\)", "statuses"]', [r"next\(\)", r"clock|now\(\)", "statuses"]),
        (r'["\.player\.(character|actor) ="]', [r"\.player\.(character|actor) ="]),
        (r'["inc[A-Z]\w*|num\(attacker"]', [r"inc[A-Z]\w*|num\(attacker"]),
        (r'["skillPoints:\s*\d|skillPoints\??:"]', [r"skillPoints:\s*\d|skillPoints\??:"]),
        (r'["[\"x\"]\s+"]', [r'["x"]\s+']),
        (r'["a\\b\("]', [r"a\b\("]),
    ],
)
def test_repairs_keep_every_pattern_and_regex_escape(supplied, expected):
    assert normalize_search_arguments({"patterns": supplied})["patterns"] == expected


@pytest.mark.asyncio
async def test_list_repairs_apply_to_aliases_and_wrappers_before_conflict_checks(tmp_path):
    (tmp_path / "code").write_text("call(x)\nother(y)\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    arguments = {
        "request": {"operation": "Content", "pattern": r'["call\("]'},
        "patterns": [r"call\("],
    }
    result = await registry.dispatch(context(tmp_path), arguments)
    assert result["data"]["content"] == "code:1:call(x)"
    arguments["patterns"] = [r"other\("]
    with pytest.raises(ValueError):
        await registry.dispatch(context(tmp_path), arguments)
    result = search_files_handler(context(tmp_path), arguments)
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern", [r'["call\("]', r"[abc]", '"item:generated"', r"\bcall\(", "a\nb", r"\u0041"]
)
async def test_actual_array_members_remain_exact_literal_payloads(tmp_path, pattern):
    (tmp_path / "text").write_text(pattern, newline="\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    # Quiet fixed-text search proves the receiving engine got the exact payload.
    arguments = {"action": "content", "patterns": [pattern], "options": ["-F", "-q", "-U"]}
    result = await registry.dispatch(context(tmp_path), arguments)
    assert result["ok"]
    assert result["data"]["matched"] is True
    assert "searched_paths" not in result["data"]
    assert normalize_search_arguments(arguments)["patterns"] == [pattern]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern", [r'["call\(",]', r'["call\("', r'["\bcall\("]', r'["\u0041\("]']
)
async def test_ambiguous_or_broken_lists_never_execute_as_character_classes(tmp_path, pattern):
    (tmp_path / "text").write_text("call(x)\nordinary comment\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    arguments = {"action": "content", "patterns": pattern}
    with pytest.raises(ValueError):
        await registry.dispatch(context(tmp_path), arguments)
    result = search_files_handler(context(tmp_path), arguments)
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_empty_result_exposes_actual_scope_and_preserves_literal_quotes(tmp_path):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (project / "events.ts").write_text("type Event = 'item:generated';\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    ctx = context(workspace, cwd=project)
    result = await registry.dispatch(ctx, {"action": "content", "patterns": '"item:generated"'})
    data = result["data"]
    assert data["searched_paths"] == [project.as_posix()]
    assert data["patterns"] == ['"item:generated"']
    assert data["complete"] is True
    found = await registry.dispatch(ctx, {"action": "content", "patterns": ["item:generated"]})
    assert found["data"]["content"] == "events.ts:1:type Event = 'item:generated';"
    empty = await registry.dispatch(
        ctx, {"action": "paths", "patterns": ["*.missing"], "paths": [workspace.as_posix()]}
    )
    assert empty["data"]["searched_paths"] == [workspace.as_posix()]
    assert empty["data"]["patterns"] == ["*.missing"]
