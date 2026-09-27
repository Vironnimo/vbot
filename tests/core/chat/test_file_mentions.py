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
from core.chat.errors import ChatError
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


def test_lists_relative_forward_slash_paths_and_honors_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "secret.txt").write_text("x", encoding="utf-8")
    (tmp_path / "debug.log").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print()", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    files, truncated = list_mention_files(tmp_path)

    assert truncated is False
    assert set(files) == {".gitignore", "README.md", "src/app.py"}
    assert list_mention_files(tmp_path / "does-not-exist") == ([], False)


def test_listing_marks_truncation_at_the_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real cap must hold real repositories; the test lowers it to observe truncation.
    assert MENTION_FILE_LIST_LIMIT >= 1000
    monkeypatch.setattr("core.chat.file_mentions.MENTION_FILE_LIST_LIMIT", 3)
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text("x", encoding="utf-8")

    files, truncated = list_mention_files(tmp_path)

    assert len(files) == 3
    assert truncated is True


# ---------------------------------------------------------------------------
# expand_file_mentions
# ---------------------------------------------------------------------------


def _expand(content: Any, mentions: list[str], root: Path, state: FileReadState) -> Any:
    return expand_file_mentions(content, mentions, root=root, session_id="s1", file_state=state)


def test_no_mentions_returns_content_unchanged(tmp_path: Path) -> None:
    assert _expand("hello", [], tmp_path, FileReadState()) == "hello"


def test_inlines_text_file_and_stamps_a_session_scoped_read(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("line one\nline two\n", encoding="utf-8", newline="\n")
    state = FileReadState()

    result = _expand("check @notes.md", ["notes.md"], tmp_path, state)

    assert isinstance(result, list)
    assert result[0] == TextBlock(type="text", text="check @notes.md")
    mention = result[1]
    assert isinstance(mention, FileMentionBlock)
    assert (mention.status, mention.path, mention.text) == (
        "inlined",
        "notes.md",
        "line one\nline two\n",
    )
    # The snapshot counts as a read: an edit without a prior read tool call
    # must pass the read-before-write guard, but only in this Session.
    assert state.check_stale("s1", target.resolve()) is None
    assert state.check_stale("other-session", target.resolve()) is not None


@pytest.mark.parametrize(
    ("name", "payload", "status", "size_bytes"),
    [
        ("gone.txt", None, "missing", None),
        ("big.txt", b"x" * 50, "too_large", 50),
        ("image.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "not_text", None),
    ],
    ids=["missing", "too-large", "not-text"],
)
def test_unreadable_mentions_degrade_without_a_read_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    payload: bytes | None,
    status: str,
    size_bytes: int | None,
) -> None:
    # Source files must comfortably inline; the cap exists for logs and dumps.
    assert MENTION_INLINE_MAX_BYTES >= 64 * 1024
    if status == "too_large":
        monkeypatch.setattr("core.chat.file_mentions.MENTION_INLINE_MAX_BYTES", 10)
    target = tmp_path / name
    if payload is not None:
        target.write_bytes(payload)
    state = FileReadState()

    result = _expand(f"@{name}", [name], tmp_path, state)

    mention = result[1]
    assert isinstance(mention, FileMentionBlock)
    assert (mention.status, mention.text) == (status, None)
    if size_bytes is not None:
        assert mention.size_bytes == size_bytes
    # Not stamped: the Agent has not seen this content.
    assert state.check_stale("s1", target.resolve()) is not None


def test_duplicate_and_blank_mentions_collapse(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")

    result = _expand("@a.txt twice @a.txt", ["a.txt", "a.txt", "  "], tmp_path, FileReadState())

    assert isinstance(result, list)
    assert len(result) == 2


def test_block_content_keeps_existing_blocks(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    existing: list[ContentBlock] = [TextBlock(type="text", text="see @a.txt")]

    result = _expand(existing, ["a.txt"], tmp_path, FileReadState())

    assert result[0] is existing[0]
    assert isinstance(result[1], FileMentionBlock)


# ---------------------------------------------------------------------------
# file_mention_request_text
# ---------------------------------------------------------------------------


def test_inlined_request_text_carries_origin_framing_and_content(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
    mention = _expand("@app.py", ["app.py"], tmp_path, FileReadState())[1]
    assert isinstance(mention, FileMentionBlock)

    text = file_mention_request_text(content_block_to_dict(mention))

    assert "@app.py" in text
    assert "attached automatically" in text
    assert "snapshot" in text
    assert text.endswith("value = 1\n")


@pytest.mark.parametrize(
    ("block", "expected_parts"),
    [
        (
            {"type": "file_mention", "path": "big.log", "status": "too_large", "size_bytes": 999},
            ("big.log", "999", "read"),
        ),
        ({"type": "file_mention", "path": "img.png", "status": "not_text"}, ("img.png", "read")),
        (
            {"type": "file_mention", "path": "gone.txt", "status": "missing"},
            ("gone.txt", "did not exist"),
        ),
    ],
    ids=["too-large", "not-text", "missing"],
)
def test_degraded_request_text_names_the_file_and_the_next_step(
    block: dict[str, Any], expected_parts: tuple[str, ...]
) -> None:
    text = file_mention_request_text(block)

    for part in expected_parts:
        assert part in text


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
        if project_id != "vbot":
            raise KeyError(project_id)
        return _FakeProject(self._cwd)


class _FakeAgent:
    def __init__(self, workspace: str, root_project_id: str | None = None) -> None:
        self.workspace = workspace
        self.root_project_id = root_project_id


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


def _runtime(tmp_path: Path, *, workspace: str, root_project_id: str | None) -> Any:
    (tmp_path / "repo").mkdir()
    return cast(
        Any,
        _FakeRuntime(
            projects=_FakeProjects(str(tmp_path / "repo")),
            agent=_FakeAgent(workspace, root_project_id),
            data_dir=tmp_path,
        ),
    )


@pytest.mark.parametrize(
    ("agent_id", "project_id", "workspace", "root_project_id", "expected"),
    [
        ("builder", "vbot", "workspace", None, "repo"),
        ("main", None, "workspace", None, "workspace"),
        ("main", None, "workspace", "vbot", "repo"),
        ("main", None, "", None, "agents/main/workspace"),
    ],
    ids=["project-cwd", "identity-workspace", "rooted-identity", "no-workspace-data-dir"],
)
def test_mention_root_follows_the_address(
    tmp_path: Path,
    agent_id: str,
    project_id: str | None,
    workspace: str,
    root_project_id: str | None,
    expected: str,
) -> None:
    runtime = _runtime(
        tmp_path,
        workspace=str(tmp_path / workspace) if workspace else "",
        root_project_id=root_project_id,
    )

    assert resolve_mention_root(runtime, agent_id, project_id) == tmp_path / expected


@pytest.mark.parametrize(
    ("root_project_id", "remove_repo", "error"),
    [("missing", False, KeyError), ("vbot", True, ChatError)],
    ids=["missing-project", "missing-cwd"],
)
def test_rooted_identity_never_falls_back_to_the_workspace(
    tmp_path: Path, root_project_id: str, remove_repo: bool, error: type[Exception]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace=str(workspace), root_project_id=root_project_id)
    if remove_repo:
        (tmp_path / "repo").rmdir()

    with pytest.raises(error):
        resolve_mention_root(runtime, "main", None)
