"""Tests for the project anchor lifecycle (ProjectStore)."""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from core.database import write_bootstrap_marker
from core.projects.paths import cwd_exists
from core.projects.projects import (
    PROJECT_DEFAULT_ALLOWED_TOOLS,
    Project,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
)
from core.projects.store import ProjectStore
from core.sessions import ChatSessionManager, SessionAddress
from core.utils import tree_move
from tests.core.database.database_test_support import frozen_members

_SEEDED_FIELDS = (
    "cwd",
    "source_format",
    "auto_load",
    "allowed_tools",
    "skills_bundled_enabled",
    "skills_global_enabled",
    "skills_project_disabled",
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    write_bootstrap_marker(directory)
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repos" / "vbot"
    repo_dir.mkdir(parents=True)
    return repo_dir


def test_create_writes_the_anchor_with_seeded_defaults(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)

    project = store.create("vbot", "vBot", repo)

    anchor = data_dir / "projects" / "vbot"
    payload = json.loads((anchor / "project.json").read_text("utf-8"))
    assert not (anchor / "agents").exists()
    assert payload == {"format_version": 1, **project.to_dict()}
    assert store.get("vbot") == project
    assert {field: payload[field] for field in _SEEDED_FIELDS} == {
        "cwd": str(Path(os.path.realpath(repo))),
        "source_format": "opencode",
        # AGENTS.md loads with zero config, yet stays a removable entry.
        "auto_load": ["AGENTS.md"],
        # The base Tool Whitelist is the ceiling; Skill rule lists start empty.
        "allowed_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
        "skills_bundled_enabled": [],
        "skills_global_enabled": [],
        "skills_project_disabled": [],
    }


def test_create_persists_explicit_fields(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)

    project = store.create(
        "vbot",
        "vBot",
        repo,
        default_agent="orchestrator",
        default_model="openai/gpt-5",
        default_temperature=0.4,
        default_thinking_effort="high",
        source_format="claude",
        auto_load=["docs/guide.md", "CONTEXT.md"],
    )

    assert store.get("vbot") == project
    assert (
        project.default_agent,
        project.default_model,
        project.default_temperature,
        project.default_thinking_effort,
        project.source_format,
    ) == ("orchestrator", "openai/gpt-5", 0.4, "high", "claude")
    # The caller's list keeps its order behind the seeded AGENTS.md.
    assert project.auto_load == ["AGENTS.md", "docs/guide.md", "CONTEXT.md"]


def test_failed_create_removes_anchor_so_retry_succeeds(
    data_dir: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(data_dir)
    original = store._write_project

    def fail_write(_project):
        raise OSError("config unavailable")

    monkeypatch.setattr(store, "_write_project", fail_write)
    with pytest.raises(OSError, match="config unavailable"):
        store.create("vbot", "vBot", repo)

    assert not (data_dir / "projects" / "vbot").exists()
    monkeypatch.setattr(store, "_write_project", original)
    assert store.create("vbot", "vBot", repo).project_id == "vbot"


@pytest.mark.parametrize("claim_cwd", [False, True])
def test_concurrent_project_mutations_preserve_config_and_unique_cwd(
    data_dir: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, claim_cwd: bool
) -> None:
    store = ProjectStore(data_dir)
    if not claim_cwd:
        store.create("vbot", "Original", repo)
    first_write = Event()
    release_first = Event()
    second_started = Event()
    second_write = Event()
    original = store._write_project

    def write(project):
        if not first_write.is_set():
            first_write.set()
            assert release_first.wait(5)
        else:
            second_write.set()
        original(project)

    def second():
        second_started.set()
        if claim_cwd:
            return store.create("duplicate", "Duplicate", repo)
        return store.set_override("vbot", "coder", "model", "other/model")

    monkeypatch.setattr(store, "_write_project", write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = (
            executor.submit(store.create, "vbot", "Changed", repo)
            if claim_cwd
            else executor.submit(store.update, "vbot", display_name="Changed")
        )
        try:
            assert first_write.wait(5)
            following = executor.submit(second)
            assert second_started.wait(5)
            second_write.wait(0.2)
        finally:
            release_first.set()
        first.result(timeout=5)
        if claim_cwd:
            with pytest.raises(ProjectAlreadyExistsError):
                following.result(timeout=5)
        else:
            following.result(timeout=5)

    persisted = store.get("vbot")
    assert persisted.display_name == "Changed"
    if claim_cwd:
        assert [project.project_id for project in store.list()] == ["vbot"]
    else:
        assert persisted.overrides == {"coder": {"model": "other/model"}}


def test_a_change_waiting_for_a_data_snapshot_never_holds_up_project_reads(
    data_dir: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "Original", repo)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with frozen_members(data_dir, monkeypatch) as gate:
            updating = executor.submit(store.update, "vbot", display_name="Changed")
            assert gate.waiting.acquire(timeout=10)
            # The change waits without the store lock that Event Loop readers take.
            assert executor.submit(store.get, "vbot").result(timeout=10).display_name == "Original"
            assert not updating.done()
        assert updating.result(timeout=10).display_name == "Changed"

    assert gate.capture is not None
    assert (gate.capture.attempts, gate.capture.waited_changes) == (1, 1)


def _payload(data_dir: Path) -> Path:
    return data_dir / "archive" / "entries" / "arc_test" / "project"


def test_archive_files_moves_only_the_anchor_and_restore_files_brings_it_back(
    data_dir: Path, repo: Path
) -> None:
    marker = repo / "keep.txt"
    marker.write_text("repo content", encoding="utf-8")
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    payload = _payload(data_dir)

    with store.archive_files("vbot", payload) as archived:
        assert archived is not None and archived.display_name == "vBot"
    document = json.loads((payload / "project.json").read_text(encoding="utf-8"))
    document["future_field"] = {"kept": True}
    (payload / "project.json").write_text(json.dumps(document), encoding="utf-8")

    assert not (data_dir / "projects" / "vbot").exists()
    # Archiving never touches the repository.
    assert marker.read_text(encoding="utf-8") == "repo content"
    inspected = store.inspect_archived(payload)
    assert inspected.problem is None and inspected.project is not None
    with store.restore_files(payload, "vbot-old") as restored:
        assert restored.project_id == "vbot-old"
    assert not payload.exists()
    assert store.get("vbot-old").display_name == "vBot"
    stored = json.loads((data_dir / "projects" / "vbot-old" / "project.json").read_text("utf-8"))
    assert (stored["project_id"], stored["future_field"]) == ("vbot-old", {"kept": True})


def test_restore_files_refuses_a_taken_id_or_a_claimed_repo_without_changes(
    data_dir: Path, repo: Path, tmp_path: Path
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    other_repo = tmp_path / "repos" / "other"
    other_repo.mkdir(parents=True)
    store.create("other", "Other", other_repo)
    payload = _payload(data_dir)
    with store.archive_files("vbot", payload):
        pass
    before = (payload / "project.json").read_bytes()

    with pytest.raises(ProjectAlreadyExistsError), store.restore_files(payload, "other"):
        pytest.fail("a taken id is refused before anything moves")
    # Another Project claimed the repo meanwhile.
    store.create("again", "Again", repo)
    with (
        pytest.raises(ProjectError, match="already points at this folder: again"),
        store.restore_files(payload, "vbot"),
    ):
        pytest.fail("a claimed repo is refused before anything moves")

    assert (payload / "project.json").read_bytes() == before
    assert store.restore_target_problem("other") == "project_id_taken"
    assert store.restore_target_problem("Not A Slug") == "invalid_target_id"
    assert store.restore_target_problem("vbot") is None


@pytest.mark.parametrize("move_back", ["succeeds", "refused"])
def test_archive_files_moves_the_anchor_back_when_the_commit_fails(
    data_dir: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, move_back: str
) -> None:
    store = ProjectStore(data_dir)
    try:
        store.create("vbot", "vBot", repo)
        anchor = data_dir / "projects" / "vbot"
        payload = _payload(data_dir)
        real_replace = os.replace

        def replace(source, destination):
            if move_back == "refused" and Path(destination) == anchor:
                raise OSError("rollback unavailable")
            real_replace(source, destination)

        monkeypatch.setattr(tree_move, "_replace", replace)
        expected = (
            (RuntimeError, "database unavailable")
            if move_back == "succeeds"
            else (ProjectError, f"Project files retained at {re.escape(str(payload))}")
        )
        with pytest.raises(expected[0], match=expected[1]), store.archive_files("vbot", payload):
            raise RuntimeError("database unavailable")

        if move_back == "succeeds":
            assert store.get("vbot").display_name == "vBot"
            assert not payload.exists()
        else:  # the Project files stay whole in the payload, never deleted
            assert (payload / "project.json").is_file()
            assert not anchor.exists()
    finally:
        store.close()


@pytest.mark.parametrize(
    "blocker",
    [
        "refused-rename",
        pytest.param(
            "open-file",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="only Windows refuses to rename a tree with open files"
            ),
        ),
    ],
)
def test_archive_files_keeps_the_anchor_when_the_move_cannot_complete(
    data_dir: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    store = ProjectStore(data_dir)
    try:
        store.create("vbot", "vBot", repo)
        anchor = data_dir / "projects" / "vbot"
        real_replace = os.replace

        def replace(source, destination):
            if Path(source) == anchor:
                raise PermissionError(errno.EACCES, "held open by another program")
            real_replace(source, destination)

        if blocker == "refused-rename":
            monkeypatch.setattr(tree_move, "_replace", replace)
            held = None
        else:
            held = (anchor / "held.txt").open("w", encoding="utf-8")
        try:
            with (
                pytest.raises(ProjectError, match="Project archival failed"),
                store.archive_files("vbot", _payload(data_dir)),
            ):
                pytest.fail("the body runs only after a complete move")
        finally:
            if held is not None:
                held.close()

        # A move that fails means nothing moved: no partial copy replaces or removes a tree.
        assert store.get("vbot").display_name == "vBot"
        assert not _payload(data_dir).exists()
    finally:
        store.close()


def test_update_rebuilds_changed_fields_and_keeps_the_rest(
    data_dir: Path, repo: Path, tmp_path: Path
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo, default_temperature=0.5, default_thinking_effort="high")
    before = store.set_override("vbot", "builder", "model", "openai/gpt-5")
    moved = tmp_path / "repos" / "moved"
    moved.mkdir(parents=True)

    updated = store.update(
        "vbot",
        display_name="vBot Renamed",
        cwd=str(moved),
        source_format="claude",
        allowed_tools=["read", "grep"],
        skills_bundled_enabled=["frontend-design"],
        skills_project_disabled=["debugging"],
        default_temperature=0.1,
        auto_load=[],
    )

    assert store.get("vbot") == updated
    # project_id, created_at, the untouched thinking effort, and overrides survive.
    assert updated.to_dict() == {
        **before.to_dict(),
        "display_name": "vBot Renamed",
        "cwd": str(Path(os.path.realpath(moved))),
        "source_format": "claude",
        "allowed_tools": ["read", "grep"],
        "skills_bundled_enabled": ["frontend-design"],
        "skills_project_disabled": ["debugging"],
        "default_temperature": 0.1,
        # Seeding is creation-only: a removed AGENTS.md is not re-seeded.
        "auto_load": [],
        "updated_at": updated.updated_at,
    }


@pytest.mark.parametrize(
    ("changes", "field", "cleared"),
    [
        pytest.param({"display_name": None}, "display_name", "vbot", id="display-name-null"),
        pytest.param({"display_name": "   "}, "display_name", "vbot", id="display-name-blank"),
        pytest.param(
            {"default_thinking_effort": None}, "default_thinking_effort", None, id="thinking"
        ),
    ],
)
def test_update_clears_a_field_with_its_empty_value(
    data_dir: Path, repo: Path, changes: dict[str, object], field: str, cleared: object
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo, default_thinking_effort="high")

    updated = store.update("vbot", **changes)

    assert getattr(updated, field) == cleared
    assert getattr(store.get("vbot"), field) == cleared


@pytest.mark.parametrize(
    ("mutate", "error", "message"),
    [
        pytest.param(
            lambda store, repos: store.create("vbot", "Again", repos / "fresh"),
            ProjectAlreadyExistsError,
            "Project already exists: vbot",
            id="duplicate-id",
        ),
        pytest.param(
            lambda store, repos: store.create("vbot-2", "Copy", repos / "vbot"),
            ProjectAlreadyExistsError,
            "A project already points at this folder: vbot",
            id="duplicate-cwd",
        ),
        pytest.param(
            lambda store, repos: store.create("vbot-2", "Copy", f"{repos / 'vbot'}{os.sep}"),
            ProjectAlreadyExistsError,
            "A project already points at this folder: vbot",
            id="duplicate-cwd-trailing-separator",
        ),
        pytest.param(
            lambda store, repos: store.update("other", cwd=str(repos / "vbot")),
            ProjectAlreadyExistsError,
            "A project already points at this folder: vbot",
            id="update-cwd-collision",
        ),
        pytest.param(
            lambda store, repos: store.update("vbot", source_format="cursor"),
            ProjectError,
            "source_format must be one of: opencode, claude",
            id="update-source-format",
        ),
        # Overrides have their own set/clear seam.
        pytest.param(
            lambda store, repos: store.update(
                "vbot", overrides={"builder": {"model": "openai/gpt-5"}}
            ),
            ProjectError,
            "Unknown project fields: overrides",
            id="update-overrides",
        ),
        pytest.param(
            lambda store, repos: store.update("vbot", team=["builder"]),
            ProjectError,
            "Unknown project fields: team",
            id="update-unknown-field",
        ),
        pytest.param(
            lambda store, repos: store.set_override("vbot", "builder", "model", "  "),
            ProjectError,
            "overrides['builder'].model must be a non-empty model string",
            id="empty-override-model",
        ),
        # An opt-in grant cannot reach beyond the Project Tool Whitelist.
        pytest.param(
            lambda store, repos: store.set_override(
                "vbot", "writer", "tool_access", {"mode": "all", "granted": ["computer"]}
            ),
            ProjectError,
            "overrides['writer'].tool_access.granted contains Tools outside the Project "
            "Tool Whitelist: computer",
            id="grant-outside-ceiling",
        ),
        pytest.param(
            lambda store, repos: store.set_override("missing", "builder", "model", "a/b"),
            ProjectNotFoundError,
            "Project not found: missing",
            id="override-unknown-project",
        ),
        pytest.param(
            lambda store, repos: store.get("missing"),
            ProjectNotFoundError,
            "Project not found: missing",
            id="get-unknown-project",
        ),
        pytest.param(
            lambda store, repos: store.archive_files("missing", repos / "payload").__enter__(),
            ProjectNotFoundError,
            "Project not found: missing",
            id="archive-unknown-project",
        ),
        # A Project id is a storage path segment; traversal is refused.
        pytest.param(
            lambda store, repos: store.get("../somewhere"),
            ProjectError,
            "Invalid project id: '../somewhere'",
            id="path-traversal",
        ),
    ],
)
def test_invalid_requests_are_rejected_without_changes(
    data_dir: Path,
    tmp_path: Path,
    mutate: Callable[[ProjectStore, Path], object],
    error: type[ProjectError],
    message: str,
) -> None:
    repos = tmp_path / "repos"
    store = ProjectStore(data_dir)
    for project_id in ("vbot", "other"):
        (repos / project_id).mkdir(parents=True)
        store.create(project_id, project_id.title(), repos / project_id)
    before = store.list()

    with pytest.raises(error, match=re.escape(message)) as exc_info:
        mutate(store, repos)

    assert exc_info.type is error
    assert store.list() == before


def test_load_requires_the_tool_whitelist_but_defaults_skill_lists(
    data_dir: Path, repo: Path
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    config_path = data_dir / "projects" / "vbot" / "project.json"
    payload = json.loads(config_path.read_text("utf-8"))
    del payload["skills_bundled_enabled"]
    del payload["skills_project_disabled"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = store.get("vbot")

    assert reloaded.skills_bundled_enabled == []
    assert reloaded.skills_project_disabled == []
    del payload["allowed_tools"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProjectError, match=r"\$\.allowed_tools: is required"):
        store.get("vbot")


def test_project_update_keeps_unknown_fields_on_disk(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    config_path = data_dir / "projects" / "vbot" / "project.json"
    payload = json.loads(config_path.read_text("utf-8"))
    payload["future_field"] = [1, 2]
    payload["overrides"] = {
        "builder": {"model": "openai/gpt-5", "future_override": True},
        "future_only": {"future": 1},
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.get("vbot")
    store.set_override("vbot", "builder", "temperature", 0.5)

    # An override with no known field overrides nothing but stays an entry.
    assert loaded.overrides == {"builder": {"model": "openai/gpt-5"}, "future_only": {}}
    rewritten = json.loads(config_path.read_text("utf-8"))
    assert rewritten["format_version"] == 1
    assert rewritten["future_field"] == [1, 2]
    assert rewritten["overrides"] == {
        "builder": {
            "model": "openai/gpt-5",
            "temperature": 0.5,
            "future_override": True,
        },
        "future_only": {"future": 1},
    }


def test_clearing_the_last_known_override_field_keeps_unknown_fields(
    data_dir: Path, repo: Path
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    store.set_override("vbot", "plain", "model", "openai/gpt-5")
    config_path = data_dir / "projects" / "vbot" / "project.json"
    payload = json.loads(config_path.read_text("utf-8"))
    payload["overrides"]["builder"] = {"model": "openai/gpt-5", "future_override": True}
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    store.clear_override("vbot", "builder", "model")
    store.clear_override("vbot", "plain", "model")

    rewritten = json.loads(config_path.read_text("utf-8"))
    # The entry with an unknown field survives; the one left with no field is removed.
    assert rewritten["overrides"] == {"builder": {"future_override": True}}
    assert store.get("vbot").overrides == {"builder": {}}


def test_project_written_by_a_newer_vbot_is_never_overwritten(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    config_path = data_dir / "projects" / "vbot" / "project.json"
    payload = json.loads(config_path.read_text("utf-8"))
    payload["format_version"] = 2
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ProjectError, match="written by a newer vBot"):
        store.set_override("vbot", "builder", "model", "openai/gpt-5")

    assert config_path.read_text(encoding="utf-8") == original


def test_override_mutations_rewrite_exactly_one_field(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    config_path = data_dir / "projects" / "vbot" / "project.json"
    builder_model = {"model": "openai/gpt-5"}
    planner = {"planner": {"model": "anthropic/claude-sonnet-4"}}
    steps: list[tuple[Callable[[], Project], dict[str, object]]] = [
        (
            lambda: store.set_override("vbot", "builder", "model", "openai/gpt-5"),
            {"builder": builder_model},
        ),
        # A second field merges into the Agent's entry.
        (
            lambda: store.set_override("vbot", "builder", "temperature", 0.4),
            {"builder": {**builder_model, "temperature": 0.4}},
        ),
        (
            lambda: store.set_override("vbot", "planner", "model", "anthropic/claude-sonnet-4"),
            {"builder": {**builder_model, "temperature": 0.4}, **planner},
        ),
        # Replacing a field leaves the other Agent's override intact.
        (
            lambda: store.set_override("vbot", "builder", "model", "openai/gpt-mini"),
            {"builder": {"model": "openai/gpt-mini", "temperature": 0.4}, **planner},
        ),
        # 0.0 (sampling floor) and "" (Provider default) are real values.
        (
            lambda: store.set_override("vbot", "planner", "temperature", 0.0),
            {
                "builder": {"model": "openai/gpt-mini", "temperature": 0.4},
                "planner": {"model": "anthropic/claude-sonnet-4", "temperature": 0.0},
            },
        ),
        (
            lambda: store.set_override("vbot", "builder", "thinking_effort", ""),
            {
                "builder": {"model": "openai/gpt-mini", "temperature": 0.4, "thinking_effort": ""},
                "planner": {"model": "anthropic/claude-sonnet-4", "temperature": 0.0},
            },
        ),
        # Clearing removes only the target field.
        (
            lambda: store.clear_override("vbot", "planner", "temperature"),
            {
                "builder": {"model": "openai/gpt-mini", "temperature": 0.4, "thinking_effort": ""},
                **planner,
            },
        ),
        # Clearing the last field removes the Agent's entry.
        (
            lambda: store.clear_override("vbot", "planner", "model"),
            {"builder": {"model": "openai/gpt-mini", "temperature": 0.4, "thinking_effort": ""}},
        ),
        # Clearing an absent field is a no-op.
        (
            lambda: store.clear_override("vbot", "planner", "model"),
            {"builder": {"model": "openai/gpt-mini", "temperature": 0.4, "thinking_effort": ""}},
        ),
    ]

    for mutate, overrides in steps:
        project = mutate()

        assert project.overrides == overrides
        assert store.get("vbot") == project
        assert json.loads(config_path.read_text("utf-8"))["overrides"] == overrides


def test_create_allows_missing_cwd_folder(data_dir: Path, tmp_path: Path) -> None:
    # A bare/not-yet-existing repo is detected at open time, not rejected here.
    store = ProjectStore(data_dir)
    missing = tmp_path / "repos" / "not-cloned-yet"

    project = store.create("future", "Future", missing)

    assert store.exists("future")
    assert cwd_exists(project.cwd) is False


def test_get_defaults_optional_metadata_in_minimal_config(data_dir: Path, repo: Path) -> None:
    config_path = data_dir / "projects" / "vbot" / "project.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {"format_version": 1, "project_id": "vbot", "cwd": str(repo), "allowed_tools": []}
        ),
        encoding="utf-8",
    )
    store = ProjectStore(data_dir)

    loaded = store.get("vbot")

    assert loaded.display_name == "vbot"
    assert loaded.created_at
    assert loaded.updated_at
    assert store.exists("vbot") is True


@pytest.mark.parametrize(
    ("stored", "message"),
    [
        pytest.param(
            {"format_version": 1, "project_id": "vbot"},
            "error $.cwd: is required; error $.allowed_tools: is required",
            id="missing-required-fields",
        ),
        pytest.param(
            {"format_version": 1, "project_id": "other", "cwd": "/srv/other", "allowed_tools": []},
            "expected vbot, got other",
            id="id-disagrees-with-anchor",
        ),
    ],
)
def test_invalid_stored_config_is_not_a_usable_project(
    data_dir: Path, stored: dict[str, object], message: str
) -> None:
    config_path = data_dir / "projects" / "vbot" / "project.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(stored), encoding="utf-8")
    store = ProjectStore(data_dir)

    with pytest.raises(ProjectError, match=re.escape(message)) as exc_info:
        store.get("vbot")

    # The Anchor exists, so this is a broken Project, not a missing one.
    assert exc_info.type is ProjectError
    assert store.exists("vbot") is False


def test_case_variant_of_a_stored_project_id_is_not_found(data_dir: Path, repo: Path) -> None:
    # Ids are exact. A case-insensitive filesystem (Windows) opens the stored ``vbot``
    # Anchor for ``VBOT``; that different id must still name no Project on every
    # platform, and archiving it must never move the real Project.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    with pytest.raises(ProjectNotFoundError):
        store.get("VBOT")
    with pytest.raises(ProjectNotFoundError):
        store.update("VBOT", display_name="Renamed")
    with pytest.raises(ProjectNotFoundError):
        store.archive_files("VBOT", _payload(data_dir)).__enter__()

    assert store.exists("VBOT") is False
    assert store.session_owning_agents("VBOT") == []
    assert store.get("vbot").display_name == "vBot"
    assert [project.project_id for project in store.list()] == ["vbot"]


def test_list_returns_valid_projects_sorted_by_id(data_dir: Path, tmp_path: Path) -> None:
    store = ProjectStore(data_dir)
    for name in ["zeta", "alpha", "mid"]:
        repo_dir = tmp_path / "repos" / name
        repo_dir.mkdir(parents=True)
        store.create(name, name.title(), repo_dir)
    bad_dir = data_dir / "projects" / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "project.json").write_text("{ not json", encoding="utf-8")

    assert [project.project_id for project in store.list()] == ["alpha", "mid", "zeta"]


@pytest.mark.parametrize(
    ("relative_path", "found"),
    [
        pytest.param("repos/vbot", "vbot", id="registered"),
        # ``.``/``..`` segments resolve to the same cwd identity key.
        pytest.param("repos/vbot/sub/..", "vbot", id="non-normalized"),
        pytest.param("somewhere/else", None, id="unregistered"),
        # An empty path (a config Agent's empty workspace) is a clean no-match.
        pytest.param(None, None, id="empty"),
    ],
)
def test_find_by_cwd_matches_the_cwd_identity(
    data_dir: Path, repo: Path, tmp_path: Path, relative_path: str | None, found: str | None
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    project = store.find_by_cwd("" if relative_path is None else tmp_path / relative_path)

    assert (project.project_id if project else None) == found


def test_archive_files_rejects_a_path_traversal_id_and_leaves_the_sibling_untouched(
    data_dir: Path, repo: Path
) -> None:
    # A traversal id must be refused before any move, so the data-dir sibling
    # that the resolved path (projects/../secret) would target survives untouched.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    sibling = data_dir / "secret"
    sibling.mkdir(parents=True)
    sibling.joinpath("keep.txt").write_text("important", encoding="utf-8")

    with pytest.raises(ProjectError):
        store.archive_files("../secret", _payload(data_dir)).__enter__()

    assert sibling.is_dir()
    assert sibling.joinpath("keep.txt").read_text(encoding="utf-8") == "important"


def test_session_owning_agents_lists_only_agents_with_sessions(data_dir: Path, repo: Path) -> None:
    sessions = ChatSessionManager(data_dir)
    store = ProjectStore(data_dir, sessions=sessions)
    store.create("vbot", "vBot", repo)
    assert store.session_owning_agents("vbot") == []
    assert store.session_owning_agents("missing") == []

    sessions.create("builder", project_id="vbot")
    sessions.create("orchestrator", project_id="vbot")
    # An Extension participant's synthetic Agent is neither a Team member nor an
    # orphan; its owner reports the Session separately.
    sessions.create_bound_temporary_session(
        SessionAddress("vbot", "tmp_participant", "ses_participant"),
        owner_name="swarm",
        group_id="swr_group",
        participant_id="prt_peer",
        config={},
    )

    assert store.session_owning_agents("vbot") == ["builder", "orchestrator"]
    sessions.close()
