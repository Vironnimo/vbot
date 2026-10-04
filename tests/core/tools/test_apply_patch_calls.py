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


def test_display_carries_each_changed_files_diff(tmp_path):
    (tmp_path / "app.txt").write_bytes(b"".join(b"line %d\n" % n for n in range(1, 11)))
    (tmp_path / "gone.txt").write_bytes(b"a\nb\n")
    (tmp_path / "old.txt").write_bytes(b"same\n")
    ctx = context(tmp_path)
    patch = (
        "*** Begin Patch\n*** Update File: app.txt\n@@\n line 5\n-line 6\n+line six\n line 7\n"
        "*** Add File: new.txt\n+first\n+second\n*** Delete File: gone.txt\n"
        "*** Move File: old.txt -> moved.txt\n*** End Patch"
    )

    result = apply(tmp_path, patch, ctx=ctx)
    display = registry().display_for_call("apply_patch", {"patch": patch}, context=ctx)

    assert result["ok"], result
    [changes] = display["details"]
    assert changes["type"] == "file_changes"
    assert changes["files"] == [
        {
            "path": "app.txt",
            "change": "updated",
            "added": 1,
            "removed": 1,
            "hunks": [
                {
                    "old_start": 3,
                    "new_start": 3,
                    "lines": [
                        " line 3",
                        " line 4",
                        " line 5",
                        "-line 6",
                        "+line six",
                        " line 7",
                        " line 8",
                        " line 9",
                    ],
                }
            ],
        },
        {
            "path": "new.txt",
            "change": "created",
            "added": 2,
            "removed": 0,
            "hunks": [{"old_start": 1, "new_start": 1, "lines": ["+first", "+second"]}],
        },
        {
            "path": "gone.txt",
            "change": "deleted",
            "added": 0,
            "removed": 2,
            "hunks": [{"old_start": 1, "new_start": 1, "lines": ["-a", "-b"]}],
        },
        {
            "path": "old.txt",
            "change": "moved",
            "destination": "moved.txt",
            "added": 0,
            "removed": 0,
            "hunks": [],
        },
    ]
    facts = {f["change"]: f["value"] for f in display["facts"] if f["kind"] == "line_change"}
    assert facts == {"added": 3, "removed": 3}


def test_display_diffs_share_one_line_budget_but_count_every_change(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tools._tool_context.MAX_DISPLAY_DIFF_LINES", 3)
    ctx = context(tmp_path)
    patch = (
        "*** Begin Patch\n*** Add File: a.txt\n+1\n+2\n*** Add File: b.txt\n+3\n+4\n*** End Patch"
    )

    apply(tmp_path, patch, ctx=ctx)

    [block] = ctx.presentation_details
    first, second = block["files"]
    assert first["hunks"][0]["lines"] == ["+1", "+2"] and "omitted_lines" not in first
    assert second["hunks"][0]["lines"] == ["+3"] and second["omitted_lines"] == 1
    assert second["added"] == 2


def test_display_notices_name_each_failed_change_without_the_recovery_text(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    ctx = context(tmp_path)
    patch = (
        "*** Begin Patch\n*** Update File: a.txt\n@@\n-one\n+ONE\n@@\n-missing\n+x\n*** End Patch"
    )

    result = apply(tmp_path, patch, ctx=ctx)
    display = registry().display_for_call(
        "apply_patch", {"patch": patch}, context=ctx, result=result
    )

    assert result["data"]["status"] == "partial"
    changes, notice = display["details"]
    assert [change["path"] for change in changes["files"]] == ["a.txt"]
    assert notice["level"] == "error" and "subject" not in notice
    assert notice["text"].startswith("a.txt, hunk 2: ")
    assert notice["text"] in result["data"]["content"]
    assert "1| ONE" not in notice["text"]


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
        {"path": "a.py", "old_str": "x = 1", "new_str": "x = 3"},  # text-editor fields
        {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3", "explanation": "fix"},
        {"file_path": "a.py", "edits": [{"old_string": "x = 1", "new_string": "x = 3"}]},
        {"patch": "*** Begin Patch\n*** Edit File: a.py\n@@\n-x = 1\n+x = 3\n*** End Patch"},
        {"path": "a.py", "patch": "@@\n-x = 1\n+x = 3"},
        {"path": "a.py", "search": "x = 1", "replace": "x = 3"},
        # The MCP filesystem server's edit_file; its preview switch is off.
        {"path": "a.py", "edits": [{"oldText": "x = 1", "newText": "x = 3"}], "dryRun": False},
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
    assert path.read_bytes() == b"x = 1\nx = 1\n"
    everywhere = await call(tmp_path, {**edit, "replace_all": True})
    assert everywhere["ok"] and path.read_bytes() == b"x = 3\nx = 3\n"


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
    made = await call(tmp_path, {"path": "made.txt", "file_text": "made\n"}, tools=tools)
    assert made["ok"] and (tmp_path / "made.txt").read_bytes() == b"made\n"
    emptied = await call(tmp_path, {"file_path": "made.txt", "content": ""}, tools=tools)
    assert text(emptied) == "Replaced the content of made.txt (empty)."
    assert (tmp_path / "made.txt").read_bytes() == b""
    windsurf = await call(tmp_path, {"TargetFile": "w.txt", "CodeContent": "w\n"})
    assert windsurf["ok"] and (tmp_path / "w.txt").read_bytes() == b"w\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        ({}, "invalid_arguments", None),
        ({"patch": ""}, "invalid_arguments", None),
        ({"patch": 42}, "invalid_patch", None),
        ({"path": "a.txt"}, "invalid_arguments", None),
        ({"patch": "  ", "path": "a.txt"}, "invalid_arguments", None),
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
        # Session shape: the lines the patch changes, copied into old_text as well.
        (
            {"patch": "*** Update File: a.txt\n@@\n-one\n+two", "old_text": "one"},
            "invalid_arguments",
            "The call gives both old_string and patch. Send one of them; for several changes, "
            "put them all in one patch.",
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
            {"file_path": "a.txt", "old_string": "one"},
            "invalid_arguments",
            'old_string needs new_string, the text that replaces it (new_string="" deletes',
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
