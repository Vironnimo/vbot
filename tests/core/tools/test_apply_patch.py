"""V4A regression scenarios, including Hermes failures and vBot file contracts."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.tools.apply_patch import make_apply_patch_handler, register_apply_patch_tool
from core.tools.change_tracker import ChangeTracker
from core.tools.file_state import FileReadState
from core.tools.read import make_read_handler
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope


def context(root: Path, **kwargs) -> ToolContext:
    return ToolContext(
        agent_id="agent-test",
        session_id="session-test",
        run_id="run-test",
        tool_call_id="call-test",
        tool_name="apply_patch",
        tool_call_index=0,
        workspace=root,
        data_root=root / "data",
        vbot_root=root,
        **kwargs,
    )


def apply(root: Path, patch: str, *, state=None, ctx=None):
    result = make_apply_patch_handler(state or FileReadState())(
        ctx or context(root), {"patch": patch}
    )
    assert isinstance(result, dict)
    assert is_tool_result_envelope(result)
    return result


def update(body: str, path: str = "file.txt") -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n{body}\n*** End Patch"


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


def test_display_uses_generic_metadata_and_bounded_previews(tmp_path):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    ctx = context(tmp_path)
    arguments = {"patch": "*** Add File: file.txt\n+" + "x" * 1000 + "\n+" + "y" * 1000}
    result = make_apply_patch_handler(FileReadState())(ctx, arguments)
    assert isinstance(result, dict)
    display = registry.display_for_call("apply_patch", arguments, result=result)
    assert display["summary"] == "file.txt"
    assert display["hidden_argument_keys"] == ["patch"]
    assert {
        fact.get("change"): fact["value"]
        for fact in ctx.presentation_facts
        if fact["kind"] == "line_change"
    } == {"added": 2, "removed": 0}
    preview = result["data"]["files"][0]["preview"][0]["after"]
    assert all(len(line) <= 255 for line in preview)
    assert preview[0].startswith("1| ") and preview[1].startswith("2| ")


def test_permission_failure_during_atomic_write_leaves_original_and_no_temp(tmp_path, monkeypatch):
    from core.tools import file_state

    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")

    def fail(*args, **kwargs):
        raise PermissionError("fixture permission error")

    monkeypatch.setattr(file_state.os, "replace", fail)
    result = apply(tmp_path, update("@@\n-old\n+new"))
    assert result["error"]["code"] == "file_write_error"
    assert path.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file.txt"]


def test_precise_post_state_retry_precedes_similar_other_block(tmp_path):
    path = tmp_path / "file.txt"
    before = b"alpha\nvalue = 222\nomega\nalpha\nvalue = 111\nomega\n"
    path.write_bytes(before)
    result = apply(tmp_path, update("@@\n alpha\n-value = 123\n+value = 222\n omega"))
    assert result["ok"] and result["data"]["already_applied"]
    assert path.read_bytes() == before


def test_read_stamp_after_noop_and_preexisting_syntax_error(tmp_path):
    path = tmp_path / "file.py"
    path.write_bytes(b"broken = (\nold\n")
    state = FileReadState()
    result = apply(tmp_path, update("@@\n-old\n+new", "file.py"), state=state)
    assert result["ok"] and result["data"]["files"][0]["syntax_warning"]
    noop_state = FileReadState()
    assert apply(tmp_path, "*** Add File: file.py\n+broken = (\n+new", state=noop_state)["ok"]
    assert noop_state.check_stale("session-test", path) is None


def test_markers_inside_content_and_long_files_are_literal(tmp_path):
    path = tmp_path / "file.txt"
    prefix = "x" * 12000 + "\n" + "unchanged\n" * 2200
    path.write_text(prefix + "*** End Patch\nold\n", encoding="utf-8")
    assert apply(tmp_path, update("@@\n *** End Patch\n-old\n+*** Begin Patch"))["ok"]
    assert path.read_text(encoding="utf-8") == prefix + "*** End Patch\n*** Begin Patch\n"


def test_sequential_operations_use_virtual_state(tmp_path):
    patch = """*** Begin Patch
*** Add File: one.txt
+alpha
+old
*** Update File: one.txt
@@
-old
+new
*** Move File: one.txt -> nested/two.txt
*** Update File: nested/two.txt
@@
-new
+final
*** Add File: obsolete.txt
+temporary
*** Delete File: obsolete.txt
*** End Patch"""
    result = apply(tmp_path, patch)
    assert result["ok"], result
    assert (tmp_path / "nested/two.txt").read_bytes() == b"alpha\nfinal\n"
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "obsolete.txt").exists()


@pytest.mark.parametrize(
    "operation",
    [
        "*** Move File: file.txt -> moved.txt",
        "*** Update File: file.txt\n*** Move to: moved.txt",
        "*** Update File: file.txt\n*** Move to: moved.txt\n@@\n-old\n+new",
    ],
)
def test_moves_preserve_bytes_permissions_and_prevent_overwrite(tmp_path, operation):
    source = tmp_path / "file.txt"
    source.write_bytes(b"old\r\n")
    if os.name != "nt":
        source.chmod(0o751)
    mode = stat.S_IMODE(source.stat().st_mode)
    assert apply(tmp_path, operation)["ok"]
    assert not source.exists()
    dest = tmp_path / "moved.txt"
    assert dest.read_bytes() == (b"new\r\n" if "+new" in operation else b"old\r\n")
    assert stat.S_IMODE(dest.stat().st_mode) == mode
    source.write_bytes(b"other\n")
    blocked = apply(tmp_path, operation)
    assert not blocked["ok"]
    if "+new" in operation:
        assert blocked["error"]["code"] == "all_changes_failed"
    else:
        assert blocked["error"]["code"] == "destination_exists"
    assert source.read_bytes() == b"other\n"


@pytest.mark.parametrize(
    "suffix",
    [
        "*** Update File: missing.txt\n@@\n-old\n+new",
        "*** Update File: file.txt\n@@\n-missing\n+new",
        "*** Delete File: missing.txt",
        "*** Add File: file.txt\n+overwrite",
    ],
)
def test_invalid_operation_keeps_successful_siblings(tmp_path, suffix):
    path = tmp_path / "file.txt"
    path.write_bytes(b"original\n")
    patch = "*** Add File: new/created.txt\n+would be new\n" + suffix
    result = apply(tmp_path, patch)
    assert result["ok"] and result["data"]["status"] == "partial"
    assert [r["status"] for r in result["data"]["results"]] == ["applied", "failed"]
    assert path.read_bytes() == b"original\n"
    assert (tmp_path / "new/created.txt").read_bytes() == b"would be new\n"


def test_multi_hunk_and_retries_do_not_repeat_changes(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"alpha\none\nbeta\ntwo\nomega\n")
    first = "@@\n alpha\n-one\n+first\n beta"
    second = "@@\n beta\n-two\n+second\n omega"
    assert apply(tmp_path, update(first))["ok"]
    assert apply(tmp_path, update(first + "\n@@\n unchanged inert anchor\n" + second))["ok"]
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = apply(tmp_path, update(first + "\n" + second))
    assert result["ok"] and result["data"]["already_applied"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_add_retry_and_context_only_patch(tmp_path):
    patch = "*** Add File: file.txt\n+content"
    assert apply(tmp_path, patch)["ok"]
    assert apply(tmp_path, patch)["data"]["already_applied"]
    assert apply(tmp_path, update("@@\n content"))["error"]["code"] == "no_changes"


@pytest.mark.parametrize("payload", [b"a\x00b", b"\xff\xfeabc"])
def test_binary_and_non_utf8_update_rejected_but_move_delete_supported(tmp_path, payload):
    path = tmp_path / "file.txt"
    path.write_bytes(payload)
    assert not apply(tmp_path, update("@@\n-a\n+b"))["ok"]
    assert path.read_bytes() == payload
    assert apply(tmp_path, "*** Move File: file.txt -> other.bin")["ok"]
    assert (tmp_path / "other.bin").read_bytes() == payload
    assert apply(tmp_path, "*** Delete File: other.bin")["ok"]
    assert not (tmp_path / "other.bin").exists()


def test_current_content_stamps_stats_and_syntax_warnings(tmp_path):
    path = tmp_path / "file.py"
    path.write_bytes(b"value = 1\n")
    state = FileReadState()
    state.record_read("session-test", path)
    path.write_bytes(b"external = True\nvalue = 1\n")
    tracker = ChangeTracker()
    ctx = context(tmp_path, change_tracker=tracker)
    result = apply(tmp_path, update("@@\n-value = 1\n+value = (", "file.py"), state=state, ctx=ctx)
    assert result["ok"]
    details = result["data"]["files"][0]
    assert details["syntax_warning"] and details["warnings"]
    assert state.check_stale("session-test", path) is None
    stats = tracker.peek_run_stats("session-test")
    assert stats["added"] == 1 and stats["removed"] == 1
    assert path.read_bytes() == b"external = True\nvalue = (\n"


def test_paths_cwd_absolute_aliases_and_parent_overlap(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "file.txt"
    path.write_bytes(b"old\n")
    ctx = context(tmp_path, cwd=root)
    assert apply(tmp_path, update("@@\n-old\n+new"), ctx=ctx)["ok"]
    patch = (
        f"*** Update File: {path.as_posix()}\n@@\n-new\n+later\n"
        "*** Update File: ./file.txt\n@@\n-later\n+final"
    )
    assert apply(tmp_path, patch, ctx=ctx)["ok"]
    assert path.read_bytes() == b"final\n"
    overlap = "*** Add File: parent\n+file\n*** Add File: parent/child\n+child"
    result = apply(root, overlap)
    assert result["error"]["code"] == "all_changes_failed"
    assert not (root / "parent").exists()


def test_write_failure_reports_actual_partial_changes(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    original = module.atomic_write_bytes

    def fail_second(path, payload, **kwargs):
        if path.name == "b.txt":
            raise OSError("fixture failure")
        original(path, payload, **kwargs)

    monkeypatch.setattr(module, "atomic_write_bytes", fail_second)
    result = apply(tmp_path, "*** Add File: a.txt\n+first\n*** Add File: b.txt\n+second")
    assert result["ok"] and result["data"]["status"] == "partial"
    assert [Path(item["path"]).name for item in result["data"]["files"]] == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == b"first\n"
    assert not (tmp_path / "b.txt").exists()
    assert result["data"]["results"][1]["error"]["code"] == "file_write_error"


def test_failed_move_keeps_source(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    source = tmp_path / "file.txt"
    source.write_bytes(b"original")

    def fail(*args, **kwargs):
        raise OSError("fixture failure")

    monkeypatch.setattr(module, "atomic_write_bytes", fail)
    result = apply(tmp_path, "*** Move File: file.txt -> new.txt")
    assert not result["ok"]
    assert source.read_bytes() == b"original"
    assert not (tmp_path / "new.txt").exists()


def test_external_change_during_planning_is_detected(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    original = module._plan
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")

    def racing_plan(*args, **kwargs):
        plan = original(*args, **kwargs)
        path.write_bytes(b"external\n")
        return plan

    monkeypatch.setattr(module, "_plan", racing_plan)
    result = apply(tmp_path, update("@@\n-old\n+new"))
    assert result["error"]["code"] == "file_changed"
    assert path.read_bytes() == b"external\n"


@pytest.mark.parametrize(
    "arguments", [{}, {"patch": ""}, {"patch": 42}, {"patch": "x", "path": "x"}]
)
def test_argument_validation(tmp_path, arguments):
    result = make_apply_patch_handler(FileReadState())(context(tmp_path), arguments)
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_registration_policy_display_and_cancellation_settlement(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    state = FileReadState()
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=state)
    tool = registry.get("apply_patch")
    assert tool.family == "files"
    assert registry.provider_definitions(allowed_tools=[]) == []
    definition = registry.provider_definitions(allowed_tools=["apply_patch"])[0]
    assert definition["parameters"]["required"] == ["patch"]
    entered, release = threading.Event(), threading.Event()
    original = module.atomic_write_bytes

    def slow_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(module, "atomic_write_bytes", slow_write)
    task = asyncio.create_task(
        tool.handler(context(tmp_path), {"patch": "*** Add File: new.txt\n+done"})
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "new.txt").read_bytes() == b"done\n"
    assert state.check_stale("session-test", tmp_path / "new.txt") is None


def test_locking_prevents_interleaving_with_edit(tmp_path):
    from core.tools.edit import make_edit_handler

    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    state = FileReadState()
    started = threading.Event()
    result = []

    def worker():
        started.set()
        result.append(apply(tmp_path, update("@@\n-middle\n+final"), state=state))

    with state.lock_path(path):
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(5)
        assert not result
        path.write_bytes(b"middle\n")
    thread.join(5)
    assert not thread.is_alive() and result[0]["ok"]
    assert path.read_bytes() == b"final\n"
    edited = make_edit_handler(state)(
        context(tmp_path),
        {"edits": [{"path": "file.txt", "old_string": "final", "new_string": "edit still works"}]},
    )
    assert edited["ok"]
    assert path.read_bytes() == b"edit still works\n"


@pytest.mark.parametrize("bad_index", [0, 3, 6])
def test_six_hunks_succeed_when_one_fails_in_same_file(tmp_path, bad_index):
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
    data = result["data"]
    assert result["ok"] and data["status"] == "partial"
    assert (data["total"], data["succeeded"], data["failed"]) == (7, 6, 1)
    assert [r["hunk"] for r in data["results"]] == list(range(1, 8))
    expected = before
    for i in range(7):
        if i != bad_index:
            expected = expected.replace(f"setting_{i}=old", f"setting_{i}=new")
    assert path.read_text(encoding="utf-8") == expected
    assert data["results"][bad_index]["error"]["code"] == "text_not_found"


def test_after_failed_precondition_similar_hunk_cannot_edit_wrong_state(tmp_path):
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
    assert [r["status"] for r in result["data"]["results"]] == ["failed", "failed", "applied"]
    assert path.read_text(encoding="utf-8") == before.replace("other = old", "other = new")


def test_failed_hunk_does_not_block_normalized_independent_match(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"    independent = old\r\n")
    result = apply(
        tmp_path,
        update("@@\n-not present anywhere\n+unused\n@@\n-independent = old\n+independent = new"),
    )
    assert result["data"]["succeeded"] == 1
    assert path.read_bytes() == b"    independent = new\r\n"


def test_closest_candidates_are_raw_reusable_and_never_applied(tmp_path):
    path = tmp_path / "file.txt"
    before = "prefix\ndef deploy():\n    timeout = 30\n    retries = 5\n"
    path.write_text(before, encoding="utf-8")
    result = apply(
        tmp_path,
        "*** Add File: other.txt\n+done\n"
        + update(
            "@@\n def deploy():\n-    completely unrelated declaration\n"
            "+    timeout = 60\n     retries = 5"
        ).replace("*** Begin Patch\n", ""),
    )
    error = result["data"]["results"][1]["error"]
    assert error["code"] == "text_not_found"
    assert error["candidates"]
    assert len(error["candidates"]) <= 3
    candidate = error["candidates"][0]
    assert candidate["line"] == 2
    assert candidate["text"] == "def deploy():\n    timeout = 30\n    retries = 5"
    assert path.read_text(encoding="utf-8") == before
    assert (tmp_path / "other.txt").read_bytes() == b"done\n"


def test_ambiguous_hint_reports_actual_locations_including_offset(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"intro\nsection\nrepeated\nleft\nrepeated\nright\n")
    result = apply(tmp_path, update("@@ section\n@@ repeated\n-left\n+changed"))
    assert result["error"]["code"] == "ambiguous_match"
    details = json.loads(result["error"]["message"].split("\n")[-1])
    assert details["occurrences"] == 2
    assert [c["line"] for c in details["candidates"]] == [2, 4]
    assert details["candidates"][0]["text"] == "section\nrepeated\nleft"
    assert path.read_bytes() == b"intro\nsection\nrepeated\nleft\nrepeated\nright\n"


def test_long_line_preview_shows_change_and_surrounding_lines(tmp_path):
    path = tmp_path / "file.txt"
    old = "x" * 400 + "TARGET=old" + "z" * 100
    new = old.replace("TARGET=old", "TARGET=new")
    path.write_text("header\n" + old + "\nfooter\n", encoding="utf-8")
    result = apply(tmp_path, update("@@\n-" + old + "\n+" + new))
    preview = result["data"]["files"][0]["preview"][0]
    assert "TARGET=old" in "\n".join(preview["before"])
    assert "TARGET=new" in "\n".join(preview["after"])
    assert preview["after"][0] == "1| header"
    assert preview["after"][-1] == "3| footer"
    assert preview["after"][1].startswith("2:")
    assert path.read_text(encoding="utf-8") == "header\n" + new + "\nfooter\n"


def test_preview_keeps_first_and_last_region_and_reports_omission(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"a=old\nseparator\nb=old\nseparator\nc=old\n")
    result = apply(tmp_path, update("\n".join(f"@@\n-{key}=old\n+{key}=new" for key in "abc")))
    details = result["data"]["files"][0]
    assert details["preview_omitted_regions"] == 1
    assert "1| a=new" in details["preview"][0]["after"]
    assert "5| c=new" in details["preview"][-1]["after"]
    assert result["data"]["succeeded"] == 3


def test_net_zero_is_not_claimed_as_already_applied(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    result = apply(tmp_path, update("@@\n-old\n+new\n@@\n-new\n+old"))
    assert result["data"]["no_change"]
    assert "already_applied" not in result["data"]
    assert [r["status"] for r in result["data"]["results"]] == ["applied", "applied"]
    assert path.read_bytes() == b"old\n"


def test_failed_add_blocks_dependent_update_but_not_other_file(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"unrelated existing content\n")
    result = apply(
        tmp_path,
        "*** Add File: file.txt\n+new file\n"
        "*** Update File: file.txt\n@@\n-unrelated existing content\n+clobbered\n"
        "*** Add File: independent.txt\n+done",
    )
    assert [r["status"] for r in result["data"]["results"]] == ["failed", "skipped", "applied"]
    assert path.read_bytes() == b"unrelated existing content\n"
    assert (tmp_path / "independent.txt").read_bytes() == b"done\n"


def test_failed_move_never_edits_existing_destination(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_bytes(b"source\n")
    destination.write_bytes(b"destination\n")
    result = apply(
        tmp_path,
        "*** Move File: source.txt -> destination.txt\n"
        "*** Update File: destination.txt\n@@\n-destination\n+clobbered\n"
        "*** Add File: independent.txt\n+done",
    )
    assert [r["status"] for r in result["data"]["results"]] == ["failed", "skipped", "applied"]
    assert source.read_bytes() == b"source\n" and destination.read_bytes() == b"destination\n"


def test_partial_move_reports_destination_and_skips_uncertain_paths(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_bytes(b"keep\n")
    original = Path.unlink

    def fail_source(path, *args, **kwargs):
        if path == source:
            raise PermissionError("fixture cannot remove source")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source)
    result = apply(
        tmp_path,
        "*** Move File: source.txt -> destination.txt\n"
        "*** Update File: destination.txt\n@@\n-keep\n+clobbered\n"
        "*** Add File: independent.txt\n+done",
    )
    assert result["ok"] and result["data"]["status"] == "partial"
    outcomes = result["data"]["results"]
    assert [r["status"] for r in outcomes] == ["partial", "skipped", "applied"]
    assert outcomes[0]["completed_paths"] == [(tmp_path / "destination.txt").as_posix()]
    assert outcomes[0]["pending_paths"] == [source.as_posix()]
    assert source.read_bytes() == (tmp_path / "destination.txt").read_bytes() == b"keep\n"


def test_successful_hunks_stay_in_source_when_update_move_has_failed_hunk(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"one=old\ntwo=old\n")
    result = apply(
        tmp_path,
        update(
            "*** Move to: moved.txt\n@@\n-one=old\n+one=new\n"
            "@@\n-completely absent\n+unused\n@@\n-two=old\n+two=new"
        ),
    )
    assert [r["status"] for r in result["data"]["results"]] == [
        "applied",
        "failed",
        "applied",
        "skipped",
    ]
    assert path.read_bytes() == b"one=new\ntwo=new\n"
    assert not (tmp_path / "moved.txt").exists()


def test_first_write_failure_does_not_block_other_files(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    original = module.atomic_write_bytes

    def fail_first(path, payload, **kwargs):
        if path.name == "a.txt":
            raise OSError("fixture write failure")
        original(path, payload, **kwargs)

    monkeypatch.setattr(module, "atomic_write_bytes", fail_first)
    result = apply(tmp_path, "*** Add File: a.txt\n+first\n*** Add File: b.txt\n+second")
    assert [r["status"] for r in result["data"]["results"]] == ["failed", "applied"]
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_bytes() == b"second\n"


@pytest.mark.parametrize("suffix", ["*** Unknown File: nope", "*** Update File: missing.txt"])
def test_unparseable_structure_rejects_before_any_write(tmp_path, suffix):
    result = apply(tmp_path, "*** Add File: good.txt\n+valid\n" + suffix)
    assert result["error"]["code"] == "invalid_patch"
    assert not list(tmp_path.iterdir())


def test_overlapping_paths_do_not_block_independent_file(tmp_path):
    result = apply(
        tmp_path,
        "*** Add File: parent\n+file\n*** Add File: parent/child\n+child\n"
        "*** Add File: valid.txt\n+done",
    )
    assert [r["status"] for r in result["data"]["results"]] == ["failed", "failed", "applied"]
    assert not (tmp_path / "parent").exists()
    assert (tmp_path / "valid.txt").read_bytes() == b"done\n"


def test_external_change_between_hunks_is_not_overwritten(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    original = module._commit
    path = tmp_path / "file.txt"
    path.write_bytes(b"one=old\ntwo=old\n")

    def racing_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(b"external\ntwo=old\n")
        return result

    monkeypatch.setattr(module, "_commit", racing_commit)
    result = apply(tmp_path, update("@@\n-one=old\n+one=new\n@@\n-two=old\n+two=new"))
    assert result["data"]["status"] == "partial"
    assert result["data"]["results"][1]["error"]["code"] == "file_changed"
    assert path.read_bytes() == b"external\ntwo=old\n"


@pytest.mark.parametrize("observation_error", [False, True])
def test_post_write_verification_failure_retains_effect_and_continues_siblings(
    tmp_path, monkeypatch, observation_error
):
    from core.tools import apply_patch as module

    original_write, original_snapshot = module.atomic_write_bytes, module._snapshot
    written = False
    path = tmp_path / "a.txt"

    def write_then_interfere(target, payload, **kwargs):
        nonlocal written
        original_write(target, payload, **kwargs)
        if target == path:
            written = True
            if not observation_error:
                path.write_bytes(b"external\n")

    def snapshot(target):
        if observation_error and written and target == path:
            raise PermissionError("fixture observation unavailable")
        return original_snapshot(target)

    monkeypatch.setattr(module, "atomic_write_bytes", write_then_interfere)
    monkeypatch.setattr(module, "_snapshot", snapshot)
    result = apply(tmp_path, "*** Add File: a.txt\n+requested\n*** Add File: b.txt\n+done")
    assert result["ok"] and result["data"]["status"] == "partial"
    assert [r["status"] for r in result["data"]["results"]] == ["partial", "applied"]
    assert result["data"]["results"][0]["completed_paths"] == [path.as_posix()]
    assert path.read_bytes() == (b"requested\n" if observation_error else b"external\n")
    assert (tmp_path / "b.txt").read_bytes() == b"done\n"


def test_move_rechecks_destination_before_deleting_source(tmp_path, monkeypatch):
    from core.tools import apply_patch as module

    original = module._snapshot
    source, destination = tmp_path / "source.txt", tmp_path / "destination.txt"
    source.write_bytes(b"source bytes\n")
    observed_written_destination = False

    def snapshot(path):
        nonlocal observed_written_destination
        result = original(path)
        if path == destination and result.payload is not None and not observed_written_destination:
            observed_written_destination = True
            destination.write_bytes(b"external bytes\n")
        return result

    monkeypatch.setattr(module, "_snapshot", snapshot)
    result = apply(tmp_path, "*** Move File: source.txt -> destination.txt")
    assert result["data"]["status"] == "partial"
    assert result["data"]["results"][0]["error"]["code"] == "file_changed"
    assert source.read_bytes() == b"source bytes\n"
    assert destination.read_bytes() == b"external bytes\n"


def test_repeated_leading_begin_markers_are_unambiguous(tmp_path):
    result = apply(
        tmp_path, "*** Begin Patch\n*** Begin Patch\n*** Add File: new.txt\n+done\n*** End Patch"
    )
    assert result["ok"]
    assert (tmp_path / "new.txt").read_bytes() == b"done\n"
    result = apply(
        tmp_path, "*** Add File: other.txt\n+no\n*** Begin Patch\n*** Add File: last.txt\n+no"
    )
    assert result["error"]["code"] == "invalid_patch"
    assert not (tmp_path / "other.txt").exists()
    assert not (tmp_path / "last.txt").exists()


def test_move_with_explicit_hunks_uses_update_move_semantics(tmp_path):
    source = tmp_path / "one.txt"
    source.write_bytes(b"old\n")
    result = apply(tmp_path, "*** Move File: one.txt -> moved.txt\n@@\n-old\n+new")
    assert result["data"]["status"] == "success"
    assert [r["action"] for r in result["data"]["results"]] == ["update", "move"]
    assert not source.exists()
    assert (tmp_path / "moved.txt").read_bytes() == b"new\n"
    source.write_bytes(b"source\n")
    result = apply(tmp_path, "*** Move File: one.txt -> other.txt\n@@\n-missing\n+unused")
    assert not result["ok"]
    assert source.read_bytes() == b"source\n"
    assert not (tmp_path / "other.txt").exists()
