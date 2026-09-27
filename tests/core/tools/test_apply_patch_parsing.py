"""apply_patch patch format: framing, operation headers, Add bodies and moves."""

from __future__ import annotations

import pytest

from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_helpers import apply, text, update


@pytest.mark.parametrize(
    ("wrapper", "markers", "crlf"),
    [
        ("```diff\n{patch}\n```", True, False),
        ("```patch\n{patch}\n```", False, True),
        ("{patch}", False, False),
        ("{patch}", True, True),
    ],
)
def test_tolerates_patch_fences_missing_markers_and_crlf(tmp_path, wrapper, markers, crlf):
    (tmp_path / "file.txt").write_bytes(b"alpha\nold\nomega\n")
    patch = update("@@\nalpha\n-old\n+new\nomega")
    if not markers:
        patch = patch.removeprefix("*** Begin Patch\n").removesuffix("\n*** End Patch")
    patch = wrapper.format(patch=patch)
    if crlf:
        patch = patch.replace("\n", "\r\n")
    assert apply(tmp_path, patch)["ok"]
    assert (tmp_path / "file.txt").read_bytes() == b"alpha\nnew\nomega\n"


def test_markers_inside_content_and_long_files_are_literal(tmp_path):
    path = tmp_path / "file.txt"
    prefix = "x" * 12000 + "\n" + "".join(f"unchanged {number}\n" for number in range(2200))
    path.write_text(prefix + "*** End Patch\nold\n", encoding="utf-8")
    assert apply(tmp_path, update("@@\n *** End Patch\n-old\n+*** Begin Patch"))["ok"]
    assert path.read_text(encoding="utf-8") == prefix + "*** End Patch\n*** Begin Patch\n"


@pytest.mark.parametrize(
    "separator",
    [
        "*** End Patch\n",
        "*** End Patch\n*** Begin Patch\n",
        "*** End Patch\n*** Begin Patch\n*** Begin Patch\n",
        # A Begin marker cannot be file content (no leading +), so it only starts a new frame.
        "*** Begin Patch\n",
    ],
)
def test_separate_frames_apply_in_order_without_replaying(tmp_path, separator):
    result = apply(
        tmp_path,
        "*** Begin Patch\n*** Begin Patch\n*** Add File: one.txt\n+first\n"
        + separator
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


def test_identical_empty_update_header_is_redundant_not_a_second_edit(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = apply(tmp_path, update("*** Update File: file.txt\n@@\n-old\n+new"))
    assert result["data"] == {"status": "applied", "content": "Updated file.txt:\n1| new"}
    assert (tmp_path / "file.txt").read_bytes() == b"new\n"


FRAME = "*** Add File: one.txt\n+hello\n*** End Patch\n"
TWO_ADDS = "*** Add File: first.txt\n+good\n*** Add File: last.txt\n"


@pytest.mark.parametrize(
    "patch",
    [
        # A bad later frame rejects the frames before it too.
        FRAME + "+stray",
        FRAME + "explanation",
        FRAME + "@@\n-old\n+new",
        FRAME + "*** Move to: elsewhere.txt",
        FRAME + "*** Begin Patch\n+stray",
        FRAME + "*** Add File: next.txt\n+good\n*** Unknown File: last.txt",
        # Unknown headers and Updates without hunks.
        "*** Add File: good.txt\n+valid\n*** Update File: missing.txt",
        # An Add body is never reinterpreted as hunks or headers.
        TWO_ADDS + "@@ section\n+new",
        TWO_ADDS + "+first\n@@\n+last",
        TWO_ADDS + "*** Unknown File: other.txt",
        # A no-newline marker before the end of the file, and another file's empty header.
        update("@@\n-old\n+new\n\\ No newline at end of file"),
        update("*** Update File: different.txt\n@@\n-old\n+bad"),
    ],
)
def test_malformed_patches_reject_the_whole_call_before_writing(tmp_path, patch):
    (tmp_path / "file.txt").write_bytes(b"old\ntail\n")

    result = apply(tmp_path, patch)

    assert result["error"]["code"] == "invalid_patch"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {"file.txt": b"old\ntail\n"}


@pytest.mark.parametrize(
    ("existing", "body", "expected"),
    [
        (None, '{\n  "enabled": true\n}', b'{\n  "enabled": true\n}\n'),
        (
            None,
            "+/**\n * explanation\n+ */\nexport interface Source {\n+  next(): number;\n+}",
            b"/**\n * explanation\n */\nexport interface Source {\n  next(): number;\n}\n",
        ),
        (None, "+first\n\n  indented\n+last", b"first\n\n  indented\nlast\n"),
        (None, "@@\n+first\n+last", b"first\nlast\n"),
        (
            None,
            "+-literal\n+@@\n+*** End Patch\n++literal",
            b"-literal\n@@\n*** End Patch\n+literal\n",
        ),
        (None, "+1| first\n+2| second", b"first\nsecond\n"),
        # Underlines, SQL comments and list items written without their + in a new file.
        (
            None,
            "+Title\n-----\n+text\n-- a comment\n- item",
            b"Title\n-----\ntext\n-- a comment\n- item\n",
        ),
        (
            b"",
            "+Title\n-----\n+text\n-- a comment\n- item",
            b"Title\n-----\ntext\n-- a comment\n- item\n",
        ),
    ],
)
def test_add_repairs_only_missing_syntax_preserving_content(tmp_path, existing, body, expected):
    if existing is not None:
        (tmp_path / "new.txt").write_bytes(existing)
    result = apply(tmp_path, f"*** Add File: new.txt\n{body}\n*** End Patch")
    assert result["ok"], result
    assert (tmp_path / "new.txt").read_bytes() == expected


def test_add_refuses_minus_lines_where_it_replaces_a_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"keep\nold\n")
    state = FileReadState()
    state.record_read("session-test", path)

    result = apply(
        tmp_path, "*** Add File: notes.txt\n+keep\n-old\n+new\n-- a comment", state=state
    )

    assert result["error"]["code"] == "invalid_patch"
    message = text(result)
    assert message.startswith("notes.txt: patch line 3 in the *** Add File body starts with -")
    assert "use *** Update File" in message and "start every line of the new content with +" in (
        message
    )
    assert message.endswith("\nNo file was changed.")
    assert path.read_bytes() == b"keep\nold\n"


def test_unified_diff_file_operations(tmp_path):
    (tmp_path / "gone.txt").write_bytes(b"bye\n")
    (tmp_path / "a.txt").write_bytes(b"one\n")
    patch = (
        "diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+x\n+y\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    result = apply(tmp_path, patch)
    assert text(result) == "Moved a.txt to b.txt.\nCreated new.txt (2 lines).\nDeleted gone.txt."
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.txt", "new.txt"]


@pytest.mark.parametrize("positions", [(1,), (2,), (0, 2)])
def test_move_metadata_position_preserves_the_operation(tmp_path, positions):
    (tmp_path / "file.txt").write_bytes(b"old\nsecond\n")
    parts = ["@@\n-old\n+new", "@@\n-second\n+last"]
    for position in reversed(positions):
        parts.insert(position, "*** Move to: moved.txt")
    result = apply(tmp_path, update("\n".join(parts)))
    assert result["data"] == {
        "status": "applied",
        "content": "Updated file.txt and moved it to moved.txt:\n1| new\n2| last",
    }
    assert not (tmp_path / "file.txt").exists()
    assert (tmp_path / "moved.txt").read_bytes() == b"new\nlast\n"


@pytest.mark.parametrize(
    "operation",
    [
        "*** Update File: file.txt\n*** Move to: first.txt\n@@\n-old\n+new\n"
        "*** Move to: second.txt",
        "*** Move File: file.txt -> first.txt\n*** Move to: second.txt",
    ],
)
def test_conflicting_moves_reject_before_any_effect(tmp_path, operation):
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = apply(tmp_path, "*** Add File: unrelated.txt\n+pending\n" + operation)
    assert result["error"]["code"] == "conflicting_move"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {"file.txt": b"old\n"}


def test_move_shaped_added_content_remains_literal(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = apply(tmp_path, update("@@\n-old\n+*** Move to: literal.txt\n*** Move to: moved.txt"))
    assert result["data"]["status"] == "applied"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        "moved.txt": b"*** Move to: literal.txt\n"
    }


def test_move_with_explicit_hunks_uses_update_move_semantics(tmp_path):
    source = tmp_path / "one.txt"
    source.write_bytes(b"old\n")
    result = apply(tmp_path, "*** Move File: one.txt -> moved.txt\n@@\n-old\n+new")
    assert text(result) == "Updated one.txt and moved it to moved.txt:\n1| new"
    assert not source.exists()
    assert (tmp_path / "moved.txt").read_bytes() == b"new\n"
    source.write_bytes(b"source\n")
    result = apply(tmp_path, "*** Move File: one.txt -> other.txt\n@@\n-missing\n+unused")
    assert not result["ok"]
    assert source.read_bytes() == b"source\n"
    assert not (tmp_path / "other.txt").exists()
