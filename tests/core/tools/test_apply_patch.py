"""Apply patch: matching behavior."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from core.tools.apply_patch import APPLY_PATCH_TOOL_PARAMETERS
from core.tools.file_state import FileReadState
from core.tools.read import make_read_handler
from tests.core.tools.apply_patch_helpers import (
    apply,
    context,
    text,
    update,
)


def test_the_example_in_the_patch_description_applies(tmp_path):
    description = APPLY_PATCH_TOOL_PARAMETERS["properties"]["patch"]["description"]
    example = description.partition("for example:\n")[2].partition("*** End Patch\n")[0]
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_bytes(b"def main():\n    count = 1\n    run(count)\n")
    (tmp_path / "old.txt").write_bytes(b"old\n")
    (tmp_path / "a.txt").write_bytes(b"a\n")
    state = FileReadState()

    result = apply(tmp_path, example + "*** End Patch", state=state)

    assert result["ok"] and result["data"]["status"] == "applied", result
    assert (tmp_path / "src/app.py").read_bytes() == b"def main():\n    count = 2\n    run(count)\n"
    assert (tmp_path / "notes.txt").read_bytes() == b"first line of a new file\n"
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "b.txt").read_bytes() == b"a\n" and not (tmp_path / "a.txt").exists()


@pytest.mark.parametrize("separator", ["\f", "\x85", " ", " "])
def test_unicode_separators_are_line_content_not_line_breaks(tmp_path, separator):
    path = tmp_path / "file.txt"
    before = f"alpha{separator}beta\nomega".encode()
    path.write_bytes(before)

    missing = apply(tmp_path, update("@@\n-beta\n+changed\n omega"))
    appended = apply(tmp_path, update(f"@@\n-alpha{separator}beta\n+changed\n omega"))

    assert missing["error"]["code"] == "text_not_found"
    assert appended["ok"], appended
    assert path.read_bytes() == b"changed\nomega"


def test_add_and_append_use_lf_when_a_file_has_only_unicode_separators(tmp_path):
    path = tmp_path / "one-line.json"
    path.write_bytes('{"k":"x y"}'.encode())
    state = FileReadState()
    state.record_read("session-test", path.resolve())

    assert apply(tmp_path, update("@@\n+more", "one-line.json"), state=state)["ok"]
    assert path.read_bytes() == '{"k":"x y"}\nmore\n'.encode()
    assert apply(tmp_path, "*** Add File: one-line.json\n+{\n+}", state=state)["ok"]
    assert path.read_bytes() == b"{\n}\n"


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("final", [True, False])
def test_update_preserves_encoding_context_and_endings(tmp_path, ending, bom, final):
    path = tmp_path / "file.txt"
    before = ending.join([b"unchanged\t  ", b"old = 1", b"tail"])
    path.write_bytes(bom + before + (ending if final else b""))
    result = apply(tmp_path, update("@@\n unchanged\t  \n-old = 1\n+new = 2\n tail"))
    assert result["ok"]
    assert path.read_bytes() == bom + before.replace(b"old = 1", b"new = 2") + (
        ending if final else b""
    )


@pytest.mark.parametrize("wrapper", ["{patch}", "```diff\n{patch}\n```", "```patch\n{patch}\n```"])
@pytest.mark.parametrize("markers", [True, False])
@pytest.mark.parametrize("crlf", [True, False])
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
        ("keep", "@@\n keep\n+added", "keep\nadded"),
        ("keep\n", "@@\n+added", "keep\nadded\n"),
        ("", "@@\n+added", "added\n"),
        ("first\nlast\n", "@@ first\n+middle", "first\nmiddle\nlast\n"),
        ("first\nold\nlast\n", "@@ -1,3 +1,3 @@\n first\n-old\n+new\n last", "first\nnew\nlast\n"),
        ("same\nfirst\nsame\n", "@@\n-same\n+new\n*** End of File", "same\nfirst\nnew\n"),
        ("old\n", "@@\n-old\n+new\n\\ No newline at end of file", "new"),
        ("a\n\nb\n", "@@\n a\n\n-b\n+c", "a\n\nc\n"),
    ],
)
def test_matching_and_line_semantics(tmp_path, before, hunk, after):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    result = apply(tmp_path, update(hunk))
    assert result["ok"], result
    assert path.read_bytes() == after.encode()


def test_repeated_blocks_require_context_and_never_select_first(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"section one\nvalue\nsection two\nvalue\n")
    before = path.read_bytes()
    failed = apply(tmp_path, update("@@\n-value\n+changed"))
    assert failed["error"]["code"] == "ambiguous_match"
    assert path.read_bytes() == before
    assert apply(tmp_path, update("@@ section two\n-value\n+changed"))["ok"]
    assert path.read_bytes() == before.replace(b"two\nvalue", b"two\nchanged")


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
    ],
)
def test_overlapping_occurrences_are_ambiguous_not_first_match(tmp_path, before, hunk):
    path = tmp_path / "file.txt"
    path.write_bytes(before)

    result = apply(tmp_path, update(hunk))

    assert result["error"]["code"] in {"ambiguous_match", "ambiguous_context"}
    assert path.read_bytes() == before


def test_whole_lines_do_not_match_substrings(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"foobar\nfoo\n")
    assert apply(tmp_path, update("@@\n-foo\n+new"))["ok"]
    assert path.read_bytes() == b"foobar\nnew\n"


@pytest.mark.parametrize("gutter", ["{n}| {text}", "{n}|{text}"])
def test_complete_read_gutters_recover_without_corrupting_added_lines(tmp_path, gutter):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\nold\nomega\n")
    lines = [gutter.format(n=n, text=t) for n, t in enumerate(["alpha", "old", "new", "omega"], 1)]
    hunk = (
        "@@\n "
        + lines[0]
        + "\n-"
        + lines[1]
        + "\n+"
        + lines[2]
        + "\n "
        + gutter.format(n=3, text="omega")
    )
    result = apply(tmp_path, update(hunk))
    assert result["ok"], result
    assert path.read_bytes() == b"alpha\nnew\nomega\n"
    assert "Note: Removed read-output line-number prefixes" in text(result)


def test_literal_gutter_shaped_content_wins(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"1| alpha\n2| old\n3| omega\n")
    result = apply(tmp_path, update("@@\n 1| alpha\n-2| old\n+2| new\n 3| omega"))
    assert result["ok"]
    assert path.read_bytes() == b"1| alpha\n2| new\n3| omega\n"


@pytest.mark.parametrize(
    "hunk",
    [
        "@@\n 1:20| alpha\n-2:20| old\n+new\n 3:20| omega",
        "@@\n alpha\n-2| old\n+2:20| new\n omega",
        "@@\n-2:20| old\n+new",
    ],
)
def test_damaged_or_continuation_gutters_fail_closed(tmp_path, hunk):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\nold\nomega\n")
    result = apply(tmp_path, update(hunk))
    assert result["error"]["code"] == "line_numbered_content"
    assert path.read_bytes() == b"alpha\nold\nomega\n"


@pytest.mark.parametrize(
    "hunk",
    [
        "@@\n 1| alpha\n-7| old\n+new\n 9| omega",
        "@@\n alpha\n-2| old\n+new\n omega",
        "@@\n-2| old\n+2| new",
        "@@\n-2|old\n+new",
    ],
)
def test_single_mixed_and_stale_read_gutters_use_unique_current_lines(tmp_path, hunk):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\nold\nomega\n")
    result = apply(tmp_path, update(hunk))
    assert result["ok"], result
    assert path.read_bytes() == b"alpha\nnew\nomega\n"
    assert "Note: Removed read-output line-number prefixes" in text(result)


def test_normalized_read_gutters_do_not_resolve_ambiguity_by_number(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\nseparator\nold\n")
    result = apply(tmp_path, update("@@\n-3| old\n+new"))
    assert result["error"]["code"] == "ambiguous_match"
    assert path.read_bytes() == b"old\nseparator\nold\n"


@pytest.mark.parametrize("separator", ["", " "])
def test_indented_gutters_preserve_indentation(tmp_path, separator):
    path = tmp_path / "file.txt"
    path.write_bytes(b"    old\n    tail\n")
    result = apply(
        tmp_path,
        update(f"@@\n-1|{separator}    old\n+1|{separator}    new\n 2|{separator}    tail"),
    )
    assert result["ok"], result
    assert path.read_bytes() == b"    new\n    tail\n"


@pytest.mark.asyncio
async def test_actual_read_output_can_be_used_directly_in_a_patch(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"\xef\xbb\xbf  anchor\r\n    old\r\n\r\n  tail\r\n")
    state = FileReadState()
    reader = make_read_handler(Mock(), Mock(), state, speech_max_size_bytes=1024)
    read_result = await reader(context(tmp_path), {"path": "file.txt"})
    assert isinstance(read_result, dict)
    lines = read_result["data"]["content"].splitlines()
    patch_lines = [" " + line for line in lines]
    patch_lines[1:2] = ["-" + lines[1], "+" + lines[1].replace("old", "new")]
    result = apply(tmp_path, update("@@\n" + "\n".join(patch_lines)), state=state)
    assert result["ok"], result
    assert path.read_bytes() == b"\xef\xbb\xbf  anchor\r\n    new\r\n\r\n  tail\r\n"


def test_escaped_text_requires_matching_evidence(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\n\told\nomega\n")
    result = apply(tmp_path, update("@@\n alpha\n-\\told\n+\\tnew\n omega"))
    assert result["ok"], result
    assert path.read_bytes() == b"alpha\n\tnew\nomega\n"
    assert "Note: Normalized escaped patch text" in text(result)


def test_literal_backslashes_are_preserved(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b'pattern = r"\\n"\n')
    assert apply(tmp_path, update('@@\n-pattern = r"\\n"\n+pattern = r"\\t"'))["ok"]
    assert path.read_bytes() == b'pattern = r"\\t"\n'


@pytest.mark.parametrize(
    ("before", "body", "expected"),
    [
        ("\told\n", '-\\told\n+\\tvalue = "\\n"', '\tvalue = "\\n"\n'),
        ("\told\n", '-\\told\n+\\tvalue = "\\t"', '\tvalue = "\\t"\n'),
        ("alpha\nold\n", '-alpha\\nold\n+value = "\\n"', 'value = "\\n"\n'),
        ('say("old")\n', '-say(\\"old\\")\n+say(\\"new\\")', 'say("new")\n'),
        ("say('old')\n", "-say(\\'old\\')\n+say(\\'new\\')", "say('new')\n"),
    ],
)
def test_escape_recovery_preserves_literal_replacement_escapes(tmp_path, before, body, expected):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())
    result = apply(tmp_path, update("@@\n" + body))
    assert result["ok"], result
    assert path.read_bytes() == expected.encode()


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
    ],
)
def test_similarity_never_replaces_a_different_removed_line(tmp_path, before, body):
    path = tmp_path / "file.txt"
    path.write_bytes(before.encode())

    result = apply(tmp_path, update("@@\n" + body))

    assert result["error"]["code"] == "text_not_found"
    assert "The closest text in the file, lines 1-" in text(result)
    assert path.read_bytes() == before.encode()


@pytest.mark.parametrize("hint", ["@@", "@@ import os"])
def test_a_removed_line_copied_with_a_misspelling_is_applied_and_named(tmp_path, hint):
    path = tmp_path / "file.py"
    path.write_bytes(b"import os\n\ndef load(order):\n    return order.reciept_total\n")

    result = apply(
        tmp_path,
        update(
            f"{hint}\n def load(order):\n-    return order.receipt_total\n"
            "+    return order.receipt_total + order.tax",
            "file.py",
        ),
    )

    assert result["ok"], result
    assert path.read_bytes() == (
        b"import os\n\ndef load(order):\n    return order.reciept_total + order.tax\n"
    )
    assert (
        "Line 4 did not match your old text exactly and was edited anyway; it read:     "
        "return order.reciept_total" in text(result)
    )


def test_similarity_absorbs_context_drift_around_precise_removed_lines(tmp_path):
    path = tmp_path / "file.py"
    path.write_bytes("def process(data):\n    value = “x”\n    return True\n".encode())

    result = apply(
        tmp_path,
        update(
            ' def process(data, strict):\n-    value = "x"\n+    value = "y"\n     return True',
            "file.py",
        ),
    )

    assert result["ok"], result
    assert path.read_bytes() == ("def process(data):\n    value = “y”\n    return True\n".encode())


@pytest.mark.parametrize(
    ("before", "old", "new"),
    [
        ('value = "original"', 'value = \\"originaL\\"', 'value = \\"changed\\"'),
        ("value = 'original'", "value = \\'originaL\\'", "value = \\'changed\\'"),
        (r"value = C:\original", r"value = C:\\originaL", r"value = C:\\changed"),
    ],
)
def test_approximate_match_cannot_introduce_doubled_escapes(tmp_path, before, old, new):
    path = tmp_path / "file.txt"
    original = f"anchor\n{before}\ntail\n".encode()
    path.write_bytes(original)
    result = apply(tmp_path, update(f"@@\n anchor\n-{old}\n+{new}\n tail"))
    assert result["error"]["code"] == "text_not_found"
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("before", "old", "new", "expected"),
    [
        ("title—old", "title--old", "title--new", "title—new"),
        ("wait…old", "wait...old", "wait...new", "wait…new"),
        ("score = −1", "score = -1", "score = -2", "score = −2"),
        ("“old” — value…", '"old" -- value...', '"new" -- value...', "“new” — value…"),
        ("name\u2009= old", "name = old", "name = new", "name\u2009= new"),
        ("    title—old", "  title--old", "  title--new", "    title—new"),
        ("    —", "--", "new", "    new"),
        ("    …", "...", "new", "    new"),
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
    ("before", "body", "expected"),
    [
        (b"alpha\n\nomega\n", "@@\n-", b"alpha\nomega\n"),
        (b"alpha\n\nomega\n", "@@\n-\n+middle", b"alpha\nmiddle\nomega\n"),
        (b"alpha\nold\nomega\n", "@@\n \n alpha\n-old\n+new\n omega\n ", b"alpha\nnew\nomega\n"),
        (b"alpha\r\nold\nomega\r", "@@\n alpha\n-old\n+new\n omega", b"alpha\r\nnew\r\nomega\r"),
        (b"old\n", "@@\n-old\n+1| first\n+2| second", b"first\nsecond\n"),
    ],
)
def test_blank_boundaries_mixed_endings_and_added_gutters(tmp_path, before, body, expected):
    path = tmp_path / "file.txt"
    path.write_bytes(before)
    result = apply(tmp_path, update(body))
    assert result["ok"], result
    assert path.read_bytes() == expected


def test_empty_line_ambiguity_and_partial_added_gutters_fail(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"a\n\nb\n\nc\n")
    assert apply(tmp_path, update("@@\n-\n+new"))["error"]["code"] == "ambiguous_match"
    assert (
        apply(tmp_path, update("@@\n-a\n+1| first\n+3| third"))["error"]["code"]
        == "line_numbered_content"
    )
    assert path.read_bytes() == b"a\n\nb\n\nc\n"


def test_add_gutters_and_exact_empty_file(tmp_path):
    assert apply(tmp_path, "*** Add File: file.txt\n+1| first\n+2| second")["ok"]
    assert (tmp_path / "file.txt").read_bytes() == b"first\nsecond\n"
    assert apply(tmp_path, "*** Add File: empty.txt")["ok"]
    assert (tmp_path / "empty.txt").read_bytes() == b""


def test_deletion_preserves_surviving_context_ending(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"keep\r\nremove\n")
    assert apply(tmp_path, update("@@\n keep\n-remove"))["ok"]
    assert path.read_bytes() == b"keep\r\n"


def test_unanchored_append_can_intentionally_repeat_a_line(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"same\n")
    assert apply(tmp_path, update("@@\n+same"))["ok"]
    assert path.read_bytes() == b"same\nsame\n"


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


def test_mid_file_no_newline_marker_rejects_without_joining_lines(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\ntail\n")
    result = apply(tmp_path, update("@@\n-old\n+new\n\\ No newline at end of file"))
    assert result["error"]["code"] == "invalid_patch"
    assert path.read_bytes() == b"old\ntail\n"


def test_end_of_file_marker_on_lines_elsewhere_is_ignored_and_named(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"first\nold\ntail\n")

    result = apply(tmp_path, update("@@\n first\n-old\n+new\n*** End of File"))

    assert result["ok"], result
    assert path.read_bytes() == b"first\nnew\ntail\n"
    assert (
        "The lines before *** End of File are not at the end of the file; the hunk was "
        "applied where they are." in text(result)
    )


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
