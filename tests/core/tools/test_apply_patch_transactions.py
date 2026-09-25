"""Apply patch: transactions behavior."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry
from tests.core.tools.apply_patch_helpers import (
    apply,
    context,
    text,
    update,
)

LEAD = (
    "The applied changes stay in place: resend only the changes listed below as not"
    " done, based on the current text shown here."
)


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
    assert result["ok"] and result["data"]["status"] == "partial"
    expected = before
    for i in range(7):
        if i != bad_index:
            expected = expected.replace(f"setting_{i}=old", f"setting_{i}=new")
    assert path.read_text(encoding="utf-8") == expected
    # One region shows the file as it is now, including the unchanged line.
    shown = "\n".join(f"{i + 1}| {line}" for i, line in enumerate(expected.splitlines()))
    assert text(result) == (
        f"6 of 7 changes applied; 1 did not. {LEAD}\nUpdated file.txt:\n{shown}\n"
        f"Failed: file.txt, hunk {bad_index + 1}: the lines to replace were not found.\n"
        'No similar text is in the file; read(path="file.txt") shows its current content.'
    )


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
    assert text(result).startswith(f"1 of 3 changes applied; 2 did not. {LEAD}")
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
    assert text(result) == (
        f"1 of 2 changes applied; 1 did not. {LEAD}\nCreated other.txt (1 line).\n"
        "Failed: file.txt: the lines to replace were not found.\n"
        "The closest text in the file, lines 2-4:\n"
        "2| def deploy():\n3|     timeout = 30\n4|     retries = 5\n"
        "First difference, line 3: the file has '    timeout = 30' where the patch has "
        "'    completely unrelated declaration'."
    )
    assert path.read_text(encoding="utf-8") == before
    assert (tmp_path / "other.txt").read_bytes() == b"done\n"


def test_missing_hunk_without_close_candidate_offers_no_excerpt_promise(tmp_path):
    path = tmp_path / "file.txt"
    before = b"alpha\nbeta\n"
    path.write_bytes(before)
    result = apply(
        tmp_path,
        update("@@\n-gamma delta epsilon zeta eta theta iota kappa lambda\n+changed"),
    )
    assert result["error"]["code"] == "text_not_found"
    assert result["error"]["message"] == (
        "file.txt: the lines to replace were not found.\n"
        'No similar text is in the file; read(path="file.txt") shows its current content.\n'
        "No file was changed."
    )
    assert path.read_bytes() == before


def test_ambiguous_hint_reports_actual_locations_including_offset(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"intro\nsection\nrepeated\nleft\nrepeated\nright\n")
    result = apply(tmp_path, update("@@ section\n@@ repeated\n-left\n+changed"))
    assert result["error"]["code"] == "ambiguous_context"
    assert result["error"]["message"] == (
        'file.txt: the @@ line "repeated" occurs 2 times (lines 3, 5). Put a line after @@ '
        "that occurs once, or add a second @@ line below it to narrow the place.\n"
        "Where it occurs:\n2| section\n3| repeated\n4| left\n5| repeated\n6| right\n"
        "No file was changed."
    )
    assert path.read_bytes() == b"intro\nsection\nrepeated\nleft\nrepeated\nright\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [(0,), (1,), (2,), (0, 2)])
async def test_move_metadata_position_preserves_operation_through_dispatch(tmp_path, positions):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    (tmp_path / "file.txt").write_bytes(b"old\nsecond\n")
    parts = ["@@\n-old\n+new", "@@\n-second\n+last"]
    for position in reversed(positions):
        parts.insert(position, "*** Move to: moved.txt")
    result = await registry.dispatch(
        context(tmp_path), {"patch": update("\n".join(parts))}, ["apply_patch"]
    )
    assert result["data"] == {
        "status": "applied",
        "content": "Updated file.txt and moved it to moved.txt:\n1| new\n2| last",
    }
    assert not (tmp_path / "file.txt").exists()
    assert (tmp_path / "moved.txt").read_bytes() == b"new\nlast\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hint", "code"), [("value", "context_not_found"), ("marker", "ambiguous_context")]
)
async def test_context_failure_returns_candidates_without_substituting_target(tmp_path, hint, code):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    path = tmp_path / "file.txt"
    before = b"first\nmarker\nvalue=1\nsecond\nmarker\nvalue=2\n"
    path.write_bytes(before)
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": update(f"@@ {hint}\n-value=2\n+value=3")},
        ["apply_patch"],
    )
    assert result["error"]["code"] == code
    # Every place the @@ line could mean is shown with line numbers; none is picked.
    assert "3| value=1" in result["error"]["message"]
    assert "6| value=2" in result["error"]["message"]
    assert path.read_bytes() == before
    corrected = await registry.dispatch(
        context(tmp_path),
        {"patch": update("@@ second\n marker\n-value=2\n+value=3")},
        ["apply_patch"],
    )
    assert corrected["data"]["status"] == "applied"
    assert path.read_bytes() == b"first\nmarker\nvalue=1\nsecond\nmarker\nvalue=3\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "*** Update File: file.txt\n*** Move to: first.txt\n@@\n-old\n+new\n"
        "*** Move to: second.txt",
        "*** Move File: file.txt -> first.txt\n*** Move to: second.txt",
    ],
)
async def test_conflicting_moves_reject_before_any_dispatch_effect(tmp_path, operation):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": "*** Add File: unrelated.txt\n+pending\n" + operation},
        ["apply_patch"],
    )
    assert result["error"]["code"] == "conflicting_move"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {"file.txt": b"old\n"}


@pytest.mark.asyncio
async def test_late_move_after_failed_hunk_keeps_success_at_source(tmp_path):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = await registry.dispatch(
        context(tmp_path),
        {
            "patch": update(
                "@@\n-old\n+new\n@@\n-missing precondition\n+unused\n*** Move to: moved.txt"
            )
        },
        ["apply_patch"],
    )
    assert text(result).startswith(f"1 of 3 changes applied; 2 did not. {LEAD}")
    assert text(result).endswith(
        "Skipped: file.txt was not moved to moved.txt because a change to it above failed; "
        "it keeps its name. Resend the move together with the failed change."
    )
    assert (tmp_path / "file.txt").read_bytes() == b"new\n"
    assert not (tmp_path / "moved.txt").exists()


@pytest.mark.asyncio
async def test_move_shaped_added_content_remains_literal_through_dispatch(tmp_path):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    (tmp_path / "file.txt").write_bytes(b"old\n")
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": update("@@\n-old\n+*** Move to: literal.txt\n*** Move to: moved.txt")},
        ["apply_patch"],
    )
    assert result["data"]["status"] == "applied"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        "moved.txt": b"*** Move to: literal.txt\n"
    }


def test_long_line_preview_shows_change_and_surrounding_lines(tmp_path):
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
    assert len(preview) == 4
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


def test_net_zero_is_not_claimed_as_already_applied(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    result = apply(tmp_path, update("@@\n-old\n+new\n@@\n-new\n+old"))
    assert result["data"] == {
        "status": "unchanged",
        "content": "The changes to file.txt cancel each other out, so it is the same as before.",
    }
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
    assert text(result).startswith(f"1 of 3 changes applied; 2 did not. {LEAD}")
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
    assert text(result) == (
        f"1 of 3 changes applied; 2 did not. {LEAD}\nCreated independent.txt (1 line).\n"
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
    assert text(result).startswith(f"2 of 4 changes applied; 2 did not. {LEAD}")
    assert "Failed: file.txt, hunk 2: the lines to replace were not found." in text(result)
    assert "Skipped: file.txt was not moved to moved.txt" in text(result)
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
    assert text(result) == (
        f"1 of 2 changes applied; 1 did not. {LEAD}\nCreated b.txt (1 line).\n"
        "Failed: Could not change a.txt: fixture write failure. Check this path before "
        "resending this change."
    )
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
    assert text(result).startswith(f"1 of 3 changes applied; 2 did not. {LEAD}")
    assert (
        "Failed: parent is used both as a file and as a folder of another path in this patch."
    ) in text(result)
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
    assert text(result).endswith(
        "Failed: file.txt changed on disk while this patch ran. Read it and resend the "
        "unfinished changes."
    )
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
    reason = (
        "a.txt was written, but reading it back failed: fixture observation unavailable. "
        "Check it before resending this change."
        if observation_error
        else "a.txt changed on disk while this patch ran. Read it and resend the unfinished "
        "changes."
    )
    assert text(result) == (
        f"1 of 2 changes fully applied; 1 only in part. {LEAD}\nCreated a.txt (1 line).\n"
        f"Created b.txt (1 line).\nIncomplete: {reason}\nAlready done: a.txt."
    )
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
    assert text(result).endswith(
        "Incomplete: destination.txt changed on disk while this patch ran. Read it and resend "
        "the unfinished changes.\nAlready done: destination.txt. Not done: source.txt."
    )
    assert source.read_bytes() == b"source bytes\n"
    assert destination.read_bytes() == b"external bytes\n"


def test_repeated_and_inner_begin_markers_only_separate_operations(tmp_path):
    result = apply(
        tmp_path, "*** Begin Patch\n*** Begin Patch\n*** Add File: new.txt\n+done\n*** End Patch"
    )
    assert result["ok"]
    assert (tmp_path / "new.txt").read_bytes() == b"done\n"
    # A Begin marker cannot be file content (no leading +), so it only starts a new frame.
    result = apply(
        tmp_path, "*** Add File: other.txt\n+yes\n*** Begin Patch\n*** Add File: last.txt\n+yes"
    )
    assert text(result) == "Created other.txt (1 line).\nCreated last.txt (1 line)."
    assert (tmp_path / "other.txt").read_bytes() == (tmp_path / "last.txt").read_bytes()


def test_move_with_explicit_hunks_uses_update_move_semantics(tmp_path):
    source = tmp_path / "one.txt"
    source.write_bytes(b"old\n")
    result = apply(tmp_path, "*** Move File: one.txt -> moved.txt\n@@\n-old\n+new")
    assert text(result) == "Updated one.txt and moved it to moved.txt:\n1| new"
    assert not source.exists()
    assert (tmp_path / "moved.txt").read_bytes() == b"new\n"
    source.write_bytes(b"source\n")
    result = apply(tmp_path, "*** Move File: one.txt -> other.txt\n@@\n-missing\n+unused")
    assert not result["ok"]
    assert source.read_bytes() == b"source\n"
    assert not (tmp_path / "other.txt").exists()
