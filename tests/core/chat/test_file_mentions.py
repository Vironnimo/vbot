"""Tests for @-mention file listing, snapshot expansion, and provider rendering."""

from __future__ import annotations

import os
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
    MentionDirectory,
    MentionIndex,
    expand_file_mentions,
    file_mention_request_text,
    list_mention_directory,
    list_mention_files,
    resolve_mention_root,
)
from core.projects import AgentResolutionError, AgentResolver, WorkingProjectMissingError
from core.sessions import SessionAddress, SessionNotFoundError
from core.tools.file_state import FileReadState
from tests.directory_links import link_directory

# ---------------------------------------------------------------------------
# list_mention_files / list_mention_directory
# ---------------------------------------------------------------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_index_lists_what_the_search_tools_search(tmp_path: Path) -> None:
    files = {
        ".git/HEAD": "ref: refs/heads/main\n",
        ".git/info/exclude": "excluded.txt\n",
        ".gitignore": "ignored/\n*.log\n",
        ".env": "x",
        "README.md": "x",
        "debug.log": "x",
        "excluded.txt": "x",
        "ignored/secret.txt": "x",
        "src/.gitignore": "generated.py\n",
        "src/app.py": "x",
        "src/generated.py": "x",
        "src/deep/mod.py": "x",
    }
    expected = [
        ".env",
        ".gitignore",
        "README.md",
        "src/.gitignore",
        "src/app.py",
        "src/deep/mod.py",
    ]
    if os.name != "nt":
        # Outside Windows a name can hold a line break; the index keeps it whole.
        files["src/line\nbreak.py"] = "x"
        expected.append("src/line\nbreak.py")
    _write(tmp_path, files)
    (tmp_path / "empty").mkdir()

    index = await list_mention_files(tmp_path)

    assert index == MentionIndex(
        files=tuple(expected), directories=("src", "src/deep"), truncated=False
    )
    assert await list_mention_files(tmp_path / "does-not-exist") == MentionIndex((), (), False)


@pytest.mark.asyncio
async def test_index_never_enters_directory_links(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    _write(root, {"src/app.py": "print()"})
    _write(outside, {"secret.txt": "x"})
    link_directory(root / "linked", outside)

    index = await list_mention_files(root)

    assert (index.files, index.truncated) == (("src/app.py",), False)


@pytest.mark.asyncio
async def test_index_marks_truncation_at_the_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real cap must hold real repositories; the test lowers it to observe truncation.
    assert MENTION_FILE_LIST_LIMIT >= 1000
    monkeypatch.setattr("core.chat.file_mentions.MENTION_FILE_LIST_LIMIT", 3)
    _write(tmp_path, {f"file-{index}.txt": "x" for index in range(5)})

    index = await list_mention_files(tmp_path)

    assert len(index.files) == 3
    assert index.truncated is True


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        pytest.param(
            "",
            [
                (".git", "directory", True),
                ("build", "directory", True),
                ("src", "directory", False),
                (".gitignore", "file", False),
                ("debug.log", "file", True),
            ],
            id="root",
        ),
        pytest.param(
            "src",
            [("gen", "directory", True), (".gitignore", "file", False), ("app.py", "file", False)],
            id="nested-ignore-file",
        ),
        pytest.param("build/out", [("bundle.js", "file", True)], id="inside-an-ignored-directory"),
    ],
)
@pytest.mark.asyncio
async def test_directory_lists_ignored_entries_and_marks_them(
    tmp_path: Path, directory: str, expected: list[tuple[str, str, bool]]
) -> None:
    _write(
        tmp_path,
        {
            ".git/HEAD": "ref: refs/heads/main\n",
            ".gitignore": "build/\n*.log\n",
            "build/out/bundle.js": "x",
            "debug.log": "x",
            "src/.gitignore": "gen/\n",
            "src/app.py": "x",
            "src/gen/types.py": "x",
        },
    )

    listed = await list_mention_directory(tmp_path, directory)

    assert [(entry.name, entry.kind, entry.ignored) for entry in listed.entries] == expected
    assert listed.truncated is False


@pytest.mark.asyncio
async def test_directory_of_a_missing_root_lists_as_empty(tmp_path: Path) -> None:
    assert await list_mention_directory(tmp_path / "missing", "") == MentionDirectory((), False)


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
    """The single Project ``vbot``, whose repo is *cwd*."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    def exists(self, project_id: str) -> bool:
        return project_id == "vbot"

    def get(self, project_id: str) -> _FakeProject:
        if project_id != "vbot":
            raise KeyError(project_id)
        return _FakeProject(self._cwd)


class _FakeAgent:
    def __init__(self, agent_id: str, workspace: str, root_project_id: str | None) -> None:
        self.id = agent_id
        self.workspace = workspace
        self.root_project_id = root_project_id


class _SessionProjects:
    """Session store double: the working Project each existing Session stores."""

    def __init__(self, projects: dict[str, str | None]) -> None:
        self._projects = projects

    def metadata_value(self, address: SessionAddress, key: str) -> str | None:
        if address.session_id not in self._projects:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return self._projects[address.session_id]


class _FakeResolver:
    """Serves one Agent; the working-Project policy is the real resolver's."""

    def __init__(
        self, agent: _FakeAgent, projects: _FakeProjects, sessions: _SessionProjects
    ) -> None:
        self._agent = agent
        self._working_projects = AgentResolver(
            cast(Any, None),
            cast(Any, projects),
            cast(Any, None),
            dict,
            sessions=cast(Any, sessions),
        )

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        return self._agent

    def resolve_working_project(self, project_id: str | None, agent: Any, **options: Any) -> Any:
        return self._working_projects.resolve_working_project(project_id, agent, **options)


class _FakeStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _FakeRuntime:
    def __init__(
        self,
        *,
        projects: _FakeProjects,
        agent: _FakeAgent,
        data_dir: Path,
        session_projects: dict[str, str | None],
    ) -> None:
        self.projects = projects
        self.agent_resolver = _FakeResolver(agent, projects, _SessionProjects(session_projects))
        self.storage = _FakeStorage(data_dir)


def _runtime(
    tmp_path: Path,
    *,
    agent_id: str = "main",
    workspace: str,
    root_project_id: str | None,
    session_projects: dict[str, str | None] | None = None,
) -> Any:
    (tmp_path / "repo").mkdir()
    return cast(
        Any,
        _FakeRuntime(
            projects=_FakeProjects(str(tmp_path / "repo")),
            agent=_FakeAgent(agent_id, workspace, root_project_id),
            data_dir=tmp_path,
            session_projects=session_projects or {},
        ),
    )


@pytest.mark.parametrize(
    ("agent_id", "project_id", "workspace", "root_project_id", "options", "expected"),
    [
        ("builder", "vbot", "workspace", None, {}, "repo"),
        ("main", None, "workspace", None, {}, "workspace"),
        ("main", None, "workspace", "vbot", {}, "repo"),
        ("main", None, "workspace", "vbot", {"working_project_id": None}, "workspace"),
        ("main", None, "workspace", None, {"working_project_id": "vbot"}, "repo"),
        ("main", None, "workspace", None, {"session_id": "in-vbot"}, "repo"),
        ("main", None, "workspace", "vbot", {"session_id": "in-workspace"}, "workspace"),
        ("main", None, "", None, {}, "agents/main/workspace"),
    ],
    ids=[
        "project-cwd",
        "identity-workspace",
        "draft-in-the-default-project",
        "draft-in-the-workspace",
        "draft-in-a-project",
        "session-in-a-project",
        "session-in-the-workspace",
        "no-workspace-data-dir",
    ],
)
def test_mention_root_follows_the_working_project(
    tmp_path: Path,
    agent_id: str,
    project_id: str | None,
    workspace: str,
    root_project_id: str | None,
    options: dict[str, Any],
    expected: str,
) -> None:
    runtime = _runtime(
        tmp_path,
        agent_id=agent_id,
        workspace=str(tmp_path / workspace) if workspace else "",
        root_project_id=root_project_id,
        session_projects={"in-vbot": "vbot", "in-workspace": None},
    )

    assert resolve_mention_root(runtime, agent_id, project_id, **options) == tmp_path / expected


@pytest.mark.parametrize(
    ("root_project_id", "options", "remove_repo", "error"),
    [
        ("missing", {}, False, AgentResolutionError),
        (None, {"session_id": "in-removed"}, False, WorkingProjectMissingError),
        ("vbot", {}, True, ChatError),
    ],
    ids=["missing-default-project", "missing-session-project", "missing-cwd"],
)
def test_mention_root_never_falls_back_to_the_workspace(
    tmp_path: Path,
    root_project_id: str | None,
    options: dict[str, Any],
    remove_repo: bool,
    error: type[Exception],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _runtime(
        tmp_path,
        workspace=str(workspace),
        root_project_id=root_project_id,
        session_projects={"in-removed": "removed"},
    )
    if remove_repo:
        (tmp_path / "repo").rmdir()

    with pytest.raises(error):
        resolve_mention_root(runtime, "main", None, **options)
