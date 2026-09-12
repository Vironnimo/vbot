"""Apply patch: operations behavior."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.tools.apply_patch import make_apply_patch_handler, register_apply_patch_tool
from core.tools.change_tracker import ChangeTracker
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry
from tests.core.tools.apply_patch_helpers import (
    apply,
    context,
    update,
)


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
