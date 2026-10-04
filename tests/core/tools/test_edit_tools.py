"""edit and write: the replacement dialect of the file edit Tools."""

from __future__ import annotations

import pytest

from core.tools import _file_changes as file_changes_module
from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.edit import (
    EDIT_TOOL_DESCRIPTION,
    EDIT_TOOL_PARAMETERS,
    WRITE_TOOL_DESCRIPTION,
    WRITE_TOOL_PARAMETERS,
    edit_dialect,
)
from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_test_support import call, registry, text


def test_edit_and_write_are_available_exactly_when_apply_patch_is() -> None:
    tools = registry()
    catalog = tools.list_tools()

    def allowed(policy: ToolAccess) -> tuple[str, ...]:
        return resolve_tool_access(policy, catalog, "off").allowed_tools

    assert allowed(ToolAccess(mode="all")) == ("apply_patch", "edit", "write")
    assert allowed(ToolAccess(mode="selected", allowed=("apply_patch",))) == (
        "apply_patch",
        "edit",
        "write",
    )
    assert allowed(ToolAccess(mode="all", denied=("apply_patch",))) == ()
    assert allowed(ToolAccess(mode="selected", allowed=("edit", "write"))) == ()
    # The user configures apply_patch; the followers never appear as a switch of their own.
    assert [tool.name for tool in tools.list_tools(include_catalog_hidden=False)] == ["apply_patch"]


def test_edit_and_write_have_minimal_definitions() -> None:
    assert EDIT_TOOL_DESCRIPTION == (
        "Replace text in a file. Put all changes to one file in one call; for several files, "
        "call edit once per file in the same response."
    )
    assert EDIT_TOOL_PARAMETERS == {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to change, relative to the working directory or absolute.",
            },
            "edits": {
                "type": "array",
                "description": "Changes, applied in order.",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "Exact text from the file. It must occur only once; "
                            "add surrounding lines until it does.",
                        },
                        "new_string": {"type": "string", "description": "Replacement text."},
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence. Omit to replace exactly one.",
                        },
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }
    assert WRITE_TOOL_DESCRIPTION == "Create a file or replace all of its content."
    assert WRITE_TOOL_PARAMETERS == {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to write, relative to the working directory or absolute.",
            },
            "content": {"type": "string", "description": "Complete file content."},
        },
        "required": ["path", "content"],
    }


@pytest.mark.parametrize(
    ("family", "dialect"),
    [
        ("gpt-5", "patch"),
        ("GPT-4.1", "patch"),
        ("gpt5-codex", "patch"),
        ("o", "patch"),
        ("o-mini", "patch"),
        ("gpt-oss", "replace"),
        ("claude", "replace"),
        ("gemini", "replace"),
        ("openai", "replace"),
        ("", "replace"),
    ],
)
def test_model_families_get_one_edit_dialect(family: str, dialect: str) -> None:
    assert edit_dialect(family) == dialect


@pytest.mark.asyncio
async def test_edits_apply_in_order_as_one_change(tmp_path) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\nx = 1\n")

    result = await call(
        tmp_path,
        {
            "path": "a.py",
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 3", "replace_all": True},
                # Sees the text the first edit left.
                {"old_string": "x = 3\ny", "new_string": "x = 3\nz"},
            ],
        },
        name="edit",
    )

    assert result["data"] == {
        "status": "applied",
        "content": "Updated a.py:\n1| x = 3\n2| z = 2\n3| x = 3",
    }
    assert (tmp_path / "a.py").read_bytes() == b"x = 3\nz = 2\nx = 3\n"


@pytest.mark.asyncio
async def test_empty_old_string_creates_a_missing_or_empty_file(tmp_path) -> None:
    (tmp_path / "empty.txt").write_bytes(b"")
    created = await call(
        tmp_path,
        {
            "path": "sub/dir/new.txt",
            "edits": [
                {"old_string": "", "new_string": "a\nb\n"},
                {"old_string": "b", "new_string": "B"},
            ],
        },
        name="edit",
    )
    filled = await call(
        tmp_path, {"path": "empty.txt", "old_string": "", "new_string": "x\n"}, name="edit"
    )

    assert text(created) == "Created sub/dir/new.txt (2 lines)."
    assert (tmp_path / "sub" / "dir" / "new.txt").read_bytes() == b"a\nB\n"
    assert filled["ok"] and (tmp_path / "empty.txt").read_bytes() == b"x\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"},
        {"filePath": "a.py", "oldString": "x = 1", "newString": "x = 3", "replaceAll": False},
        {"filename": "a.py", "old_str": "x = 1", "new_str": "x = 3"},
        {"path": "a.py", "edits": [{"oldText": "x = 1", "newText": "x = 3"}]},
        {"path": "a.py", "edits": [{"old_text": "x = 1", "new_text": "x = 3"}]},
        {"path": "a.py", "edits": '[{"old_string": "x = 1", "new_string": "x = 3"}]'},
        # An item may repeat the call's file.
        {
            "path": "a.py",
            "edits": [{"path": "./a.py", "old_string": "x = 1", "new_string": "x = 3"}],
        },
        {"edits": [{"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"}]},
    ],
)
async def test_other_edit_spellings_make_the_same_change(tmp_path, arguments) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")

    result = await call(tmp_path, arguments, name="edit")

    assert result["data"] == {"status": "applied", "content": "Updated a.py:\n1| x = 3\n2| y = 2"}


@pytest.mark.asyncio
async def test_write_creates_replaces_after_a_read_and_keeps_identical_content(tmp_path) -> None:
    state = FileReadState()
    tools = registry(state)
    path = tmp_path / "a.txt"
    path.write_bytes(b"old\n")

    created = await call(
        tmp_path, {"file_path": "deep/w.txt", "file_text": "w\r\n"}, tools=tools, name="write"
    )
    unchanged = await call(
        tmp_path, {"path": "a.txt", "contents": "old\n"}, tools=tools, name="write"
    )
    state.record_read("session-test", path)
    replaced = await call(
        tmp_path, {"path": "a.txt", "content": "new\n"}, tools=tools, name="write"
    )

    assert text(created) == "Created deep/w.txt (1 line)."
    assert (tmp_path / "deep" / "w.txt").read_bytes() == b"w\r\n"
    assert unchanged["data"] == {
        "status": "unchanged",
        "content": "a.txt already has this content. No file was changed.",
    }
    assert text(replaced) == "Replaced the content of a.txt (1 line)."
    assert path.read_bytes() == b"new\n"


FILES = {
    "a.txt": b"one\ntwo\none\n",
    "bin.dat": b"\xff\x00\x01 binary",
    "latin.txt": "caf\xe9\n".encode("latin-1"),
}
NOT_FOUND = 'No similar text is in the file; read(path="a.txt") shows its current content.'
NUL = (
    "the new text contains a NUL character (U+0000), which only binary files hold. To produce "
    "that character in source code, write its escape sequence instead, such as \\x00."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "code", "message"),
    [
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "zzz", "new_string": "z"}]},
            "text_not_found",
            f"a.txt: old_string was not found.\n{NOT_FOUND}\nNo file was changed.",
            id="not-found",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [
                    {"old_string": "two", "new_string": "2"},
                    {"old_string": "TWO", "new_string": "x"},
                ],
            },
            "text_not_found",
            f"a.txt, edit 2 of 2: old_string was not found.\n{NOT_FOUND}\n"
            "Neither edit was applied, so no file was changed. Send both edits again with edit 2 "
            "corrected.",
            id="second-of-two-not-found",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [
                    {"old_string": "two", "new_string": "one"},
                    {"old_string": "two", "new_string": "2"},
                    {"old_string": "one", "new_string": "1"},
                ],
            },
            "text_not_found",
            f"a.txt, edit 2 of 3: old_string was not found.\n{NOT_FOUND}\n"
            "None of the 3 edits were applied, so no file was changed. Send all 3 edits again "
            "with edit 2 corrected.",
            id="second-of-three-not-found",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "one", "new_string": "1"}]},
            "ambiguous_match",
            "a.txt: old_string occurs 2 times (lines 1, 3). Include more of the surrounding text "
            "so it matches once, or set replace_all to true to change every occurrence.\n"
            "Where it occurs:\n1| one\n2| two\n3| one\nNo file was changed.",
            id="ambiguous",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [
                    {"old_string": "two", "new_string": "one"},
                    {"old_string": "one", "new_string": "1"},
                ],
            },
            "ambiguous_match",
            "a.txt, edit 2 of 2: old_string occurs 3 times (lines 1, 2, 3). Include more of the "
            "surrounding text so it matches once, or set replace_all to true to change every "
            "occurrence.\nWhere it occurs:\n1| one\n2| one\n3| one\n"
            "Line numbers count the text as edit 1 left it.\n"
            "Neither edit was applied, so no file was changed. Send both edits again with edit 2 "
            "corrected.",
            id="ambiguous-after-an-earlier-edit",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "", "new_string": "x"}]},
            "file_exists",
            "a.txt: old_string is empty, which creates a file, but the file already has content. "
            "Put the current text to replace in old_string, or call write to replace the whole "
            "file.\nNo file was changed.",
            id="create-over-content",
        ),
        pytest.param(
            "edit",
            {"path": "gone.md", "edits": [{"old_string": "a", "new_string": "b"}]},
            "file_not_found",
            'File not found: gone.md. read(path=".") lists its directory.\nNo file was changed.',
            id="missing-file",
        ),
        pytest.param(
            "edit",
            {"path": "sub", "edits": [{"old_string": "a", "new_string": "b"}]},
            "not_a_file",
            "sub is not a regular file.\nNo file was changed.",
            id="folder",
        ),
        pytest.param(
            "edit",
            {"path": "bin.dat", "edits": [{"old_string": "binary", "new_string": "b"}]},
            "binary_file",
            "bin.dat is a binary file, so edit cannot change its text.\nNo file was changed.",
            id="binary",
        ),
        pytest.param(
            "edit",
            {"path": "latin.txt", "edits": [{"old_string": "caf", "new_string": "b"}]},
            "unsupported_encoding",
            "latin.txt is not UTF-8 text, so edit cannot change its text.\nNo file was changed.",
            id="encoding",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "two", "new_string": "t\x00o"}]},
            "binary_file",
            f"a.txt: {NUL}\nNo file was changed.",
            id="nul",
        ),
        pytest.param(
            "write",
            {"path": "a.txt", "content": "new\n"},
            "file_not_read",
            "a.txt already exists and this Session has not read it, so it was not replaced. Its "
            "current content follows and now counts as read: send the same call again to replace "
            "it, or change only the parts that need it.\n1| one\n2| two\n3| one\n"
            "No file was changed.",
            id="write-unread",
        ),
        pytest.param(
            "write",
            {"path": "w.txt", "content": "a\x00b"},
            "binary_file",
            f"w.txt: {NUL}\nNo file was changed.",
            id="write-nul",
        ),
        pytest.param(
            "write",
            {"path": "sub", "content": "x"},
            "not_a_file",
            "sub is not a regular file.\nNo file was changed.",
            id="write-folder",
        ),
    ],
)
async def test_failed_changes_change_nothing_and_say_what_to_send(
    tmp_path, tool, arguments, code, message
) -> None:
    for name, payload in FILES.items():
        (tmp_path / name).write_bytes(payload)
    (tmp_path / "sub").mkdir()

    result = await call(tmp_path, arguments, name=tool)

    assert (result["error"]["code"], text(result)) == (code, message)
    assert {name: (tmp_path / name).read_bytes() for name in FILES} == FILES
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*FILES, "sub"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        pytest.param(
            "edit",
            {"path": "a.txt", "content": "x"},
            "edit replaces text inside a file and takes no content. To replace the whole file, "
            "call write with path and content.",
            id="edit-with-content",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [{"path": "b.txt", "old_string": "one", "new_string": "1"}],
            },
            "edits item 1 changes b.txt, but this call changes a.txt. Call edit once per file in "
            "the same response, each with the edits of its file.",
            id="edit-another-file",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": []},
            "The call names a.txt but no change. Send edits, each with old_string, the current "
            "text, and new_string, its replacement.",
            id="edit-no-change",
        ),
        pytest.param(
            "edit",
            {"edits": [{"old_string": "one", "new_string": "1"}]},
            "The call names no file. Add path, relative to the working directory or absolute.",
            id="edit-no-path",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "old_string": "two"},
            'old_string needs new_string, the text that replaces it (new_string "" deletes '
            "old_string).",
            id="flat-without-new",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "new_string": "two"},
            'new_string needs old_string, the current text it replaces (old_string "" creates a '
            "file).",
            id="flat-without-old",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"new_string": "x"}]},
            "edits item 1 has no old_string, the current text to replace. Send old_string and "
            'new_string (old_string "" creates a file).',
            id="item-without-old",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "one"}]},
            'edits item 1 has old_string but no new_string. Send new_string, "" to delete the '
            "text.",
            id="item-without-new",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "one", "new_string": "1", "line": 2}]},
            'edits item 1 has "line", which is not a field of an edit. Each edit takes '
            "old_string, new_string and replace_all.",
            id="item-unknown-field",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "a", "new_string": "b", "oldText": "c"}]},
            "edits item 1 gives old_string twice with different values; send one.",
            id="item-conflict",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [{"old_string": "two", "new_string": "x"}],
                "old_string": "a",
                "new_string": "b",
            },
            "The call gives both edits and old_string/new_string. Put every change into edits.",
            id="edits-and-flat",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [{"old_string": "two", "new_string": "x"}],
                "replace_all": True,
            },
            "replace_all belongs to one edit. Put it into the edits item it applies to.",
            id="root-replace-all",
        ),
        pytest.param(
            "write",
            {"path": "a.txt", "old_string": "x", "new_string": "y"},
            "write replaces the whole file and has no old_string. To replace text inside a file, "
            "call edit with path and edits.",
            id="write-with-old-string",
        ),
        pytest.param(
            "write",
            {"path": "a.txt"},
            "The call names a.txt but no content. Send content, the complete text of the file "
            '("" empties it).',
            id="write-no-content",
        ),
        pytest.param(
            "write",
            {"content": "x"},
            "The call names no file. Add path, relative to the working directory or absolute.",
            id="write-no-path",
        ),
    ],
)
async def test_open_or_misdirected_calls_say_which_call_to_send(
    tmp_path, tool, arguments, message
) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")

    result = await call(tmp_path, arguments, name=tool)

    assert result["error"]["code"] == "invalid_arguments"
    assert text(result) == f"{message}\nNo file was changed."
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == b"one\ntwo\n"


@pytest.mark.asyncio
async def test_unknown_parameters_are_refused_like_any_tool_call(tmp_path) -> None:
    edit = await call(
        tmp_path,
        {"path": "a.txt", "edits": [{"old_string": "a", "new_string": "b"}], "dryRun": True},
        name="edit",
    )
    write = await call(tmp_path, {"path": "a.txt", "content": "x", "mode": "append"}, name="write")

    assert text(edit) == (
        'edit was not run:\n- "dryRun" is not a parameter.\n'
        "edit parameters: path (required), edits (required)."
    )
    assert text(write) == (
        'write was not run:\n- "mode" is not a parameter.\n'
        "write parameters: path (required), content (required)."
    )
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        (
            "edit",
            {"path": "a.txt", "old_string": "one", "new_string": "1"},
            "a.txt changed on disk while this edit ran. Read it, then send the edits it still "
            "needs.",
        ),
        (
            "write",
            {"path": "a.txt", "content": "new\n"},
            "a.txt changed on disk while this write ran. Read it before writing it again.",
        ),
    ],
)
async def test_a_file_changed_during_the_call_is_not_overwritten(
    tmp_path, monkeypatch, tool, arguments, message
) -> None:
    # The change is injected at the private planning step, which no public seam reaches.
    original = file_changes_module._plan
    path = tmp_path / "a.txt"
    if tool == "edit":
        path.write_bytes(b"one\n")

    def racing_plan(*args, **kwargs):
        plan = original(*args, **kwargs)
        path.write_bytes(b"external\n")
        return plan

    monkeypatch.setattr(file_changes_module, "_plan", racing_plan)
    result = await call(tmp_path, arguments, name=tool)

    assert (result["error"]["code"], text(result)) == (
        "file_changed",
        f"{message}\nNo file was changed.",
    )
    assert path.read_bytes() == b"external\n"
