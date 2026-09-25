"""Session-derived patch shapes, with independent effects and refusal checks."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolCall, ToolExecutionConfig, ToolExecutor, ToolRegistry
from tests.core.tools.apply_patch_helpers import apply, context, update


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["input", "duplicate", "wrapped", "formatted"])
async def test_patch_argument_repair_preserves_payload_and_input(tmp_path, shape):
    patch = '*** Add File: new.txt\n+{"input": "literal", "patch": "payload"}'
    arguments = {
        "input": {"input": patch},
        "duplicate": {"input": patch, "patch": patch},
        "wrapped": {"arguments": {"input": patch}},
        "formatted": {"Patch": patch},
    }[shape]
    original = deepcopy(arguments)
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(context(tmp_path), arguments, ["apply_patch"])
    assert result["ok"]
    assert (tmp_path / "new.txt").read_bytes() == b'{"input": "literal", "patch": "payload"}\n'
    assert arguments == original
    display = registry.display_for_call("apply_patch", arguments, result=result)
    assert display["summary"] == "new.txt"
    assert "input" in display["hidden_argument_keys"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"input": "*** Add File: other.txt\n+different"},
        {"input": "placeholder"},
        {"input": ""},
        {"input": None},
        {"path": "other.txt"},
        {"dry_run": True},
        {"arguments": {"patch": "*** Add File: other.txt\n+different"}},
    ],
)
async def test_conflicting_or_unsupported_arguments_have_no_effect(tmp_path, extra):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    results = await ToolExecutor(registry).execute_many(
        [
            ToolCall(
                id="call-test",
                name="apply_patch",
                arguments={"patch": "*** Add File: new.txt\n+hello", **extra},
            )
        ],
        ToolExecutionConfig(
            agent_id="agent-test",
            session_id="session-test",
            run_id="run-test",
            workspace=tmp_path,
            data_root=tmp_path,
            vbot_root=tmp_path,
            allowed_tools=["apply_patch"],
        ),
    )
    result = results[0]
    assert result["error"]["code"] == "invalid_arguments"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("begin", ["", "*** Begin Patch\n", "*** Begin Patch\n*** Begin Patch\n"])
def test_separate_frames_apply_in_order_without_replaying(tmp_path, begin):
    result = apply(
        tmp_path,
        "*** Begin Patch\n*** Add File: one.txt\n+first\n*** End Patch\n"
        + begin
        + "*** Update File: one.txt\n@@\n+second\n*** End Patch\n"
        "*** Move File: one.txt -> moved.txt\n*** End Patch\n"
        "*** Add File: last.txt\n+*** End Patch\n*** End Patch",
    )
    assert result["data"] == {
        "status": "applied",
        "content": "Created moved.txt (2 lines).\nCreated last.txt (1 line).",
    }
    assert not (tmp_path / "one.txt").exists()
    assert (tmp_path / "moved.txt").read_bytes() == b"first\nsecond\n"
    assert (tmp_path / "last.txt").read_bytes() == b"*** End Patch\n"


@pytest.mark.parametrize(
    "tail",
    [
        "+stray",
        "explanation",
        "@@\n-old\n+new",
        "*** Move to: elsewhere.txt",
        "*** Begin Patch\n+stray",
        "*** Add File: next.txt\n+good\n*** Unknown File: last.txt",
    ],
)
def test_bad_later_frame_rejects_entire_call_before_writing(tmp_path, tail):
    result = apply(tmp_path, "*** Add File: one.txt\n+hello\n*** End Patch\n" + tail)
    assert result["error"]["code"] == "invalid_patch"
    assert list(tmp_path.iterdir()) == []


def test_identical_empty_update_header_is_redundant_not_a_second_edit(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = apply(tmp_path, update("*** Update File: file.txt\n@@\n-old\n+new"))
    assert result["data"] == {"status": "applied", "content": "Updated file.txt:\n1| new"}
    assert (tmp_path / "file.txt").read_bytes() == b"new\n"
    result = apply(tmp_path, update("*** Update File: different.txt\n@@\n-new\n+bad"))
    assert result["error"]["code"] == "invalid_patch"
    assert (tmp_path / "file.txt").read_bytes() == b"new\n"


@pytest.mark.parametrize("anchor", [" second", " second\n   details"])
def test_context_block_constrains_later_edit(tmp_path, anchor):
    (tmp_path / "file.txt").write_bytes(b"first\nvalue=1\nsecond\n  details\nvalue=1\n")
    result = apply(tmp_path, update(f"@@\n{anchor}\n@@\n-value=1\n+value=2"))
    assert result["data"] == {
        "status": "applied",
        "content": "Updated file.txt:\n4|   details\n5| value=2",
    }
    assert (tmp_path / "file.txt").read_bytes() == b"first\nvalue=1\nsecond\n  details\nvalue=2\n"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("@@\n missing\n@@\n-value=1\n+value=2", "context_not_found"),
        ("@@\n marker\n@@\n-value=1\n+value=2", "ambiguous_context"),
        ("@@\n first\n@@\n-value=1\n+value=2", "ambiguous_match"),
        ("@@\n second\n@@\n first\n@@\n-value=1\n+value=2", "context_not_found"),
    ],
)
def test_context_constraints_cannot_be_ignored_or_choose_ambiguous_target(tmp_path, body, code):
    original = b"first\nmarker\nvalue=1\nsecond\nmarker\nvalue=1\n"
    (tmp_path / "file.txt").write_bytes(original)
    result = apply(tmp_path, update(body))
    assert result["error"]["code"] == code
    assert (tmp_path / "file.txt").read_bytes() == original


def test_context_anchor_does_not_leak_to_next_file_or_edit(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"before=1\nsection\nafter=1\n")
    (tmp_path / "other.txt").write_bytes(b"old\n")
    result = apply(
        tmp_path,
        update(
            "@@\n section\n@@\n-after=1\n+after=2\n@@\n-before=1\n+before=2\n"
            "*** Update File: other.txt\n@@\n-old\n+new"
        ),
    )
    assert result["ok"]
    assert (tmp_path / "file.txt").read_bytes() == b"before=2\nsection\nafter=2\n"
    assert (tmp_path / "other.txt").read_bytes() == b"new\n"
