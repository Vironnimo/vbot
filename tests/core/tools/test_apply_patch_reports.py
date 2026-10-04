"""apply_patch results: change previews, unchanged files and failure diagnostics."""

from __future__ import annotations

import ast
import re

import pytest

from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_test_support import apply, call, registry, text, update


def test_long_line_preview_shows_the_change_and_surrounding_lines(tmp_path):
    path = tmp_path / "file.txt"
    old = "x" * 400 + "TARGET=old" + "z" * 100
    new = old.replace("TARGET=old", "TARGET=new")
    path.write_text("header\n" + old + "\nfooter\n", encoding="utf-8")
    result = apply(tmp_path, update("@@\n-" + old + "\n+" + new))
    preview = text(result).splitlines()
    assert preview[0] == "Updated file.txt:"
    assert preview[1] == "1| header"
    assert preview[3] == "3| footer"
    # The long line is shown around the change, with its starting column in the gutter.
    assert preview[2].startswith("2:") and "TARGET=new" in preview[2]
    assert len(preview) == 4 and all(len(line) <= 255 for line in preview)
    assert path.read_text(encoding="utf-8") == "header\n" + new + "\nfooter\n"


def test_preview_keeps_first_and_last_region_and_reports_omission(tmp_path):
    path = tmp_path / "file.txt"
    gap = "s\n" * 3
    path.write_text(f"a=old\n{gap}b=old\n{gap}c=old\n", encoding="utf-8")
    result = apply(tmp_path, update("\n".join(f"@@\n-{key}=old\n+{key}=new" for key in "abc")))
    assert result["data"] == {
        "status": "applied",
        "content": "Updated file.txt:\n1| a=new\n2| s\n-- (1 more changed place not shown)\n"
        "8| s\n9| c=new",
    }


def test_nearby_changes_read_as_one_region(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"a=old\nseparator\nb=old\nseparator\nc=old\n")
    result = apply(tmp_path, update("\n".join(f"@@\n-{key}=old\n+{key}=new" for key in "abc")))
    assert text(result) == (
        "Updated file.txt:\n1| a=new\n2| separator\n3| b=new\n4| separator\n5| c=new"
    )


LABEL_OLD = '        "Savings 2 % p.a.": pd.Series('
LABEL_NEW = '        "Savings 2 % p.a. (1 year = 365 days)": pd.Series('
LABELS = f"# Concurrent explanation\n{LABEL_NEW}\n            values,\n        )\n"
FIRST = "@@\n alpha\n-one\n+first\n beta"
SECOND = "@@\n beta\n-two\n+second\n omega"


@pytest.mark.parametrize(
    ("before", "earlier", "patch", "content"),
    [
        # Add with the file's own content needs no read.
        (
            b"same\n",
            [],
            "*** Add File: file.txt\n+same",
            "file.txt already has this content. No file was changed.",
        ),
        *(
            (
                LABELS.replace("\n", ending).encode(),
                [],
                f"*** Update File: file.txt\n@@\n-{LABEL_OLD}\n+{LABEL_NEW}",
                "file.txt already contains this change. No file was changed.",
            )
            for ending in ("\n", "\r\n")
        ),
        (
            b"old\n",
            [],
            update("@@\n-old\n+new\n@@\n-new\n+old"),
            "The changes to file.txt cancel each other out, so it is the same as before.",
        ),
        (
            b"hi\n",
            [],
            "*** Move File: file.txt -> file.txt",
            "file.txt already has that name. No file was changed.",
        ),
        # Retried hunks are not applied twice.
        (
            b"alpha\none\nbeta\ntwo\nomega\n",
            [update(FIRST), update(FIRST + "\n@@\n beta\n" + SECOND)],
            update(FIRST + "\n" + SECOND),
            None,
        ),
    ],
)
def test_changes_already_made_leave_the_file_untouched(tmp_path, before, earlier, patch, content):
    path = tmp_path / "file.txt"
    path.write_bytes(before)
    for applied in earlier:
        assert apply(tmp_path, applied)["ok"]
    current = path.read_bytes(), path.stat().st_mtime_ns

    result = apply(tmp_path, patch)

    assert result["ok"] and result["data"]["status"] == "unchanged", result
    if content is not None:
        assert text(result) == content
    assert path.read_bytes() == current[0]
    # Changes that cancel each other out are written one after the other.
    if "cancel each other out" not in text(result):
        assert path.stat().st_mtime_ns == current[1]
    assert [entry.name for entry in tmp_path.iterdir()] == ["file.txt"]


MATCH = "The unchanged lines match file.txt line 2:\n1| start();\n2| summary();\n"
UNPREFIXED = (
    "The patch line 'final();' is not in file.txt. That patch line has no + prefix, so it "
    "must already be in the file; if it is new, start it with +.\n"
)


@pytest.mark.parametrize(
    ("locator", "tail"),
    [
        (" summary();", MATCH),
        ("summary();", MATCH),
        ("@@ summary();", None),
        # A new line written without its +.
        (" summary();\nfinal();", UNPREFIXED),
    ],
)
def test_context_only_patch_changes_nothing_and_shows_where_it_matches(tmp_path, locator, tail):
    path = tmp_path / "file.txt"
    path.write_bytes(b"start();\nsummary();\n")

    result = apply(tmp_path, f"*** Update File: file.txt\n@@\n{locator}\n*** End Patch")

    assert result["error"]["code"] == "no_changes"
    assert path.read_bytes() == b"start();\nsummary();\n"
    if tail is not None:
        assert text(result).startswith("The patch changes nothing: it has no - or + line")
        assert text(result).endswith(tail + "No file was changed.")


def test_context_only_patch_shows_the_lines_around_each_occurrence(tmp_path):
    lines = [f"line {number}" for number in range(1, 13)]
    lines[2] = lines[9] = "    return total"
    (tmp_path / "file.txt").write_text("\n".join(lines) + "\n")

    result = apply(tmp_path, "*** Update File: file.txt\n@@\n     return total\n*** End Patch")

    assert result["error"]["code"] == "no_changes"
    assert (
        "The unchanged lines occur 2 times in file.txt:\n"
        "1| line 1\n2| line 2\n3|     return total\n4| line 4\n5| line 5\n"
        "--\n"
        "8| line 8\n9| line 9\n10|     return total\n11| line 11\n12| line 12\n"
    ) in text(result)
    assert text(result).endswith("\nNo file was changed.")


@pytest.mark.asyncio
async def test_missing_old_string_shows_closest_text_and_existing_new_text(tmp_path):
    (tmp_path / "a.py").write_bytes(b"def f():\n    return 1\n")
    (tmp_path / "b.py").write_bytes(b"x = 3\n")
    (tmp_path / "c.py").write_bytes(b"x = 5\ny = 2\n")
    result = await call(
        tmp_path, {"file_path": "a.py", "old_string": "def f():\n    return 2", "new_string": "x"}
    )
    assert result["error"]["message"] == (
        "a.py: old_string was not found.\nThe closest text in the file, lines 1-2:\n"
        "1| def f():\n2|     return 1\nFirst difference, line 2: the file has '    return 1' "
        "where old_string has '    return 2'.\nNo file was changed."
    )
    done = await call(tmp_path, {"file_path": "b.py", "old_string": "x = 1", "new_string": "x = 3"})
    assert "The new text already occurs at line 1" in text(done)
    # What remains of a deletion occurs anyway and proves nothing.
    leftover = await call(
        tmp_path, {"file_path": "c.py", "old_string": "x = 1\ny = 2", "new_string": "y = 2"}
    )
    assert "already occurs" not in text(leftover)


@pytest.mark.parametrize(
    ("body", "report"),
    [
        (
            "@@\n-gamma delta epsilon zeta eta theta iota kappa lambda\n+changed",
            "The patch line 'gamma delta epsilon zeta eta theta iota kappa lambda' is not in the "
            "file. A - line names a line to remove, so it must match a line of the file.",
        ),
        # Session shape: new lines before the first + line were written without +.
        (
            "@@\n        'gamma delta epsilon zeta eta theta',\n+    ):\n beta",
            "The patch line \"'gamma delta epsilon zeta eta theta',\" is not in the file. "
            "That patch line has no + prefix, so it must already be in the file; if it is new, "
            "start it with +.",
        ),
        # An unchanged line away from + lines is not a new line missing its +.
        (
            "@@\n gamma delta epsilon zeta eta theta iota kappa\n-beta\n+changed",
            "The patch line 'gamma delta epsilon zeta eta theta iota kappa' is not in the file. "
            "A line without + or - is unchanged, so it must match a line of the file.",
        ),
    ],
)
def test_missing_hunk_without_close_candidate_names_a_line_the_file_lacks(tmp_path, body, report):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\nbeta\n")
    result = apply(tmp_path, update(body))
    assert result["error"] == {
        "code": "text_not_found",
        "message": f"file.txt: the lines to replace were not found.\n{report} "
        'read(path="file.txt") shows its current content.\n'
        "No file was changed.",
    }
    assert path.read_bytes() == b"alpha\nbeta\n"


def test_closest_candidates_are_raw_reusable_and_never_applied(tmp_path):
    path = tmp_path / "file.txt"
    before = "prefix\ndef deploy():\n    timeout = 30\n    retries = 5\n"
    path.write_text(before, encoding="utf-8")
    result = apply(
        tmp_path,
        "*** Add File: other.txt\n+done\n*** Update File: file.txt\n"
        "@@\n def deploy():\n-    completely unrelated declaration\n"
        "+    timeout = 60\n     retries = 5",
    )
    assert text(result).startswith("1 of 2 changes applied; 1 did not.")
    assert text(result).endswith(
        "\nCreated other.txt (1 line).\n"
        "Failed: file.txt: the lines to replace were not found.\n"
        "The closest text in the file, lines 2-4:\n"
        "2| def deploy():\n3|     timeout = 30\n4|     retries = 5\n"
        "First difference, line 3: the file has '    timeout = 30' where the patch has "
        "'    completely unrelated declaration'."
    )
    assert path.read_text(encoding="utf-8") == before
    assert (tmp_path / "other.txt").read_bytes() == b"done\n"


@pytest.mark.parametrize("bad_index", [0, 6])
def test_failed_hunk_report_shows_the_file_as_it_is_now(tmp_path, bad_index):
    path = tmp_path / "file.txt"
    before = "".join(f"setting_{i}=old\n" for i in range(7))
    path.write_text(before, encoding="utf-8")
    hunks = [
        f"@@\n-setting_{i}=old\n+setting_{i}=new"
        if i != bad_index
        else "@@\n-unrelated missing declaration\n+replacement"
        for i in range(7)
    ]
    result = apply(tmp_path, update("\n".join(hunks)))
    assert result["ok"] and result["data"]["status"] == "partial"
    expected = before
    for i in range(7):
        if i != bad_index:
            expected = expected.replace(f"setting_{i}=old", f"setting_{i}=new")
    assert path.read_text(encoding="utf-8") == expected
    # One region shows the file as it is now, including the unchanged line.
    shown = "\n".join(f"{i + 1}| {line}" for i, line in enumerate(expected.splitlines()))
    assert text(result).startswith("6 of 7 changes applied; 1 did not.")
    assert text(result).endswith(
        f"\nUpdated file.txt:\n{shown}\n"
        f"Failed: file.txt, hunk {bad_index + 1}: the lines to replace were not found.\n"
        "The patch line 'unrelated missing declaration' is not in the file. A - line names a "
        'line to remove, so it must match a line of the file. read(path="file.txt") shows its '
        "current content."
    )


def test_failed_change_to_a_file_changed_since_its_read_says_so(tmp_path):
    path = tmp_path / "file.py"
    path.write_bytes(b"value = 1\n")
    state = FileReadState()
    state.record_read("session-test", path)
    path.write_bytes(b"total = 22\n")

    result = apply(tmp_path, update("@@\n-value = 1\n+value = 3", "file.py"), state=state)

    assert result["error"]["code"] == "text_not_found"
    assert "file.py changed after this Session last read it; read it again before resending." in (
        text(result)
    )
    assert path.read_bytes() == b"total = 22\n"


def test_ambiguous_hint_reports_actual_locations_including_offset(tmp_path):
    path = tmp_path / "file.txt"
    # The lines to replace follow both occurrences of the @@ line.
    path.write_bytes(b"intro\nsection\nrepeated\nleft\nrepeated\nleft\n")
    result = apply(tmp_path, update("@@ section\n@@ repeated\n-left\n+changed"))
    assert result["error"]["code"] == "ambiguous_context"
    assert 'the @@ line "repeated" occurs 2 times (lines 3, 5).' in text(result)
    assert text(result).endswith(
        "Where it occurs:\n2| section\n3| repeated\n4| left\n5| repeated\n6| left\n"
        "No file was changed."
    )
    assert path.read_bytes() == b"intro\nsection\nrepeated\nleft\nrepeated\nleft\n"


def test_context_block_that_is_not_found_shows_its_first_difference(tmp_path):
    path = tmp_path / "file.txt"
    before = b"first\nmarker\nvalue=1\nsecond\nmarker\nvalue=1\n"
    path.write_bytes(before)
    result = apply(tmp_path, update("@@\n second\n markr\n@@\n-value=1\n+value=2"))
    assert result["error"]["code"] == "context_not_found"
    assert text(result) == (
        "file.txt: the lines of the @@ block above the lines to replace were not found "
        "together. That block has no - or + line, so it only locates the lines to replace. "
        "Copy its lines exactly from the file, or leave that block out.\n"
        "The closest text in the file, lines 4-5:\n4| second\n5| marker\n"
        "First difference, line 5: the file has 'marker' where the patch has 'markr'.\n"
        "No file was changed."
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("hint", "removed", "code", "third"),
    [
        # Without the @@ line, the lines to replace place nothing either.
        ("value", "value=5", "context_not_found", "value=1"),
        # The lines to replace follow both occurrences of "marker".
        ("marker", "value=2", "ambiguous_context", "value=2"),
    ],
)
def test_context_failure_returns_candidates_without_substituting_target(
    tmp_path, hint, removed, code, third
):
    path = tmp_path / "file.txt"
    before = f"first\nmarker\n{third}\nsecond\nmarker\nvalue=2\n".encode()
    path.write_bytes(before)
    result = apply(tmp_path, update(f"@@ {hint}\n-{removed}\n+value=3"))
    assert result["error"]["code"] == code
    # Every place the @@ line could mean is shown with line numbers; none is picked.
    assert f"3| {third}" in text(result) and "6| value=2" in text(result)
    assert path.read_bytes() == before
    corrected = apply(tmp_path, update("@@ second\n marker\n-value=2\n+value=3"))
    assert corrected["data"]["status"] == "applied"
    assert path.read_bytes() == f"first\nmarker\n{third}\nsecond\nmarker\nvalue=3\n".encode()


@pytest.mark.parametrize(
    ("before", "body", "listed"),
    [
        (
            'import os\n\n    g = Greeter("world")  # hi\n    h = Greeter("world")\n',
            '@@\n import os\n-Greeter("world")\n+Greeter("mars")',
            "The patch line 'Greeter(\"world\")' is only part of lines 3, 4. Each patch line is "
            "a whole line, so copy all of the line you mean:\n"
            '3|     g = Greeter("world")  # hi\n4|     h = Greeter("world")\n',
        ),
        # Inside many lines, the closest texts are shown instead.
        (
            "".join(f"{prefix}_x = compute(1)\n" for prefix in "abcde"),
            "@@\n-x = compute(1)\n+y",
            "The closest texts in the file:\n1| a_x = compute(1)\n",
        ),
        # Session shapes: short text inside an unrelated line occurs there by chance.
        (
            "start = 0\nnote = 'a false-pass shape'\n",
            "@@\n start = 0\n-pass\n+return",
            "The patch line 'pass' is not in the file.",
        ),
        (
            "start = 0\nA, B, C = 0, 1, 2\n",
            "@@\n start = 0\n-1,\n+3,",
            "First difference, line 2: the file has 'A, B, C = 0, 1, 2' where the patch has '1,'.",
        ),
    ],
)
def test_patch_line_inside_longer_lines_names_those_lines(tmp_path, before, body, listed):
    (tmp_path / "file.txt").write_bytes(before.encode())
    result = apply(tmp_path, update(body))
    assert result["error"]["code"] == "text_not_found"
    assert listed in text(result)
    assert ("is only part of" in text(result)) is ("Greeter" in before)


FIRST_DIFFERENCE_CASES = {
    # An unprefixed line between additions that the file lacks there names the prefix.
    "unprefixed": (
        "def total(values):\n    result = 0\n    for value in values:\n"
        "        result += value\n    return result\n\n\ndef other():\n    pass\n",
        "@@\n     result = 0\n+    if not values:\n+        raise ValueError(\n"
        '            "values must not be empty")\n+    checked = True\n'
        "     for value in values:\n         result += value\n     return result_value",
        "First difference, line 3: the file has '    for value in values:' where the patch "
        "has '           \"values must not be empty\")'.\n"
        "That patch line has no + prefix, so it must already be in the file there; "
        "if it is new, start it with +.\n",
    ),
    # Session shape: the last line, right after the + lines, was written without +.
    # A line written without + before a block's first + line is read as unchanged.
    "leading": (
        "def f():\n    return 1\n\n\ndef g():\n    pass\n",
        "@@\n def f():\n     return 1\n \n# first note line\n+# second note line\n \n def g():",
        "First difference, line 4: the file has '' where the patch has '# first note line'.\n"
        "That patch line has no + prefix, so it must already be in the file there; "
        "if it is new, start it with +.\n",
    ),
    # New lines without a prefix shift the copy; the difference is still found.
    "shifted": (
        "".join(f"line_{index:02d} = {index}\n" for index in range(1, 12))
        + "\nPATTERNS = {\n    'a': 1,\n    'b': 2,\n}\n\n\ndef check(rows):\n    return rows\n",
        "@@\n PATTERNS = {\n     'a': 1,\n"
        "+    # a long explanatory comment about the new pattern entry\n"
        "     # and its continuation line that the Agent forgot to mark as added text\n"
        "+    'c': 3,\n     'b': 2,\n }\n \n \n-def check(rows):\n+def check(rowz):\n"
        "     return rowz",
        "First difference, line 15: the file has \"    'b': 2,\" where the patch has "
        "'    # and its continuation line that the Agent forgot to mark as added text'.\n"
        "That patch line has no + prefix",
    ),
    # Reworded lines are compared with their counterpart, not a quote line.
    "reworded": (
        "".join(f"value_{index:02d} = compute({index})\n" for index in range(1, 21))
        + '\n\ndef describe():\n    """Describe the table.\n\n'
        "    Each row lists the owner and the date it was last checked.\n"
        "    Rows without an owner are skipped.\n    The order follows the file.\n"
        '    """\n    return rows\n',
        "@@ def describe():\n"
        "     Each record names its owner and the time of the last review.\n"
        '     Records that have no owner are left out.\n     """\n+    rows = load()',
        "First difference, line 26: the file has '    Each row lists the owner and the date "
        "it was last checked.' where the patch has '    Each record names its owner and the "
        "time of the last review.'.\n",
    ),
    # Joined lines are compared from the first of them.
    "joined": (
        "".join(f"value_{index:02d} = compute({index})\n" for index in range(1, 21))
        + "\n\ndef owners(table, owner):\n"
        "    query = select(table.c.name, table.c.owner,\n"
        "                   from_=table, limit=100)\n"
        "    rows = run(query)\n    return [row.name for row in rows]\n",
        "@@ def owners(table, owner):\n"
        "     query = select(table.c.title, from_=table, limit=100)\n"
        "     rows = execute(query)\n-    return [row.title for row in rows]\n"
        "+    return sorted(row.title for row in rows)",
        "First difference, line 24: the file has '    query = select(",
    ),
    # Long new lines without a prefix do not hide the copied ones.
    "hidden": (
        "".join(f"value_{index:02d} = compute({index})\n" for index in range(1, 40))
        + "\n\ndef parse(tokens):\n    head = tokens[0]\n    rest = tokens[1:]\n"
        + "    return head, rest\n"
        + "".join(f"other_{index:02d} = compute({index})\n" for index in range(1, 40)),
        "@@\n def parse(tokens):\n     head = tokens[0]\n"
        "+    # Validate the token stream before splitting it into head and rest parts.\n"
        "     # An empty stream has no head, so it is rejected here with a clear message\n"
        "     # instead of failing later with an IndexError deep inside the parser code.\n"
        "+    if not tokens:\n+        raise ValueError('empty')\n"
        "     rest = tokens[1:]\n     return head, rst",
        "The closest text in the file, lines 42-47:\n42| def parse(tokens):\n",
    ),
}


@pytest.mark.parametrize("case", FIRST_DIFFERENCE_CASES)
def test_first_difference_is_aligned_with_the_copied_lines(tmp_path, case):
    before, body, expected = FIRST_DIFFERENCE_CASES[case]
    path = tmp_path / "file.py"
    path.write_bytes(before.encode())
    result = apply(tmp_path, update(body, "file.py"))
    assert result["error"]["code"] == "text_not_found"
    assert expected in text(result)
    assert path.read_bytes() == before.encode()
    if case == "unprefixed":
        marked = body.replace('\n            "values', '\n+            "values')
        assert "no + prefix" not in text(apply(tmp_path, update(marked, "file.py")))
    if case == "hidden":
        assert "No similar text" not in text(result)
        assert (
            "First difference, line 44: the file has '    rest = tokens[1:]' where the patch "
            "has '    # An empty stream has no head, so it is rejected here with a clear message'."
        ) in text(result)


def _read_calls(message):
    """Interpret the displayed calls as a caller would, without inventing offsets."""
    calls = []
    for match in re.finditer(r"read\([^\n]*?\)", message):
        node = ast.parse(match[0], mode="eval").body
        assert isinstance(node, ast.Call)
        calls.append({item.arg: ast.literal_eval(item.value) for item in node.keywords})
    return calls


DIFFERENCE = re.compile(
    r"First difference, file line (?P<line>\d+), character (?P<character>\d+); "
    r"copied line (?P<copy_line>\d+), character (?P<copy_character>\d+) \(excerpts truncated\):\n"
    r"File characters (?P<file_start>\d+)-(?P<file_end>\d+): '(?P<file>[^\n]*)'\n"
    r"(?:The patch|old_string) characters (?P<copy_start>\d+)-(?P<copy_end>\d+): "
    r"'(?P<copy>[^\n]*)'\n"
)


def _excerpts(message: str) -> dict[str, object]:
    found = DIFFERENCE.search(message)
    assert found, message
    parts: dict[str, object] = {
        key: int(value) for key, value in found.groupdict().items() if value.isdigit()
    }
    parts["file"], parts["copy"] = found["file"], found["copy"]
    return parts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "shape"), [("\n", "patch"), ("\r\n", "old_string"), ("\r", "patch")]
)
async def test_long_mismatch_is_visible_and_read_continuation_recovers(tmp_path, ending, shape):
    name = "café file.txt"
    path = tmp_path / name
    actual = "\t" + "café 🙂 " * 230 + "setting_111" + " suffix" * 60
    wanted = "    " + actual[1:].replace("setting_111", "setting_123")
    before = ending.join(("header", actual, "tail", "")).encode()
    path.write_bytes(before)
    tools = registry(read=True)
    arguments = (
        {"patch": update(f"@@\n-{wanted}\n+changed", name)}
        if shape == "patch"
        else {"path": name, "old_string": wanted, "new_string": "changed"}
    )

    result = await call(tmp_path, arguments, tools=tools)

    assert result["error"]["code"] == "text_not_found"
    assert path.read_bytes() == before
    message = text(result)
    assert len(message) < 2500
    difference = _excerpts(message)
    # Earlier indentation differences must not hide the actual conflicting value.
    assert (difference["line"], difference["copy_line"]) == (2, 1)
    assert difference["character"] == actual.index("111") + 2
    assert difference["copy_character"] == wanted.index("123") + 2
    assert difference["file"] == actual[difference["file_start"] - 1 : difference["file_end"]]
    assert difference["copy"] == wanted[difference["copy_start"] - 1 : difference["copy_end"]]
    assert "setting_111" in difference["file"] and "setting_123" in difference["copy"]
    assert len(difference["file"]) <= 240 and len(difference["copy"]) <= 240
    calls = _read_calls(message)
    assert calls == [{"path": name, "offset": "2:1201", "limit": 1}]
    continued = await call(tmp_path, calls[0], tools=tools, name="read")
    continued_line = continued["data"]["content"].split("\n")[0]
    assert continued_line == "2:1201| " + actual[1200:]
    # The initial raw candidate plus the returned continuation are a reusable locator.
    prefix = next(line.partition("| ")[2] for line in message.split("\n") if line.startswith("2| "))
    recovered = prefix + continued_line.partition("| ")[2]
    assert recovered == actual
    corrected = await call(
        tmp_path, {"patch": update(f"@@\n-{recovered}\n+changed", name)}, tools=tools
    )
    assert corrected["data"]["status"] == "applied"
    assert path.read_bytes() == ending.join(("header", "changed", "tail", "")).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["file", "copy"])
async def test_missing_suffix_keeps_end_of_line_and_extra_text_visible(tmp_path, side):
    shorter = "shared text " * 180
    if side == "file":
        # A shorter old_string inside one line would match that part, so a next line follows.
        content, old = f"header\n{shorter}extra_value\ntail\n", f"{shorter}\ntail"
    else:
        content, old = f"{shorter}\n", f"{shorter}extra_value"
    (tmp_path / "file.txt").write_bytes(content.encode())

    result = await call(tmp_path, {"path": "file.txt", "old_string": old, "new_string": "x"})

    difference = _excerpts(text(result))
    assert difference["character"] == difference["copy_character"] == len(shorter) + 1
    assert difference[side].endswith("extra_value")
    assert difference["copy" if side == "file" else "file"].endswith("shared text ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "first", "resume"),
    [
        (12, 0, 9),
        # Overlapping closest excerpts merge; one continuation follows all of them.
        (30, 10, 18),
    ],
)
async def test_candidate_cut_between_lines_resumes_at_next_line(tmp_path, count, first, resume):
    lines = [f"section_{index} = value_{index}" for index in range(count)]
    before = "\n".join(lines) + "\n"
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    tools = registry(read=True)
    result = await call(
        tmp_path,
        {
            "path": "file.txt",
            "old_string": "\n".join(lines[first : first + 12]).replace("value_10", "value_99")
            + "\n",
            "new_string": "changed",
            "replace_all": True,
        },
        tools=tools,
    )
    assert result["error"]["code"] == "text_not_found"
    assert path.read_bytes() == before.encode()
    assert "value_10" in text(result) and "value_99" in text(result)
    assert f"\n{resume - 1}| " in text(result) and f"\n{resume}| " not in text(result)
    calls = _read_calls(text(result))
    assert calls == [{"path": "file.txt", "offset": f"{resume}:1", "limit": 4}]
    continued = await call(tmp_path, calls[0], tools=tools, name="read")
    assert continued["data"]["content"].split("\n")[:4] == [
        f"{number}| {lines[number - 1]}" for number in range(resume, resume + 4)
    ]


@pytest.mark.asyncio
async def test_ambiguous_long_lines_preserve_each_read_continuation(tmp_path):
    line = "repeated " * 200 + "tail_value"
    before = f"{line}\nseparator\n{line}\n"
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    tools = registry(read=True)
    result = await call(tmp_path, {"patch": update(f"@@\n-{line}\n+changed")}, tools=tools)
    assert result["error"]["code"] == "ambiguous_match"
    assert path.read_bytes() == before.encode()
    calls = _read_calls(text(result))
    assert calls == [
        {"path": "file.txt", "offset": "1:241", "limit": 1},
        {"path": "file.txt", "offset": "3:241", "limit": 1},
    ]
    for read_call in calls:
        continued = await call(tmp_path, read_call, tools=tools, name="read")
        assert continued["data"]["content"].split("\n")[0] == (
            f"{read_call['offset']}| {line[240:]}"
        )
