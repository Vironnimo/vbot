"""Failed patches expose the mismatch and a working continuation into long text."""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from unittest.mock import Mock

import pytest

from core.tools._patch_hunks import _not_found
from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.read import register_read_tool
from core.tools.tools import ToolRegistry
from tests.core.tools.apply_patch_helpers import context, text, update


@pytest.fixture
def registry():
    state = FileReadState()
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=state)
    register_read_tool(
        registry,
        attachment_store=Mock(),
        speech_service=Mock(),
        file_state=state,
        speech_max_size_bytes=1024,
    )
    return registry


def _read_calls(message):
    """Interpret the displayed calls as a caller would, without inventing offsets."""
    calls = []
    for match in re.finditer(r"read\([^\n]*?\)", message):
        call = ast.parse(match[0], mode="eval").body
        assert isinstance(call, ast.Call)
        calls.append({item.arg: ast.literal_eval(item.value) for item in call.keywords})
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("shape", ["patch", "old_string"])
async def test_long_mismatch_is_visible_and_read_continuation_recovers(
    tmp_path, registry, ending, shape
):
    name = "café file.txt"
    path = tmp_path / name
    actual = "\t" + "café 🙂 " * 230 + "setting_111" + " suffix" * 60
    wanted = "    " + actual[1:].replace("setting_111", "setting_123")
    before = ending.join(("header", actual, "tail", "")).encode()
    path.write_bytes(before)
    arguments = (
        {"patch": update(f"@@\n-{wanted}\n+changed", name)}
        if shape == "patch"
        else {"path": name, "old_string": wanted, "new_string": "changed"}
    )

    result = await registry.dispatch(context(tmp_path), arguments, ["apply_patch", "read"])

    assert result["error"]["code"] == "text_not_found"
    assert path.read_bytes() == before
    message = text(result)
    assert "setting_111" in message and "setting_123" in message
    assert len(message) < 2500
    difference = _not_found("header\n" + actual + "\ntail\n", wanted, source=shape)["difference"]
    # Earlier indentation differences must not hide the actual conflicting value.
    assert difference["line"] == 2 and difference["copy_line"] == 1
    assert difference["character"] == actual.index("111") + 2
    assert difference["copy_character"] == wanted.index("123") + 2
    assert (
        difference["file"]
        == actual[
            difference["file_start"] - 1 : difference["file_start"] - 1 + len(difference["file"])
        ]
    )
    assert (
        difference["copy"]
        == wanted[
            difference["copy_start"] - 1 : difference["copy_start"] - 1 + len(difference["copy"])
        ]
    )
    assert difference["truncated"] and len(difference["file"]) <= 240
    assert len(difference["copy"]) <= 240
    calls = _read_calls(message)
    assert calls == [{"path": name, "offset": "2:1201", "limit": 1}]
    continued = await registry.dispatch(
        replace(context(tmp_path), tool_name="read"), calls[0], ["apply_patch", "read"]
    )
    assert continued["ok"]
    continued_line = continued["data"]["content"].split("\n")[0]
    assert continued_line == "2:1201| " + actual[1200:]
    # The initial raw candidate plus the returned continuation are a reusable locator.
    prefix = next(line.partition("| ")[2] for line in message.split("\n") if line.startswith("2| "))
    recovered = prefix + continued_line.partition("| ")[2]
    assert recovered == actual
    corrected = await registry.dispatch(
        context(tmp_path), {"patch": update(f"@@\n-{recovered}\n+changed", name)}, ["apply_patch"]
    )
    assert corrected["data"]["status"] == "applied"
    assert path.read_bytes() == ending.join(("header", "changed", "tail", "")).encode()


@pytest.mark.parametrize("side", ["file", "copy"])
def test_missing_suffix_keeps_end_of_line_and_extra_text_visible(side):
    shorter = "shared text " * 180
    longer = shorter + "extra_value"
    actual, wanted = (longer, shorter) if side == "file" else (shorter, longer)
    difference = _not_found(actual, wanted, source="patch")["difference"]
    assert difference["character"] == len(shorter) + 1
    assert difference["copy_character"] == len(shorter) + 1
    assert difference[side].endswith("extra_value")
    assert difference["copy" if side == "file" else "file"].endswith("shared text ")


@pytest.mark.asyncio
async def test_candidate_cut_between_lines_resumes_at_next_line(tmp_path, registry):
    lines = [f"section_{index} = value_{index}" for index in range(12)]
    before = "\n".join(lines) + "\n"
    wanted = before.replace("value_10", "value_99")
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    result = await registry.dispatch(
        context(tmp_path),
        {
            "path": "file.txt",
            "old_string": wanted,
            "new_string": "changed",
            "replace_all": True,
        },
        ["apply_patch", "read"],
    )
    assert result["error"]["code"] == "text_not_found"
    assert path.read_bytes() == before.encode()
    assert "value_10" in text(result) and "value_99" in text(result)
    calls = _read_calls(text(result))
    assert calls == [{"path": "file.txt", "offset": "9:1", "limit": 4}]
    continued = await registry.dispatch(
        replace(context(tmp_path), tool_name="read"), calls[0], ["read"]
    )
    assert continued["ok"]
    assert continued["data"]["content"].split("\n")[:4] == [
        f"{number}| {lines[number - 1]}" for number in range(9, 13)
    ]


@pytest.mark.asyncio
async def test_ambiguous_long_lines_preserve_each_read_continuation(tmp_path, registry):
    line = "repeated " * 200 + "tail_value"
    before = f"{line}\nseparator\n{line}\n"
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    result = await registry.dispatch(
        context(tmp_path), {"patch": update(f"@@\n-{line}\n+changed")}, ["apply_patch", "read"]
    )
    assert result["error"]["code"] == "ambiguous_match"
    assert path.read_bytes() == before.encode()
    calls = _read_calls(text(result))
    assert calls == [
        {"path": "file.txt", "offset": "1:241", "limit": 1},
        {"path": "file.txt", "offset": "3:241", "limit": 1},
    ]
    for call in calls:
        continued = await registry.dispatch(
            replace(context(tmp_path), tool_name="read"), call, ["read"]
        )
        assert continued["ok"]
        assert continued["data"]["content"].split("\n")[0] == f"{call['offset']}| {line[240:]}"
