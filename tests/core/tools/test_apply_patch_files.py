"""apply_patch file effects: formats, read guard, paths and links, stamps, locks and failures."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from core.sessions import SessionAddress
from core.tools import _file_changes as file_changes_module
from core.tools import file_state as file_state_module
from core.tools.change_tracker import ChangeTracker
from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_test_support import apply, call, context, registry, text, update

BOM = b"\xef\xbb\xbf"


@pytest.mark.parametrize(
    ("ending", "bom", "final"),
    [
        (b"\n", b"", True),
        (b"\n", BOM, False),
        (b"\r\n", b"", False),
        (b"\r\n", BOM, True),
        (b"\r", b"", True),
        (b"\r", BOM, False),
    ],
)
def test_update_and_replacement_preserve_encoding_endings_and_permissions(
    tmp_path, ending, bom, final
):
    path = tmp_path / "file.txt"
    before = ending.join([b"unchanged\t  ", b"old = 1", b"tail"])
    path.write_bytes(bom + before + (ending if final else b""))
    mode = stat.S_IMODE(path.stat().st_mode)
    state = FileReadState()

    updated = apply(tmp_path, update("@@\n unchanged\t  \n-old = 1\n+new = 2\n tail"), state=state)

    assert updated["ok"], updated
    assert path.read_bytes() == bom + before.replace(b"old = 1", b"new = 2") + (
        ending if final else b""
    )
    patch = "*** Add File: file.txt\n+new\n+content"
    if not final:
        patch += "\n\\ No newline at end of file"
    replaced = apply(tmp_path, patch, state=state)
    assert text(replaced) == "Replaced the content of file.txt (2 lines)."
    assert path.read_bytes() == bom + b"new" + ending + b"content" + (ending if final else b"")
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert state.check_stale("session-test", path) is None
    assert apply(tmp_path, patch, state=state)["data"]["status"] == "unchanged"


def test_add_and_append_use_lf_when_a_file_has_only_unicode_separators(tmp_path):
    path = tmp_path / "one-line.json"
    path.write_bytes('{"k":"x y"}'.encode())
    state = FileReadState()
    state.record_read("session-test", path.resolve())

    assert apply(tmp_path, update("@@\n+more", "one-line.json"), state=state)["ok"]
    assert path.read_bytes() == '{"k":"x y"}\nmore\n'.encode()
    assert apply(tmp_path, "*** Add File: one-line.json\n+{\n+}", state=state)["ok"]
    assert path.read_bytes() == b"{\n}\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_session", "arguments", "ending"),
    [
        (None, {"file_path": "file.txt", "content": "new\n"}, b"\r\n"),
        # A recovered Add body without + still needs this Session's read.
        ("another-session", {"patch": "*** Add File: file.txt\nnew"}, b"\r"),
        ("session-test", {"patch": "*** Add File: file.txt\n+new"}, b"\n"),
    ],
)
async def test_full_replacement_needs_a_current_read_by_this_session(
    tmp_path, read_session, arguments, ending
):
    path = tmp_path / "file.txt"
    path.write_bytes(b"one" + ending + b"two" + ending)
    state = FileReadState()
    tools = registry(state)
    code = "file_not_read"
    if read_session:
        state.record_read(read_session, path)
    if read_session == "session-test":
        path.write_bytes(b"one\ntwo\nthree\n")
        code = "file_modified_since_read"
    before = path.read_bytes()

    refused = await call(tmp_path, arguments, tools=tools)

    assert refused["error"]["code"] == code
    message = text(refused)
    # The error shows the whole small file, which counts as the read the guard asks for.
    assert "Its current content follows and now counts as read" in message
    assert "\n1| one\n2| two\n" in message and "\r" not in message
    assert message.endswith("\nNo file was changed.")
    assert path.read_bytes() == before
    replaced = await call(tmp_path, arguments, tools=tools)
    assert text(replaced) == "Replaced the content of file.txt (1 line)."
    assert path.read_bytes() == b"new" + ending


@pytest.mark.parametrize("payload", [b"line\n" * 401, b"\xff\x00\x01 binary"])
def test_overwrite_guard_names_read_for_large_or_binary_files(tmp_path, payload):
    path = tmp_path / "file.txt"
    path.write_bytes(payload)
    state = FileReadState()
    result = apply(tmp_path, "*** Add File: file.txt\n+new", state=state)
    assert result["error"]["code"] == "file_not_read"
    assert 'Read it with read(path="file.txt"), then send the call again.' in text(result)
    assert state.check_stale("session-test", path) is not None
    assert path.read_bytes() == payload


@pytest.mark.parametrize(
    ("existing", "body", "expected"),
    [
        (None, "", b""),
        (b"old content\n", "\n+", b"\n"),
        (b"old content\n", "\n+false", b"false\n"),
        # An empty existing file needs no read to be replaced.
        (b"\n", "\n+new", b"new\n"),
    ],
)
def test_empty_and_literal_replacements(tmp_path, existing, body, expected):
    path = tmp_path / "file.txt"
    state = FileReadState()
    if existing is not None:
        path.write_bytes(existing)
        if existing.strip():
            state.record_read("session-test", path)
    assert apply(tmp_path, "*** Add File: file.txt" + body, state=state)["ok"]
    assert path.read_bytes() == expected


def test_guard_switch_still_allows_full_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tools.file_state.FILE_STATE_GUARD_ENABLED", False)
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    assert apply(tmp_path, "*** Add File: file.txt\n+new")["ok"]
    assert path.read_bytes() == b"new\n"


def test_unchanged_add_counts_as_a_read(tmp_path):
    path = tmp_path / "file.py"
    path.write_bytes(b"broken = (\nnew\n")
    state = FileReadState()
    assert apply(tmp_path, "*** Add File: file.py\n+broken = (\n+new", state=state)["ok"]
    assert state.check_stale("session-test", path) is None


@pytest.mark.asyncio
async def test_read_alias_and_ordered_full_replacements_share_state(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    tools = registry(FileReadState(), read=True)
    assert (await call(tmp_path, {"path": str(path)}, tools=tools, name="read"))["ok"]
    result = await call(
        tmp_path,
        {
            "patch": "*** Add File: ./file.txt\n+first\n*** Add File: file.txt\n+second\n"
            "*** Update File: file.txt\n@@\n-second\n+final"
        },
        tools=tools,
    )
    assert result["data"]["status"] == "applied"
    assert path.read_bytes() == b"final\n"


def test_paths_resolve_from_cwd_and_absolute_aliases_share_history(tmp_path):
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


# Session shape: Add File text for a Python source file held a NUL character.
@pytest.mark.parametrize(
    ("before", "operation"),
    [
        (None, "*** Add File: file.txt\n+a\x00b"),
        (b"a\n", "*** Update File: file.txt\n@@\n-a\n+a\x00b"),
    ],
)
def test_nul_character_in_new_text_is_refused_with_its_escape_sequence(tmp_path, before, operation):
    path = tmp_path / "file.txt"
    if before is not None:
        path.write_bytes(before)
    result = apply(tmp_path, operation)
    assert result["error"]["code"] == "binary_file"
    assert text(result) == (
        "file.txt: the new text contains a NUL character (U+0000), which only binary files "
        "hold. To produce that character in source code, write its escape sequence instead, "
        "such as \\x00.\nNo file was changed."
    )
    assert (path.read_bytes() if path.exists() else None) == before


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def _linked_file(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "shared").mkdir()
    target = tmp_path / "shared" / "real.txt"
    target.write_bytes(b"precious\n")
    link = tmp_path / "link.txt"
    _symlink_or_skip(link, target)
    return link, target


@pytest.mark.parametrize(
    ("operation", "content", "moved"),
    [
        ("*** Delete File: link.txt", "Deleted link.txt.", None),
        ("*** Move File: link.txt -> moved.txt", "Moved link.txt to moved.txt.", "moved.txt"),
        # A later Add at the name creates a file there, never writing through the old link.
        (
            "*** Delete File: link.txt\n*** Add File: link.txt\n+fresh",
            "Replaced the content of link.txt (1 line).",
            None,
        ),
        (
            "*** Move File: link.txt -> moved.txt\n*** Add File: link.txt\n+fresh",
            "Moved link.txt to moved.txt.\nCreated link.txt (1 line).",
            "moved.txt",
        ),
    ],
)
def test_delete_and_move_act_on_the_link_and_keep_its_target(tmp_path, operation, content, moved):
    link, target = _linked_file(tmp_path)
    state = FileReadState()
    # The Session knows the target, so no read guard stands between the Add and it.
    state.record_read("session-test", target.resolve())

    result = apply(tmp_path, operation, state=state)

    assert text(result) == content
    if "Add File" in operation:
        assert not link.is_symlink() and link.read_bytes() == b"fresh\n"
    else:
        assert not os.path.lexists(link)
    assert target.read_bytes() == b"precious\n"
    if moved:
        assert (tmp_path / moved).is_symlink()
        assert (tmp_path / moved).resolve() == target.resolve()


def test_update_through_a_link_edits_the_target_and_move_to_renames_the_link(tmp_path):
    link, target = _linked_file(tmp_path)

    result = apply(
        tmp_path, "*** Update File: link.txt\n*** Move to: renamed.txt\n@@\n-precious\n+edited"
    )

    # The edit lands in the link's target, and the link itself is renamed.
    assert text(result) == "Updated shared/real.txt:\n1| edited\nMoved link.txt to renamed.txt."
    assert target.read_bytes() == b"edited\n"
    assert (tmp_path / "renamed.txt").is_symlink()
    assert not os.path.lexists(link)


def test_a_link_retargeted_while_the_patch_runs_is_not_written_through(tmp_path, monkeypatch):
    link, target = _linked_file(tmp_path)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"keep\n")
    original = file_changes_module._run_step

    def retarget_after_first_step(*args, **kwargs):
        original(*args, **kwargs)
        if os.readlink(link) != str(elsewhere):
            link.unlink()
            os.symlink(elsewhere, link)

    monkeypatch.setattr(file_changes_module, "_run_step", retarget_after_first_step)

    result = apply(tmp_path, "*** Add File: first.txt\n+x\n*** Add File: link.txt\n+fresh")

    assert result["data"]["status"] == "partial"
    assert text(result).endswith(
        "Created first.txt (1 line).\nFailed: link.txt now leads to another file than when this "
        "patch started, because a link on its way changed. Send this change again in a separate "
        "call."
    )
    assert elsewhere.read_bytes() == b"keep\n"
    assert target.read_bytes() == b"precious\n"


def test_dangling_links_are_entries_for_delete_and_move_destinations(tmp_path):
    missing = tmp_path / "missing.txt"
    dangling = tmp_path / "dangling.txt"
    _symlink_or_skip(dangling, missing)
    (tmp_path / "source.txt").write_bytes(b"data\n")

    blocked = apply(tmp_path, "*** Move File: source.txt -> dangling.txt")

    assert blocked["error"]["code"] == "destination_exists"
    assert not missing.exists()
    assert (tmp_path / "source.txt").read_bytes() == b"data\n"
    deleted = apply(tmp_path, "*** Delete File: dangling.txt")
    assert deleted["ok"], deleted
    assert not os.path.lexists(dangling)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions")
def test_windows_junction_is_deleted_and_moved_as_a_link(tmp_path):
    import _winapi

    (tmp_path / "outside").mkdir()
    secret = tmp_path / "outside" / "secret.txt"
    secret.write_bytes(b"keep\n")
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(tmp_path / "outside"), str(junction))

    moved = apply(tmp_path, "*** Move File: junction -> renamed")

    assert moved["ok"], moved
    assert not os.path.lexists(junction)
    assert (tmp_path / "renamed" / "secret.txt").read_bytes() == b"keep\n"
    deleted = apply(tmp_path, "*** Delete File: renamed")
    assert deleted["ok"], deleted
    assert not os.path.lexists(tmp_path / "renamed")
    assert secret.read_bytes() == b"keep\n"


def test_case_only_move_renames_the_file(tmp_path):
    (tmp_path / "readme.txt").write_bytes(b"hi\n")
    state = FileReadState()

    result = apply(tmp_path, "*** Move File: readme.txt -> README.txt", state=state)

    assert result["data"] == {"status": "applied", "content": "Moved readme.txt to README.txt."}
    assert os.listdir(tmp_path) == ["README.txt"]
    assert (tmp_path / "README.txt").read_bytes() == b"hi\n"
    # The renamed file counts as read for a later full replacement.
    assert apply(tmp_path, "*** Add File: README.txt\n+hello", state=state)["ok"]


def test_update_with_case_only_move_to_edits_and_renames(tmp_path):
    (tmp_path / "readme.txt").write_bytes(b"hi\n")

    result = apply(tmp_path, update("*** Move to: README.TXT\n@@\n-hi\n+hello", "readme.txt"))

    assert text(result) == "Updated readme.txt and moved it to README.TXT:\n1| hello"
    assert os.listdir(tmp_path) == ["README.TXT"]
    assert (tmp_path / "README.TXT").read_bytes() == b"hello\n"


WRITE_WARNING = "Warning: Syntax check failed after this write: "


@pytest.mark.parametrize(
    ("original", "name", "content", "warning"),
    [
        # The parser reads bytes, so a coding cookie is no error.
        (None, "module.py", "# -*- coding: utf-8 -*-\nx = 1", None),
        (None, "broken.py", "if True:\npass", WRITE_WARNING + "IndentationError"),
        (None, "data.json", '{"a": 1,}', WRITE_WARNING + "JSONDecodeError"),
        (None, "data.json", '{"a": 1, "b": [2, 3]}', None),
        (None, "config.yaml", "a: 1\n  b: 2\n bad: indent", WRITE_WARNING + "YAMLError"),
        (None, "config.yml", "a: 1\nb:\n  - 2", None),
        (None, "pyproject.toml", "key = = 1", WRITE_WARNING + "TOMLDecodeError"),
        (None, "pyproject.toml", '[tool]\nname = "x"', None),
        (None, "notes.txt", "this is not { valid json but txt is unchecked", None),
        # A full replacement is checked even where the original did not parse.
        (b"def old(\n", "file.py", "def new(", WRITE_WARNING + "SyntaxError"),
        (b"\xff\x00", "file.py", "def new(", WRITE_WARNING + "SyntaxError"),
    ],
)
def test_written_files_are_syntax_checked(tmp_path, original, name, content, warning):
    state = FileReadState()
    if original is not None:
        (tmp_path / name).write_bytes(original)
        state.record_read("session-test", tmp_path / name)
    body = "\n".join("+" + line for line in content.split("\n"))

    result = apply(tmp_path, f"*** Add File: {name}\n{body}", state=state)

    assert result["ok"], result
    if warning is None:
        assert "Warning:" not in text(result)
    else:
        assert warning in text(result)


@pytest.mark.parametrize(
    ("before", "hunk", "warning"),
    [
        ("x = 1\n", "@@\n-x = 1\n+x = 2", None),
        (
            "x = 1\n",
            "@@\n-x = 1\n+x = (1",
            "Warning: Syntax check failed after this edit: SyntaxError",
        ),
        # An error the file had before is not blamed on the edit, and fixing it clears it.
        (
            "def f(:\n    return 1\n",
            "@@\n-    return 1\n+    return 2",
            "Warning: File was already syntactically invalid before this edit",
        ),
        ("def f(:\n    return 1\n", "@@\n-def f(:\n+def f():", None),
    ],
)
def test_edited_files_are_syntax_checked_without_blaming_earlier_errors(
    tmp_path, before, hunk, warning
):
    (tmp_path / "file.py").write_bytes(before.encode())

    result = apply(tmp_path, update(hunk, "file.py"))

    assert result["ok"], result
    if warning is None:
        assert "Warning:" not in text(result)
    else:
        assert warning in text(result)


def test_current_content_stamps_stats_and_notes_an_external_change(tmp_path):
    path = tmp_path / "file.py"
    path.write_bytes(b"value = 1\n")
    state = FileReadState()
    state.record_read("session-test", path)
    path.write_bytes(b"external = True\nvalue = 1\n")
    tracker = ChangeTracker()
    ctx = context(tmp_path, change_tracker=tracker)
    result = apply(tmp_path, update("@@\n-value = 1\n+value = 2", "file.py"), state=state, ctx=ctx)
    assert result["ok"]
    assert "Note: file.py changed after this Session last read it" in text(result)
    assert state.check_stale("session-test", path) is None
    stats = tracker.peek_run_stats(
        (SessionAddress(ctx.project_id, ctx.agent_id, ctx.session_id), ctx.run_id)
    )
    assert stats["added"] == 1 and stats["removed"] == 1
    assert path.read_bytes() == b"external = True\nvalue = 2\n"


@pytest.mark.parametrize("scope_change", [{"project_id": "project"}, {"agent_id": "other-agent"}])
def test_patch_stats_keep_identical_session_and_run_ids_in_separate_scopes(tmp_path, scope_change):
    tracker = ChangeTracker()
    first = context(tmp_path, change_tracker=tracker)
    second = replace(first, **scope_change)
    assert apply(tmp_path, "*** Add File: first.txt\n+one", ctx=first)["ok"]
    assert apply(tmp_path, "*** Add File: second.txt\n+two\n+three", ctx=second)["ok"]
    for ctx, name, added in [(first, "first.txt", 1), (second, "second.txt", 2)]:
        key = (SessionAddress(ctx.project_id, ctx.agent_id, ctx.session_id), ctx.run_id)
        assert tracker.take_run_stats(key) == {
            "files": 1,
            "added": added,
            "removed": 0,
            "paths": [str(tmp_path / name)],
            "file_stats": [{"path": str(tmp_path / name), "added": added, "removed": 0}],
        }


@pytest.mark.asyncio
async def test_locking_and_read_stamp_are_shared_with_replacement(tmp_path):
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
    written = apply(tmp_path, "*** Add File: file.txt\n+replacement works", state=state)
    assert written["ok"]
    assert path.read_bytes() == b"replacement works\n"


@pytest.mark.asyncio
async def test_cancelled_call_finishes_its_write_before_cancelling(tmp_path, monkeypatch):
    state = FileReadState()
    tool = registry(state).get("apply_patch")
    entered, release = threading.Event(), threading.Event()
    original = file_changes_module.atomic_write_bytes

    def slow_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(file_changes_module, "atomic_write_bytes", slow_write)
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
    result = apply(tmp_path, "*** Add File: new/created.txt\n+would be new\n" + suffix)
    assert result["ok"] and result["data"]["status"] == "partial"
    assert text(result).startswith("1 of 2 changes applied; 1 did not.")
    assert "\nFailed: " in text(result)
    assert path.read_bytes() == b"original\n"
    assert (tmp_path / "new/created.txt").read_bytes() == b"would be new\n"


def test_overlapping_paths_do_not_block_independent_file(tmp_path):
    result = apply(
        tmp_path,
        "*** Add File: parent\n+file\n*** Add File: parent/child\n+child\n"
        "*** Add File: valid.txt\n+done",
    )
    assert text(result).startswith("1 of 3 changes applied; 2 did not.")
    assert (
        "Failed: parent is used both as a file and as a folder of another path in this patch."
    ) in text(result)
    assert not (tmp_path / "parent").exists()
    assert (tmp_path / "valid.txt").read_bytes() == b"done\n"


@pytest.mark.parametrize(("failing", "written"), [("a.txt", "b.txt"), ("b.txt", "a.txt")])
def test_write_failure_reports_actual_partial_changes(tmp_path, monkeypatch, failing, written):
    original = file_changes_module.atomic_write_bytes

    def fail_one(path, payload, **kwargs):
        if path.name == failing:
            raise OSError("fixture failure")
        original(path, payload, **kwargs)

    monkeypatch.setattr(file_changes_module, "atomic_write_bytes", fail_one)
    result = apply(tmp_path, "*** Add File: a.txt\n+first\n*** Add File: b.txt\n+second")
    assert result["ok"] and result["data"]["status"] == "partial"
    assert text(result).startswith("1 of 2 changes applied; 1 did not.")
    assert f"Created {written} (1 line)." in text(result)
    assert text(result).endswith(
        f"Failed: Could not change {failing}: fixture failure. Check this path before "
        "resending this change."
    )
    assert not (tmp_path / failing).exists()
    assert (tmp_path / written).exists()


def test_failed_move_keeps_source(tmp_path, monkeypatch):
    source = tmp_path / "file.txt"
    source.write_bytes(b"original")

    def fail(*args, **kwargs):
        raise OSError("fixture failure")

    monkeypatch.setattr(file_changes_module, "atomic_write_bytes", fail)
    result = apply(tmp_path, "*** Move File: file.txt -> new.txt")
    assert not result["ok"]
    assert source.read_bytes() == b"original"
    assert not (tmp_path / "new.txt").exists()


def test_failed_add_blocks_dependent_update_but_not_other_file(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"unrelated existing content\n")
    result = apply(
        tmp_path,
        "*** Add File: file.txt\n+new file\n"
        "*** Update File: file.txt\n@@\n-unrelated existing content\n+clobbered\n"
        "*** Add File: independent.txt\n+done",
    )
    assert text(result).startswith("1 of 3 changes applied; 2 did not.")
    assert "\nFailed: file.txt already exists and this Session has not read it" in text(result)
    assert text(result).endswith(
        "Skipped: file.txt was not tried because an earlier change to file.txt in this patch "
        "did not complete. Resend it together with that change."
    )
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
    assert text(result).startswith("1 of 3 changes applied; 2 did not.")
    assert text(result).endswith(
        "\nCreated independent.txt (1 line).\n"
        "Failed: Cannot move to destination.txt: it already exists. Delete it earlier in the "
        "same patch or choose another destination.\n"
        "Skipped: destination.txt was not tried because an earlier change to destination.txt "
        "in this patch did not complete. Resend it together with that change."
    )
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
    assert text(result).startswith("1 of 3 changes fully applied; 1 only in part; 1 not at all.")
    assert (
        "Incomplete: Could not change source.txt: fixture cannot remove source. Check this "
        "path before resending this change.\nAlready done: destination.txt. Not done: "
        "source.txt.\nSkipped: destination.txt was not tried"
    ) in text(result)
    assert source.read_bytes() == (tmp_path / "destination.txt").read_bytes() == b"keep\n"


@pytest.mark.parametrize(
    ("body", "summary", "after"),
    [
        (
            "*** Move to: moved.txt\n@@\n-one=old\n+one=new\n"
            "@@\n-completely absent\n+unused\n@@\n-two=old\n+two=new",
            "2 of 4 changes applied; 2 did not.",
            b"one=new\ntwo=new\n",
        ),
        (
            "@@\n-one=old\n+one=new\n@@\n-completely absent\n+unused\n*** Move to: moved.txt",
            "1 of 3 changes applied; 2 did not.",
            b"one=new\ntwo=old\n",
        ),
    ],
)
def test_successful_hunks_stay_in_source_when_update_move_has_failed_hunk(
    tmp_path, body, summary, after
):
    path = tmp_path / "file.txt"
    path.write_bytes(b"one=old\ntwo=old\n")
    result = apply(tmp_path, update(body))
    assert text(result).startswith(summary)
    assert "Failed: file.txt, hunk 2: the lines to replace were not found." in text(result)
    assert text(result).endswith(
        "Skipped: file.txt was not moved to moved.txt because a change to it above failed; "
        "it keeps its name. Resend the move together with the failed change."
    )
    assert path.read_bytes() == after
    assert not (tmp_path / "moved.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attribute")
def test_update_of_read_only_file_fails_clearly_without_retry_or_leftovers(tmp_path):
    path = tmp_path / "read-only.txt"
    path.write_bytes(b"a\nb\n")
    path.chmod(stat.S_IREAD)
    try:
        result = apply(tmp_path, update("@@\n-a\n+A", "read-only.txt"))

        assert result["error"]["code"] == "file_write_error"
        assert "read-only" in result["error"]["message"]
        assert "retryable" not in result["error"]
        assert "attempts_made" not in result["error"]
        assert path.read_bytes() == b"a\nb\n"
        assert [entry.name for entry in tmp_path.iterdir()] == ["read-only.txt"]
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


# External changes while a patch runs. They are injected at the private planning, commit
# and observation steps of core.tools._file_changes, which no public seam reaches.


def test_external_change_during_planning_is_detected(tmp_path, monkeypatch):
    original = file_changes_module._plan
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")

    def racing_plan(*args, **kwargs):
        plan = original(*args, **kwargs)
        path.write_bytes(b"external\n")
        return plan

    monkeypatch.setattr(file_changes_module, "_plan", racing_plan)
    result = apply(tmp_path, update("@@\n-old\n+new"))
    assert result["error"]["code"] == "file_changed"
    assert path.read_bytes() == b"external\n"


def test_external_change_between_hunks_is_not_overwritten(tmp_path, monkeypatch):
    original = file_changes_module._commit
    path = tmp_path / "file.txt"
    path.write_bytes(b"one=old\ntwo=old\n")

    def racing_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(b"external\ntwo=old\n")
        return result

    monkeypatch.setattr(file_changes_module, "_commit", racing_commit)
    result = apply(tmp_path, update("@@\n-one=old\n+one=new\n@@\n-two=old\n+two=new"))
    assert result["data"]["status"] == "partial"
    assert text(result).endswith(
        "Failed: file.txt changed on disk while this patch ran. Read it and resend the "
        "unfinished changes."
    )
    assert path.read_bytes() == b"external\ntwo=old\n"


@pytest.mark.parametrize("observation_error", [False, True])
def test_post_write_verification_failure_retains_effect_and_continues_siblings(
    tmp_path, monkeypatch, observation_error
):
    original_write, original_snapshot = (
        file_changes_module.atomic_write_bytes,
        file_changes_module._snapshot,
    )
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

    monkeypatch.setattr(file_changes_module, "atomic_write_bytes", write_then_interfere)
    monkeypatch.setattr(file_changes_module, "_snapshot", snapshot)
    result = apply(tmp_path, "*** Add File: a.txt\n+requested\n*** Add File: b.txt\n+done")
    assert result["ok"] and result["data"]["status"] == "partial"
    reason = (
        "a.txt was written, but reading it back failed: fixture observation unavailable. "
        "Check it before resending this change."
        if observation_error
        else "a.txt changed on disk while this patch ran. Read it and resend the unfinished "
        "changes."
    )
    assert text(result).startswith("1 of 2 changes fully applied; 1 only in part.")
    assert text(result).endswith(
        f"\nCreated a.txt (1 line).\nCreated b.txt (1 line).\nIncomplete: {reason}\n"
        "Already done: a.txt."
    )
    assert path.read_bytes() == (b"requested\n" if observation_error else b"external\n")
    assert (tmp_path / "b.txt").read_bytes() == b"done\n"


def test_move_rechecks_destination_before_deleting_source(tmp_path, monkeypatch):
    original = file_changes_module._snapshot
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

    monkeypatch.setattr(file_changes_module, "_snapshot", snapshot)
    result = apply(tmp_path, "*** Move File: source.txt -> destination.txt")
    assert result["data"]["status"] == "partial"
    assert text(result).endswith(
        "Incomplete: destination.txt changed on disk while this patch ran. Read it and resend "
        "the unfinished changes.\nAlready done: destination.txt. Not done: source.txt."
    )
    assert source.read_bytes() == b"source bytes\n"
    assert destination.read_bytes() == b"external bytes\n"


def test_replacement_rechecks_bytes_after_guard(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    state = FileReadState()
    state.record_read("session-test", path)
    original = state.check_stale

    def drift(session_id, resolved):
        result = original(session_id, resolved)
        path.write_bytes(b"external change\n")
        return result

    monkeypatch.setattr(state, "check_stale", drift)
    result = apply(tmp_path, "*** Add File: file.txt\n+new", state=state)
    assert result["error"]["code"] == "file_changed"
    assert path.read_bytes() == b"external change\n"


class _WindowsReplaceError(PermissionError):
    """A failed replace carrying a Windows error code, on every platform."""

    def __init__(self, code: int) -> None:
        super().__init__("replace temporarily unavailable")
        self.winerror = code


def sharing_error(code: int = 5) -> OSError:
    return _WindowsReplaceError(code)


def test_windows_retry_replaces_once_without_replaying_earlier_append(tmp_path, monkeypatch):
    (tmp_path / "log.txt").write_bytes(b"start\n")
    (tmp_path / "file.txt").write_bytes(b"old\n")
    original = file_state_module.os.replace
    attempts = []
    sleeps = []

    def flaky_replace(source, target):
        attempts.append(Path(target).name)
        if Path(target).name == "file.txt" and attempts.count("file.txt") <= 2:
            raise sharing_error(32)
        original(source, target)

    monkeypatch.setattr(file_state_module.os, "replace", flaky_replace)
    monkeypatch.setattr(file_state_module.time, "sleep", sleeps.append)
    result = apply(
        tmp_path, "*** Update File: log.txt\n@@\n+done\n*** Update File: file.txt\n@@\n-old\n+new"
    )
    assert result["ok"] and result["data"]["status"] == "applied"
    assert attempts == ["log.txt", "file.txt", "file.txt", "file.txt"]
    assert len(sleeps) == 2
    assert (tmp_path / "log.txt").read_bytes() == b"start\ndone\n"
    assert (tmp_path / "file.txt").read_bytes() == b"new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file.txt", "log.txt"]


@pytest.mark.parametrize("changed_path", ["source", "move-source", "move-destination"])
def test_retry_rechecks_source_and_destination_and_keeps_external_changes(
    tmp_path, monkeypatch, changed_path
):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    attempts = []

    def busy_replace(source, target):
        attempts.append(target)
        raise sharing_error()

    def external_write(delay):
        changed = tmp_path / "moved.txt" if changed_path == "move-destination" else path
        changed.write_bytes(b"external change\n")

    monkeypatch.setattr(file_state_module.os, "replace", busy_replace)
    monkeypatch.setattr(file_state_module.time, "sleep", external_write)
    patch = (
        "*** Move File: file.txt -> moved.txt"
        if changed_path.startswith("move-")
        else "*** Update File: file.txt\n@@\n-old\n+new"
    )
    result = apply(tmp_path, patch)
    assert result["error"]["code"] == "file_changed"
    assert len(attempts) == 1
    if changed_path == "move-destination":
        assert path.read_bytes() == b"old\n"
        assert (tmp_path / "moved.txt").read_bytes() == b"external change\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["file.txt", "moved.txt"]
    else:
        assert path.read_bytes() == b"external change\n"
        assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_two_sessions_use_same_path_lock_during_retry(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_bytes(b"first=old\nsecond=old\n")
    tool = registry().get("apply_patch")
    original = file_state_module.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sharing_error()
        original(source, target)

    monkeypatch.setattr(file_state_module.os, "replace", flaky_replace)
    results = await asyncio.gather(
        tool.handler(context(tmp_path), {"patch": update("@@\n-first=old\n+first=new")}),
        tool.handler(
            replace(context(tmp_path), session_id="another-session"),
            {"patch": update("@@\n-second=old\n+second=new")},
        ),
    )
    assert all(result["ok"] for result in results)
    assert path.read_bytes() == b"first=new\nsecond=new\n"


@pytest.mark.parametrize(
    ("code", "other_file"), [(5, True), (5, False), (32, False), (28, True), (None, False)]
)
def test_replace_failure_is_bounded_and_keeps_other_operations(
    tmp_path, monkeypatch, code, other_file
):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    original = file_state_module.os.replace
    attempts = []
    sleeps = []

    def failing_replace(source, target):
        if Path(target) == path:
            attempts.append(target)
            error = OSError("fixture unavailable")
            if code is not None:
                error.winerror = code
            raise error
        original(source, target)

    monkeypatch.setattr(file_state_module.os, "replace", failing_replace)
    monkeypatch.setattr(file_state_module.time, "sleep", sleeps.append)
    patch = "*** Update File: file.txt\n@@\n-old\n+new"
    if other_file:
        patch += "\n*** Add File: other.txt\n+done"
    result = apply(tmp_path, patch)
    retried = code in {5, 32}
    assert 1 < len(attempts) <= 10 if retried else len(attempts) == 1
    tried = f"The write was attempted {len(attempts)} times."
    if other_file:
        assert result["data"]["status"] == "partial"
        assert "Created other.txt (1 line).\nFailed: Could not change file.txt" in text(result)
        assert (tried in text(result)) is retried
        assert (tmp_path / "other.txt").read_bytes() == b"done\n"
    else:
        error = result["error"]
        assert error["code"] == "file_write_error"
        if retried:
            assert error["retryable"] and error["attempts_made"] == len(attempts)
            assert tried in error["message"]
        else:
            assert "retryable" not in error and "attempts_made" not in error
        if code == 32:
            assert error["message"].startswith(
                "Could not change file.txt: another program is using it. file.txt is "
                "unchanged; send this change again after that program has finished, for "
                "example a running test or script."
            )
    assert len(sleeps) == len(attempts) - 1 and sum(sleeps) < 6
    assert path.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == (
        ["file.txt", "other.txt"] if other_file else ["file.txt"]
    )
