"""Real-call patch recovery without inventing content or replaying completed effects."""

from __future__ import annotations

import asyncio
from dataclasses import replace as replace_context
from pathlib import Path

import pytest

from core.tools import file_state as file_state_module
from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry
from tests.core.tools.apply_patch_helpers import context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{\n  "enabled": true\n}', b'{\n  "enabled": true\n}\n'),
        (
            "+/**\n * explanation\n+ */\nexport interface Source {\n+  next(): number;\n+}",
            b"/**\n * explanation\n */\nexport interface Source {\n  next(): number;\n}\n",
        ),
        ("+first\n\n  indented\n+last", b"first\n\n  indented\nlast\n"),
        ("@@\n+first\n+last", b"first\nlast\n"),
        ("+-literal\n+@@\n+*** End Patch\n++literal", b"-literal\n@@\n*** End Patch\n+literal\n"),
    ],
)
async def test_add_repairs_only_missing_syntax_preserving_content(tmp_path, body, expected):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": f"*** Add File: new.txt\n{body}\n*** End Patch"},
        ["apply_patch"],
    )
    assert result["ok"]
    assert (tmp_path / "new.txt").read_bytes() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", ["-old\n+new", "@@ section\n+new", "+first\n@@\n+last", "*** Unknown File: other.txt"]
)
async def test_add_does_not_reinterpret_removals_or_unknown_constraints(tmp_path, body):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": f"*** Add File: first.txt\n+good\n*** Add File: last.txt\n{body}"},
        ["apply_patch"],
    )
    assert result["error"]["code"] == "invalid_patch"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recovered_add_still_requires_this_sessions_read_stamp(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"keep\n")
    state = FileReadState()
    state.record_read("other-session", path)
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=state)
    result = await registry.dispatch(
        context(tmp_path), {"patch": "*** Add File: file.txt\nreplacement"}, ["apply_patch"]
    )
    assert result["error"]["code"] == "file_not_read"
    assert path.read_bytes() == b"keep\n"
    state.record_read("session-test", path)
    result = await registry.dispatch(
        context(tmp_path), {"patch": "*** Add File: file.txt\nreplacement"}, ["apply_patch"]
    )
    assert result["ok"]
    assert path.read_bytes() == b"replacement\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("locator", [" summary();", "summary();", "@@ summary();"])
async def test_context_only_patch_fails_without_inventing_an_insertion(tmp_path, locator):
    path = tmp_path / "file.txt"
    path.write_bytes(b"start();\nsummary();\n")
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": f"*** Update File: file.txt\n@@\n{locator}\n*** End Patch"},
        ["apply_patch"],
    )
    assert result["error"]["code"] == "no_changes"
    assert path.read_bytes() == b"start();\nsummary();\n"
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": "*** Update File: file.txt\n@@\n+check();\n summary();\n*** End Patch"},
        ["apply_patch"],
    )
    assert result["ok"]
    assert path.read_bytes() == b"start();\ncheck();\nsummary();\n"


def sharing_error(code: int = 5) -> OSError:
    error = PermissionError("replace temporarily unavailable")
    error.winerror = code
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [5, 32, 33])
async def test_windows_retry_replaces_once_without_replaying_earlier_append(
    tmp_path, monkeypatch, code
):
    (tmp_path / "log.txt").write_bytes(b"start\n")
    (tmp_path / "file.txt").write_bytes(b"old\n")
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    original = file_state_module.os.replace
    attempts = []
    sleeps = []

    def replace(source, target):
        attempts.append(Path(target).name)
        if Path(target).name == "file.txt" and attempts.count("file.txt") <= 2:
            raise sharing_error(code)
        original(source, target)

    monkeypatch.setattr(file_state_module.os, "replace", replace)
    monkeypatch.setattr(file_state_module.time, "sleep", sleeps.append)
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": "*** Update File: log.txt\n@@\n+done\n*** Update File: file.txt\n@@\n-old\n+new"},
        ["apply_patch"],
    )
    assert result["ok"] and result["data"]["status"] == "success"
    assert attempts == ["log.txt", "file.txt", "file.txt", "file.txt"]
    assert len(sleeps) == 2
    assert (tmp_path / "log.txt").read_bytes() == b"start\ndone\n"
    assert (tmp_path / "file.txt").read_bytes() == b"new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file.txt", "log.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_path", ["source", "move-source", "move-destination"])
async def test_retry_rechecks_source_and_destination_and_keeps_external_changes(
    tmp_path, monkeypatch, changed_path
):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    attempts = []

    def replace(source, target):
        attempts.append(target)
        raise sharing_error()

    def external_write(delay):
        changed = tmp_path / "moved.txt" if changed_path == "move-destination" else path
        changed.write_bytes(b"external change\n")

    monkeypatch.setattr(file_state_module.os, "replace", replace)
    monkeypatch.setattr(file_state_module.time, "sleep", external_write)
    patch = (
        "*** Move File: file.txt -> moved.txt"
        if changed_path.startswith("move-")
        else "*** Update File: file.txt\n@@\n-old\n+new"
    )
    result = await registry.dispatch(context(tmp_path), {"patch": patch}, ["apply_patch"])
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
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    original = file_state_module.os.replace
    attempts = 0

    def replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sharing_error()
        original(source, target)

    monkeypatch.setattr(file_state_module.os, "replace", replace)
    first = context(tmp_path)
    second = replace_context(context(tmp_path), session_id="another-session")
    results = await asyncio.gather(
        registry.dispatch(
            first,
            {"patch": "*** Update File: file.txt\n@@\n-first=old\n+first=new"},
            ["apply_patch"],
        ),
        registry.dispatch(
            second,
            {"patch": "*** Update File: file.txt\n@@\n-second=old\n+second=new"},
            ["apply_patch"],
        ),
    )
    assert all(result["ok"] for result in results)
    assert path.read_bytes() == b"first=new\nsecond=new\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [5, 28, None])
@pytest.mark.parametrize("other_file", [False, True])
async def test_replace_failure_is_bounded_and_keeps_other_operations(
    tmp_path, monkeypatch, code, other_file
):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    original = file_state_module.os.replace
    attempts = []
    sleeps = []

    def replace(source, target):
        if Path(target) == path:
            attempts.append(target)
            error = OSError("fixture unavailable")
            if code is not None:
                error.winerror = code
            raise error
        original(source, target)

    monkeypatch.setattr(file_state_module.os, "replace", replace)
    monkeypatch.setattr(file_state_module.time, "sleep", sleeps.append)
    patch = "*** Update File: file.txt\n@@\n-old\n+new"
    if other_file:
        patch += "\n*** Add File: other.txt\n+done"
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": patch},
        ["apply_patch"],
    )
    if other_file:
        assert result["data"]["status"] == "partial"
        error = result["data"]["results"][0]["error"]
        assert (tmp_path / "other.txt").read_bytes() == b"done\n"
    else:
        assert not result["ok"]
        error = result["error"]
    assert error["code"] == "file_write_error"
    assert 1 < len(attempts) <= 10 if code == 5 else len(attempts) == 1
    if code == 5:
        assert error["retryable"] and error["attempts_made"] == len(attempts)
    else:
        assert "retryable" not in error and "attempts_made" not in error
    assert len(sleeps) == len(attempts) - 1 and sum(sleeps) < 2
    assert path.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == (
        ["file.txt", "other.txt"] if other_file else ["file.txt"]
    )
