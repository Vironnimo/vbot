"""apply_patch matching: which lines a hunk or old_string changes, and when it refuses."""

from __future__ import annotations

import pytest

from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_test_support import apply, call, registry, text, update

GUTTER_NOTE = "Note: Removed read-output line-number prefixes"


@pytest.mark.parametrize(
    ("before", "hunk", "after"),
    [
        (
            "  anchor\n    old\n  tail\n",
            "@@\n anchor\n-  old\n+  new\n tail",
            "  anchor\n    new\n  tail\n",
        ),
        (
            "label = “quoted”\nold\ntail\n",
            '@@\n label = "quoted"\n-old\n+new\n tail',
            "label = “quoted”\nnew\ntail\n",
        ),
        (
            "alpha\t = 1\nold\ntail\n",
            "@@\n alpha  = 1\n-old\n+new\n tail",
            "alpha\t = 1\nnew\ntail\n",
        ),
        ("keep\nremove\ntail\n", "@@\n-remove", "keep\ntail\n"),
        ("keep\nremove", "@@\n-remove", "keep\n"),
        ("keep\r\nremove\n", "@@\n keep\n-remove", "keep\r\n"),
        ("keep", "@@\n keep\n+added", "keep\nadded"),
        ("keep\n", "@@\n+added", "keep\nadded\n"),
        ("same\n", "@@\n+same", "same\nsame\n"),
        ("", "@@\n+added", "added\n"),
        ("first\nlast\n", "@@ first\n+middle", "first\nmiddle\nlast\n"),
        ("start\nlast\n", "@@\n+above\n last", "start\nabove\nlast\n"),
        ("same\nfirst\nsame\n", "@@\n-same\n+new\n*** End of File", "same\nfirst\nnew\n"),
        ("old\n", "@@\n-old\n+new\n\\ No newline at end of file", "new"),
        ("a\n\nb\n", "@@\n a\n\n-b\n+c", "a\n\nc\n"),
        ("foobar\nfoo\n", "@@\n-foo\n+new", "foobar\nnew\n"),
        ('pattern = r"\\n"\n', '@@\n-pattern = r"\\n"\n+pattern = r"\\t"', 'pattern = r"\\t"\n'),
        ("alpha\n\nomega\n", "@@\n-", "alpha\nomega\n"),
        ("alpha\n\nomega\n", "@@\n-\n+middle", "alpha\nmiddle\nomega\n"),
        # A blank last unchanged line past the final line break keeps it.
        ("last\r\n", "@@\n last\n \n+added\n+more", "last\r\n\r\nadded\r\nmore\r\n"),
        ("alpha\n\nomega\n", "@@\n alpha\n \n+middle\n omega", "alpha\n\nmiddle\nomega\n"),
        ("alpha\nold\nomega\n", "@@\n \n alpha\n-old\n+new\n omega\n ", "alpha\nnew\nomega\n"),
        ("alpha\r\nold\nomega\r", "@@\n alpha\n-old\n+new\n omega", "alpha\r\nnew\r\nomega\r"),
        # Gutter-shaped file content matches literally; indented gutters keep indentation.
        (
            "1| alpha\n2| old\n3| omega\n",
            "@@\n 1| alpha\n-2| old\n+2| new\n 3| omega",
            "1| alpha\n2| new\n3| omega\n",
        ),
        ("    old\n    tail\n", "@@\n-1|    old\n+1|    new\n 2|    tail", "    new\n    tail\n"),
        (
            "    old\n    tail\n",
            "@@\n-1|     old\n+1|     new\n 2|     tail",
            "    new\n    tail\n",
        ),
        ("old\n", "@@\n-old\n+1| first\n+2| second", "first\nsecond\n"),
    ],
)
def test_update_changes_exactly_the_matched_lines(tmp_path, before, hunk, after):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    result = apply(tmp_path, update(hunk))
    assert result["ok"], result
    assert path.read_bytes() == after.encode()


@pytest.mark.parametrize("separator", ["\f", "\u2028"])
def test_unicode_separators_are_line_content_not_line_breaks(tmp_path, separator):
    path = tmp_path / "file.txt"
    path.write_bytes(f"alpha{separator}beta\nomega".encode())

    missing = apply(tmp_path, update("@@\n-beta\n+changed\n omega"))
    appended = apply(tmp_path, update(f"@@\n-alpha{separator}beta\n+changed\n omega"))

    assert missing["error"]["code"] == "text_not_found"
    assert appended["ok"], appended
    assert path.read_bytes() == b"changed\nomega"


@pytest.mark.parametrize(
    ("before", "hunk"),
    [
        (b"x\n}\n}\n}\ny\n", "@@\n }\n+INSERTED\n }"),
        (b"a {\n}\n}\nb {\n  c {\n    }\n}\n}\n", "@@\n }\n+INSERTED\n }"),
        (
            b"def a():\n    return 1\n    return 1\n\ndef b():\n"
            b"    if x:\n        return 1\n    return 1\n    return 1\n",
            "@@\n-    return 1\n     return 1",
        ),
        # A multi-line context anchor can overlap its own other occurrence too.
        (b"x\n}\n}\n}\ny\n", "@@\n }\n }\n@@\n+INSERTED"),
        (b"a\n\nb\n\nc\n", "@@\n-\n+new"),
        # Normalized read gutters do not choose an occurrence by their number.
        (b"old\nseparator\nold\n", "@@\n-3| old\n+new"),
    ],
)
def test_repeated_or_overlapping_occurrences_are_ambiguous_not_first_match(tmp_path, before, hunk):
    path = tmp_path / "file.txt"
    path.write_bytes(before)

    result = apply(tmp_path, update(hunk))

    assert result["error"]["code"] in {"ambiguous_match", "ambiguous_context"}
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "hunk",
    [
        # Complete read gutters, spaced and compact.
        "@@\n 1| alpha\n-2| old\n+3| new\n 3| omega",
        "@@\n 1|alpha\n-2|old\n+3|new\n 3|omega",
        # Single, mixed and stale gutters, located by the unique current line.
        "@@\n 1| alpha\n-7| old\n+new\n 9| omega",
        "@@\n alpha\n-2| old\n+new\n omega",
        "@@\n-2| old\n+2| new",
        "@@\n-2|old\n+new",
    ],
)
def test_read_gutters_are_removed_without_corrupting_added_lines(tmp_path, hunk):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\nold\nomega\n")
    result = apply(tmp_path, update(hunk))
    assert result["ok"], result
    assert path.read_bytes() == b"alpha\nnew\nomega\n"
    assert GUTTER_NOTE in text(result)


@pytest.mark.parametrize(
    "hunk",
    [
        "@@\n 1:20| alpha\n-2:20| old\n+new\n 3:20| omega",
        "@@\n alpha\n-2| old\n+2:20| new\n omega",
        "@@\n-2:20| old\n+new",
        "@@\n-alpha\n+1| first\n+3| third",
    ],
)
def test_damaged_continuation_or_inconsistent_gutters_fail_closed(tmp_path, hunk):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\nold\nomega\n")
    result = apply(tmp_path, update(hunk))
    assert result["error"]["code"] == "line_numbered_content"
    assert path.read_bytes() == b"alpha\nold\nomega\n"


@pytest.mark.parametrize(
    ("patch", "name", "after"),
    [
        (
            "*** Add File: new.txt\n+Rows:\n+10|ann |100.0\n+11|bob | 90.0\n+Done.\n*** End Patch",
            "new.txt",
            b"Rows:\n10|ann |100.0\n11|bob | 90.0\nDone.\n",
        ),
        (
            update("@@\n alpha\n-old\n+Rows:\n+10|ann\n+11|bob\n omega"),
            "file.txt",
            b"alpha\nRows:\n10|ann\n11|bob\nomega\n",
        ),
    ],
)
def test_some_gutter_shaped_added_lines_are_written_as_content(tmp_path, patch, name, after):
    (tmp_path / "file.txt").write_bytes(b"alpha\nold\nomega\n")

    result = apply(tmp_path, patch)
    missing = apply(tmp_path, update("@@\n-absent\n+Rows:\n+10|ann\n+11|bob"))

    assert result["ok"], result
    assert (tmp_path / name).read_bytes() == after
    assert "2 added lines start with a number and | like read output, such as '10|ann" in (
        text(result)
    )
    assert missing["error"]["code"] == "text_not_found"


@pytest.mark.asyncio
async def test_actual_read_output_can_be_used_directly_in_a_patch(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"\xef\xbb\xbf  anchor\r\n    old\r\n\r\n  tail\r\n")
    tools = registry(FileReadState(), read=True)
    read_result = await call(tmp_path, {"path": "file.txt"}, tools=tools, name="read")
    lines = read_result["data"]["content"].splitlines()
    patch_lines = [" " + line for line in lines]
    patch_lines[1:2] = ["-" + lines[1], "+" + lines[1].replace("old", "new")]

    result = await call(tmp_path, {"patch": update("@@\n" + "\n".join(patch_lines))}, tools=tools)

    assert result["ok"], result
    assert path.read_bytes() == b"\xef\xbb\xbf  anchor\r\n    new\r\n\r\n  tail\r\n"


@pytest.mark.parametrize(
    ("before", "body", "expected"),
    [
        ("alpha\n\told\nomega\n", "@@\n alpha\n-\\told\n+\\tnew\n omega", "alpha\n\tnew\nomega\n"),
        ("\told\n", '@@\n-\\told\n+\\tvalue = "\\n"', '\tvalue = "\\n"\n'),
        ("\told\n", '@@\n-\\told\n+\\tvalue = "\\t"', '\tvalue = "\\t"\n'),
        ("alpha\nold\n", '@@\n-alpha\\nold\n+value = "\\n"', 'value = "\\n"\n'),
        ('say("old")\n', '@@\n-say(\\"old\\")\n+say(\\"new\\")', 'say("new")\n'),
        ("say('old')\n", "@@\n-say(\\'old\\')\n+say(\\'new\\')", "say('new')\n"),
    ],
)
def test_escaped_patch_text_is_recovered_only_with_matching_evidence(
    tmp_path, before, body, expected
):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    result = apply(tmp_path, update(body))
    assert result["ok"], result
    # Escapes in the replacement stay literal where the file's text holds them.
    assert path.read_bytes() == expected.encode()
    assert "Note: Normalized escaped patch text" in text(result)


@pytest.mark.parametrize(
    ("before", "body"),
    [
        # A similar block with exact boundaries must not delete another call.
        (
            "def process(data):\n    validate(data)\n    save_to_database(data)\n    return True\n",
            " def process(data):\n-    validate(data)\n-    log(data)\n"
            "+    validate(data, strict=True)\n     return True",
        ),
        # A similar line must not stand in for a different removed value.
        (
            "TIMEOUT = 30\nRETRIES = 5\nMAX_SIZE = 1024\n",
            " TIMEOUT = 30\n-RETRIES = 3\n+RETRIES = 4\n MAX_SIZE = 1024",
        ),
        ("start\nvalue = 100\nend\n", " start\n-value = 101\n+value = 200\n end"),
        # An approximate match cannot introduce doubled escapes.
        (
            'anchor\nvalue = "original"\ntail\n',
            ' anchor\n-value = \\"originaL\\"\n+value = \\"changed\\"\n tail',
        ),
        (
            "anchor\nvalue = 'original'\ntail\n",
            " anchor\n-value = \\'originaL\\'\n+value = \\'changed\\'\n tail",
        ),
        (
            "anchor\nvalue = C:\\original\ntail\n",
            " anchor\n-value = C:\\\\originaL\n+value = C:\\\\changed\n tail",
        ),
    ],
)
def test_similarity_never_replaces_a_different_removed_line(tmp_path, before, body):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())

    result = apply(tmp_path, update("@@\n" + body))

    assert result["error"]["code"] == "text_not_found"
    assert "The closest text in the file, lines 1-" in text(result)
    assert path.read_bytes() == before.encode()


LOAD_HUNK = (
    " def load(order):\n-    return order.receipt_total\n"
    "+    return order.receipt_total + order.tax"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"patch": update(f"@@\n{LOAD_HUNK}", "a.py")},
        {"patch": update(f"@@ import os\n{LOAD_HUNK}", "a.py")},
        {
            "file_path": "a.py",
            "old_string": "def load(order):\n    return order.receipt_total",
            "new_string": "def load(order):\n    return order.receipt_total + order.tax",
        },
    ],
)
async def test_text_copied_with_a_misspelling_is_applied_and_named(tmp_path, arguments):
    path = tmp_path / "a.py"
    path.write_bytes(b"import os\n\ndef load(order):\n    return order.reciept_total\n")

    result = await call(tmp_path, arguments)

    assert result["ok"], result
    assert path.read_bytes() == (
        b"import os\n\ndef load(order):\n    return order.reciept_total + order.tax\n"
    )
    assert (
        "Line 4 did not match your old text exactly and was edited anyway; it read:     "
        "return order.reciept_total" in text(result)
    )
    assert 'Your new text uses the file\'s spelling: "reciept_total" for "receipt_total".' in (
        text(result)
    )


@pytest.mark.asyncio
async def test_old_string_copied_with_errors_resembling_several_places_is_refused(tmp_path):
    line = "total = reciept_total + shipping_cost + handling_fee"
    path = tmp_path / "a.py"
    path.write_bytes(f"{line}\n{line}\n".encode())
    result = await call(
        tmp_path,
        {"file_path": "a.py", "old_string": line.replace("reciept", "receipt"), "new_string": "x"},
    )
    assert "resembles 2 places (lines 1, 2)" in text(result)
    assert path.read_bytes() == f"{line}\n{line}\n".encode()


_BUFFER = """fn undo(&mut self) -> bool {
    let ok = self.history.undo(&mut self.text, &mut self.cursor);
    if ok {
        if self.track_revision {
            self.revision += 1;
        }
        self.shadow = Some(self.text.clone());
        self.shadow_cursor = self.cursor;
    }
    ok
}

fn redo(&mut self) -> bool {
    let ok = self.history.redo(&mut self.text, &mut self.cursor);
    if ok {
        self.revision += 1;
        self.shadow = Some(self.text.clone());
        self.shadow_cursor = self.cursor;
    }
    ok
}
"""


def _buffer_hunk(operation, *, guard=False):
    revision = (
        "         if self.track_revision {\n             self.revision += 1;\n         }\n"
        if guard
        else "         self.revision += 1;\n"
    )
    return (
        "@@\n"
        f"     let ok = self.history.{operation}(&mut self.text, &mut self.cursor);\n"
        "     if ok {\n" + revision + "-        self.shadow = Some(self.text.clone());\n"
        "+        self.shadow = Some(counted_clone(&self.text));\n"
        "         self.shadow_cursor = self.cursor;\n"
        "     }\n"
        "     ok\n"
    )


@pytest.mark.parametrize("both", [False, True])
def test_stale_code_context_never_selects_a_similar_function(tmp_path, both):
    target = tmp_path / "buffer.rs"
    target.write_text(_BUFFER, encoding="utf-8")
    patch = "*** Update File: buffer.rs\n" + _buffer_hunk("undo")
    if both:
        patch += _buffer_hunk("redo")

    result = apply(tmp_path, patch)

    expected = _BUFFER
    if both:
        start = expected.index("fn redo")
        expected = expected[:start] + expected[start:].replace(
            "Some(self.text.clone())", "Some(counted_clone(&self.text))"
        )
        assert result["data"]["status"] == "partial"
        assert "hunk 1" in result["data"]["content"]
    else:
        assert result["error"]["code"] == "text_not_found"
    assert target.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_actual_code_context_changes_only_its_function(tmp_path, indent):
    target = tmp_path / "buffer.rs"
    original = _BUFFER.replace("    ", indent)
    target.write_text(original, encoding="utf-8")

    result = apply(tmp_path, "*** Update File: buffer.rs\n" + _buffer_hunk("undo", guard=True))

    assert result["data"]["status"] == "applied"
    assert target.read_text(encoding="utf-8") == original.replace(
        "Some(self.text.clone())", "Some(counted_clone(&self.text))", 1
    )


TAIL = "    let value = data.current_state();\n    let answer = value + delta;\n"
KEPT_TAIL = "     let value = data.current_state();\n-    let answer = value + delta;\n"


@pytest.mark.parametrize(
    ("original", "body"),
    [
        *(
            (held + "\n" + TAIL, f" {copied}\n{KEPT_TAIL}+    let answer = value + offset;")
            for copied, held in [
                ("let ok = journal.undo(data);", "let ok = journal.redo(data);"),
                ("let ok = undo(data);", "let ok = redo(data);"),
                ("let ok = journal.primary;", "let ok = journal.backup;"),
                ("fn undo(data: State) {", "fn redo(data: State) {"),
                ("let ok = Undo::execute(data);", "let ok = Redo::execute(data);"),
                ("let ok = journal->undo(data);", "let ok = journal->redo(data);"),
            ]
        ),
        # Surrounding anchors do not override a known member name.
        (
            "let previous = journal.primary;\nfn dispatch(data: State) {\n"
            "    let selected = journal.backup;\n    let result = selected + delta;\n"
            "    return result;\n",
            " fn dispatch(data: State) {\n     let selected = journal.primary;\n"
            "-    let result = selected + delta;\n+    let result = selected + offset;\n"
            "     return result;",
        ),
        # A name the file holds nearby is not a typo.
        (
            "let elsewhere = journal.restroe(other);\nlet ok = journal.restore(data);\n"
            "    let answer = value + delta;\n",
            " let ok = journal.restroe(data);\n-    let answer = value + delta;\n"
            "+    let answer = value + offset;",
        ),
    ],
)
def test_code_names_are_not_treated_as_copy_errors(tmp_path, original, body):
    target = tmp_path / "target.txt"
    target.write_text(original, encoding="utf-8")

    result = apply(tmp_path, update("@@\n" + body, "target.txt"))

    assert not result["ok"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("original", "body", "expected"),
    [
        # A unique misspelled call name still recovers.
        (
            "let ok = journal.restore(data);\n    let answer = value + delta;\n",
            " let ok = journal.restroe(data);\n-    let answer = value + delta;\n"
            "+    let answer = value + offset;",
            "let ok = journal.restore(data);\n    let answer = value + offset;\n",
        ),
        # Explicit name changes apply to the named lines.
        (
            "let ok = journal.undo(data);\n    let answer = value + delta;\n",
            "-let ok = journal.undo(data);\n+let ok = journal.redo(data);\n"
            "     let answer = value + delta;",
            "let ok = journal.redo(data);\n    let answer = value + delta;\n",
        ),
    ],
)
def test_code_name_edits_apply_to_their_target(tmp_path, original, body, expected):
    target = tmp_path / "target.txt"
    target.write_text(original, encoding="utf-8")

    result = apply(tmp_path, update("@@\n" + body, "target.txt"))

    assert result["data"]["status"] == "applied"
    assert target.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    ("before", "old", "new", "expected"),
    [
        ("score = −1", "score = -1", "score = -2", "score = −2"),
        # A glyph the new text writes itself is kept, even where the file has another.
        ("score = −1", "score = -1", "score = –2", "score = –2"),
        ("“old” — value…", '"old" -- value...', '"new" -- value...', "“new” — value…"),
        ("name\u2009= old", "name = old", "name = new", "name\u2009= new"),
        ("    title—old", "  title--old", "  title--new", "    title—new"),
        ("    —", "--", "new", "    new"),
        ("title—old", "title--old", "title: new", "title: new"),
        ("title—old", "title—old", "title--new", "title--new"),
    ],
)
def test_normalized_changed_lines_preserve_only_unchanged_typography(
    tmp_path, before, old, new, expected
):
    path = tmp_path / "file.txt"
    path.write_bytes((before + "\r\n").encode())
    result = apply(tmp_path, update(f"@@\n-{old}\n+{new}"))
    assert result["ok"], result
    assert path.read_bytes() == (expected + "\r\n").encode()


@pytest.mark.parametrize(
    ("marker", "code"),
    [("*** End of File", "text_not_found"), ("\\ No newline at end of file", "invalid_patch")],
)
def test_hinted_additions_cannot_ignore_end_of_file_anchors(tmp_path, marker, code):
    path = tmp_path / "file.txt"
    path.write_bytes(b"section\ntail\n")
    result = apply(tmp_path, update(f"@@ section\n+inserted\n{marker}"))
    assert result["error"]["code"] == code
    assert path.read_bytes() == b"section\ntail\n"


def test_end_of_file_marker_on_lines_elsewhere_is_ignored_and_named(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"first\nold\ntail\n")

    result = apply(tmp_path, update("@@\n first\n-old\n+new\n*** End of File"))

    assert result["ok"], result
    assert path.read_bytes() == b"first\nnew\ntail\n"
    assert "The lines before *** End of File are not at the end of the file" in text(result)


def test_hint_may_be_repeated_in_context_and_insert_retry_is_anchored(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"section\nold\ntail\n")
    assert apply(tmp_path, update("@@ section\n section\n-old\n+new\n tail"))["ok"]
    patch = update("@@ section\n+inserted")
    assert apply(tmp_path, patch)["ok"]
    before = path.read_bytes()
    retried = apply(tmp_path, patch)
    assert retried["data"]["status"] == "unchanged"
    assert "file.txt already contains this change" in text(retried)
    assert path.read_bytes() == before == b"section\ninserted\nnew\ntail\n"


@pytest.mark.parametrize(
    ("added", "noted"),
    [
        # Session shape: a rewritten table row was inserted below the row it rewrote.
        ("timeout_seconds = 60", True),
        ("retry_delay = 5", False),
    ],
)
def test_insertion_that_resembles_its_at_at_line_is_named(tmp_path, added, noted):
    path = tmp_path / "file.txt"
    path.write_bytes(b"timeout_seconds = 30\nretries = 2\n")
    result = apply(tmp_path, update(f"@@ timeout_seconds = 30\n+{added}"))
    assert result["data"]["status"] == "applied", result
    assert path.read_bytes() == f"timeout_seconds = 30\n{added}\nretries = 2\n".encode()
    note = (
        "Note: The + lines were inserted below the @@ line 'timeout_seconds = 30', which stays "
        "in the file, and the first of them resembles it. If that line was meant to be "
        "replaced, remove it with a - line."
    )
    assert text(result).endswith(note) is noted


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
            "places (lines 3, 7)."
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
    ("body", "note"),
    [
        # Session shape: the @@ line paraphrases a line the file lacks.
        (
            "@@ missing\n-value=1\n+value=2",
            "The @@ line 'missing' was not found, but the lines to replace occur once in the "
            "file; they were changed there, at line 2.",
        ),
        # Session shape: the @@ line names a line below the lines to replace.
        (
            "@@ second\n-value=1\n+value=2",
            "The lines to replace are not after the @@ line 'second', but they occur once in "
            "the file; they were changed there, at line 2.",
        ),
        (
            "@@\n first\n secnd\n@@\n-value=1\n+value=2",
            "The lines of the @@ block above the lines to replace were not found together, but "
            "the lines to replace occur once in the file; they were changed there, at line 2.",
        ),
    ],
)
def test_lines_that_occur_once_are_changed_where_the_at_at_line_misses_them(tmp_path, body, note):
    path = tmp_path / "file.txt"
    path.write_bytes(b"first\nvalue=1\nsecond\nend\n")
    result = apply(tmp_path, update(body))
    assert result["data"]["status"] == "applied", result
    assert path.read_bytes() == b"first\nvalue=2\nsecond\nend\n"
    assert text(result).endswith(f"Note: {note}")


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


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Session shape: as many identical hunks as the file holds their lines.
        (b"x=0\nmid\nx=0\n", b"x=1\nmid\nx=2\n"),
        # With more occurrences than hunks, which ones are meant stays open.
        (b"x=0\nmid\nx=0\nx=0\n", None),
    ],
)
def test_identical_hunks_change_as_many_occurrences_in_order(tmp_path, before, after):
    path = tmp_path / "file.txt"
    path.write_bytes(before)
    result = apply(tmp_path, update("@@\n-x=0\n+x=1\n@@\n-x=0\n+x=2"))
    if after is None:
        assert "the lines to replace occur 3 times (lines 1, 3, 4)" in text(result)
        assert path.read_bytes() == before
    else:
        assert path.read_bytes() == after
        assert text(result).endswith(
            "Note: The lines to replace occur 2 times, as many times as hunks of this patch "
            "name them; the hunks change them in order, so the first, at line 1, was changed."
        )


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


LONG_LINE = "The quick brown fox jumps over the lazy dog, and then keeps running home."


def test_one_removed_line_inside_a_longer_line_is_replaced_within_it(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(f"# Title\n{LONG_LINE}\n".encode())

    result = apply(tmp_path, update("@@\n-quick brown fox\n+quick red fox"))

    assert result["ok"], result
    assert path.read_bytes() == f"# Title\n{LONG_LINE.replace('brown', 'red')}\n".encode()
    assert "Note: The - line is part of line 2; only that part of the line was replaced." in (
        text(result)
    )


@pytest.mark.parametrize(
    ("before", "body"),
    [
        # Unchanged lines around the part keep whole-line semantics.
        (f"# Title\n{LONG_LINE}\n", "@@\n # Title\n-quick brown fox\n+quick red fox"),
        # Removing a part of a line could also mean removing the whole line.
        (f"# Title\n{LONG_LINE}\n", "@@\n-quick brown fox"),
        # "aa" occurs twice in "aaa": the place to change is unclear.
        ("aaa\n", "@@\n-aa\n+b"),
    ],
)
def test_part_of_a_line_is_not_replaced_when_the_place_or_meaning_is_open(tmp_path, before, body):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())

    result = apply(tmp_path, update(body))

    assert result["error"]["code"] == "text_not_found"
    assert path.read_bytes() == before.encode()


@pytest.mark.parametrize(
    ("body", "added", "example"),
    [
        # Session shape: the + on a statement's continuation lines was left off.
        (
            "@@ def f(rows):\n         check(row)\n+        if row in seen:\n"
            '+            raise ValueError(\n                f"duplicate {row}"\n'
            "+            )\n\n+        seen.add(row)\n     return rows",
            b"        if row in seen:\n            raise ValueError(\n"
            b'                f"duplicate {row}"\n            )\n\n        seen.add(row)\n',
            'f"duplicate {row}"',
        ),
        # Session shape: statements written with the space prefix of unchanged
        # lines, one space deeper than the + lines around them.
        (
            "@@\n         check(row)\n+        if row in seen:\n+            seen.discard(row)\n"
            "             log(row)\n             count(row)\n+        seen.add(row)\n"
            "     return rows",
            b"        if row in seen:\n            seen.discard(row)\n            log(row)\n"
            b"            count(row)\n        seen.add(row)\n",
            "log(row)",
        ),
        # Session shape: one line written with the space prefix, the next without it.
        (
            "@@\n         check(row)\n+        if row in seen:\n             seen.discard(row)\n"
            "            log(row)\n+        seen.add(row)\n     return rows",
            b"        if row in seen:\n            seen.discard(row)\n            log(row)\n"
            b"        seen.add(row)\n",
            "seen.discard(row)",
        ),
        # Session shapes: continuation lines aligned to an odd column after an open
        # bracket, or to the start of the last item inside it, stay aligned.
        (
            "@@\n         check(row)\n+        res = call(ky,\n                   alpha,\n"
            "                   beta)\n+        done(res)\n     return rows",
            b"        res = call(ky,\n                   alpha,\n                   beta)\n"
            b"        done(res)\n",
            "alpha,",
        ),
        (
            "@@\n         check(row)\n+        res = call(ky, alpha +\n"
            "                       beta +\n                       gamma)\n"
            "+        done(res)\n     return rows",
            b"        res = call(ky, alpha +\n                       beta +\n"
            b"                       gamma)\n        done(res)\n",
            "beta +",
        ),
    ],
)
def test_unprefixed_lines_between_additions_are_added_as_written(tmp_path, body, added, example):
    path = tmp_path / "file.py"
    path.write_bytes(b"def f(rows):\n    for row in rows:\n        check(row)\n    return rows\n")
    result = apply(tmp_path, update(body, "file.py"))
    assert result["ok"], text(result)
    assert path.read_bytes() == (
        b"def f(rows):\n    for row in rows:\n        check(row)\n" + added + b"    return rows\n"
    )
    assert text(result).endswith(
        "Note: 2 patch lines next to + lines have no + prefix, but the file does not have "
        f"them there, so they were added as + lines; for example {example!r}. Nothing more is "
        "needed for those lines; in later patches, start every added line with +."
    )


@pytest.mark.parametrize(
    ("before", "body", "after", "note"),
    [
        (
            b"start\nkeep\nend\n",
            "@@\n start\n+one\nkeep\n+two\n    three\n+four\n end",
            b"start\none\nkeep\ntwo\n    three\nfour\nend\n",
            "The patch line 'three' next to + lines has no + prefix, but the file does not "
            "have it there, so it was added as a + line. Nothing more is needed for that line;",
        ),
        # Session shape: the line after a replaced one stays unchanged while
        # blank lines without + follow in the added block.
        (
            b'__all__ = [\n    "a",\n]\n',
            '@@\n __all__ = [\n-    "a",\n+    "a", "b",\n ]\n+\n+\n+def b():\n+    x = 1\n\n'
            "+    y = 2\n\n+    return x",
            b'__all__ = [\n    "a", "b",\n]\n\n\n'
            b"def b():\n    x = 1\n\n    y = 2\n\n    return x\n",
            "2 blank patch lines next to + lines have no + prefix, but the file does not "
            "have them there, so they were added as + lines. Nothing more is needed for those "
            "lines;",
        ),
        # Session shape: a blank line without + after the last unchanged line is
        # added too, so the file's own blank line after that line stays.
        (
            b"def f():\n    start()\n\ndef g():\n    pass\n",
            "@@\n     start()\n+    if x:\n        new()\n+    done()\n\n+    more()",
            b"def f():\n    start()\n    if x:\n        new()\n    done()\n\n    more()\n\n"
            b"def g():\n    pass\n",
            "2 patch lines next to + lines have no + prefix, but the file does not have them "
            "there, so they were added as + lines; for example 'new()'. Nothing more is needed "
            "for those lines;",
        ),
        # After the last unchanged line, unprefixed lines new there are added too.
        (
            b"def f(seed):\n    produced = gen(seed)\n    return produced\n",
            "@@\n     produced = gen(seed)\n+    if not produced:\n        raise ValueError(\n"
            "            seed)\n+    log(seed)",
            b"def f(seed):\n    produced = gen(seed)\n    if not produced:\n"
            b"        raise ValueError(\n            seed)\n    log(seed)\n    return produced\n",
            None,
        ),
        # Session shape: after the last + line, a continuation line without +
        # before an unchanged line the file has is added; that line stays.
        (
            b"def f(seed):\n    produced = gen(seed)\n    return produced\n",
            "@@\n-    produced = gen(seed)\n+    produced = gen(seed,\n"
            "                   strict=True)\n     return produced",
            b"def f(seed):\n    produced = gen(seed,\n                   strict=True)\n"
            b"    return produced\n",
            "The patch line 'strict=True)' next to + lines has no + prefix, but the file does "
            "not have it there, so it was added as a + line. Nothing more is needed for that "
            "line;",
        ),
        # The hunk's last lines, written without +, are added when the file lacks them.
        (
            b"start\nkeep this line\nend\n",
            "@@\n start\n+one\n missing",
            b"start\none\nmissing\nkeep this line\nend\n",
            "The patch line 'missing' next to + lines has no + prefix, but the file does not "
            "have it there, so it was added as a + line. Nothing more is needed for that line;",
        ),
    ],
)
def test_unprefixed_lines_the_file_lacks_there_are_added(tmp_path, before, body, after, note):
    path = tmp_path / "file.py"
    path.write_bytes(before)
    result = apply(tmp_path, update(body, "file.py"))
    assert result["ok"], text(result)
    assert path.read_bytes() == after
    if note is not None:
        assert f"\nNote: {note} in later patches, start every added line with +." in text(result)


@pytest.mark.parametrize(
    "body",
    [
        # A misspelled copy of the line the file has there is not a new line.
        "@@\n start\n+one\n keep this lien\n+two\n end",
        # Without an unchanged line, nothing places the lines.
        "@@\n+one\n missing\n+two",
        # Lines before a block's first + line are never read as added.
        "@@\n missing\n+one\n start",
        # Session shape: a blank line, then a less indented line the file lacks,
        # is the Model's view of the text below its block, such as a heading.
        "@@\n start\n+    one\n+    two\n \n missing section",
    ],
)
def test_unprefixed_lines_are_added_only_where_unchanged_lines_place_them(tmp_path, body):
    path = tmp_path / "file.txt"
    path.write_bytes(b"start\nkeep this line\nend\n")
    result = apply(tmp_path, update(body))
    assert "added as a" not in text(result)
    assert b"lien" not in path.read_bytes() and b"missing" not in path.read_bytes()


@pytest.mark.parametrize(
    "body",
    [
        # Session shape: after the last unchanged line the file holds, unchanged
        # lines differ from the file in one line. Added, they would repeat the
        # loop body one space deeper.
        "@@\n     total = 0\n+    unordered = []\n     for seed in SEEDS:\n"
        "         produced = gen(seed)\n+        unordered.extend(produced)\n"
        "         for entry in produced:\n             skipped = False\n"
        "             record(entry)\n+    assert unordered",
        # The same before the first unchanged line.
        "@@\n+import os\n import sys\n SEED = 4\n+import json\n \n \n def check():",
    ],
)
def test_unprefixed_lines_are_not_added_where_the_file_has_them(tmp_path, body):
    path = tmp_path / "file.py"
    before = (
        b"import sys\nimport time\n\n\ndef check():\n    total = 0\n    for seed in SEEDS:\n"
        b"        produced = gen(seed)\n        for entry in produced:\n"
        b"            total += entry.size\n            record(entry)\n    return total\n"
    )
    path.write_bytes(before)
    result = apply(tmp_path, update(body, "file.py"))
    assert result["error"]["code"] == "text_not_found"
    assert "That patch line has no + prefix, so it must already be in the file there" in (
        text(result)
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("current", "patch", "applied"),
    [
        # An existing old line is changed although another line already reads as new.
        (
            '"Series old": pd.Series(\n"Series new": pd.Series(\n',
            '@@\n-"Series old": pd.Series(\n+"Series new": pd.Series(',
            True,
        ),
        # A precise post-state retry wins over a similar other block.
        (
            "alpha\nvalue = 222\nomega\nalpha\nvalue = 111\nomega\n",
            "@@\n alpha\n-value = 123\n+value = 222\n omega",
            False,
        ),
        # The new text without a newline at the end of the file.
        (
            '"Series new": pd.Series(',
            '@@\n-"Series old": pd.Series(\n+"Series new": pd.Series(\n'
            "\\ No newline at end of file",
            False,
        ),
    ],
)
def test_changes_already_in_the_file_are_recognized(tmp_path, current, patch, applied):
    path = tmp_path / "file.txt"
    path.write_bytes(current.encode())
    result = apply(tmp_path, update(patch))
    assert result["ok"], result
    if applied:
        assert result["data"]["status"] == "applied"
        assert path.read_bytes() == b'"Series new": pd.Series(\n"Series new": pd.Series(\n'
    else:
        assert result["data"]["status"] == "unchanged"
        assert path.read_bytes() == current.encode()


@pytest.mark.parametrize(
    ("old", "new", "current", "end"),
    [
        ("old_value", "new_value", "new_value\n", ""),
        ("return 1", "return 2", "return 2\n", ""),
        ('label("old");', 'label("new");', 'label("new");\n', ""),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\n"Series other": pd.Series(\n',
            "",
        ),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\n"Series new": pd.Series(\n',
            "",
        ),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\ntrailing\n',
            "\n*** End of File",
        ),
        # The new text is there, but not at the end of the file the patch asks for.
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\n',
            "\n\\ No newline at end of file",
        ),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\nother',
            "\n\\ No newline at end of file",
        ),
    ],
)
def test_new_text_without_a_unique_target_or_requested_end_is_not_success(
    tmp_path, old, new, current, end
):
    path = tmp_path / "file.txt"
    path.write_bytes(current.encode())
    result = apply(tmp_path, update(f"@@\n-{old}\n+{new}{end}"))
    assert not result["ok"]
    assert path.read_bytes() == current.encode()


def test_after_a_failed_hunk_later_hunks_need_exact_lines(tmp_path):
    path = tmp_path / "file.txt"
    before = "section\nvalue = 111\ntail\nother = old\n"
    path.write_text(before, encoding="utf-8")
    result = apply(
        tmp_path,
        update(
            "@@\n-missing precondition\n+value = 123\n"
            "@@\n section\n-value = 123\n+value = 222\n tail\n"
            "@@\n-other = old\n+other = new"
        ),
    )
    assert text(result).startswith("1 of 3 changes applied; 2 did not.")
    assert "Failed: file.txt, hunk 1: the lines to replace were not found." in text(result)
    # The second hunk may not approximate its way onto the similar line after hunk 1 failed.
    assert (
        "Failed: file.txt, hunk 2: the lines to replace were not found. After an earlier "
        "failure in this file, this change needs lines that match the file exactly"
    ) in text(result)
    assert (
        "First difference, line 2: the file has 'value = 111' where the patch has 'value = 123'."
    ) in text(result)
    assert path.read_text(encoding="utf-8") == before.replace("other = old", "other = new")


def test_failed_hunk_does_not_block_normalized_independent_match(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"    independent = old\r\n")
    result = apply(
        tmp_path,
        update("@@\n-not present anywhere\n+unused\n@@\n-independent = old\n+independent = new"),
    )
    assert text(result).startswith("1 of 2 changes applied; 1 did not.")
    assert "Updated file.txt:\n1|     independent = new\n" in text(result)
    assert path.read_bytes() == b"    independent = new\r\n"
