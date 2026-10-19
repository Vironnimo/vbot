"""edit and write: the replacement dialect of the file edit Tools."""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

from core.tools import _file_changes as file_changes_module
from core.tools._patch_report import edit_outcome
from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.edit import (
    EDIT_TOOL_DESCRIPTION,
    EDIT_TOOL_PARAMETERS,
    WRITE_TOOL_DESCRIPTION,
    WRITE_TOOL_PARAMETERS,
    edit_dialect,
)
from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_test_support import call, context, registry, text


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
        "Replace text in a file. Calls that change different files never need each other "
        "finished first; send them in the same response."
    )
    assert EDIT_TOOL_PARAMETERS == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "description": "All changes to this file.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "Text to replace. Without replace_all, it must "
                            "occur exactly once.",
                        },
                        "new_string": {"type": "string"},
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence. Omit to replace one.",
                        },
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }
    assert WRITE_TOOL_DESCRIPTION == (
        "Create a file or replace all of its content. To replace an existing file, read it first."
    )
    assert WRITE_TOOL_PARAMETERS == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
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
@pytest.mark.parametrize("multiple_files", [False, True])
async def test_edits_apply_in_order_as_one_change(tmp_path, monkeypatch, multiple_files) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\nx = 1\n")
    edits: list[dict[str, Any]] = [
        {"old_string": "x = 1", "new_string": "x = 3", "replace_all": True},
        # Sees the text the first edit left.
        {"old_string": "x = 3\ny", "new_string": "x = 3\nz"},
    ]
    arguments = {"path": "a.py", "edits": edits}
    state = FileReadState()
    locked = set()
    lock = state.lock_path

    @contextmanager
    def observed_lock(path):
        with lock(path):
            locked.add(path)
            try:
                yield
            finally:
                locked.remove(path)

    written = []
    write = file_changes_module.atomic_write_bytes

    def observed_write(path, payload, **kwargs):
        if multiple_files:
            assert locked == {tmp_path / "a.py", tmp_path / "b.txt"}
        written.append(path.name)
        return write(path, payload, **kwargs)

    monkeypatch.setattr(state, "lock_path", observed_lock)
    monkeypatch.setattr(file_changes_module, "atomic_write_bytes", observed_write)
    if multiple_files:
        (tmp_path / "b.txt").write_bytes(b"second\n")
        for edit in edits:
            edit["path"] = "a.py"
        edits.insert(1, {"path": "b.txt", "old_string": "second", "new_string": "changed"})
        arguments = {"edits": edits}

    result = await call(tmp_path, arguments, tools=registry(state), name="edit")

    assert result["ok"]
    if multiple_files:
        assert (tmp_path / "b.txt").read_bytes() == b"changed\n"
        assert written == ["a.py", "b.txt", "a.py"]
        assert re.findall(r"^Edit (\d+) of 3", text(result), re.M) == ["1", "2", "3"]
    else:
        assert written == ["a.py"]
    assert not locked
    assert (tmp_path / "a.py").read_bytes() == b"x = 3\nz = 2\nx = 3\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("items", "expected", "applied", "failed"),
    [
        pytest.param(
            [("a.txt", "one", "1"), ("missing.txt", "x", "y"), ("a.txt", "1", "first")],
            {"a.txt": b"first\n", "b.txt": b"two\n"},
            2,
            1,
            id="missing-file-does-not-block-other-target",
        ),
        pytest.param(
            [("a.txt", "one", "1"), {"path": "b.txt", "new_string": "2"}, ("a.txt", "1", "first")],
            {"a.txt": b"first\n", "b.txt": b"two\n"},
            2,
            1,
            id="refused-content-keeps-its-target-and-index",
        ),
        pytest.param(
            [("a.txt", "one", "one"), ("b.txt", "two", "two")],
            {"a.txt": b"one\n", "b.txt": b"two\n"},
            0,
            0,
            id="all-no-op",
        ),
        pytest.param(
            [("a.txt", "absent", "x"), ("b.txt", "missing", "y")],
            {"a.txt": b"one\n", "b.txt": b"two\n"},
            0,
            2,
            id="all-failed",
        ),
        pytest.param(
            [("a.txt", "absent", "x"), ("b.txt", "two", "two")],
            {"a.txt": b"one\n", "b.txt": b"two\n"},
            0,
            1,
            id="failure-with-no-op-is-not-applied",
        ),
        pytest.param(
            [("a.txt", "one", "1"), ("b.txt", "two", "2"), ("a.txt", "1", "one")],
            {"a.txt": b"one\n", "b.txt": b"2\n"},
            3,
            0,
            id="applied-edits-with-cancelled-net-effect",
        ),
        pytest.param(
            [("new.txt", "", "start\n"), ("a.txt", "one", "1"), ("new.txt", "start", "final")],
            {"a.txt": b"1\n", "b.txt": b"two\n", "new.txt": b"final\n"},
            3,
            0,
            id="create-and-edit-same-file",
        ),
    ],
)
async def test_multi_file_edits_report_each_items_actual_effect(
    tmp_path, items, expected, applied, failed
) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\n")
    (tmp_path / "b.txt").write_bytes(b"two\n")
    arguments = {
        "edits": [
            item
            if isinstance(item, dict)
            else {"path": item[0], "old_string": item[1], "new_string": item[2]}
            for item in items
        ]
    }
    tools = registry()
    ctx = replace(context(tmp_path), tool_name="edit")

    result = await tools.dispatch(ctx, arguments)

    assert result["ok"] == (not failed or bool(applied))
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == expected
    assert re.findall(r"^Edit (\d+) of \d+", text(result), re.M) == list(
        map(str, range(1, len(items) + 1))
    )
    display = tools.display_for_call("edit", arguments, context=ctx, result=result)
    notices = [block for block in display["details"] if block.get("level") == "error"]
    assert len(notices) == failed
    counts = {fact["unit"]: fact["value"] for fact in display["facts"] if fact["kind"] == "count"}
    assert counts.get("edits", 0) == applied


LINES = b"one\ntwo\none\n"
JOINABLE = b"import a from 'a';\nimport b from 'b';\n\nend\n"
COMPUTE = b"def compute(total, count):\n    value = total / count\n    return value\n"
WRAPPED = (
    "def spawn(db, api, *, fault=None, pace=1.0,\n"
    "          exit_code=70, start=None):\n"
    "    return 1\n"
    "\n"
    "\n"
    "def check(rows):\n"
    "    total = count(rows)        print(total)\n"
    "    return total\n"
)
REWRAPPED_SIGNATURE = (
    "def spawn(db, api, *, fault=None, pace=1.0, exit_code=70,\n          start=None):"
)
ONE_LINE_JSON = '{"note": "Marked as estimated.", "status": "predicted", "unit": "kg/m3", "v": 6}\n'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "edits", "after", "failed"),
    [
        # Each edit sees the text the applied edits before it left.
        pytest.param(
            LINES,
            [("two", "2"), ("TWO", "x"), ("one\n2", "1\n2")],
            b"1\n2\none\n",
            [2],
            id="not-found",
        ),
        pytest.param(LINES, [("two", "one"), ("two", "2")], b"one\none\none\n", [2], id="replaced"),
        pytest.param(
            LINES, [("two", "one"), ("one", "1")], b"one\none\none\n", [2], id="ambiguous"
        ),
        pytest.param(
            JOINABLE,
            [
                ("import a from 'a';\n", "import a from 'a';"),
                ("end", "fin"),
                ("import b from 'b';\n", "import b from 'b';"),
            ],
            JOINABLE.replace(b"end", b"fin"),
            [1, 3],
            id="line-join",
        ),
        # An item that leaves its edit open is one failed edit.
        pytest.param(
            LINES,
            [
                {"new_string": "x"},
                ("two", "2"),
                {"old_string": "a", "new_string": "b", "line": 2},
            ],
            b"one\n2\none\n",
            [1, 3],
            id="refused-items",
        ),
        # After a failed edit, old_string must match up to whitespace: a copy with errors
        # could stand for text the failed edit would have written. Alone, this copy applies.
        pytest.param(
            COMPUTE,
            [
                ("zzz", "z"),
                (
                    "    value = total / count\n    retrun value",
                    "    value = total / count\n    return 0",
                ),
                ("def compute", "def calc"),
            ],
            COMPUTE.replace(b"compute", b"calc"),
            [1, 2],
            id="copy-after-a-failure",
        ),
        # None applied: an error that names every failed edit; the file stays.
        pytest.param(LINES, [("zzz", "z"), ("TWO", "x")], LINES, [1, 2], id="none-applied"),
    ],
)
async def test_edits_that_fail_are_skipped_and_the_others_written_once(
    tmp_path, monkeypatch, content, edits, after, failed
) -> None:
    (tmp_path / "a.txt").write_bytes(content)
    # Writes are counted at the private commit step: the file is written once per call,
    # which the written file alone cannot show.
    writes: list[int] = []
    commit = file_changes_module._commit

    def counted_commit(*args: Any, **kwargs: Any) -> Any:
        writes.append(1)
        return commit(*args, **kwargs)

    monkeypatch.setattr(file_changes_module, "_commit", counted_commit)
    tools = registry()
    ctx = replace(context(tmp_path), tool_name="edit")
    arguments = {
        "path": "a.txt",
        "edits": [
            edit if isinstance(edit, dict) else {"old_string": edit[0], "new_string": edit[1]}
            for edit in edits
        ],
    }

    result = await tools.dispatch(ctx, arguments)

    message = text(result)
    assert (tmp_path / "a.txt").read_bytes() == after
    named = re.findall(r"^(?:Failed: )?(?:a\.txt, edit|edits item) (\d+)\b", message, re.M)
    assert [int(number) for number in named] == failed
    if after == content:
        assert not result["ok"] and not writes
        return
    assert result["ok"] and writes == [1]
    assert edit_outcome(message) == "partial"
    # The WebUI shows the call as partial: its display records an error notice per failed edit.
    display = tools.display_for_call("edit", arguments, context=ctx, result=result)
    notices = [block for block in display["details"] if block.get("level") == "error"]
    assert len(notices) == len(failed)
    # The row counts the applied edits; one file goes without a file count.
    counts = {f["unit"]: f["value"] for f in display["facts"] if f["kind"] == "count"}
    assert counts == {"edits": len(edits) - len(failed)}


@pytest.mark.asyncio
@pytest.mark.parametrize("absolute", [False, True])
async def test_refused_item_blocks_fuzzy_edits_of_the_same_resolved_file(
    tmp_path, absolute
) -> None:
    (tmp_path / "a.py").write_bytes(COMPUTE)
    earlier_path = str(tmp_path / "a.py") if absolute else "./a.py"
    result = await call(
        tmp_path,
        {
            "edits": [
                {"path": earlier_path, "new_string": "missing old text"},
                {"path": "b.txt", "old_string": "", "new_string": "created"},
                {
                    "path": "a.py",
                    "old_string": "    value = total / count\n    retrun value",
                    "new_string": "    value = total / count\n    return 0",
                },
            ]
        },
        name="edit",
    )
    assert result["ok"]
    assert edit_outcome(text(result)) == "partial"
    assert (tmp_path / "a.py").read_bytes() == COMPUTE
    assert (tmp_path / "b.txt").read_bytes() == b"created\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "edit", "after", "noted"),
    [
        # A changed line that lost its final line break keeps it, in the file's line
        # ending, where more lines follow: a line of text stays apart, a blank line stays.
        pytest.param(
            JOINABLE,
            ("import a from 'a';\n", "import a2 from 'a';"),
            JOINABLE.replace(b"a from", b"a2 from"),
            True,
            id="kept-break",
        ),
        pytest.param(
            JOINABLE.replace(b"\n", b"\r\n"),
            ("import a from 'a';\n", "import a2 from 'a';"),
            JOINABLE.replace(b"a from", b"a2 from").replace(b"\n", b"\r\n"),
            True,
            id="kept-break-crlf",
        ),
        pytest.param(
            JOINABLE,
            ("import b from 'b';\n", "import b2 from 'b';"),
            JOINABLE.replace(b"b from", b"b2 from"),
            True,
            id="blank-line-follows",
        ),
        # Nearby edits stay as sent. At the end of the file nothing follows.
        pytest.param(JOINABLE, ("end\n", "fin"), JOINABLE[:-4] + b"fin", False, id="end-of-file"),
        pytest.param(JOINABLE, ("end\n", "end"), JOINABLE[:-1], False, id="final-break-only"),
        # A new text that still ends its line deletes the blank line on purpose.
        pytest.param(
            JOINABLE,
            ("import b from 'b';\n\n", "import b from 'b';\n"),
            b"import a from 'a';\nimport b from 'b';\nend\n",
            False,
            id="blank-line-deleted",
        ),
        # A join that takes in the next line is written as sent.
        pytest.param(
            JOINABLE,
            ("import a from 'a';\nimport b from 'b';\n", "import a from 'a'; import b from 'b';\n"),
            b"import a from 'a'; import b from 'b';\n\nend\n",
            False,
            id="join-with-next-line",
        ),
        # A last line of only spaces holds no text: it joins as sent, with a note
        # quoting the joined line.
        pytest.param(
            JOINABLE,
            ("import a from 'a';\n", "import a2 from 'a';\n  "),
            JOINABLE.replace(b"a from 'a';\n", b"a2 from 'a';\n  "),
            True,
            id="spaces-joined",
        ),
        # An edit that changes nothing but drops the line break is refused, naming
        # the lines it would join.
        pytest.param(JOINABLE, ("import a from 'a';\n", "import a from 'a';"), None, False),
    ],
)
async def test_an_edit_that_drops_a_final_line_break_keeps_the_next_line(
    tmp_path, content, edit, after, noted
) -> None:
    (tmp_path / "a.ts").write_bytes(content)

    result = await call(
        tmp_path,
        {"path": "a.ts", "edits": [{"old_string": edit[0], "new_string": edit[1]}]},
        name="edit",
    )

    if after is None:
        assert result["error"]["code"] == "line_join"
        # The refusal, naming the lines, then the closing.
        assert len(text(result).split("\n")) == 2
        assert "import b from 'b';" in text(result).split("\n")[1]
        assert (tmp_path / "a.ts").read_bytes() == content
        return
    notes = [line for line in text(result).split("\n") if line.startswith("Note: ")]
    assert result["ok"]
    assert (tmp_path / "a.ts").read_bytes() == after
    assert len(notes) == noted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "edit", "shown"),
    [
        # The first difference quotes the file's line, its backslash single: an Agent
        # copies the quote into its next old_string.
        pytest.param(
            "s = 'a\\u00b0b'",
            ("s = 'a\\u00b0c'", "s = 'x'"),
            ["s = 'a\\u00b0b'", "s = 'a\\u00b0c'"],
            id="first-difference",
        ),
        # The refused line join quotes the line it names the same way.
        pytest.param(
            'path = "C:\\\\data"',
            ('path = "C:\\\\data"\n', 'path = "C:\\\\data"'),
            ['path = "C:\\\\data"'],
            id="line-join",
        ),
        # A character that shows nothing is spelled out.
        pytest.param("s = 'a\u200bb'", ("s = 'ab' + q", "s = 'x'"), ["\\u200b"], id="invisible"),
    ],
)
async def test_reports_quote_text_as_the_file_has_it(tmp_path, line, edit, shown) -> None:
    content = f"{line}\nnext = 1\n"
    (tmp_path / "a.py").write_text(content, encoding="utf-8", newline="\n")

    result = await call(
        tmp_path,
        {"path": "a.py", "edits": [{"old_string": edit[0], "new_string": edit[1]}]},
        name="edit",
    )

    message = text(result)
    assert not result["ok"]
    assert all(part in message for part in shown)
    # No backslash is doubled, as repr would.
    assert "\\\\" not in message.replace(line, "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "edit", "replace_all", "after", "error"),
    [
        # An escape's character form in old_string proves only that spelling:
        # a new kind of escape in new_string remains literal.
        pytest.param(
            'name = "grün"\nunit = "°C"\n',
            ('name = "gr\\u00fcn"', 'name = "gr\\u00fcn \\u00b0"'),
            False,
            'name = "grün \\u00b0"\nunit = "°C"\n',
            None,
            id="escapes-for-characters",
        ),
        pytest.param(
            'label = "\\u221212 °C"\n',
            ('label = "\\u221212 \\u00b0C"', 'label = "\\u221213 \\u00b0C \\u00e4"'),
            False,
            'label = "\\u221213 °C \\u00e4"\n',
            None,
            id="matching-escape-stays-literal",
        ),
        pytest.param(
            'label = "\\u00b0 °"\n',
            ('label = "\\u00b0 \\u00b0"', 'label = "\\u00b0"'),
            False,
            'label = "\\u00b0"\n',
            None,
            id="one-spelling-has-both-forms",
        ),
        pytest.param(
            'label = "\\u2212°"\n',
            ('label = "\\u2212\\u00b0"', 'label = "\\u2212\\u00b0!"'),
            False,
            'label = "\\u2212°!"\n',
            None,
            id="adjacent-escape-spellings",
        ),
        pytest.param(
            'label = "😀"\n',
            ('label = "\\ud83d\\ude00"', 'label = "\\ud83d\\ude00!"'),
            False,
            'label = "😀!"\n',
            None,
            id="surrogate-pair",
        ),
        pytest.param(
            'label = "\\u2212 °"\nlabel = "− °"\n',
            ('label = "\\u2212 \\u00b0"', 'label = "x"'),
            False,
            None,
            "ambiguous_match",
            id="mixed-and-decoded-targets",
        ),
        pytest.param(
            'label = "\\u2212 °"\nlabel = "− °"\n',
            ('label = "\\u2212 \\u00b0"', 'label = "\\u2212 \\u00b0!"'),
            True,
            'label = "\\u2212 °!"\nlabel = "\\u2212 °!"\n',
            None,
            id="all-spellings-share-only-degree-interpretation",
        ),
        pytest.param(
            'label = "\\u2212 \\u00b0"\nlabel = "− °"\n',
            ('label = "\\u2212 \\u00b0"', 'label = "literal"'),
            False,
            'label = "literal"\nlabel = "− °"\n',
            None,
            id="exact-spelling-selects-its-target",
        ),
        pytest.param(
            'label = "\\u221214 °C"\n',
            ('label = "\\u221212 \\u00b0C"', 'label = "x"'),
            False,
            None,
            "text_not_found",
            id="different-number",
        ),
        pytest.param(
            'header\r\nlabel = "\\u2212 °"\n',
            ('\nlabel = "\\u2212 \\u00b0"', '\nlabel = "x"'),
            False,
            None,
            "text_not_found",
            id="mixed-spelling-cannot-split-crlf",
        ),
        # Nearby: an escaped backslash ends no escape, and an escape of an ASCII
        # character is no slip of the kind.
        pytest.param(
            'name = "grün"\n',
            ('name = "gr\\\\u00fcn"', 'name = "x"'),
            False,
            None,
            "text_not_found",
            id="escaped-backslash",
        ),
        pytest.param(
            'name = "grün"\n',
            ('n\\u0061me = "grün"', 'name = "x"'),
            False,
            None,
            "text_not_found",
            id="ascii-escape",
        ),
    ],
)
async def test_unicode_escapes_in_old_string_match_the_characters_of_the_file(
    tmp_path, content, edit, replace_all, after, error
) -> None:
    (tmp_path / "a.py").write_text(content, encoding="utf-8", newline="\n")

    result = await call(
        tmp_path,
        {
            "path": "a.py",
            "edits": [{"old_string": edit[0], "new_string": edit[1], "replace_all": replace_all}],
        },
        name="edit",
    )

    written = (tmp_path / "a.py").read_bytes().decode("utf-8")
    if after is None:
        assert result["error"]["code"] == error
        assert written == content
        return
    assert written == after
    assert result["ok"]
    if edit[0] not in content:
        assert len([line for line in text(result).split("\n") if line.startswith("Note: ")]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "edit", "outcome"),
    [
        # Every word is the file's, only the line breaks fall elsewhere: new_string
        # is written as sent, with a note naming the lines it matched.
        pytest.param(
            WRAPPED,
            (REWRAPPED_SIGNATURE, REWRAPPED_SIGNATURE.replace("None):", "None, rows=1):")),
            WRAPPED.replace(
                "pace=1.0,\n          exit_code=70, start=None):",
                "pace=1.0, exit_code=70,\n          start=None, rows=1):",
            ),
            id="signature",
        ),
        pytest.param(
            "# Notes\n\n- Rendering uses the engine's own\n  digit rules, not repr.\n- Next\n",
            ("- Rendering uses the engine's own digit rules, not repr.", "- Rendering is exact."),
            "# Notes\n\n- Rendering is exact.\n- Next\n",
            id="prose",
        ),
        # Swarm run 2026-10-13: an indented copy of a passage inside a line.
        pytest.param(
            ONE_LINE_JSON,
            (
                '    "status": "predicted",\n    "unit": "kg/m3",',
                '    "status": "predicted",\n    "reason": "estimated",\n    "unit": "kg/m3",',
            ),
            ONE_LINE_JSON.replace(
                ' "unit": "kg/m3",', '\n"reason": "estimated",\n"unit": "kg/m3",'
            ),
            id="inside-a-line",
        ),
        pytest.param(
            WRAPPED.replace("\n", "\r\n"),
            (REWRAPPED_SIGNATURE, REWRAPPED_SIGNATURE.replace("None):", "None, rows=1):")),
            WRAPPED.replace(
                "pace=1.0,\n          exit_code=70, start=None):",
                "pace=1.0, exit_code=70,\n          start=None, rows=1):",
            ).replace("\n", "\r\n"),
            id="crlf",
        ),
        # Nearby inputs stay refused, with the line the line break differs on.
        # An edit of whitespace alone is about the line breaks old_string misstates.
        pytest.param(
            WRAPPED,
            ("    total = count(rows)\n    print(total)",) * 2,
            ("text_not_found", {"7"}),
            id="whitespace-only",
        ),
        pytest.param(
            WRAPPED,
            (
                "    total = count(rows)\n    print(total)\n",
                "    total = count(rows)\n    print(total)",
            ),
            ("text_not_found", {"7"}),
            id="whitespace-only-final-break",
        ),
        # A copy that starts a line must start one in the file.
        pytest.param(
            WRAPPED,
            ("\n    print(total)\n    return total", "\n    print(total)\n    return total + 1"),
            ("text_not_found", {"7-8", "7"}),
            id="starts-a-line",
        ),
        # Inside a line, indentation stands for a space or tab before the passage.
        pytest.param(
            ONE_LINE_JSON.replace(' "status"', '"status"'),
            ('    "status": "predicted",\n    "unit": "kg/m3",', '    "status": "measured",'),
            ("text_not_found", {"1"}),
            id="inside-a-line-without-space",
        ),
        # Too few words to tell the place.
        pytest.param(
            WRAPPED,
            ("rows)\nprint(total)", "rows) + print(total)"),
            ("text_not_found", {"7"}),
            id="few-words",
        ),
        # A blank line must be in the file as well.
        pytest.param(
            WRAPPED,
            (REWRAPPED_SIGNATURE.replace(",\n", ",\n\n"), "def spawn():"),
            ("text_not_found", {"1-2", "1"}),
            id="blank-line",
        ),
        pytest.param(
            WRAPPED + "\n\n" + WRAPPED.split("\n\n\n")[0] + "\n",
            (REWRAPPED_SIGNATURE, "def spawn():"),
            ("ambiguous_match", {"1"}),
            id="ambiguous",
        ),
    ],
)
async def test_old_string_with_line_breaks_in_other_places_than_the_file(
    tmp_path, content, edit, outcome
) -> None:
    (tmp_path / "a.py").write_bytes(content.encode())
    old, new = edit

    result = await call(
        tmp_path, {"path": "a.py", "edits": [{"old_string": old, "new_string": new}]}, name="edit"
    )

    if isinstance(outcome, str):
        assert result["ok"], text(result)
        assert (tmp_path / "a.py").read_bytes() == outcome.encode()
        assert len([line for line in text(result).split("\n") if line.startswith("Note: ")]) == 1
        return
    code, lines = outcome
    assert result["error"]["code"] == code
    assert (tmp_path / "a.py").read_bytes() == content.encode()
    assert set(re.findall(r"\blines? (\d+(?:-\d+)?)", text(result))) == lines


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "seen", "named"),
    [
        # Swarm run 2026-10-13: an edit meant for another file the Agent worked on.
        pytest.param("def load(rows):\n    return rows", True, True, id="read-elsewhere"),
        # Nearby: a file the Session never read, or text too short to tell a file.
        pytest.param("def load(rows):\n    return rows", False, False, id="not-read"),
        pytest.param("return rows", True, False, id="short"),
    ],
)
async def test_an_old_string_from_another_file_of_the_session_names_that_file(
    tmp_path, old, seen, named
) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    other = tmp_path / "b.py"
    other.write_bytes(b"def load(rows):\n    return rows\n")
    state = FileReadState()
    if seen:
        state.record_read("session-test", other)

    result = await call(
        tmp_path,
        {"path": "a.py", "edits": [{"old_string": old, "new_string": "pass"}]},
        tools=registry(state),
        name="edit",
    )

    assert result["error"]["code"] == "text_not_found"
    assert ("b.py" in text(result)) is named
    # The edit is never applied to the other file.
    assert (tmp_path / "a.py").read_bytes() == b"x = 1\n"
    assert other.read_bytes() == b"def load(rows):\n    return rows\n"


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
        {"path": "a.py", "edits": ['{"old_string": "x = 1", "new_string": "x = 3"}']},
        # An item may repeat the call's file.
        {
            "path": "a.py",
            "edits": [{"path": "./a.py", "old_string": "x = 1", "new_string": "x = 3"}],
        },
        {"edits": [{"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"}]},
        # An empty value under a name that is not a field requests nothing; a
        # value there is refused (item-unknown-field).
        {"path": "a.py", "edits": [{"old_string": "x = 1", "new_string": "x = 3", "new_2": ""}]},
    ],
)
async def test_other_edit_spellings_make_the_same_change(tmp_path, arguments) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")

    result = await call(tmp_path, arguments, name="edit")

    assert result["data"] == {"content": "Updated a.py (line 1)."}


_CUT_EDITS = '[{"old_string": "x = 1", "new_string": "x = 2\ny'
_PART_EDITS = '[{"old_string": "x = 1", '


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "outcome"),
    [
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = f("a")"}]'},
            b'x = f("a")\n',
            id="array",
        ),
        pytest.param(
            {"edits": '{"old_string": "x = 1", "new_string": "x = "a""}'}, b'x = "a"\n', id="one"
        ),
        # A raw line break inside a text stands for itself.
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = 2\ny = 3"}]'},
            b"x = 2\ny = 3\n",
            id="raw-line-break",
        ),
        pytest.param(
            {
                "edits": '[{"old_string":"x = 1", "new_string":"x = 2\ny = 3"}, '
                '{"old_string":"y = 3", "new_string":"y = 4\\nz = 5"}]'
            },
            b"x = 2\ny = 4\nz = 5\n",
            id="line-breaks-differ-between-items",
        ),
        # A quote before ":" ends the text only where a value follows the colon.
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "got "x": not a number"}]'},
            b'got "x": not a number\n',
            id="quote-colon-text",
        ),
        # Swarm run 2026-10-13: closing brackets after the value close nothing.
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = "a""}]}\n'},
            b'x = "a"\n',
            id="stray-closers",
        ),
        # These have another reading than slips inside new_string: nothing runs,
        # and the error names the field the JSON breaks in.
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = 2\\ny = 3\nz = 4"}]'},
            "new_string",
            id="mixed-line-breaks",
        ),
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = {"a": 2}"}]'},
            "new_string",
            id="quote-colon-value",
        ),
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = f("a", "b")"}]'},
            "new_string",
            id="comma-quote",
        ),
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = "a"", "line": 2}]'},
            "new_string",
            id="undeclared-field",
        ),
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = \\_"}]'},
            "new_string",
            id="other",
        ),
        # A value after the end can be an edit meant to run: nothing runs.
        pytest.param(
            {"edits": '[{"old_string": "x = 1", "new_string": "x = 2"}], {"old_string": "x"}]'},
            "character 48",
            id="value-after-the-end",
        ),
        # Text that ends early lost part of the call, also when the rest arrived
        # beside it: nothing runs, and the error says where the text ends.
        pytest.param({"edits": _CUT_EDITS}, f"character {len(_CUT_EDITS)}", id="cut-off"),
        pytest.param(
            {"edits": _PART_EDITS, "new_string": 'x = 3"}]'},
            f"character {len(_PART_EDITS)}",
            id="field-beside",
        ),
    ],
)
async def test_edits_json_text_runs_only_when_it_has_one_reading(
    tmp_path, arguments, outcome
) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\n")

    result = await call(tmp_path, {"path": "a.py", **arguments}, name="edit")

    if isinstance(outcome, bytes):
        assert result["ok"]
        assert (tmp_path / "a.py").read_bytes() == outcome
        return
    assert result["error"]["code"] == "invalid_arguments"
    assert outcome in text(result)
    assert (tmp_path / "a.py").read_bytes() == b"x = 1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (
            [{"old_string": "x = 1", "new_string": "x = 1"}],
            "a.py: the new text equals the current text; nothing to change.",
        ),
        (
            [
                {"old_string": "x = 1", "new_string": "x = 2"},
                {"old_string": "x = 2", "new_string": "x = 1"},
            ],
            "a.py already reads as these edits would leave it.",
        ),
    ],
)
async def test_edits_that_leave_the_file_as_it_is_say_so(tmp_path, edits, message) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\n")

    result = await call(tmp_path, {"path": "a.py", "edits": edits}, name="edit")

    assert result["data"] == {"content": f"{message} No file was changed."}


TOTALS = (
    "def f(rows):\n    total = 0\n    for row in rows:\n        total += row.size\n"
    "    return total\n"
)
# An edit of the loop of TOTALS, already made: old_string is not in the file.
TOTALS_EDIT = (
    "    for row in rows:\n        total += row\n    return total",
    "    for row in rows:\n        total += row.size\n    return total",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "edit", "done"),
    [
        # Swarm run 2026-10-13: an edit sent again after it was made failed as not found.
        pytest.param(TOTALS, TOTALS_EDIT, True, id="made-earlier"),
        # Nearby: the new text tells nothing about where old_string would be.
        pytest.param("x = 2\n", ("x = 1", "x = 2"), False, id="no-common-line"),
        pytest.param(
            "function f() {\n  return 2;\n}\n",
            ("  return 1;\n}", "  return 2;\n}"),
            False,
            id="punctuation-in-common",
        ),
        pytest.param(TOTALS + "\n" + TOTALS, TOTALS_EDIT, False, id="new-text-twice"),
        # A copy of old_string with errors stands elsewhere: the edit can be meant there.
        pytest.param(
            TOTALS + "\ndef g(rows):\n    total = 0\n    for row in rows:\n        total += rows\n"
            "    return total\n",
            TOTALS_EDIT,
            False,
            id="old-text-copied-elsewhere",
        ),
    ],
)
async def test_an_edit_whose_result_already_stands_has_nothing_to_do(
    tmp_path, content, edit, done
) -> None:
    (tmp_path / "a.py").write_bytes(content.encode())

    result = await call(
        tmp_path,
        {"path": "a.py", "edits": [{"old_string": edit[0], "new_string": edit[1]}]},
        name="edit",
    )

    assert (tmp_path / "a.py").read_bytes() == content.encode()
    if not done:
        assert result["error"]["code"] == "text_not_found"
        return
    assert result["ok"]
    assert edit_outcome(text(result)) == "unchanged"
    # It names the lines that hold the new text.
    assert "3-5" in text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True], ids=["all-matched", "one-failed"])
@pytest.mark.parametrize("made", [False, True], ids=["same-text", "made-earlier"])
async def test_an_edit_that_changes_nothing_is_named_apart_from_the_applied_ones(
    tmp_path, fails, made
) -> None:
    # Swarm runs 2026-10-11/12: 12 written calls held an edit whose new_string was its
    # old_string; the results counted it as applied and never named it. Swarm run
    # 2026-10-13: an edit whose new text already stood there failed as not found.
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\nw = 4\nz = 3\n")
    edits = [
        {"old_string": "x = 1", "new_string": "x = 9"},
        {"old_string": "y = 2\nw = 5" if made else "y = 2", "new_string": "y = 2\nw = 4"},
        {"old_string": "zzz" if fails else "z = 3", "new_string": "z = 9"},
    ]
    if not made:
        edits[1]["new_string"] = "y = 2"
    tools = registry()
    ctx = replace(context(tmp_path), tool_name="edit")
    arguments = {"path": "a.py", "edits": edits}

    result = await tools.dispatch(ctx, arguments)

    message = text(result)
    # The row counts only the edits that changed the file.
    display = tools.display_for_call("edit", arguments, context=ctx, result=result)
    counts = {f["unit"]: f["value"] for f in display["facts"] if f["kind"] == "count"}
    assert counts == {"edits": 1 if fails else 2}
    after = b"x = 9\ny = 2\nw = 4\nz = 3\n" if fails else b"x = 9\ny = 2\nw = 4\nz = 9\n"
    assert (tmp_path / "a.py").read_bytes() == after
    assert edit_outcome(message) == ("partial" if fails else "applied")
    # Edit 2 is named once, in its own sentence: not as applied, not as failed.
    lines = [line for line in message.split("\n") if "edit 2" in line.lower()]
    assert len(lines) == 1 and not lines[0].startswith("Failed:")
    if fails:
        applied = re.match(r"Edits? ([\d, and-]+?) (?:was|were) applied", message)
        assert applied and applied[1] == "1"


@pytest.mark.asyncio
async def test_write_creates_replaces_after_a_read_and_keeps_identical_content(tmp_path) -> None:
    state = FileReadState()
    tools = registry(state)
    path = tmp_path / "a.txt"
    path.write_bytes(b"old\n")
    bare = tmp_path / "bare.txt"
    bare.write_bytes(b"old")

    created = await call(
        tmp_path, {"file_path": "deep/w.txt", "file_text": "w\r\nx"}, tools=tools, name="write"
    )
    unchanged = await call(
        tmp_path, {"path": "a.txt", "contents": "old"}, tools=tools, name="write"
    )
    state.record_read("session-test", path)
    state.record_read("session-test", bare)
    replaced = await call(tmp_path, {"path": "a.txt", "content": "new"}, tools=tools, name="write")
    await call(tmp_path, {"path": "bare.txt", "content": "new"}, tools=tools, name="write")

    assert text(created) == "Created deep/w.txt (2 lines)."
    # A missing final line break is added to a new file and follows a replaced one.
    assert (tmp_path / "deep" / "w.txt").read_bytes() == b"w\r\nx\r\n"
    assert unchanged["data"] == {"content": "a.txt already has this content. No file was changed."}
    assert text(replaced) == "Replaced the content of a.txt (1 line)."
    assert path.read_bytes() == b"new\n"
    assert bare.read_bytes() == b"new"


ESCAPED_BREAKS = "    # checks" + "\\n    for page in pages:\\n        check(page)" * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "line", "noted"),
    [
        pytest.param("check.py", ESCAPED_BREAKS, True, id="behind-a-comment"),
        pytest.param("check.py", f'SCRIPT = "{ESCAPED_BREAKS}"', False, id="inside-a-string"),
        pytest.param("data.json", ESCAPED_BREAKS, False, id="data-file"),
    ],
)
@pytest.mark.parametrize("tool", ["write", "edit"])
async def test_a_line_of_escaped_line_breaks_is_written_as_sent_with_a_note(
    tmp_path, tool, path, line, noted
) -> None:
    state = FileReadState()
    target = tmp_path / path
    target.write_bytes(b"def main():\n    pass\n")
    state.record_read("session-test", target)
    content = f"def main():\n{line}\n"
    arguments = (
        {"path": path, "content": content}
        if tool == "write"
        else {"path": path, "old_string": "    pass", "new_string": line}
    )

    result = await call(tmp_path, arguments, tools=registry(state), name=tool)

    assert target.read_bytes() == content.encode()
    assert ("Note: Line 2 " in text(result)) is noted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "tool", "arguments", "after"),
    [
        # A stray CRLF or lone CR does not set the ending of every written line.
        pytest.param(
            None,
            "write",
            {"path": "a.txt", "content": "one\ntwo\r\nthree\n"},
            b"one\ntwo\nthree\n",
            id="new-file",
        ),
        pytest.param(
            None,
            "write",
            {"path": "a.txt", "content": "one\r\ntwo\rthree\r\n"},
            b"one\r\ntwo\r\nthree\r\n",
            id="new-file-crlf",
        ),
        pytest.param(
            b"one\ntwo\r\nthree\n",
            "write",
            {"path": "a.txt", "content": "1\r\n2\r\n"},
            b"1\n2\n",
            id="replaced-file",
        ),
        pytest.param(
            b"one\r\ntwo\nthree\n",
            "edit",
            {"path": "a.txt", "old_string": "three", "new_string": "three\nfour"},
            b"one\r\ntwo\nthree\nfour\n",
            id="added-lines",
        ),
    ],
)
async def test_written_lines_end_as_most_lines_of_the_file_or_content(
    tmp_path, before, tool, arguments, after
) -> None:
    state = FileReadState()
    path = tmp_path / "a.txt"
    if before is not None:
        path.write_bytes(before)
        state.record_read("session-test", path)

    result = await call(tmp_path, arguments, tools=registry(state), name=tool)

    assert result["ok"], result
    assert path.read_bytes() == after


FILES = {
    "a.txt": b"one\ntwo\none\n",
    "app.py": b"def main():\n    total = compute(1, 2)\n    print(total)\n    return total\n",
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
            f"a.txt: old_string was not found.\n{NOT_FOUND}",
            id="not-found",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "one", "new_string": "1"}]},
            "ambiguous_match",
            "a.txt: old_string matches 2 places (lines 1, 3). Include more of the surrounding "
            "text so it matches once, or set replace_all to true to change every match.\n"
            "Where it occurs:\n1| one\n2| two\n3| one",
            id="ambiguous",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "", "new_string": "x"}]},
            "file_exists",
            "a.txt: old_string is empty, which creates a file, but the file already has content. "
            "Put the current text to replace in old_string, or call write to replace the whole "
            "file.",
            id="create-over-content",
        ),
        pytest.param(
            "edit",
            {"path": "gone.md", "edits": [{"old_string": "a", "new_string": "b"}]},
            "file_not_found",
            'File not found: gone.md. read(path=".") lists its directory.',
            id="missing-file",
        ),
        pytest.param(
            "edit",
            {"path": "sub", "edits": [{"old_string": "a", "new_string": "b"}]},
            "not_a_file",
            "sub is not a regular file.",
            id="folder",
        ),
        pytest.param(
            "edit",
            {"path": "bin.dat", "edits": [{"old_string": "binary", "new_string": "b"}]},
            "binary_file",
            "bin.dat is a binary file, so edit cannot change its text.",
            id="binary",
        ),
        pytest.param(
            "edit",
            {"path": "latin.txt", "edits": [{"old_string": "caf", "new_string": "b"}]},
            "unsupported_encoding",
            "latin.txt is not UTF-8 text, so edit cannot change its text.",
            id="encoding",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "two", "new_string": "t\x00o"}]},
            "binary_file",
            f"a.txt: {NUL}",
            id="nul",
        ),
        pytest.param(
            "write",
            {"path": "a.txt", "content": "new\n"},
            "file_not_read",
            "a.txt already exists and was not read with read in this conversation, so it was "
            "not replaced. Its "
            "current content follows and now counts as read: send the same call again to replace "
            "it, or call edit to change only part of it.\n1| one\n2| two\n3| one",
            id="write-unread",
        ),
        pytest.param(
            "write",
            {"path": "w.txt", "content": "a\x00b"},
            "binary_file",
            f"w.txt: {NUL}",
            id="write-nul",
        ),
        pytest.param(
            "write",
            {"path": "sub", "content": "x"},
            "not_a_file",
            "sub is not a regular file.",
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

    assert (result["error"]["code"], text(result)) == (code, f"No file was changed.\n{message}")
    assert {name: (tmp_path / name).read_bytes() for name in FILES} == FILES
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*FILES, "sub"])


SOURCE = (
    b"def load(path):\n    with open(path) as handle:\n        return handle.read()\n\n\n"
    b"def main():\n    total = compute(1, 2)\n    print(total)\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edits", "shown", "lines", "named_edits"),
    [
        # The text earlier edits left is what a later edit's report shows, and only the
        # earlier edit that wrote a shown line is named, at that line as it moved.
        pytest.param(
            [
                {"old_string": "def load(path):", "new_string": "import io\n\n\ndef load(path):"},
                {"old_string": "print(total)", "new_string": "log(total)"},
                {"old_string": "print(total)", "new_string": "emit(total)"},
            ],
            [11],
            {"11"},
            {"3", "1", "2"},
            id="text-an-earlier-edit-replaced-below-another",
        ),
        # Whole lines from places apart are named as those places, and only they show.
        pytest.param(
            [
                {
                    "old_string": "def main():\n    total = compute(1, 2)\n\n\n"
                    "def load(path):\n    with open(path) as handle:",
                    "new_string": "def main():\n    return 3",
                }
            ],
            [1, 2, 6, 7],
            {"1-2", "5-6", "6-7"},
            set(),
            id="lines-from-different-places",
        ),
        # Single lines occur elsewhere by chance: the closest text shows instead.
        pytest.param(
            [
                {
                    "old_string": "    print(total)\n    total = compute(1, 2)",
                    "new_string": "    total = compute(1, 2)",
                }
            ],
            [6, 7, 8],
            set(),
            set(),
            id="single-lines-from-different-places",
        ),
        # A passage that left out a few lines is one place: its first difference
        # names the first line left out.
        pytest.param(
            [
                {
                    "old_string": "def load(path):\n    with open(path) as handle:\n\n\n"
                    "def main():\n    total = compute(1, 2)",
                    "new_string": "def main():\n    return 3",
                }
            ],
            [1, 2, 3, 4, 5, 6, 7, 8],
            {"3"},
            set(),
            id="passage-that-left-out-a-line",
        ),
    ],
)
async def test_a_failed_edit_names_the_lines_and_edits_its_text_came_from(
    tmp_path, edits, shown, lines, named_edits
) -> None:
    (tmp_path / "app.py").write_bytes(SOURCE)

    result = await call(tmp_path, {"path": "app.py", "edits": edits}, name="edit")

    message = text(result)
    if len(edits) > 1:
        # The other edits were written; the failed edit's report follows them.
        assert edit_outcome(message) == "partial"
        message = message[message.index("\nFailed: ") + 1 :]
    else:
        assert result["error"]["code"] == "text_not_found"
        assert (tmp_path / "app.py").read_bytes() == SOURCE
    assert [int(line.split("|")[0]) for line in message.split("\n") if "| " in line] == shown
    assert set(re.findall(r"\blines? (\d+(?:-\d+)?)", message)) == lines
    assert set(re.findall(r"\b[Ee]dits? (\d+(?:-\d+)?)", message)) == named_edits


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
            None,
            id="edit-another-file",
        ),
        pytest.param(
            "edit",
            {
                "path": "a.txt",
                "edits": [
                    {"old_string": "one", "new_string": "1"},
                    {"path": "b.txt", "old_string": "", "new_string": "created"},
                ],
            },
            None,
            id="contradictory-root-refuses-before-first-edit",
        ),
        pytest.param(
            "edit",
            {
                "edits": [
                    {"path": "a.txt", "old_string": "one", "new_string": "1"},
                    {"path": "b.txt", "old_string": "", "new_string": "created"},
                    {"old_string": "two", "new_string": "2"},
                ],
            },
            None,
            id="no-guess-for-missing-item-path",
        ),
        pytest.param(
            "edit",
            {
                "edits": [
                    {"path": "a.txt", "old_string": "one", "new_string": "1"},
                    {"path": ["b.txt"], "old_string": "", "new_string": "created"},
                ],
            },
            None,
            id="invalid-item-path-refuses-before-first-edit",
        ),
        pytest.param(
            "edit",
            {
                "edits": [
                    {"path": "a.txt", "old_string": "one", "new_string": "1"},
                    {
                        "path": "b.txt",
                        "filename": "c.txt",
                        "old_string": "",
                        "new_string": "created",
                    },
                ],
            },
            None,
            id="contradictory-item-path-refuses-before-first-edit",
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
            "new_string needs old_string, the current text it replaces. To insert text, put the "
            "line above it in old_string, and that line followed by the new text in new_string; "
            'old_string "" creates a file.',
            id="flat-without-old",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"new_string": "x"}]},
            "edits item 1 has no old_string, the current text to replace. To insert text, put "
            "the line above it in old_string, and that line followed by the new text in "
            'new_string; old_string "" creates a file.',
            id="item-without-old",
        ),
        # With no item to apply, the call names every item's problem. Another item's
        # old text shows that the file has text: creating it is no option.
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": [{"old_string": "one"}, {"new_string": "x"}]},
            'edits item 1 has old_string but no new_string. Send new_string, "" to delete the '
            "text.\nedits item 2 has no old_string, the current text to replace. To insert "
            "text, put the line above it in old_string, and that line followed by the new text "
            "in new_string.",
            id="items-without-old-beside-old-text",
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
            {"path": "a.txt", "edits": "one -> 1"},
            "edits arrived as text, not as an array. Send edits as an array of objects, each "
            "with old_string and new_string.",
            id="edits-as-plain-text",
        ),
        pytest.param(
            "edit",
            {"path": "a.txt", "edits": ["one -> 1"]},
            "edits item 1 is not an object. Send each edit as an object with old_string and "
            "new_string.",
            id="item-not-an-object",
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
    if message is not None:
        assert text(result) == f"No file was changed.\n{message}"
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
        f"No file was changed.\n{message}",
    )
    assert path.read_bytes() == b"external\n"
