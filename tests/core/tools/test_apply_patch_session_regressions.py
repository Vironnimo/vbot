"""Session-derived patch shapes, with independent effects and refusal checks."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolCall, ToolExecutionConfig, ToolExecutor, ToolRegistry
from tests.core.tools.apply_patch_helpers import apply, context, text, update


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
    "arguments",
    [
        # The input object holds the path the patch names, beside the patch.
        {"input": {"path": "new.txt"}, "patch": "*** Add File: new.txt\n+hello"},
        {"input": '{"path": "new.txt"}', "patch": "*** Add File: new.txt\n+hello"},
        {"input": {"path": "new.txt", "patch": "*** Add File: new.txt\n+hello"}},
    ],
)
async def test_input_object_of_call_fields_wraps_them(tmp_path, arguments):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(context(tmp_path), arguments, ["apply_patch"])
    assert result["ok"], result
    assert (tmp_path / "new.txt").read_bytes() == b"hello\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"input": "*** Add File: other.txt\n+different"},
        {"path": "other.txt"},
        {"input": {"path": "other.txt"}},
        {"input": {"path": "new.txt", "colour": "red"}},
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


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ["placeholder", "", None])
async def test_placeholder_beside_the_patch_asks_for_nothing(tmp_path, placeholder):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    arguments = {"input": placeholder, "patch": "*** Add File: new.txt\n+hello"}
    result = await registry.dispatch(context(tmp_path), arguments, ["apply_patch"])
    assert result["ok"], result
    assert (tmp_path / "new.txt").read_bytes() == b"hello\n"


@pytest.mark.asyncio
async def test_conflicting_patch_spellings_name_both_values(tmp_path):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    arguments = {"input": "*** Add File: a.txt\n+a", "patch": "*** Add File: b.txt\n+b"}
    with pytest.raises(ValueError) as refused:
        await registry.dispatch(context(tmp_path), arguments, ["apply_patch"])
    assert str(refused.value) == (
        'Conflicting values for patch: input is "*** Add File: a.txt\\n+a" and patch is '
        '"*** Add File: b.txt\\n+b". Send only the intended one.\nNo file was changed.'
    )
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
        ("@@\n second\n@@\n first\n@@\n-value=1\n+value=2", "context_not_found"),
        # Without an @@ line or an earlier change, nothing orders the occurrences.
        ("@@\n-value=1\n+value=2", "ambiguous_match"),
        # Lines found only with copy errors never choose among occurrences.
        ("@@ first\n alpha beta gama delta\n-value=1\n+value=2", "ambiguous_match"),
    ],
)
def test_context_constraints_cannot_be_ignored_or_choose_ambiguous_target(tmp_path, body, code):
    original = (
        b"first\nmarker\nalpha beta gamma delta\nvalue=1\n"
        b"second\nmarker\nalpha beta gamma delta\nvalue=1\n"
    )
    (tmp_path / "file.txt").write_bytes(original)
    result = apply(tmp_path, update(body))
    assert result["error"]["code"] == code, result
    assert (tmp_path / "file.txt").read_bytes() == original
    if "gama" in body:
        assert text(result).startswith(
            "file.txt: the lines to replace do not match the file exactly and resemble 2 "
            "places (lines 3, 7). Copy the current lines of the one to change exactly, with "
            "enough unchanged lines around them to tell it apart."
        )
    elif code == "ambiguous_match":
        assert "the first occurrence after that line is changed." in text(result)


def test_repeated_context_line_places_lines_that_occur_once_after_it(tmp_path):
    # Session shape: the @@ line names a method that two classes define, and the
    # lines to replace follow only the second one.
    path = tmp_path / "file.py"
    path.write_bytes(
        b"class Reader:\n    def close(self):\n        self.handle.close()\n\n\n"
        b"class Pool:\n    def close(self):\n        self.pool.release()\n"
    )
    result = apply(
        tmp_path,
        update(
            "@@ def close(self):\n-        self.pool.release()\n+        self.pool.stop()",
            "file.py",
        ),
    )
    assert result["ok"], text(result)
    assert path.read_bytes() == (
        b"class Reader:\n    def close(self):\n        self.handle.close()\n\n\n"
        b"class Pool:\n    def close(self):\n        self.pool.stop()\n"
    )
    assert "occur" not in text(result)


@pytest.mark.parametrize(
    ("body", "expected", "note"),
    [
        # Session shape: a context block names the enclosing function, and the
        # function's last lines occur again in a later function.
        (
            "@@\n def second():\n@@\n     return value\n+\n+\n+def third():\n+    return 3",
            b"def first():\n    return value\n\n\ndef second():\n    return value\n\n\n"
            b"def third():\n    return 3\n\n\ndef last():\n    return value\n",
            "The lines to replace occur 2 times after 'def second():'; the first, at line 6, "
            "was changed.",
        ),
        # Session shape: identical hunks change successive occurrences in file order.
        (
            "@@\n-def first():\n+def first(value):\n@@\n-    return value\n+    return 1\n"
            "@@\n-    return value\n+    return 2",
            b"def first(value):\n    return 1\n\n\ndef second():\n    return 2\n\n\n"
            b"def last():\n    return value\n",
            "The lines to replace occur 2 times; the first after the previous change in this "
            "file, at line 6, was changed.",
        ),
    ],
)
def test_first_occurrence_after_an_at_at_line_or_previous_change_is_changed(
    tmp_path, body, expected, note
):
    path = tmp_path / "file.py"
    path.write_bytes(
        b"def first():\n    return value\n\n\ndef second():\n    return value\n\n\n"
        b"def last():\n    return value\n"
    )
    result = apply(tmp_path, update(body, "file.py"))
    assert result["ok"], result
    assert path.read_bytes() == expected
    assert text(result).endswith(f"Note: {note}")


@pytest.mark.parametrize(
    "body",
    [
        # Every occurrence comes before the previous change.
        "@@\n-end\n+END\n@@\n-x=0\n+x=1",
        # A change in an earlier Update of the same file orders nothing.
        "@@\n-start\n+START\n*** Update File: file.txt\n@@\n-x=0\n+x=1",
        # After a failed hunk, the earlier change no longer tells which one is meant.
        "@@\n-start\n+START\n@@\n-missing\n+found\n@@\n-x=0\n+x=1",
    ],
)
def test_previous_change_orders_only_later_occurrences_of_the_same_update(tmp_path, body):
    path = tmp_path / "file.txt"
    path.write_bytes(b"start\nx=0\nx=0\nend\n")
    result = apply(tmp_path, update(body))
    assert result["data"]["status"] == "partial"
    assert "the lines to replace occur 2 times (lines 2, 3)" in text(result)
    assert path.read_bytes().count(b"x=0") == 2


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
