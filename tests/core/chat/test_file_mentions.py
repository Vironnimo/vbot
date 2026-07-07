"""Tests for @-mention file listing, snapshot expansion, and provider rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.content_blocks import (
    ContentBlock,
    FileMentionBlock,
    TextBlock,
    content_block_to_dict,
)
from core.chat.file_mentions import (
    MENTION_FILE_LIST_LIMIT,
    MENTION_INLINE_MAX_BYTES,
    expand_file_mentions,
    file_mention_request_text,
    list_mention_files,
    resolve_mention_root,
)
from core.tools.file_state import FileReadState

# ---------------------------------------------------------------------------
# list_mention_files
# ---------------------------------------------------------------------------


class TestListMentionFiles:
    def test_lists_files_relative_with_forward_slashes(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print()", encoding="utf-8")
        (tmp_path / "README.md").write_text("hi", encoding="utf-8")

        files, truncated = list_mention_files(tmp_path)

        assert truncated is False
        assert set(files) == {"README.md", "src/app.py"}

    def test_honors_gitignore(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
        (tmp_path / "ignored").mkdir()
        (tmp_path / "ignored" / "secret.txt").write_text("x", encoding="utf-8")
        (tmp_path / "debug.log").write_text("x", encoding="utf-8")
        (tmp_path / "kept.txt").write_text("x", encoding="utf-8")

        files, _ = list_mention_files(tmp_path)

        assert "kept.txt" in files
        assert ".gitignore" in files
        assert "debug.log" not in files
        assert not any(entry.startswith("ignored/") for entry in files)
        assert not any(entry.startswith(".git/") for entry in files)

    def test_missing_root_lists_empty(self, tmp_path: Path) -> None:
        files, truncated = list_mention_files(tmp_path / "does-not-exist")

        assert files == []
        assert truncated is False

    def test_marks_truncation_at_file_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("core.chat.file_mentions.MENTION_FILE_LIST_LIMIT", 3)
        for index in range(5):
            (tmp_path / f"file-{index}.txt").write_text("x", encoding="utf-8")

        files, truncated = list_mention_files(tmp_path)

        assert len(files) == 3
        assert truncated is True

    def test_default_cap_is_generous(self) -> None:
        # Guard against an accidental tiny cap: the picker must hold real repos.
        assert MENTION_FILE_LIST_LIMIT >= 1000


# ---------------------------------------------------------------------------
# expand_file_mentions
# ---------------------------------------------------------------------------


class TestExpandFileMentions:
    def test_no_mentions_returns_content_unchanged(self, tmp_path: Path) -> None:
        state = FileReadState()

        result = expand_file_mentions("hello", [], root=tmp_path, session_id="s1", file_state=state)

        assert result == "hello"

    def test_inlines_text_file_and_stamps_read(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.md"
        target.write_text("line one\nline two\n", encoding="utf-8", newline="\n")
        state = FileReadState()

        result = expand_file_mentions(
            "check @notes.md", ["notes.md"], root=tmp_path, session_id="s1", file_state=state
        )

        assert isinstance(result, list)
        assert result[0] == TextBlock(type="text", text="check @notes.md")
        mention = result[1]
        assert isinstance(mention, FileMentionBlock)
        assert mention.status == "inlined"
        assert mention.text == "line one\nline two\n"
        assert mention.path == "notes.md"
        # The snapshot counts as a read: an edit without a prior read tool call
        # must pass the read-before-write guard.
        assert state.check_stale("s1", target.resolve()) is None

    def test_stamp_is_session_scoped(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.md"
        target.write_text("content", encoding="utf-8")
        state = FileReadState()

        expand_file_mentions(
            "@notes.md", ["notes.md"], root=tmp_path, session_id="s1", file_state=state
        )

        assert state.check_stale("other-session", target.resolve()) is not None

    def test_missing_file_degrades_without_stamp(self, tmp_path: Path) -> None:
        state = FileReadState()

        result = expand_file_mentions(
            "@gone.txt", ["gone.txt"], root=tmp_path, session_id="s1", file_state=state
        )

        mention = result[1]
        assert isinstance(mention, FileMentionBlock)
        assert mention.status == "missing"
        assert mention.text is None

    def test_oversized_file_degrades_with_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("core.chat.file_mentions.MENTION_INLINE_MAX_BYTES", 10)
        target = tmp_path / "big.txt"
        target.write_text("x" * 50, encoding="utf-8")
        state = FileReadState()

        result = expand_file_mentions(
            "@big.txt", ["big.txt"], root=tmp_path, session_id="s1", file_state=state
        )

        mention = result[1]
        assert isinstance(mention, FileMentionBlock)
        assert mention.status == "too_large"
        assert mention.text is None
        assert mention.size_bytes == 50
        # Not stamped: the agent has not seen this content.
        assert state.check_stale("s1", target.resolve()) is not None

    def test_binary_file_degrades_as_not_text(self, tmp_path: Path) -> None:
        target = tmp_path / "image.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        state = FileReadState()

        result = expand_file_mentions(
            "@image.png", ["image.png"], root=tmp_path, session_id="s1", file_state=state
        )

        mention = result[1]
        assert isinstance(mention, FileMentionBlock)
        assert mention.status == "not_text"
        assert mention.text is None

    def test_duplicate_and_blank_mentions_collapse(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        state = FileReadState()

        result = expand_file_mentions(
            "@a.txt twice @a.txt",
            ["a.txt", "a.txt", "  "],
            root=tmp_path,
            session_id="s1",
            file_state=state,
        )

        assert isinstance(result, list)
        assert len(result) == 2

    def test_block_content_keeps_existing_blocks(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        state = FileReadState()
        existing: list[ContentBlock] = [TextBlock(type="text", text="see @a.txt")]

        result = expand_file_mentions(
            existing, ["a.txt"], root=tmp_path, session_id="s1", file_state=state
        )

        assert result[0] is existing[0]
        assert isinstance(result[1], FileMentionBlock)

    def test_default_inline_cap_is_generous(self) -> None:
        # Source files must comfortably inline; the cap exists for logs and dumps.
        assert MENTION_INLINE_MAX_BYTES >= 64 * 1024


# ---------------------------------------------------------------------------
# file_mention_request_text
# ---------------------------------------------------------------------------


class TestFileMentionRequestText:
    def test_inlined_carries_origin_framing_and_content(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
        state = FileReadState()
        blocks = expand_file_mentions(
            "@app.py", ["app.py"], root=tmp_path, session_id="s1", file_state=state
        )
        mention = blocks[1]
        assert isinstance(mention, FileMentionBlock)

        text = file_mention_request_text(content_block_to_dict(mention))

        assert "@app.py" in text
        assert "attached automatically" in text
        assert "snapshot" in text
        assert text.endswith("value = 1\n")

    def test_too_large_points_to_read_tool(self) -> None:
        text = file_mention_request_text(
            {"type": "file_mention", "path": "big.log", "status": "too_large", "size_bytes": 999}
        )

        assert "big.log" in text
        assert "999" in text
        assert "read" in text

    def test_not_text_points_to_read_tool(self) -> None:
        text = file_mention_request_text(
            {"type": "file_mention", "path": "img.png", "status": "not_text"}
        )

        assert "img.png" in text
        assert "read" in text

    def test_missing_states_absence_at_send_time(self) -> None:
        text = file_mention_request_text(
            {"type": "file_mention", "path": "gone.txt", "status": "missing"}
        )

        assert "gone.txt" in text
        assert "did not exist" in text


# ---------------------------------------------------------------------------
# resolve_mention_root
# ---------------------------------------------------------------------------


class _FakeProject:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd


class _FakeProjects:
    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    def get(self, project_id: str) -> _FakeProject:
        return _FakeProject(self._cwd)


class _FakeAgent:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace


class _FakeResolver:
    def __init__(self, agent: _FakeAgent) -> None:
        self._agent = agent

    def resolve_agent(self, project_id: str | None, agent_id: str) -> _FakeAgent:
        return self._agent


class _FakeStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _FakeRuntime:
    def __init__(self, *, projects: _FakeProjects, agent: _FakeAgent, data_dir: Path) -> None:
        self.projects = projects
        self.agent_resolver = _FakeResolver(agent)
        self.storage = _FakeStorage(data_dir)


class TestResolveMentionRoot:
    def test_project_address_uses_project_cwd(self, tmp_path: Path) -> None:
        runtime = _FakeRuntime(
            projects=_FakeProjects(str(tmp_path / "repo")),
            agent=_FakeAgent(str(tmp_path / "workspace")),
            data_dir=tmp_path,
        )

        root = resolve_mention_root(cast(Any, runtime), "builder", "vbot")

        assert root == Path(tmp_path / "repo")

    def test_identity_address_uses_agent_workspace(self, tmp_path: Path) -> None:
        runtime = _FakeRuntime(
            projects=_FakeProjects(str(tmp_path / "repo")),
            agent=_FakeAgent(str(tmp_path / "workspace")),
            data_dir=tmp_path,
        )

        root = resolve_mention_root(cast(Any, runtime), "main", None)

        assert root == Path(tmp_path / "workspace")

    def test_identity_without_workspace_falls_back_to_data_dir(self, tmp_path: Path) -> None:
        runtime = _FakeRuntime(
            projects=_FakeProjects(str(tmp_path / "repo")),
            agent=_FakeAgent(""),
            data_dir=tmp_path,
        )

        root = resolve_mention_root(cast(Any, runtime), "main", None)

        assert root == tmp_path / "agents" / "main" / "workspace"
