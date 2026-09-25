"""Full replacement through Add File, including the retired write Tool's guards."""

from __future__ import annotations

import stat

import pytest

from core.tools.file_state import FileReadState
from core.tools.read import make_read_handler
from tests.core.tools.apply_patch_helpers import apply, context, text


async def read_file(root, state, path="file.txt"):
    result = await make_read_handler(None, None, state, speech_max_size_bytes=1024)(
        context(root), {"path": path}
    )
    assert result["ok"]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("final", [True, False])
@pytest.mark.asyncio
async def test_add_replaces_read_file_preserving_format_and_permissions(
    tmp_path, ending, bom, final
):
    path = tmp_path / "file.txt"
    path.write_bytes(bom + b"old" + ending + b"discard" + ending)
    mode = stat.S_IMODE(path.stat().st_mode)
    state = FileReadState()
    await read_file(tmp_path, state)
    patch = "*** Add File: file.txt\n+new\n+content"
    if not final:
        patch += "\n\\ No newline at end of file"
    result = apply(tmp_path, patch, state=state)
    assert result["ok"]
    assert path.read_bytes() == bom + b"new" + ending + b"content" + (ending if final else b"")
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert state.check_stale("session-test", path) is None
    assert text(result) == "Replaced the content of file.txt (2 lines)."
    repeated = apply(tmp_path, patch, state=state)
    assert repeated["data"]["status"] == "unchanged"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("body,expected", [("", b""), ("\n+", b"\n"), ("\n+false", b"false\n")])
@pytest.mark.asyncio
async def test_empty_and_literal_replacements(tmp_path, existing, body, expected):
    path = tmp_path / "file.txt"
    state = FileReadState()
    if existing:
        path.write_bytes(b"old content\n")
        await read_file(tmp_path, state)
    assert apply(tmp_path, "*** Add File: file.txt" + body, state=state)["ok"]
    assert path.read_bytes() == expected


@pytest.mark.parametrize("read_session", [None, "another-session", "session-test"])
@pytest.mark.asyncio
async def test_overwrite_requires_current_session_read_and_unchanged_file(tmp_path, read_session):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    state = FileReadState()
    if read_session:
        state.record_read(read_session, path)
    expected = "file_not_read"
    if read_session == "session-test":
        path.write_bytes(b"externally changed\n")
        expected = "file_modified_since_read"
    before = path.read_bytes()
    result = apply(tmp_path, "*** Add File: file.txt\n+new", state=state)
    assert result["error"]["code"] == expected
    # The error shows the whole small file, which counts as the read the guard asks for.
    assert "Its current content follows and now counts as read" in text(result)
    assert "1| " + before.decode().rstrip("\n") in text(result)
    assert path.read_bytes() == before
    assert apply(tmp_path, "*** Add File: file.txt\n+new", state=state)["ok"]
    assert path.read_bytes() == b"new\n"


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


def test_empty_existing_file_needs_no_read_to_replace(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"\n")
    assert apply(tmp_path, "*** Add File: file.txt\n+new")["ok"]
    assert path.read_bytes() == b"new\n"


def test_identical_add_is_noop_without_read_or_metadata_change(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"same\n")
    before = path.stat().st_mtime_ns
    result = apply(tmp_path, "*** Add File: file.txt\n+same")
    assert result["data"]["status"] == "unchanged"
    assert path.stat().st_mtime_ns == before


def test_guard_switch_still_allows_full_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tools.file_state.FILE_STATE_GUARD_ENABLED", False)
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    assert apply(tmp_path, "*** Add File: file.txt\n+new")["ok"]
    assert path.read_bytes() == b"new\n"


@pytest.mark.asyncio
async def test_read_alias_and_ordered_full_replacements_share_state(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    state = FileReadState()
    await read_file(tmp_path, state, str(path))
    result = apply(
        tmp_path,
        "*** Add File: ./file.txt\n+first\n*** Add File: file.txt\n+second\n"
        "*** Update File: file.txt\n@@\n-second\n+final",
        state=state,
    )
    assert result["data"]["status"] == "applied"
    assert path.read_bytes() == b"final\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [b"def old(\n", b"\xff\x00"])
async def test_replacement_reports_syntax_error_even_if_original_was_invalid(tmp_path, original):
    path = tmp_path / "file.py"
    path.write_bytes(original)
    state = FileReadState()
    await read_file(tmp_path, state, "file.py")
    result = apply(tmp_path, "*** Add File: file.py\n+def new(", state=state)
    assert result["ok"]
    assert "Warning: Syntax check failed after this write" in text(result)


@pytest.mark.asyncio
async def test_replacement_rechecks_bytes_after_guard(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\n")
    state = FileReadState()
    await read_file(tmp_path, state)
    original = state.check_stale

    def drift(session_id, resolved):
        result = original(session_id, resolved)
        path.write_bytes(b"external change\n")
        return result

    monkeypatch.setattr(state, "check_stale", drift)
    result = apply(tmp_path, "*** Add File: file.txt\n+new", state=state)
    assert result["error"]["code"] == "file_changed"
    assert path.read_bytes() == b"external change\n"
