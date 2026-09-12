"""Apply patch: transactions behavior."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry
from tests.core.tools.apply_patch_helpers import (
    apply,
    context,
    update,
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
