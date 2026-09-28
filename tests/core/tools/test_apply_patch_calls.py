"""apply_patch definition and call shapes: argument repair, other harnesses' fields, refusals."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.tools.apply_patch import APPLY_PATCH_TOOL_PARAMETERS
from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_test_support import apply, call, context, registry, text


def test_the_example_in_the_patch_description_applies(tmp_path):
    tools = registry()
    assert tools.get("apply_patch").family == "files"
    definition = tools.provider_definitions(allowed_tools=["apply_patch"])[0]["parameters"]
    # Other harnesses' fields are accepted but never advertised.
    assert definition["required"] == ["patch"] and list(definition["properties"]) == ["patch"]
    description = APPLY_PATCH_TOOL_PARAMETERS["properties"]["patch"]["description"]
    example = description.partition("for example:\n")[2].partition("*** End Patch\n")[0]
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_bytes(b"def main():\n    count = 1\n    run(count)\n")
    (tmp_path / "old.txt").write_bytes(b"old\n")
    (tmp_path / "a.txt").write_bytes(b"a\n")

    result = apply(tmp_path, example + "*** End Patch", state=FileReadState())

    assert result["ok"] and result["data"]["status"] == "applied", result
    assert (tmp_path / "src/app.py").read_bytes() == b"def main():\n    count = 2\n    run(count)\n"
    assert (tmp_path / "notes.txt").read_bytes() == b"first line of a new file\n"
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "b.txt").read_bytes() == b"a\n" and not (tmp_path / "a.txt").exists()


@pytest.mark.parametrize(
    ("arguments", "summary"),
    [
        ({"patch": "*** Add File: file.txt\n+" + "x" * 1000 + "\n+y"}, "file.txt"),
        ({"file_path": "src/a.py", "old_string": "a", "new_string": "b"}, "src/a.py"),
        ({"arguments": {"input": "*** Add File: new.txt\n+x"}}, "new.txt"),
    ],
)
def test_display_names_the_file_and_hides_the_edit_text(tmp_path, arguments, summary):
    tools = registry()
    ctx = context(tmp_path)
    result = apply(tmp_path, arguments["patch"], ctx=ctx) if "patch" in arguments else None

    display = tools.display_for_call("apply_patch", arguments, result=result)

    assert display["summary"] == summary
    assert {"patch", "input", "old_string", "content"} <= set(display["hidden_argument_keys"])
    if result is not None:
        assert text(result) == "Created file.txt (2 lines)."
        facts = ctx.presentation_facts
        assert {f.get("change"): f["value"] for f in facts if f["kind"] == "line_change"} == {
            "added": 2,
            "removed": 0,
        }


PAYLOAD = '*** Add File: new.txt\n+{"input": "literal", "patch": "payload"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"input": {"input": PAYLOAD}},
        {"input": PAYLOAD, "patch": PAYLOAD},
        {"arguments": {"input": PAYLOAD}},
        {"Patch": PAYLOAD},
        # An input object holding the path the patch names, beside or around the patch.
        {"input": {"path": "new.txt"}, "patch": PAYLOAD},
        {"input": '{"path": "new.txt"}', "patch": PAYLOAD},
        {"input": {"path": "new.txt", "patch": PAYLOAD}},
        # A placeholder beside the patch asks for nothing.
        {"input": "placeholder", "patch": PAYLOAD},
        {"input": "", "patch": PAYLOAD},
        {"input": None, "patch": PAYLOAD},
    ],
)
async def test_patch_spellings_and_wrappers_apply_the_patch_as_sent(tmp_path, arguments):
    original = deepcopy(arguments)

    result = await call(tmp_path, arguments)

    assert result["ok"], result
    assert (tmp_path / "new.txt").read_bytes() == b'{"input": "literal", "patch": "payload"}\n'
    assert arguments == original


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
async def test_one_replacement_in_any_shape(tmp_path, arguments):
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")

    result = await call(tmp_path, arguments)

    assert result["data"] == {"status": "applied", "content": "Updated a.py:\n1| x = 3\n2| y = 2"}
    assert (tmp_path / "a.py").read_bytes() == b"x = 3\ny = 2\n"


@pytest.mark.asyncio
async def test_old_string_alone_beside_a_patch_is_ignored(tmp_path):
    # Session shape: the lines the patch changes, copied into old_text as well.
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")

    result = await call(
        tmp_path, {"patch": "*** Update File: a.py\n@@\n-x = 1\n+x = 3", "old_text": "x = 1"}
    )

    assert result["data"] == {
        "status": "applied",
        "content": "Updated a.py:\n1| x = 3\n2| y = 2\n"
        "old_string was ignored because patch describes the change.",
    }
    assert (tmp_path / "a.py").read_bytes() == b"x = 3\ny = 2\n"


@pytest.mark.asyncio
async def test_text_editor_insert_after_a_line(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    result = await call(
        tmp_path, {"command": "insert", "path": "a.txt", "insert_line": 1, "new_str": "inserted"}
    )
    assert text(result) == "Updated a.txt:\n1| one\n2| inserted\n3| two"


@pytest.mark.asyncio
async def test_empty_new_string_deletes_and_empty_old_string_creates(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"keep\ndrop\n")
    deleted = await call(tmp_path, {"file_path": "a.txt", "old_string": "drop\n", "new_string": ""})
    assert deleted["ok"] and path.read_bytes() == b"keep\n"
    created = await call(tmp_path, {"file_path": "new.txt", "old_string": "", "new_string": "hi\n"})
    assert text(created) == "Created new.txt (1 line)."
    refused = await call(tmp_path, {"file_path": "a.txt", "old_string": "", "new_string": "x\n"})
    assert refused["error"]["code"] == "file_exists"
    assert path.read_bytes() == b"keep\n"


@pytest.mark.asyncio
async def test_replacement_counts_follow_the_call(tmp_path):
    path = tmp_path / "a.py"
    path.write_bytes(b"x = 1\nx = 1\n")
    edit = {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"}

    ambiguous = await call(tmp_path, edit)
    message = text(ambiguous)
    assert "old_string occurs 2 times (lines 1, 2)" in message and "replace_all" in message
    assert message.endswith("Where it occurs:\n1| x = 1\n2| x = 1\nNo file was changed.")
    mismatch = await call(tmp_path, {**edit, "expected_replacements": 3})
    assert mismatch["error"]["code"] == "occurrence_mismatch"
    assert path.read_bytes() == b"x = 1\nx = 1\n"
    counted = await call(tmp_path, {**edit, "expected_replacements": 2})
    assert counted["ok"] and path.read_bytes() == b"x = 3\nx = 3\n"
    everywhere = await call(
        tmp_path,
        {"file_path": "a.py", "old_string": "x = 3", "new_string": "x = 4", "replace_all": True},
    )
    assert everywhere["ok"] and path.read_bytes() == b"x = 4\nx = 4\n"


@pytest.mark.asyncio
async def test_multi_edit_keeps_applied_edits_and_names_the_failed_one(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a\nb\n")
    result = await call(
        tmp_path,
        {
            "file_path": "a.txt",
            "edits": [{"old_string": "a", "new_string": "A"}, {"old": "zzz", "new": "B"}],
        },
    )
    assert result["data"]["status"] == "partial"
    assert "Updated a.txt:\n1| A\n2| b\nFailed: a.txt, edit 2: old_string was not found." in (
        text(result)
    )
    assert (tmp_path / "a.txt").read_bytes() == b"A\nb\n"


@pytest.mark.asyncio
async def test_write_shapes_create_replace_and_empty_files(tmp_path):
    created = await call(tmp_path, {"file_path": "new.txt", "content": "one\r\ntwo\r\n"})
    assert text(created) == "Created new.txt (2 lines)."
    assert (tmp_path / "new.txt").read_bytes() == b"one\r\ntwo\r\n"
    tools = registry()
    made = await call(
        tmp_path, {"command": "create", "path": "made.txt", "file_text": "made\n"}, tools=tools
    )
    assert made["ok"] and (tmp_path / "made.txt").read_bytes() == b"made\n"
    # Roo's write_to_file adds a line count, which requests no effect of its own.
    roo = await call(tmp_path, {"path": "roo.txt", "content": "a\nb\n", "line_count": 2})
    assert roo["ok"] and (tmp_path / "roo.txt").read_bytes() == b"a\nb\n"
    emptied = await call(tmp_path, {"file_path": "made.txt", "content": ""}, tools=tools)
    assert text(emptied) == "Replaced the content of made.txt (empty)."
    assert (tmp_path / "made.txt").read_bytes() == b""
    # Windsurf's write_to_file names an empty file with a switch.
    windsurf = await call(
        tmp_path, {"TargetFile": "w.txt", "CodeContent": "w\n", "EmptyFile": False}
    )
    assert windsurf["ok"] and (tmp_path / "w.txt").read_bytes() == b"w\n"
    blank = await call(tmp_path, {"TargetFile": "blank.txt", "EmptyFile": True})
    assert text(blank) == "Created blank.txt (empty)."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        ({}, "invalid_arguments", None),
        ({"patch": ""}, "invalid_arguments", None),
        ({"patch": 42}, "invalid_patch", None),
        ({"path": "a.txt"}, "invalid_arguments", None),
        ({"patch": "  ", "path": "a.txt"}, "invalid_arguments", None),
        ({"command": "view", "path": "a.txt"}, "invalid_arguments", 'call read(path="a.txt")'),
        (
            {"command": "undo_edit", "path": "a.txt"},
            "invalid_arguments",
            "An earlier edit cannot be undone by name. Send the reverse change as a patch.",
        ),
        (
            {
                "patch": "*** Add File: b.txt\n+x",
                "path": "a.txt",
                "old_string": "o",
                "new_string": "t",
            },
            "invalid_arguments",
            "The call gives both old_string/new_string and patch.",
        ),
        (
            {"file_path": "a.txt", "old_string": "one", "new_string": "two", "content": "x"},
            "invalid_arguments",
            "The call gives both content and old_string/new_string.",
        ),
        (
            {"patch": "*** Add File: a.txt\n+x", "path": "a.txt", "content": "x"},
            "invalid_arguments",
            None,
        ),
        (
            {"target_file": "a.txt", "code_edit": "// ... existing code ...\ntwo"},
            "invalid_arguments",
            'Send the exact lines instead: patch="*** Begin Patch\\n*** Update File: a.txt\\n@@',
        ),
        (
            {"file_path": "a.txt", "old_string": "one"},
            "invalid_arguments",
            'old_string needs new_string, the text that replaces it (new_string="" deletes',
        ),
        (
            {"mode": "replace", "patch": "*** Update File: a.txt\n@@\n-one\n+two"},
            "invalid_arguments",
            'mode "replace" does not fit the other fields.',
        ),
        (
            {"path": "a.txt", "edits": [{"oldText": "one", "newText": "two"}], "dryRun": True},
            "invalid_arguments",
            '"dryRun" is not a parameter',
        ),
        (
            {"input": "*** Add File: n.txt\n+a", "patch": "*** Add File: b.txt\n+b"},
            "invalid_arguments",
            'Conflicting values for patch: input is "*** Add File: n.txt\\n+a" and patch is '
            '"*** Add File: b.txt\\n+b".',
        ),
        (
            {"path": "b.txt", "patch": "*** Update File: a.txt\n@@\n-one\n+two"},
            "invalid_arguments",
            "path is b.txt, but the patch changes a.txt. Send only the patch, or a path that "
            "matches it.",
        ),
        *(
            ({"patch": "*** Add File: new.txt\n+hello", **extra}, "invalid_arguments", None)
            for extra in (
                {"input": "*** Add File: other.txt\n+different"},
                {"path": "other.txt"},
                {"input": {"path": "other.txt"}},
                {"input": {"path": "new.txt", "colour": "red"}},
                {"dry_run": True},
                {"arguments": {"patch": "*** Add File: other.txt\n+different"}},
            )
        ),
    ],
)
async def test_open_or_conflicting_calls_change_nothing(tmp_path, arguments, code, message):
    (tmp_path / "a.txt").write_bytes(b"one\n")

    result = await call(tmp_path, arguments)

    assert result["error"]["code"] == code, result
    # Every refusal says that nothing ran or changed.
    refusal = text(result)
    assert refusal.endswith("\nNo file was changed.") or refusal.startswith(
        "apply_patch was not run:"
    )
    if message is not None:
        assert message in text(result)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {"a.txt": b"one\n"}
