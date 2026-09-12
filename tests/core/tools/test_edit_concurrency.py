"""Edit: concurrency behavior."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.tools.change_tracker import ChangeTracker
from core.tools.edit import (
    edit_handler,
)
from core.tools.file_state import FileReadState
from tests.core.tools.edit_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)


def test_edit_allows_unread_existing_file_when_match_is_unique(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "ALPHA beta\n"


def test_edit_guard_allows_after_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "ALPHA beta\n"


def test_edit_uses_current_content_when_file_changed_since_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    # External change after the read (longer content → size drift).
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert "changed since this Session last read it" in data["stale_warning"]
    assert target.read_text(encoding="utf-8") == "ALPHA beta gamma\n"


def test_edit_changed_file_still_fails_when_current_text_no_longer_matches(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())
    target.write_text("gamma beta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert_failure_envelope(result, "text_not_found")
    assert target.read_text(encoding="utf-8") == "gamma beta\n"


def test_edit_guard_restamps_so_next_edit_needs_no_reread(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    first = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )
    second = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
        file_state=file_state,
    )

    assert_success_envelope(first)
    assert_success_envelope(second)
    assert target.read_text(encoding="utf-8") == "ALPHA BETA\n"


def test_concurrent_session_edits_serialize_and_merge_current_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-a", target.resolve())
    file_state.record_read("session-b", target.resolve())

    from core.tools.file_state import atomic_write_bytes as real_atomic_write_bytes

    first_write_started = threading.Event()
    release_first_write = threading.Event()
    second_write_started = threading.Event()
    call_count = 0
    call_count_lock = threading.Lock()

    def observed_atomic_write(path: Path, payload: bytes) -> None:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_write_started.set()
            release_first_write.wait(timeout=1)
        else:
            second_write_started.set()
        real_atomic_write_bytes(path, payload)

    monkeypatch.setattr("core.tools.edit.atomic_write_bytes", observed_atomic_write)
    results: dict[str, dict[str, object]] = {}

    def edit_alpha() -> None:
        results["a"] = edit_handler(
            make_context(workspace, session_id="session-a"),
            {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
            file_state=file_state,
        )

    def edit_beta() -> None:
        results["b"] = edit_handler(
            make_context(workspace, session_id="session-b"),
            {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
            file_state=file_state,
        )

    first = threading.Thread(target=edit_alpha)
    second = threading.Thread(target=edit_beta)
    first.start()
    assert first_write_started.wait(timeout=1)
    second.start()
    assert second_write_started.wait(timeout=0.05) is False
    release_first_write.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert results["a"]["ok"] is True
    assert results["b"]["ok"] is True
    second_data = results["b"]["data"]
    assert isinstance(second_data, dict)
    assert "stale_warning" in second_data
    assert target.read_text(encoding="utf-8") == "ALPHA BETA\n"


def test_edit_change_stats_count_single_line_once(tmp_path: Path) -> None:
    # Regression: an edit must diff against the file's real content, never the
    # numbered `N| ` read rendering. A single-line edit of a multi-line file
    # must count exactly one added and one removed line, not every line.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tracker = ChangeTracker()

    result = edit_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
    )

    assert_success_envelope(result)
    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 1
    assert stats["removed"] == 1
    assert stats["paths"] == [str(target.resolve())]


def test_edit_stats_ignore_external_changes_since_last_read(tmp_path: Path) -> None:
    # The run delta must be computed against the actual on-disk content at
    # mutation time: a formatter/shell rewrite between the session's read and
    # this edit belongs to nobody's run stats except its own moment of change,
    # so only this edit's real lines may be reported.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tracker = ChangeTracker()

    # External rewrite after the session last saw the file (e.g. formatter).
    target.write_text("ALPHA-FORMATTED\nbeta\ngamma\ndelta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
    )

    assert_success_envelope(result)
    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 1
    assert stats["removed"] == 1


def test_concurrent_edits_to_one_file_compose(tmp_path: Path) -> None:
    """Sibling parallel edits to the same path must all land, never clobber.

    Regression guard for a lost-update incident where three same-file edits
    in one parallel tool-call batch each reported success but only one
    survived on disk (whole-file last-writer-wins).
    """

    from concurrent.futures import ThreadPoolExecutor

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "imports.py"
    target.write_text(
        "from a import alpha\nfrom b import beta\n",
        encoding="utf-8",
    )

    file_state = FileReadState()
    file_state.record_read("session-1", target)
    context = make_context(workspace)
    edits = [
        ("from a import alpha", "from a import alpha\nfrom aa import gamma"),
        ("from b import beta", "from b import beta\nfrom bb import delta"),
        ("from b import beta", "from b import beta\nfrom bb import epsilon"),
    ]

    def run(old: str, new: str) -> dict[str, object]:
        return edit_handler(
            context,
            {"path": str(target), "old_string": old, "new_string": new},
            file_state=file_state,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda item: run(*item), edits))

    for result in results:
        assert_success_envelope(result)

    final = target.read_text(encoding="utf-8")
    assert "from a import alpha" in final
    assert "from aa import gamma" in final
    # Under the per-path lock both competing edits to line 2 compose: each
    # matches against the previous writer's on-disk content.
    assert "from bb import delta" in final
    assert "from bb import epsilon" in final
