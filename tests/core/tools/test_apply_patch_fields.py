"""apply_patch calls in other harnesses' shapes, through production dispatch."""

from __future__ import annotations

import pytest

from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry
from tests.core.tools.apply_patch_helpers import context, text


@pytest.fixture
def run(tmp_path):
    state = FileReadState()
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=state)

    async def dispatch(arguments, **files):
        for name, content in files.items():
            (tmp_path / name).write_bytes(content.encode())
        return await registry.dispatch(context(tmp_path), arguments, ["apply_patch"])

    dispatch.state = state
    dispatch.root = tmp_path
    return dispatch


def content_of(run, name):
    return (run.root / name).read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"},  # Claude Code Edit
        {"filePath": "a.py", "oldString": "x = 1", "newString": "x = 3"},  # opencode edit
        {"command": "str_replace", "path": "a.py", "old_str": "x = 1", "new_str": "x = 3"},
        {"mode": "replace", "path": "a.py", "old_string": "x = 1", "new_string": "x = 3"},
        {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3", "explanation": "fix"},
        {"file_path": "a.py", "edits": [{"old_string": "x = 1", "new_string": "x = 3"}]},
        {"path": "a.py", "diff": "<<<<<<< SEARCH\nx = 1\n=======\nx = 3\n>>>>>>> REPLACE"},
        {"path": "a.py", "diff": "------- SEARCH\nx = 1\n=======\nx = 3\n+++++++ REPLACE"},
        {
            "path": "a.py",
            "diff": "<<<<<<< SEARCH\n:start_line:1\n-------\nx = 1\n=======\nx = 3"
            "\n>>>>>>> REPLACE",
        },
        {"patch": "a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 3\n>>>>>>> REPLACE"},
        {"patch": "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 3\n y = 2\n"},
        {"patch": "*** Begin Patch\n*** Edit File: a.py\n@@\n-x = 1\n+x = 3\n*** End Patch"},
        {"path": "a.py", "patch": "@@\n-x = 1\n+x = 3"},
        {"mode": "patch", "patch": "*** Update File: a.py\n@@\n-x = 1\n+x = 3"},
        # Switches that are off ask for nothing extra.
        {"path": "a.py", "search": "x = 1", "replace": "x = 3", "use_regex": False},
        {"path": "a.py", "edits": [{"oldText": "x = 1", "newText": "x = 3"}], "dryRun": False},
        {
            "TargetFile": "a.py",
            "ReplacementChunks": [
                {"AllowMultiple": False, "TargetContent": "x = 1", "ReplacementContent": "x = 3"}
            ],
            "Instruction": "Bump x.",
        },
        # A header repeated before Begin Patch names the same single change.
        {
            "patch": "*** Update File: a.py\n*** Begin Patch\n*** Update File: a.py\n@@\n-x = 1\n"
            "+x = 3\n*** End Patch"
        },
    ],
)
async def test_one_replacement_in_any_shape(run, arguments):
    result = await run(arguments, **{"a.py": "x = 1\ny = 2\n"})
    assert result["data"] == {"status": "applied", "content": "Updated a.py:\n1| x = 3\n2| y = 2"}
    assert content_of(run, "a.py") == b"x = 3\ny = 2\n"


@pytest.mark.asyncio
async def test_empty_new_string_deletes_and_empty_old_string_creates(run):
    result = await run(
        {"file_path": "a.txt", "old_string": "drop\n", "new_string": ""},
        **{"a.txt": "keep\ndrop\n"},
    )
    assert result["ok"] and content_of(run, "a.txt") == b"keep\n"
    created = await run({"file_path": "new.txt", "old_string": "", "new_string": "hello\n"})
    assert text(created) == "Created new.txt (1 line)."
    refused = await run({"file_path": "a.txt", "old_string": "", "new_string": "other\n"})
    assert refused["error"]["code"] == "file_exists"
    assert content_of(run, "a.txt") == b"keep\n"


@pytest.mark.asyncio
async def test_replacement_counts_follow_the_call(run):
    files = {"a.py": "x = 1\nx = 1\n"}
    ambiguous = await run(
        {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"}, **files
    )
    assert ambiguous["error"]["message"] == (
        "a.py: old_string occurs 2 times (lines 1, 2). Include more of the surrounding text so "
        "it matches once, or set replace_all to true to change every occurrence.\n"
        "Where it occurs:\n1| x = 1\n2| x = 1\nNo file was changed."
    )
    mismatch = await run(
        {
            "file_path": "a.py",
            "old_string": "x = 1",
            "new_string": "x = 3",
            "expected_replacements": 3,
        }
    )
    assert mismatch["error"]["code"] == "occurrence_mismatch"
    assert content_of(run, "a.py") == b"x = 1\nx = 1\n"
    counted = await run(
        {
            "file_path": "a.py",
            "old_string": "x = 1",
            "new_string": "x = 3",
            "expected_replacements": 2,
        }
    )
    assert counted["ok"] and content_of(run, "a.py") == b"x = 3\nx = 3\n"
    everywhere = await run(
        {"file_path": "a.py", "old_string": "x = 3", "new_string": "x = 4", "replace_all": True}
    )
    assert everywhere["ok"] and content_of(run, "a.py") == b"x = 4\nx = 4\n"


@pytest.mark.asyncio
async def test_missing_old_string_shows_closest_text_and_existing_new_text(run):
    result = await run(
        {"file_path": "a.py", "old_string": "def f():\n    return 2", "new_string": "x"},
        **{"a.py": "def f():\n    return 1\n"},
    )
    assert result["error"]["message"] == (
        "a.py: old_string was not found.\nThe closest text in the file, lines 1-2:\n"
        "1| def f():\n2|     return 1\nFirst difference, line 2: the file has '    return 1' "
        "where the patch has '    return 2'.\nNo file was changed."
    )
    done = await run(
        {"file_path": "b.py", "old_string": "x = 1", "new_string": "x = 3"}, **{"b.py": "x = 3\n"}
    )
    assert (
        "The new text already occurs at line 1; if this change was made earlier, nothing more "
        "is needed."
    ) in done["error"]["message"]


@pytest.mark.asyncio
async def test_multi_edit_keeps_applied_edits_and_names_the_failed_one(run):
    result = await run(
        {
            "file_path": "a.txt",
            "edits": [{"old_string": "a", "new_string": "A"}, {"old": "zzz", "new": "B"}],
        },
        **{"a.txt": "a\nb\n"},
    )
    assert result["data"]["status"] == "partial"
    assert "Updated a.txt:\n1| A\n2| b\nFailed: a.txt, edit 2: old_string was not found." in (
        text(result)
    )
    assert content_of(run, "a.txt") == b"A\nb\n"


@pytest.mark.asyncio
async def test_write_shapes_create_replace_and_empty_files(run):
    created = await run({"file_path": "new.txt", "content": "one\r\ntwo\r\n"})
    assert text(created) == "Created new.txt (2 lines)."
    assert content_of(run, "new.txt") == b"one\r\ntwo\r\n"
    made = await run({"command": "create", "path": "made.txt", "file_text": "made\n"})
    assert made["ok"] and content_of(run, "made.txt") == b"made\n"
    # Roo's write_to_file adds a line count, which requests no effect of its own.
    roo = await run({"path": "roo.txt", "content": "a\nb\n", "line_count": 2})
    assert roo["ok"] and content_of(run, "roo.txt") == b"a\nb\n"
    emptied = await run({"file_path": "made.txt", "content": ""})
    assert text(emptied) == "Replaced the content of made.txt (empty)."
    assert content_of(run, "made.txt") == b""
    # Windsurf's write_to_file names an empty file with a switch.
    windsurf = await run({"TargetFile": "w.txt", "CodeContent": "w\n", "EmptyFile": False})
    assert windsurf["ok"] and content_of(run, "w.txt") == b"w\n"
    blank = await run({"TargetFile": "blank.txt", "EmptyFile": True})
    assert text(blank) == "Created blank.txt (empty)."


@pytest.mark.asyncio
async def test_unread_file_is_shown_before_a_full_replacement(run):
    arguments = {"file_path": "old.txt", "content": "new\n"}
    refused = await run(arguments, **{"old.txt": "keep\n"})
    assert refused["error"] == {
        "code": "file_not_read",
        "message": "old.txt already exists and this Session has not read it, so it was not "
        "replaced. Its current content follows and now counts as read: send the same call "
        "again to replace it, or change only the parts that need it.\n1| keep\n"
        "No file was changed.",
    }
    assert content_of(run, "old.txt") == b"keep\n"
    replaced = await run(arguments)
    assert text(replaced) == "Replaced the content of old.txt (1 line)."


@pytest.mark.asyncio
async def test_text_editor_insert_after_a_line(run):
    result = await run(
        {"command": "insert", "path": "a.txt", "insert_line": 1, "new_str": "inserted"},
        **{"a.txt": "one\ntwo\n"},
    )
    assert text(result) == "Updated a.txt:\n1| one\n2| inserted\n3| two"


@pytest.mark.asyncio
async def test_unified_diff_file_operations(run):
    (run.root / "gone.txt").write_bytes(b"bye\n")
    (run.root / "a.txt").write_bytes(b"one\n")
    patch = (
        "diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+x\n+y\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    result = await run({"patch": patch})
    assert text(result) == "Moved a.txt to b.txt.\nCreated new.txt (2 lines).\nDeleted gone.txt."
    assert sorted(p.name for p in run.root.iterdir()) == ["b.txt", "new.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"command": "view", "path": "a.txt"},
            'To view a file, call read(path="a.txt").',
        ),
        (
            {"command": "undo_edit", "path": "a.txt"},
            "An earlier edit cannot be undone by name. Send the reverse change as a patch.",
        ),
        (
            {
                "patch": "*** Add File: b.txt\n+x",
                "path": "a.txt",
                "old_string": "one",
                "new_string": "two",
            },
            "The call gives both old_string/new_string and patch.",
        ),
        (
            {"file_path": "a.txt", "old_string": "one", "new_string": "two", "content": "x"},
            "The call gives both content and old_string/new_string.",
        ),
        (
            {"target_file": "a.txt", "code_edit": "// ... existing code ...\ntwo"},
            "code_edit cannot be applied: it marks unchanged code with placeholder comments",
        ),
        (
            {"file_path": "a.txt", "old_string": "one"},
            'old_string needs new_string, the text that replaces it (new_string="" deletes',
        ),
        (
            {"mode": "replace", "patch": "*** Update File: a.txt\n@@\n-one\n+two"},
            'mode "replace" does not fit the other fields.',
        ),
    ],
)
async def test_open_or_conflicting_shapes_fail_before_any_change(run, arguments, message):
    with pytest.raises(ValueError, match=None) as raised:
        await run(arguments, **{"a.txt": "one\n"})
    assert message in str(raised.value)
    assert sorted(p.name for p in run.root.iterdir()) == ["a.txt"]
    assert content_of(run, "a.txt") == b"one\n"


@pytest.mark.asyncio
async def test_a_switch_that_asks_for_a_dry_run_writes_nothing(run):
    with pytest.raises(ValueError, match='"dryRun" is not a parameter'):
        await run(
            {"path": "a.txt", "edits": [{"oldText": "one", "newText": "two"}], "dryRun": True},
            **{"a.txt": "one\n"},
        )
    assert content_of(run, "a.txt") == b"one\n"


@pytest.mark.asyncio
async def test_path_that_contradicts_the_patch_changes_nothing(run):
    result = await run(
        {"path": "b.txt", "patch": "*** Update File: a.txt\n@@\n-one\n+two"}, **{"a.txt": "one\n"}
    )
    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "path is b.txt, but the patch changes a.txt. Send only the patch, or a path "
        "that matches it.\nNo file was changed.",
    }
    assert content_of(run, "a.txt") == b"one\n"


def test_display_names_the_file_of_an_edit_shape():
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    display = registry.display_for_call(
        "apply_patch", {"file_path": "src/a.py", "old_string": "a", "new_string": "b"}
    )
    assert display["summary"] == "src/a.py"
