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
from tests.core.tools.apply_patch_helpers import context, text


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
async def test_redundant_inline_replacement_identifies_same_label_without_writing(tmp_path, ending):
    path = tmp_path / "file.txt"
    old = '        "Savings 2 % p.a.": pd.Series('
    new = '        "Savings 2 % p.a. (1 year = 365 days)": pd.Series('
    content = f"# Concurrent explanation\n{new}\n            values,\n        )\n".replace(
        "\n", ending
    ).encode()
    path.write_bytes(content)
    stamp = path.stat().st_mtime_ns
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(
        context(tmp_path), {"patch": f"*** Update File: file.txt\n@@\n-{old}\n+{new}"}
    )
    assert result["data"] == {
        "status": "unchanged",
        "content": "file.txt already contains this change. No file was changed.",
    }
    assert path.read_bytes() == content and path.stat().st_mtime_ns == stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["", "\n", "\nother"])
async def test_inline_poststate_preserves_no_newline_constraint(tmp_path, ending):
    path = tmp_path / "file.txt"
    path.write_bytes(('"Series new": pd.Series(' + ending).encode())
    before = path.read_bytes()
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(
        context(tmp_path),
        {
            "patch": '*** Update File: file.txt\n@@\n-"Series old": pd.Series(\n'
            '+"Series new": pd.Series(\n\\ No newline at end of file'
        },
    )
    assert result["ok"] is (ending == "")
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old,new,current",
    [
        ("old_value", "new_value", "new_value\n"),
        ("return 1", "return 2", "return 2\n"),
        ('label("old");', 'label("new");', 'label("new");\n'),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\n"Series other": pd.Series(\n',
        ),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\n"Series new": pd.Series(\n',
        ),
        (
            '"Series old": pd.Series(',
            '"Series new": pd.Series(',
            '"Series new": pd.Series(\ntrailing\n',
        ),
    ],
)
async def test_poststate_without_unique_target_or_requested_eof_is_not_success(
    tmp_path, old, new, current
):
    path = tmp_path / "file.txt"
    path.write_bytes(current.encode())
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    end = "\n*** End of File" if current.endswith("trailing\n") else ""
    result = await registry.dispatch(
        context(tmp_path),
        {"patch": f"*** Update File: file.txt\n@@\n-{old}\n+{new}{end}"},
    )
    assert not result["ok"]
    assert path.read_bytes() == current.encode()


@pytest.mark.asyncio
async def test_existing_prestate_is_updated_even_if_another_line_is_already_poststate(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b'"Series old": pd.Series(\n"Series new": pd.Series(\n')
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    result = await registry.dispatch(
        context(tmp_path),
        {
            "patch": "*** Update File: file.txt\n@@\n"
            '-"Series old": pd.Series(\n+"Series new": pd.Series('
        },
    )
    assert result["ok"] and result["data"]["status"] == "applied"
    assert path.read_bytes() == b'"Series new": pd.Series(\n"Series new": pd.Series(\n'


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
    assert result["ok"] and result["data"]["status"] == "applied"
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
    assert 1 < len(attempts) <= 10 if code == 5 else len(attempts) == 1
    busy = f"The file was busy; {len(attempts)} attempts were made."
    if other_file:
        assert result["data"]["status"] == "partial"
        assert "Created other.txt (1 line).\nFailed: Could not change file.txt" in text(result)
        assert (busy in text(result)) is (code == 5)
        assert (tmp_path / "other.txt").read_bytes() == b"done\n"
    else:
        error = result["error"]
        assert error["code"] == "file_write_error"
        if code == 5:
            assert error["retryable"] and error["attempts_made"] == len(attempts)
            assert busy in error["message"]
        else:
            assert "retryable" not in error and "attempts_made" not in error
    assert len(sleeps) == len(attempts) - 1 and sum(sleeps) < 2
    assert path.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == (
        ["file.txt", "other.txt"] if other_file else ["file.txt"]
    )
