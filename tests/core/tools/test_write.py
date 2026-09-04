"""Tests for the built-in write tool."""

import asyncio
import os
import threading
from pathlib import Path

import pytest

from core.tools.change_tracker import ChangeTracker
from core.tools.file_state import FileReadState
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.write import (
    WRITE_TOOL_DESCRIPTION,
    WRITE_TOOL_NAME,
    WRITE_TOOL_PARAMETERS,
    register_write_tool,
    write_handler,
)
from core.utils.paths import model_path


def make_context(
    workspace: Path,
    tool_name: str = WRITE_TOOL_NAME,
    *,
    cwd: Path | None = None,
    session_id: str = "session-1",
    change_tracker: ChangeTracker | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id=session_id,
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
        change_tracker=change_tracker,
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"path", "bytes"}
    assert isinstance(data["path"], str)
    assert isinstance(data["bytes"], int)
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


def test_register_write_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_write_tool(registry, file_state=FileReadState())

    tool = registry.get("write")
    assert tool.name == WRITE_TOOL_NAME == "write"
    assert tool.description == WRITE_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == WRITE_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["write"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "write"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path", "content"]
    assert "additionalProperties" not in parameters
    assert set(parameters["properties"]) == {"path", "content"}
    assert parameters["properties"]["path"]["type"] == "string"
    assert parameters["properties"]["content"]["type"] == "string"


@pytest.mark.asyncio
async def test_dispatch_write_offloads_sync_file_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ToolRegistry()
    register_write_tool(registry, file_state=FileReadState())

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handler_started = asyncio.Event()
    release_handler = threading.Event()
    original_replace = os.replace
    loop = asyncio.get_running_loop()

    def blocking_replace(source: Path, target: Path) -> None:
        loop.call_soon_threadsafe(handler_started.set)
        release_handler.wait(timeout=1)
        original_replace(source, target)

    monkeypatch.setattr("core.tools.file_state.os.replace", blocking_replace)

    dispatch_task = asyncio.create_task(
        registry.dispatch(
            make_context(workspace),
            {"path": "notes.txt", "content": "hello"},
            ["write"],
        )
    )

    await handler_started.wait()
    assert dispatch_task.done() is False

    ticked: list[str] = []

    async def tick() -> None:
        ticked.append("tick")

    await asyncio.create_task(tick())
    assert ticked == ["tick"]

    release_handler.set()
    result = await dispatch_task
    assert result["ok"] is True


def test_write_writes_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = make_context(workspace)

    result = write_handler(
        context,
        {"path": "notes.txt", "content": "hello\nworkspace\n"},
    )

    data = assert_success_envelope(result)
    target = workspace / "notes.txt"
    assert target.read_bytes() == b"hello\nworkspace\n"
    assert data["path"] == model_path(target.resolve())
    assert data["bytes"] == len(b"hello\nworkspace\n")
    assert context.presentation_facts == [
        {"kind": "line_change", "change": "added", "value": 2},
        {"kind": "line_change", "change": "removed", "value": 0},
    ]


def test_write_resolves_relative_path_against_cwd_not_workspace(tmp_path: Path) -> None:
    # A project session sets cwd to the repo; a relative path must land in the
    # repo (cwd), never in the agent workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    result = write_handler(
        make_context(workspace, cwd=repo),
        {"path": "notes.txt", "content": "in repo\n"},
    )

    data = assert_success_envelope(result)
    target = repo / "notes.txt"
    assert target.read_bytes() == b"in repo\n"
    assert data["path"] == model_path(target.resolve())
    assert not (workspace / "notes.txt").exists()


def test_write_writes_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"

    result = write_handler(
        make_context(workspace),
        {"path": str(target), "content": "absolute\npath\n"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"absolute\npath\n"
    assert data["path"] == model_path(target.resolve())


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "nested" / "deeper" / "notes.txt"

    result = write_handler(
        make_context(workspace),
        {"path": "nested/deeper/notes.txt", "content": "created parents"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"created parents"


def test_write_replaces_full_file_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"old content\r\nthat should disappear\r\n")

    context = make_context(workspace)
    result = write_handler(
        context,
        {"path": "notes.txt", "content": "new\nbody\nthree"},
    )

    assert_success_envelope(result)
    # The existing file's CRLF endings are preserved — the model's LF output
    # is normalized to match the file's style.
    assert target.read_bytes() == b"new\r\nbody\r\nthree"
    assert context.presentation_facts == [
        {"kind": "line_change", "change": "added", "value": 3},
        {"kind": "line_change", "change": "removed", "value": 2},
    ]


def test_write_preserves_exact_supplied_content_at_byte_level(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "lf\ncrlf\r\ncr\rend\nemoji: 🚀\n"

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": content},
    )

    data = assert_success_envelope(result)
    assert (workspace / "notes.txt").read_bytes() == content.encode("utf-8")
    assert data["bytes"] == len(content.encode("utf-8"))


def test_write_rejects_pasted_line_number_gutter(tmp_path: Path) -> None:
    # A model that pastes read's ``N| `` gutter back must be stopped before it
    # corrupts the file with line-number prefixes.
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "config.py", "content": "1| import os\n2| import sys\n3| \n"},
    )

    error = assert_failure_envelope(result, "line_numbered_content")
    assert "line-number" in error["message"]
    assert not (workspace / "config.py").exists()


def test_write_allows_non_consecutive_pipe_lines(tmp_path: Path) -> None:
    # Numbered-looking lines that do not run consecutively are real content,
    # not the gutter — the guard must let them through.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "1|alpha\n5|beta\n"

    result = write_handler(make_context(workspace), {"path": "data.txt", "content": content})

    assert_success_envelope(result)
    assert (workspace / "data.txt").read_text(encoding="utf-8") == content


def test_write_warns_on_broken_syntax_without_blocking(tmp_path: Path) -> None:
    # The file is still written (warn, don't block); the result carries a
    # non-fatal syntax warning so the model can fix it next turn.
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "config.json", "content": '{"a": 1,}'},
    )

    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    target = workspace / "config.json"
    assert target.read_text(encoding="utf-8") == '{"a": 1,}'
    data = result["data"]
    assert isinstance(data, dict)
    assert "JSONDecodeError" in data["syntax_warning"]


def test_write_no_syntax_warning_for_valid_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "config.json", "content": '{"a": 1}'},
    )

    data = assert_success_envelope(result)
    assert "syntax_warning" not in data


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "notes.txt"},
        {"content": "hello"},
        {"path": "", "content": "hello"},
        {"path": 1, "content": "hello"},
        {"path": "notes.txt", "content": 1},
        {"path": "notes.txt", "content": None},
    ],
)
def test_write_returns_failure_envelope_for_invalid_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(make_context(workspace), arguments)

    assert_failure_envelope(result, "invalid_arguments")


def test_write_returns_failure_envelope_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "hello", "filePath": "legacy.txt"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "filePath" in error["message"]


def test_write_preserves_existing_bom(tmp_path: Path) -> None:
    # A full-file rewrite of content the model read BOM-free must keep the BOM the
    # file already had.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "config.txt"
    target.write_bytes(b"\xef\xbb\xbfold\n")

    result = write_handler(make_context(workspace), {"path": "config.txt", "content": "new\n"})

    assert_success_envelope(result)
    assert target.read_bytes() == b"\xef\xbb\xbfnew\n"


def test_write_does_not_add_bom_to_new_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(make_context(workspace), {"path": "fresh.txt", "content": "x\n"})

    assert_success_envelope(result)
    assert (workspace / "fresh.txt").read_bytes() == b"x\n"


def test_write_does_not_double_existing_bom(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "c.txt"
    target.write_bytes(b"\xef\xbb\xbfold\n")

    # Content already starts with a BOM; do not prepend a second one.
    result = write_handler(
        make_context(workspace), {"path": "c.txt", "content": chr(0xFEFF) + "new\n"}
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"\xef\xbb\xbfnew\n"


def test_write_returns_failure_envelope_for_filesystem_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"

    def raise_permission_error(_source: Path, _target: Path) -> None:
        raise PermissionError("access denied while writing")

    monkeypatch.setattr("core.tools.file_state.os.replace", raise_permission_error)

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "hello"},
    )

    error = assert_failure_envelope(result, "file_write_error")
    assert model_path(target.resolve()) in error["message"]
    assert "access denied while writing" in error["message"]


def test_write_success_and_failure_results_are_valid_envelopes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    success = write_handler(make_context(workspace), {"path": "notes.txt", "content": "hello"})
    failure = write_handler(make_context(workspace), {"path": "notes.txt", "content": 1})

    assert is_tool_result_envelope(success) is True
    assert is_tool_result_envelope(failure) is True


def test_write_guard_blocks_overwrite_of_unread_existing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original\n")
    file_state = FileReadState()

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "replacement\n"},
        file_state=file_state,
    )

    assert_failure_envelope(result, "file_not_read")
    assert target.read_bytes() == b"original\n"


def test_write_guard_allows_new_file_without_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_state = FileReadState()

    result = write_handler(
        make_context(workspace),
        {"path": "fresh.txt", "content": "brand new\n"},
        file_state=file_state,
    )

    assert_success_envelope(result)
    assert (workspace / "fresh.txt").read_bytes() == b"brand new\n"


def test_write_guard_allows_overwrite_after_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original\n")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "replacement\n"},
        file_state=file_state,
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"replacement\n"


def test_write_guard_blocks_when_file_changed_since_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original\n")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    # An external change after the read (different byte length → size drift).
    target.write_bytes(b"changed out of band\n")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "replacement\n"},
        file_state=file_state,
    )

    assert_failure_envelope(result, "file_modified_since_read")
    assert target.read_bytes() == b"changed out of band\n"


def test_write_guard_restamps_so_next_write_needs_no_reread(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original\n")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    first = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "first\n"},
        file_state=file_state,
    )
    second = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "second\n"},
        file_state=file_state,
    )

    assert_success_envelope(first)
    assert_success_envelope(second)
    assert target.read_bytes() == b"second\n"


def test_concurrent_session_writes_serialize_and_reject_stale_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"original\n")
    file_state = FileReadState()
    file_state.record_read("session-a", target.resolve())
    file_state.record_read("session-b", target.resolve())

    from core.tools.file_state import atomic_write_bytes as real_atomic_write_bytes

    first_write_started = threading.Event()
    release_first_write = threading.Event()

    def blocking_atomic_write(path: Path, payload: bytes) -> None:
        first_write_started.set()
        release_first_write.wait(timeout=1)
        real_atomic_write_bytes(path, payload)

    monkeypatch.setattr("core.tools.write.atomic_write_bytes", blocking_atomic_write)
    results: dict[str, dict[str, object]] = {}

    def write_first() -> None:
        results["a"] = write_handler(
            make_context(workspace, session_id="session-a"),
            {"path": "notes.txt", "content": "first\n"},
            file_state=file_state,
        )

    def write_second() -> None:
        results["b"] = write_handler(
            make_context(workspace, session_id="session-b"),
            {"path": "notes.txt", "content": "second\n"},
            file_state=file_state,
        )

    first = threading.Thread(target=write_first)
    second = threading.Thread(target=write_second)
    first.start()
    assert first_write_started.wait(timeout=1)
    second.start()
    release_first_write.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert_success_envelope(results["a"])
    assert_failure_envelope(results["b"], "file_modified_since_read")
    assert target.read_bytes() == b"first\n"


def test_write_preserves_existing_crlf_endings(tmp_path: Path) -> None:
    # The model naturally produces LF, but a CRLF file must stay CRLF after a
    # full-file rewrite — no silent line-ending switch that would cause a
    # full-file git diff.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"line one\r\nline two\r\n")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "new one\nnew two\nnew three\n"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"new one\r\nnew two\r\nnew three\r\n"


def test_write_preserves_existing_lf_endings(tmp_path: Path) -> None:
    # An LF file stays LF even when the model sends CRLF content.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"line one\nline two\n")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "new one\r\nnew two\r\n"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"new one\nnew two\n"


def test_write_new_file_keeps_agent_line_endings_verbatim(tmp_path: Path) -> None:
    # A new file has no existing style to preserve — the agent's content is
    # written exactly as supplied.
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "fresh.txt", "content": "line one\r\nline two\r\n"},
    )

    assert_success_envelope(result)
    assert (workspace / "fresh.txt").read_bytes() == b"line one\r\nline two\r\n"


def test_write_preserves_existing_cr_endings(tmp_path: Path) -> None:
    # Old Mac CR-only endings are detected and preserved.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"line one\rline two\r")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "new one\nnew two\n"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"new one\rnew two\r"


def test_write_preserves_existing_exotic_line_endings(tmp_path: Path) -> None:
    # The read tool renders U+2028 as a line break; a full-file rewrite must
    # keep that style instead of switching the file to LF.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes("line one\u2028line two\u2028".encode("utf-8"))

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "new one\nnew two\n"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == "new one\u2028new two\u2028".encode("utf-8")


def test_write_no_normalization_for_single_line_file(tmp_path: Path) -> None:
    # A file with no line endings (single line) has nothing to detect — the
    # agent's content is written verbatim.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"single line no newline")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "replacement"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"replacement"


def test_write_normalizes_mixed_endings_to_dominant_style(tmp_path: Path) -> None:
    # A file with mixed endings (mostly CRLF) is detected as CRLF; the agent's
    # mixed content is normalized to CRLF.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"crlf line\r\nlf line\ncrlf line\r\n")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "a\nb\r\nc\n"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"a\r\nb\r\nc\r\n"


def test_write_crlf_preservation_works_with_bom(tmp_path: Path) -> None:
    # BOM preservation and line-ending normalization are independent: a BOM
    # CRLF file keeps both its BOM and its CRLF endings.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "config.txt"
    target.write_bytes(b"\xef\xbb\xbfold\r\nbody\r\n")

    result = write_handler(
        make_context(workspace),
        {"path": "config.txt", "content": "new\nbody\n"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"\xef\xbb\xbfnew\r\nbody\r\n"


def test_write_new_file_counts_whole_content_as_added(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = ChangeTracker()

    result = write_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "fresh.txt", "content": "one\ntwo\n"},
    )

    assert_success_envelope(result)
    assert tracker.take_run_stats("session-1") == {
        "files": 1,
        "added": 2,
        "removed": 0,
        "paths": [str((workspace / "fresh.txt").resolve())],
    }


def test_write_overwrite_tracks_existing_file_without_full_read(tmp_path: Path) -> None:
    # The old semantics skipped tracking unless the session had fully read the
    # file first; the write now diffs against actual on-disk content, so a
    # partial read (or none at all in this guard-free call) still counts.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("keep\nold\nkeep\n", encoding="utf-8")
    tracker = ChangeTracker()

    result = write_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "notes.txt", "content": "keep\nnew\nkeep\n"},
    )

    assert_success_envelope(result)
    assert tracker.take_run_stats("session-1") == {
        "files": 1,
        "added": 1,
        "removed": 1,
        "paths": [str(target.resolve())],
    }


def test_write_bom_file_diffs_against_bom_free_content(tmp_path: Path) -> None:
    # A leading BOM must not count as a changed line: the pre-state is
    # compared BOM-free, exactly like the content the write produces.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "config.txt"
    target.write_bytes(b"\xef\xbb\xbfold\nbody\n")
    tracker = ChangeTracker()

    result = write_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "config.txt", "content": "new\nbody\n"},
    )

    assert_success_envelope(result)
    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["added"] == 1
    assert stats["removed"] == 1


def test_write_repeated_writes_in_one_run_count_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("a\n", encoding="utf-8")
    tracker = ChangeTracker()

    first = write_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "notes.txt", "content": "a\nb\n"},
    )
    second = write_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "notes.txt", "content": "a\nb\nc\n"},
    )

    assert_success_envelope(first)
    assert_success_envelope(second)
    # Both writes diff against the run's first pre-write state: net +2 lines.
    assert tracker.take_run_stats("session-1") == {
        "files": 1,
        "added": 2,
        "removed": 0,
        "paths": [str(target.resolve())],
    }
