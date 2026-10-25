"""Memory history: recorded revisions, external edits, past states and reverts."""

import errno
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import core.memory._file_backend as memory_module
from core.memory import (
    MemoryError,
    MemoryRevertError,
    MemoryRevertIncompleteError,
    MemoryService,
    MemoryWriter,
)
from core.memory._history import MemoryHistory
from core.tools.file_state import FileRead

_TOOL = MemoryWriter(
    agent_id="coder", actor="tool", session_id="s-1", run_id="r-1", run_kind="user"
)
_RPC = MemoryWriter(agent_id="coder", actor="rpc")


@pytest.fixture
def agents_root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "coder").mkdir(parents=True)
    return tmp_path / "agents"


@pytest.fixture
def workspace(agents_root: Path) -> Path:
    return agents_root / "coder" / "workspace"


@pytest.fixture
def service(agents_root: Path) -> MemoryService:
    return MemoryService(history_root=agents_root)


def _changes(revision_data: dict[str, object]) -> list[tuple[object, ...]]:
    changes = revision_data["changes"]
    assert isinstance(changes, list)
    return [(change["op"], change.get("previous"), change["text"]) for change in changes]


def test_project_memory_is_shared_across_writers_with_an_independent_history(
    tmp_path: Path, agents_root: Path, workspace: Path
) -> None:
    projects_root = tmp_path / "projects"
    anchor = projects_root / "demo"
    anchor.mkdir(parents=True)
    service = MemoryService(history_root=agents_root, project_history_root=projects_root)
    first = MemoryWriter(agent_id="coder", project_id="demo", actor="tool", run_id="r-1")
    second = MemoryWriter(agent_id="reviewer", project_id="demo", actor="tool", run_id="r-2")
    service.add_entry(workspace, "agent", "Personal preference.", writer=_TOOL)
    added = service.add_entry(
        anchor, "project", "Discuss design before implementing.", writer=first
    )
    replaced = service.replace_matching(
        anchor, "project", "Discuss design", "Agree on design before implementing.", writer=second
    )

    revisions = service.history(anchor, project_id="demo")
    assert [(r.id, r.scope, r.agent_id, r.run_id) for r in revisions] == [
        (1, "project", "coder", "r-1"),
        (2, "project", "reviewer", "r-2"),
    ]
    assert (added.revision, replaced.revision) == (1, 2)
    assert service.entries_at(anchor, revision_id=1, project_id="demo") == {
        "project": ["Discuss design before implementing."]
    }
    assert service.compare(anchor, None, 1, project_id="demo")["project"][0].op == "replaced"
    assert service.check_revert(anchor, [2], writer=first) == ("project",)
    reverted = service.revert(anchor, [2], writer=first)
    assert reverted.changed == ("project",)
    assert service.entries_at(anchor, project_id="demo") == {
        "project": ["Discuss design before implementing."]
    }
    assert service.entries_at(workspace, "coder") == {"user": [], "agent": ["Personal preference."]}
    assert len(service.recorded_revisions("coder")) == 1
    assert len(service.recorded_revisions(project_id="demo")) == 3
    assert not (anchor / "USER.md").exists()
    assert (anchor / "memory-history.jsonl").is_file()

    reads: list[FileRead] = []
    rendered = service.read_prompt_scopes(
        anchor, ("project",), memory_tool=None, on_read=reads.append
    )
    assert "Discuss design before implementing." in rendered
    assert "Personal preference." not in rendered
    assert "/3000 chars used" in rendered
    assert [path for path, _stamp in reads] == [(anchor / "MEMORY.md").resolve()]
    assert service.read_prompt_scopes(anchor, (), memory_tool=None) == ""


def test_changes_are_recorded_with_their_writer_and_replay_to_every_state(
    service: MemoryService, workspace: Path, agents_root: Path
) -> None:
    service.add_entry(workspace, "agent", "Uses pytest.", writer=_TOOL)
    service.add_entry(workspace, "user", "Prefers German.", writer=_RPC)
    service.replace_matching(workspace, "agent", "pytest", "Uses pytest with xdist.", writer=_TOOL)
    service.remove_matching(workspace, "user", "German", writer=_TOOL)
    service.add_entry(workspace, "agent", "Uses pytest with xdist.", writer=_TOOL)  # no change

    revisions = service.history(workspace, "coder")

    assert [(r.id, r.scope, r.kind, r.actor) for r in revisions] == [
        (1, "agent", "edit", "tool"),
        (2, "user", "edit", "rpc"),
        (3, "agent", "edit", "tool"),
        (4, "user", "edit", "tool"),
    ]
    assert (revisions[0].session_id, revisions[0].run_id, revisions[0].run_kind) == (
        "s-1",
        "r-1",
        "user",
    )
    assert revisions[1].run_kind is None
    assert [_changes(r.to_dict()) for r in revisions] == [
        [("added", None, "Uses pytest.")],
        [("added", None, "Prefers German.")],
        [("replaced", "Uses pytest.", "Uses pytest with xdist.")],
        [("removed", None, "Prefers German.")],
    ]
    assert service.entries_at(workspace, "coder", 2) == {
        "user": ["Prefers German."],
        "agent": ["Uses pytest."],
    }
    assert service.entries_at(workspace, "coder", None) == {
        "user": [],
        "agent": ["Uses pytest with xdist."],
    }
    # One JSON object per revision beside the Agent's Workspace, outside it.
    lines = (agents_root / "coder" / "memory-history.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["id"] for line in lines.splitlines()] == [1, 2, 3, 4]
    assert [json.loads(line).get("run_kind") for line in lines.splitlines()] == [
        "user",
        None,
        "user",
        "user",
    ]


def test_edits_outside_the_service_are_recorded_when_next_noticed(
    service: MemoryService, workspace: Path
) -> None:
    workspace.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_text("- Existing fact.\n", encoding="utf-8")
    os.utime(memory_file, (1_700_000_000, 1_700_000_000))

    service.add_entry(workspace, "agent", "Added by the Tool.", writer=_TOOL)
    memory_file.write_text("- Existing fact, edited by hand.\n- Added by the Tool.\n", "utf-8")
    revisions = service.history(workspace, "coder")

    assert [(r.kind, r.actor) for r in revisions] == [
        ("baseline", "external"),
        ("edit", "tool"),
        ("external", "external"),
    ]
    # The history starts with the entries that already existed, dated by the file.
    assert revisions[0].at.startswith("2023-11-14T22:13:20")
    assert _changes(revisions[0].to_dict()) == [("added", None, "Existing fact.")]
    assert _changes(revisions[2].to_dict()) == [
        ("replaced", "Existing fact.", "Existing fact, edited by hand.")
    ]
    assert service.history(workspace, "coder") == revisions


@pytest.mark.parametrize(
    ("setup", "revert", "expected"),
    [
        pytest.param(
            [("add", "A"), ("add", "B"), ("add", "C"), ("remove", "B")],
            [4],
            ["A", "B", "C"],
            id="removed-entry-returns-to-its-position",
        ),
        pytest.param(
            [("add", "A"), ("replace", "A", "A2")], [2], ["A"], id="replaced-entry-text-returns"
        ),
        pytest.param([("add", "A"), ("add", "B")], [1], ["B"], id="added-entry-is-removed"),
        pytest.param(
            [("add", "A"), ("replace", "A", "A2"), ("remove", "A2")],
            [2, 3],
            ["A"],
            id="several-revisions-newest-first",
        ),
        pytest.param([("add", "A"), ("remove", "A")], [1], [], id="already-undone-needs-nothing"),
        pytest.param(
            [("add", "A"), ("replace", "A", "A2"), ("add", "A")],
            [2],
            ["A", "A"],
            id="replacement-restores-entry-even-if-text-exists-elsewhere",
        ),
        pytest.param(
            [("external", "A", "B"), ("external", "B", "A")],
            [2],
            ["A", "B"],
            id="reordered-entries-return-to-their-positions",
        ),
        pytest.param(
            [
                ("external", "A", "B", "C", "D"),
                ("external", "C", "D", "A", "B"),
                ("add", "Unrelated"),
            ],
            [2],
            ["A", "B", "C", "D", "Unrelated"],
            id="rotation-preserves-later-unrelated-entry",
        ),
        pytest.param(
            [("external", "A", "A", "B"), ("external", "A", "B")],
            [2],
            ["A", "A", "B"],
            id="removing-a-duplicate-restores-its-occurrence",
        ),
        pytest.param(
            [("external", "A", "B", "A", "B", "A"), ("replace-index", "2", "C")],
            [2],
            ["A", "B", "A", "B", "A"],
            id="rpc-replacement-among-duplicates-replays-and-reverts",
        ),
        pytest.param(
            [("external", "A", "B", "C", "D"), ("external", "D", "A2", "B", "C")],
            [2],
            ["A", "B", "C", "D"],
            id="replacement-positions-account-for-moved-entries",
        ),
        pytest.param(
            [
                ("external", "A", "C", "B"),
                ("replace-index", "3", "C"),
                ("external", "X", "A", "C", "C"),
            ],
            [2],
            ["X", "A", "C", "B"],
            id="replacement-among-duplicates-after-unrelated-prefix-addition",
        ),
        pytest.param(
            [
                ("external", "A", "C", "B"),
                ("replace-index", "3", "C"),
                ("external", "C", "C"),
            ],
            [2],
            ["C", "B"],
            id="replacement-among-duplicates-after-unrelated-prefix-removal",
        ),
        pytest.param(
            [("external", "A", "B"), ("external", "B", "A"), ("external", "X", "B", "A")],
            [2],
            ["X", "A", "B"],
            id="reorder-preserves-later-prefix-insertion",
        ),
        pytest.param(
            [
                ("external", "A", "C", "B"),
                ("replace-index", "3", "C"),
                ("remove-index", "3"),
                ("external", "X", "A", "C"),
            ],
            [2, 3],
            ["X", "A", "C", "B"],
            id="several-reverts-rebase-restored-duplicate-occurrence",
        ),
        pytest.param(
            [("external", "C"), ("external", "C", "C"), ("remove-index", "2")],
            [2],
            ["C"],
            id="already-removed-addition-leaves-identical-survivor-alone",
        ),
        pytest.param(
            [
                ("external", "A", "B"),
                ("replace-index", "2", "C"),
                ("remove-index", "2"),
                ("revert", "3"),
            ],
            [2],
            ["A", "B"],
            id="separate-undo-restores-replacement-after-restored-removal",
        ),
        pytest.param(
            [("external", "A"), ("add", "B"), ("remove-index", "2"), ("revert", "3")],
            [2],
            ["A"],
            id="separate-undo-removes-addition-after-restored-removal",
        ),
        pytest.param(
            [
                ("external", "A", "C", "B"),
                ("replace-index", "3", "C"),
                ("remove-index", "3"),
                ("revert", "3"),
                ("external", "X", "A", "C", "C"),
            ],
            [2],
            ["X", "A", "C", "B"],
            id="separate-undo-retains-duplicate-identity-through-restore-and-prefix",
        ),
        pytest.param(
            [
                ("external", "A"),
                ("remove-index", "1"),
                ("revert", "2"),
                ("replace-index", "1", "B"),
            ],
            [2],
            ["B"],
            id="already-restored-removal-does-not-duplicate-later-replacement",
        ),
    ],
)
def test_revert_takes_back_the_named_revisions(
    service: MemoryService,
    workspace: Path,
    setup: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    revert: list[int],
    expected: list[str],
) -> None:
    for step in setup:
        if step[0] == "add":
            service.add_entry(workspace, "agent", step[1], writer=_TOOL)
        elif step[0] == "replace":
            service.replace_matching(workspace, "agent", step[1], step[2], writer=_TOOL)
        elif step[0] == "replace-index":
            service.replace_entry(workspace, "agent", int(step[1]), step[2], writer=_RPC)
        elif step[0] == "remove-index":
            service.remove_entry(workspace, "agent", int(step[1]), writer=_RPC)
        elif step[0] == "revert":
            service.revert(workspace, [int(item) for item in step[1:]], writer=_RPC)
        elif step[0] == "external":
            workspace.mkdir(exist_ok=True)
            (workspace / "MEMORY.md").write_text(
                "".join(f"- {entry}\n" for entry in step[1:]), encoding="utf-8"
            )
            service.history(workspace, "coder")
        else:
            service.remove_matching(workspace, "agent", step[1], writer=_TOOL)

    # Reload the persisted log, as after a restart: a cached state must not hide
    # a replay failure or cause a spurious external revision on the next read.
    recorded = service.recorded_revisions("coder")
    monkeypatch.setattr(MemoryHistory, "_logs", {})
    before = [entry.content for entry in service.list_entries(workspace, "agent")]
    assert service.entries_at(workspace, "coder", None)["agent"] == before
    assert service.history(workspace, "coder") == recorded
    reverted = service.revert(workspace, revert, writer=_RPC)

    monkeypatch.setattr(MemoryHistory, "_logs", {})
    assert service.entries_at(workspace, "coder", None)["agent"] == expected
    assert [entry.content for entry in service.list_entries(workspace, "agent")] == expected
    if reverted.revisions:
        service.revert(workspace, [r.id for r in reverted.revisions], writer=_RPC)
        assert service.entries_at(workspace, "coder", None)["agent"] == before


def test_revert_is_recorded_and_can_itself_be_reverted(
    service: MemoryService, workspace: Path
) -> None:
    service.add_entry(workspace, "agent", "Keep me.", writer=_TOOL)
    service.remove_matching(workspace, "agent", "Keep me.", writer=_TOOL)

    [reverted] = service.revert(workspace, [2], writer=_RPC).revisions
    service.revert(workspace, [reverted.id], writer=_RPC)

    assert (reverted.id, reverted.kind, reverted.reverts) == (3, "revert", (2,))
    assert _changes(reverted.to_dict()) == [("added", None, "Keep me.")]
    assert service.entries_at(workspace, "coder", None)["agent"] == []
    assert service.revert(workspace, [2, 4], writer=_RPC).changed == ("agent",)


def test_revert_refuses_to_overwrite_later_changes_and_changes_nothing(
    service: MemoryService, workspace: Path
) -> None:
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("- Baseline.\n", encoding="utf-8")
    service.add_entry(workspace, "agent", "Old wording.", writer=_TOOL)
    service.add_entry(workspace, "agent", "Unrelated.", writer=_TOOL)
    service.replace_matching(workspace, "agent", "Old wording", "New wording.", writer=_TOOL)
    service.replace_matching(workspace, "agent", "New wording", "Newest wording.", writer=_TOOL)
    before = (workspace / "MEMORY.md").read_text(encoding="utf-8")

    # The check refuses exactly like the revert would, before it writes.
    for attempt in (service.check_revert, service.revert):
        with pytest.raises(MemoryRevertError) as conflict:
            attempt(workspace, [3, 4], writer=_RPC)

        assert [(c.revision, c.text, c.later) for c in conflict.value.conflicts] == [
            (4, "New wording.", (5,))
        ]
        assert "revision 5 changed it since" in str(conflict.value)
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == before
    with pytest.raises(MemoryError, match="where the history starts"):
        service.revert(workspace, [1], writer=_RPC)
    with pytest.raises(MemoryError, match="revision 9 does not exist; revisions run from 1 to 5"):
        service.revert(workspace, [9], writer=_RPC)

    assert service.check_revert(workspace, [2, 4, 5], writer=_RPC) == ("agent",)
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == before
    assert len(service.recorded_revisions("coder")) == 5
    service.revert(workspace, [2, 4, 5], writer=_RPC)

    assert service.entries_at(workspace, "coder", None)["agent"] == ["Baseline.", "Unrelated."]


@pytest.mark.parametrize("later_action", ["remove", "replace"])
def test_revert_does_not_confuse_a_changed_occurrence_with_an_identical_survivor(
    service: MemoryService, workspace: Path, later_action: str
) -> None:
    workspace.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_text("- A\n- C\n- B\n", encoding="utf-8")
    service.history(workspace, "coder")
    replacement = service.replace_entry(workspace, "agent", 3, "C", writer=_RPC)
    assert replacement.revision is not None
    if later_action == "remove":
        service.remove_entry(workspace, "agent", 3, writer=_RPC)
    else:
        service.replace_entry(workspace, "agent", 3, "D", writer=_RPC)
    before = memory_file.read_bytes()
    revisions = service.recorded_revisions("coder")

    with pytest.raises(MemoryRevertError) as error:
        service.revert(workspace, [replacement.revision], writer=_RPC)

    assert [
        (conflict.revision, conflict.text, conflict.later) for conflict in error.value.conflicts
    ] == [(2, "C", (3,))]
    assert memory_file.read_bytes() == before
    assert service.recorded_revisions("coder") == revisions


def test_a_revert_whose_write_fails_changes_nothing(
    service: MemoryService, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.add_entry(workspace, "user", "Prefers German.", writer=_TOOL)
    service.add_entry(workspace, "agent", "Uses pytest.", writer=_TOOL)
    files = _memory_files(workspace)
    revisions = service.history(workspace, "coder")

    with monkeypatch.context() as patch:
        _fail_writes(patch, lambda path: path.name == "MEMORY.md")
        with pytest.raises(MemoryError) as failure:
            service.revert(workspace, [1, 2], writer=_RPC)

    assert not isinstance(failure.value, MemoryRevertIncompleteError)
    assert _memory_files(workspace) == files
    assert service.history(workspace, "coder") == revisions
    # Nothing half-done is left for a retry to trip over.
    service.revert(workspace, [1, 2], writer=_RPC)
    assert service.entries_at(workspace, "coder", None) == {"user": [], "agent": []}


def test_a_revert_that_cannot_be_undone_names_and_records_the_scopes_it_changed(
    service: MemoryService, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.add_entry(workspace, "user", "Prefers German.", writer=_TOOL)
    service.add_entry(workspace, "agent", "Uses pytest.", writer=_TOOL)
    writes: list[Path] = []

    def fails(path: Path) -> bool:
        # Only the first write succeeds: USER.md is reverted, then writing
        # MEMORY.md and restoring USER.md fail.
        writes.append(path)
        return len(writes) > 1

    with monkeypatch.context() as patch:
        _fail_writes(patch, fails)
        with pytest.raises(MemoryRevertIncompleteError) as incomplete:
            service.revert(workspace, [1, 2], writer=_RPC)

    assert incomplete.value.changed == ("user",)
    assert service.entries_at(workspace, "coder", None) == {
        "user": [],
        "agent": ["Uses pytest."],
    }
    # The scope left reverted is recorded, so reverting again takes back only the rest.
    [recorded] = incomplete.value.revisions
    assert service.history(workspace, "coder")[2:] == [recorded]
    assert (recorded.id, recorded.scope, recorded.kind, recorded.reverts) == (
        3,
        "user",
        "revert",
        (1,),
    )
    assert service.revert(workspace, [2], writer=_RPC).changed == ("agent",)
    assert service.entries_at(workspace, "coder", None) == {"user": [], "agent": []}


def _memory_files(workspace: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(workspace.iterdir())}


def _fail_writes(monkeypatch: pytest.MonkeyPatch, fails: Callable[[Path], bool]) -> None:
    """Make the Memory file writes that *fails* selects fail like a full disk."""
    write = memory_module.atomic_write_bytes

    def atomic_write_bytes(path: Path, data: bytes, **kwargs: Any) -> None:
        if fails(Path(path)):
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, data, **kwargs)

    monkeypatch.setattr(memory_module, "atomic_write_bytes", atomic_write_bytes)


def test_compare_describes_changes_between_two_states(
    service: MemoryService, workspace: Path
) -> None:
    service.add_entry(workspace, "agent", "A", writer=_TOOL)
    service.add_entry(workspace, "user", "U", writer=_TOOL)
    service.replace_matching(workspace, "agent", "A", "A2", writer=_TOOL)

    changes = service.compare(workspace, "coder", 1)

    assert [c.to_dict() for c in changes["agent"]] == [
        {"op": "replaced", "text": "A2", "index": 0, "previous": "A"}
    ]
    assert [c.to_dict() for c in changes["user"]] == [{"op": "added", "text": "U", "index": 0}]
    assert service.compare(workspace, "coder", 1, 2)["agent"] == ()


def test_memory_without_an_agent_directory_works_without_history(tmp_path: Path) -> None:
    service = MemoryService(history_root=tmp_path / "agents")
    workspace = tmp_path / "elsewhere"
    writer = MemoryWriter(agent_id="coder", actor="tool")

    service.add_entry(workspace, "agent", "Still saved.", writer=writer)

    assert [entry.content for entry in service.list_entries(workspace, "agent")] == ["Still saved."]
    assert not (tmp_path / "agents").exists()
    with pytest.raises(MemoryError, match="has no Memory history"):
        service.history(workspace, "coder")


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(b'{"v": 1, "id": "broken"}\nnot json\n', id="invalid-lines"),
        pytest.param('{"v":1,"id":2,"at":"ä'.encode()[:-1], id="torn-utf-8-sequence"),
        pytest.param(b'{"v":1,"id":2,"at":"', id="incomplete-last-line"),
        pytest.param(
            b'{"v":1,"id":2,"at":"x","scope":["agent"],"kind":"edit","actor":"tool","state":"s"}\n',
            id="mistyped-field",
        ),
    ],
)
def test_unreadable_history_lines_are_skipped(
    service: MemoryService, workspace: Path, agents_root: Path, damage: bytes
) -> None:
    service.add_entry(workspace, "agent", "A", writer=_TOOL)
    history_file = agents_root / "coder" / "memory-history.jsonl"
    with history_file.open("ab") as handle:
        handle.write(damage)

    service.add_entry(workspace, "agent", "B", writer=_TOOL)

    assert [(r.id, r.kind, r.run_id) for r in service.history(workspace, "coder")] == [
        (1, "edit", "r-1"),
        (2, "edit", "r-1"),
    ]
    # The new revision is a line of its own, so it is read back after a restart.
    last = json.loads(history_file.read_bytes().splitlines()[-1])
    assert (last["id"], last["actor"], last["run_id"]) == (2, "tool", "r-1")


@pytest.mark.parametrize(
    "error",
    [MemoryError("failed to write Memory history"), RuntimeError("history defect")],
    ids=["history-io-failure", "unexpected-history-error"],
)
def test_a_failing_history_never_fails_a_change(
    service: MemoryService,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    service.add_entry(workspace, "user", "Prefers German.", writer=_TOOL)
    service.add_entry(workspace, "agent", "Uses pytest.", writer=_TOOL)

    def failing_record(*_args: object, **_kwargs: object) -> None:
        raise error

    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="vbot.memory"):
        patch.setattr(MemoryHistory, "record", failing_record)
        added = service.add_entry(workspace, "agent", "Deploys from main.", writer=_TOOL)
        reverted = service.revert(workspace, [1, 2], writer=_RPC)

    # The files changed although the history recorded nothing.
    assert (added.revision, reverted.changed, reverted.revisions) == (
        None,
        ("user", "agent"),
        (),
    )
    # Every change the history missed is noticed as an external one.
    assert [(r.id, r.scope, r.kind) for r in service.history(workspace, "coder")[2:]] == [
        (3, "agent", "external"),
        (4, "user", "external"),
        (5, "agent", "external"),
    ]
    assert service.entries_at(workspace, "coder", None) == {
        "user": [],
        "agent": ["Deploys from main."],
    }
    logged = [r for r in caplog.records if r.name == "vbot.memory" and r.levelno >= logging.WARNING]
    assert len(logged) == 3
