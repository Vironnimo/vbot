"""Apply patch: matching behavior."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from core.tools.file_state import FileReadState
from core.tools.read import make_read_handler
from tests.core.tools.apply_patch_helpers import (
    apply,
    context,
    update,
)


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r", b"\xc2\x85", b"\xe2\x80\xa8"])
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
        (
            "start\nvalue = 100\nend\n",
            "@@\n start\n-value = 101\n+value = 200\n end",
            "start\nvalue = 200\nend\n",
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
    assert result["data"]["files"][0]["warnings"]


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
    assert result["data"]["files"][0]["warnings"]


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
    assert result["data"]["files"][0]["warnings"]


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
    assert apply(tmp_path, patch)["data"]["already_applied"]
    assert path.read_bytes() == before == b"section\ninserted\nnew\ntail\n"


def test_mid_file_no_newline_marker_rejects_without_joining_lines(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\ntail\n")
    result = apply(tmp_path, update("@@\n-old\n+new\n\\ No newline at end of file"))
    assert result["error"]["code"] == "invalid_patch"
    assert path.read_bytes() == b"old\ntail\n"


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
