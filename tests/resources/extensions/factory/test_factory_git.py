"""Fixed-argv, bounded Git inspection and exact submitted commit facts."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import override

import pytest

from resources.extensions.factory import _git
from resources.extensions.factory._git import GitRepository
from resources.extensions.factory.store import FactoryError

_HEAD = "a" * 40
_BASE = "b" * 40
_MERGE = "c" * 40
_MOVED = "d" * 40
_TARGET = "e" * 40
_LANDING = "f" * 40
_LATER = "1" * 40
_ALIEN = "2" * 40


class Repository(GitRepository):
    def __init__(self, *, branch=True, base=True, count="2", merge=True, merged=False):
        super().__init__("C:/repo")
        self.branch_exists = branch
        self.base_exists = base
        self.count = count
        self.common = merge
        self.merged = merged
        self.commands = []

    @override
    async def _run(self, *arguments: str, allowed=(0,)):
        self.commands.append(arguments)
        command = arguments[0]
        if command == "check-ref-format":
            return 0, arguments[-1]
        if command == "rev-parse":
            branch = arguments[-1].startswith("refs/heads/")
            commit = arguments[-1].removesuffix("^{commit}")
            return (
                (0, _HEAD if branch else commit if commit in {_BASE, _MERGE} else _BASE)
                if (self.branch_exists if branch else self.base_exists)
                else (128, "")
            )
        if command == "rev-list":
            return 0, "0" if self.merged and arguments[-1].startswith(_BASE) else self.count
        return (0, _MERGE) if self.common else (1, "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({}, None),
        ({"branch": False}, "missing_branch"),
        ({"base": False}, "missing_base"),
        ({"count": "0"}, "no_commits"),
        ({"merge": False}, "unrelated_history"),
    ],
)
async def test_submission_resolves_local_branch_base_and_nonempty_exact_range(options, code):
    repository = Repository(**options)
    if code:
        with pytest.raises(FactoryError) as caught:
            await repository.submission("topic", "main")
        assert caught.value.code == code
    else:
        assert await repository.submission("topic", "main") == {
            "branch": "topic",
            "base": "main",
            "base_sha": _MERGE,
            "head_sha": _HEAD,
        }
        assert ("rev-list", "--count", f"{_BASE}..{_HEAD}") in repository.commands
        assert (
            "rev-parse",
            "--verify",
            "--end-of-options",
            "refs/heads/topic^{commit}",
        ) in repository.commands


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "merged",
        "missing_review_base",
        "rewritten_head",
        "diverged_review_base",
        "invalid_review_base",
    ],
)
async def test_submission_pins_captured_head_and_distinguishes_diverged_review_base(case):
    repository = Repository(
        base=case != "missing_review_base",
        merge=case != "rewritten_head",
        merged=case != "diverged_review_base",
    )
    review_base = (
        "--unsafe"
        if case == "invalid_review_base"
        else _BASE
        if case == "diverged_review_base"
        else _MERGE
    )
    if case == "merged":
        # The mutable base already contains the moved head, but this review's
        # original base still names a nonempty exact range.
        assert await repository.submission(
            "topic", "main", head_sha=_MOVED, base_sha=review_base
        ) == {
            "branch": "topic",
            "base": "main",
            "base_sha": _MERGE,
            "head_sha": _MOVED,
        }
        assert ("rev-list", "--count", f"{_MERGE}..{_MOVED}") in repository.commands
        assert ("merge-base", _MERGE, _MOVED) in repository.commands
    else:
        with pytest.raises(FactoryError) as caught:
            await repository.submission("topic", "main", head_sha=_MOVED, base_sha=review_base)
        assert (
            caught.value.code
            == {
                "missing_review_base": "missing_base",
                "rewritten_head": "unrelated_history",
                "diverged_review_base": "review_base_changed",
                "invalid_review_base": "git_failed",
            }[case]
        )
    # The snapshot is not silently replaced by a second branch/base lookup.
    assert not any(
        command[0] == "rev-parse" and command[-1] in {"refs/heads/topic^{commit}", "main^{commit}"}
        for command in repository.commands
    )


class GraphRepository(GitRepository):
    """Small immutable commit DAG, with the real owner's Git argument protocol."""

    def __init__(self, *, branch=_HEAD, target=_TARGET, reflog=()):
        super().__init__("C:/repo")
        self.branch = branch
        self.target = target
        self.reflog = reflog
        self.parents = {
            _MERGE: (),
            _BASE: (_MERGE,),
            _HEAD: (_BASE,),
            _TARGET: (_BASE,),
            _MOVED: (_TARGET,),
            _LANDING: (_TARGET, _HEAD),
            _LATER: (_LANDING,),
            _ALIEN: (),
        }
        self.changed_paths = {(_BASE, _HEAD): {"feature.txt"}}
        self.combined_paths = set()
        self.commands = []

    def ancestors(self, head):
        result = {head}
        for parent in self.parents[head]:
            result.update(self.ancestors(parent))
        return result

    @override
    async def _run(self, *arguments: str, allowed=(0,)):
        self.commands.append(arguments)
        command = arguments[0]
        if command == "check-ref-format":
            return 0, arguments[-1]
        if command == "rev-parse":
            if arguments[1].startswith("--disambiguate="):
                prefix = arguments[1].split("=", 1)[1]
                return 0, "\n".join(sha for sha in self.parents if sha.startswith(prefix))
            name = arguments[-1].removesuffix("^{commit}")
            resolved = {"refs/heads/topic": self.branch, "main": self.target}.get(name, name)
            return (0, resolved) if resolved in self.parents else (128, "")
        if command == "merge-base":
            left, right = arguments[-2:]
            if arguments[1] == "--is-ancestor":
                return (0 if left in self.ancestors(right) else 1), ""
            common = self.ancestors(left) & self.ancestors(right)
            closest = [
                sha
                for sha in common
                if not any(sha != other and sha in self.ancestors(other) for other in common)
            ]
            return (0, closest[0]) if closest else (1, "")
        if command == "rev-list":
            left, right = arguments[-1].split("..")
            if arguments[1] == "--count":
                return 0, str(len(self.ancestors(right) - self.ancestors(left)))
            assert arguments[1:4] == ("--first-parent", "--ancestry-path", "--parents")
            limit = int(arguments[-2].split("=")[1])
            rows = []
            while right not in self.ancestors(left):
                parents = self.parents[right]
                if left in self.ancestors(right):
                    rows.append(" ".join((right, *parents)))
                if not parents or len(rows) == limit:
                    break
                right = parents[0]
            return 0, "\n".join(rows)
        if command == "reflog":
            return 0, "\n".join(self.reflog)
        if command == "diff":
            assert arguments[1:4] == ("--name-only", "--no-renames", "-z")
            paths = self.changed_paths[arguments[-3:-1]]
        elif command == "diff-tree":
            assert "--cc" in arguments and "-z" in arguments
            paths = self.combined_paths
        else:
            raise AssertionError(arguments)
        return 0, "".join(f"{path}\x00" for path in sorted(paths))


def assert_single_snapshot(repository):
    assert (
        repository.commands.count(
            ("rev-parse", "--verify", "--end-of-options", "refs/heads/topic^{commit}")
        )
        == 1
    )
    assert (
        repository.commands.count(("rev-parse", "--verify", "--end-of-options", "main^{commit}"))
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topology", "verify_merged"), [("advanced", True), ("rewritten", True), ("advanced", False)]
)
async def test_integration_reviews_moved_head_against_pinned_current_target(
    topology, verify_merged
):
    repository = GraphRepository(branch=_MOVED)
    if topology == "rewritten":
        repository.parents[_TARGET] = (_MERGE,)
    facts = await repository.integration(
        "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD, verify_merged=verify_merged
    )
    assert facts == {
        "branch_head_sha": _MOVED,
        "target_sha": _TARGET,
        "landed": False,
        "review_required": True,
        "base_sha": _TARGET,
        "head_sha": _MOVED,
        "landing_sha": "",
        "extra_paths": [],
        "merge_paths": [],
        "base_source": "merge_base",
    }
    assert_single_snapshot(repository)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "source", "review_base"),
    [
        ("explicit", "base_commit", _TARGET),
        ("reflog", "reflog", _TARGET),
        ("later_target", "reflog", _TARGET),
        ("newer_reflog_update", "reflog", _TARGET),
        ("snapshot", "accepted_base", _TARGET),
        ("missing_reflog_snapshot", "accepted_base", _TARGET),
        ("unknown", "unknown", ""),
        ("empty_snapshot", "unknown", ""),
        ("diverged_snapshot", "unknown", ""),
    ],
)
async def test_landed_rewrite_keeps_visible_facts_and_recovers_only_nonempty_bases(
    case, source, review_base
):
    repository = GraphRepository(branch=_MOVED, target=_MOVED)
    options = {}
    if case == "explicit":
        options["base_commit"] = _TARGET
    elif case in {"reflog", "later_target", "newer_reflog_update"}:
        repository.reflog = (_MOVED, _TARGET, _BASE)
        if case != "reflog":
            repository.parents[_LATER] = (_MOVED,)
            repository.reflog = (_LATER, *repository.reflog)
        if case == "later_target":
            repository.target = _LATER
    elif case in {"snapshot", "missing_reflog_snapshot", "diverged_snapshot"}:
        options["accepted_base"] = _TARGET
        if case == "missing_reflog_snapshot":
            repository.reflog = (_TARGET, _BASE)
        if case == "diverged_snapshot":
            repository.parents[_MOVED] = (_MERGE,)
    elif case == "empty_snapshot":
        options["accepted_base"] = _MOVED
    facts = await repository.integration(
        "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD, **options
    )
    assert facts["landed"] and facts["review_required"]
    assert facts["base_sha"] == review_base and facts["base_source"] == source
    assert facts["branch_head_sha"] == facts["head_sha"] == facts["landing_sha"] == _MOVED
    assert facts["target_sha"] == repository.target
    assert_single_snapshot(repository)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_commit", "code"),
    [
        ("--unsafe", "git_failed"),
        ("3" * 40, "missing_base"),
        (_HEAD, "review_base_changed"),
        (_ALIEN, "unrelated_history"),
        (_MOVED, "no_commits"),
    ],
)
async def test_explicit_integration_base_requires_an_existing_nonempty_ancestor(base_commit, code):
    repository = GraphRepository(branch=_MOVED, target=_MOVED)
    with pytest.raises(FactoryError) as caught:
        await repository.integration(
            "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD, base_commit=base_commit
        )
    assert caught.value.code == code
    if base_commit == "--unsafe":
        assert not repository.commands


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "clean",
        "extra_paths",
        "combined_paths",
        "moved_clean",
        "moved_extra_paths",
        "moved_combined_paths",
    ],
)
async def test_integration_checks_first_parent_merge_scope_and_does_not_repeat_its_review(case):
    repository = GraphRepository(target=_LATER)
    moved = case.startswith("moved_")
    options = {}
    if moved:
        repository.branch = _MOVED
        repository.parents[_MOVED] = (_HEAD,)
        repository.parents[_LANDING] = (_TARGET, _MOVED)
        # The actual pre-merge target is not an ancestor of the feature tip,
        # but it is the exact first parent of the landing merge under review.
        options["base_commit"] = _TARGET
    repository.changed_paths[(_TARGET, _LANDING)] = {"feature.txt"}
    if case.endswith("extra_paths"):
        repository.changed_paths[(_BASE, _HEAD)].update({"boundary ", "raw\udcfe"})
        repository.changed_paths[(_TARGET, _LANDING)].update({" boundary ", "raw\udcff"})
    if case.endswith("combined_paths"):
        repository.combined_paths = {"feature.txt"}
    facts = await repository.integration(
        "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD, **options
    )
    assert facts["landed"] and facts["landing_sha"] == _LANDING
    assert facts["review_required"] == (case != "clean")
    assert facts["branch_head_sha"] == (_MOVED if moved else _HEAD)
    assert facts["extra_paths"] == (
        [" boundary ", '"raw\\udcff"'] if case.endswith("extra_paths") else []
    )
    assert facts["merge_paths"] == (["feature.txt"] if case.endswith("combined_paths") else [])
    json.dumps(facts, ensure_ascii=False).encode("utf-8")
    assert_single_snapshot(repository)
    if case == "clean":
        assert (facts["base_sha"], facts["head_sha"]) == (_BASE, _HEAD)
        return
    assert (facts["base_sha"], facts["head_sha"], facts["base_source"]) == (
        _TARGET,
        _LANDING,
        "landing_parent",
    )
    reviewed = await repository.integration(
        "topic",
        "main",
        reviewed_base=_TARGET,
        reviewed_head=_LANDING,
        expected_branch_head=_MOVED if moved else _HEAD,
    )
    assert reviewed["landed"] and not reviewed["review_required"]
    assert reviewed["landing_sha"] == reviewed["head_sha"] == _LANDING


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", [_HEAD, None])
async def test_reference_only_unmoved_or_cleaned_branch_does_not_inspect_target(branch):
    repository = GraphRepository(branch=branch, target=None)
    facts = await repository.integration(
        "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD, verify_merged=False
    )
    assert not facts["review_required"] and not facts["landed"]
    assert facts["target_sha"] == facts["landing_sha"] == ""
    assert not any(
        command[0] != "check-ref-format" and command[-1] != "refs/heads/topic^{commit}"
        for command in repository.commands
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", [_HEAD, None])
async def test_fast_forwarded_reviewed_head_remains_verifiable_after_branch_cleanup(branch):
    repository = GraphRepository(branch=branch, target=_LATER)
    repository.parents[_LATER] = (_HEAD,)
    facts = await repository.integration("topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD)
    assert facts["landed"] and not facts["review_required"]
    assert facts["branch_head_sha"] == branch
    assert facts["landing_sha"] == _HEAD
    assert_single_snapshot(repository)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "full",
        "prefix",
        "uppercase",
        "not_landed",
        "ambiguous",
        "missing",
        "url",
        "text",
        "same_head",
    ],
)
async def test_removed_branch_uses_only_a_resolved_commit_hint_to_recover_rewritten_work(case):
    repository = GraphRepository(branch=None, target=_MOVED, reflog=(_MOVED, _TARGET))
    reference = _MOVED
    if case in {"prefix", "ambiguous", "not_landed"}:
        reference = _MOVED[:7]
    if case == "uppercase":
        reference = _MOVED[:12].upper()
    if case == "not_landed":
        repository.target = _TARGET
    if case == "ambiguous":
        repository.parents[_MOVED[:7] + "0" * 33] = (_BASE,)
    if case == "missing":
        reference = "3" * 40
    if case == "url":
        reference = "https://example.invalid/pull/123"
    if case == "text":
        reference = "main"
    if case == "same_head":
        reference = _HEAD[:12]
    facts = await repository.integration(
        "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD, reported_reference=reference
    )
    recovered = case in {"full", "prefix", "uppercase", "not_landed"}
    assert facts["branch_head_sha"] is None
    assert facts["head_sha"] == (_MOVED if recovered else _HEAD)
    assert facts["review_required"] == recovered
    assert facts["landed"] == (recovered and case != "not_landed")
    if recovered:
        assert facts["base_sha"] == _TARGET
        assert facts["base_source"] == ("merge_base" if case == "not_landed" else "reflog")
    assert_single_snapshot(repository)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [False, True])
async def test_cleaned_ordinary_merge_reference_does_not_replace_the_reviewed_feature_head(extra):
    repository = GraphRepository(branch=None, target=_LATER)
    repository.changed_paths[(_TARGET, _LANDING)] = {"feature.txt"}
    if extra:
        repository.changed_paths[(_TARGET, _LANDING)].add("outside.txt")
    facts = await repository.integration(
        "topic",
        "main",
        reviewed_base=_BASE,
        reviewed_head=_HEAD,
        reported_reference=_LANDING[:12],
    )
    assert facts["landed"] and facts["branch_head_sha"] is None
    assert facts["review_required"] == extra
    assert facts["landing_sha"] == _LANDING
    assert facts["head_sha"] == (_LANDING if extra else _HEAD)
    assert facts.get("base_source") == ("landing_parent" if extra else None)
    assert_single_snapshot(repository)


@pytest.mark.asyncio
async def test_unrelated_moved_branch_is_not_misrepresented_as_a_new_review_base():
    repository = GraphRepository(branch=_ALIEN)
    with pytest.raises(FactoryError) as caught:
        await repository.integration("topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD)
    assert caught.value.code == "unrelated_history"


@pytest.mark.asyncio
async def test_landing_inspection_fails_closed_instead_of_accepting_a_truncated_history():
    repository = GraphRepository()
    previous = _HEAD
    for index in range(_git._HISTORY_LIMIT + 1):
        commit = f"{index:040x}"
        repository.parents[commit] = (previous,)
        previous = commit
    repository.target = previous
    with pytest.raises(FactoryError) as caught:
        await repository.integration("topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD)
    assert caught.value.code == "git_output_limit"


@pytest.mark.asyncio
async def test_entire_integration_inspection_has_one_deadline_not_one_per_reflog_step(monkeypatch):
    repository = GraphRepository(branch=_MOVED, target=_MOVED)
    original = repository._run
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_at_reflog(*arguments, **kwargs):
        if arguments[0] == "reflog":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(*arguments, **kwargs)

    monkeypatch.setattr(repository, "_run", wait_at_reflog)
    monkeypatch.setattr(_git, "_TIMEOUT", 0)
    with pytest.raises(FactoryError) as caught:
        await repository.integration("topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD)
    assert caught.value.code == "git_timeout"
    assert started.is_set() and cancelled.is_set()


@pytest.fixture(scope="module")
def real_graph(tmp_path_factory):
    """One tiny object graph verifies real Git's ancestry, -z and reflog semantics."""
    cwd = tmp_path_factory.mktemp("factory-git-graph")
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }

    def git(*arguments, data=None):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=cwd,
            env=environment,
            input=data,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    git("init", "--bare", "--quiet", "--initial-branch=main")
    blob = git("hash-object", "-w", "--stdin", data=b"payload\n")

    def commit(names, *parents):
        tree = git(
            "mktree",
            "-z",
            data=b"".join(
                b"100644 blob " + blob + b"\t" + name + b"\x00" for name in sorted(names)
            ),
        )
        parent_args = [argument for parent in parents for argument in ("-p", parent)]
        return git("commit-tree", tree.decode(), *parent_args, data=b"graph fixture\n").decode()

    root_paths = {b"root.txt"}
    root = commit(root_paths)
    base_paths = root_paths | {b"base.txt"}
    base = commit(base_paths, root)
    reviewed_paths = base_paths | {b"feature.txt", b"raw\xfe"}
    head = commit(reviewed_paths, base)
    target_paths = base_paths | {b"main-only.txt"}
    target = commit(target_paths, base)
    rebased = commit(target_paths | reviewed_paths, target)
    landing = commit(target_paths | reviewed_paths | {b" edge ", b"raw\xff"}, target, head)
    for name, sha in (
        ("merge/topic", head),
        ("merge/main", landing),
        ("rebase/topic", rebased),
        ("rebase/main", target),
    ):
        git("update-ref", "--create-reflog", f"refs/heads/{name}", sha)
    git(
        "update-ref",
        "--create-reflog",
        "-m",
        "integration",
        "refs/heads/rebase/main",
        rebased,
        target,
    )
    return cwd, {
        "base": base,
        "head": head,
        "target": target,
        "rebased": rebased,
        "landing": landing,
    }


@pytest.mark.asyncio
async def test_real_git_merge_scope_preserves_whitespace_and_non_utf8_path_identities(real_graph):
    cwd, graph = real_graph
    repository = GitRepository(cwd)
    facts = await repository.integration(
        "merge/topic", "merge/main", reviewed_base=graph["base"], reviewed_head=graph["head"]
    )
    assert facts["landed"] and facts["review_required"]
    assert (facts["base_sha"], facts["head_sha"]) == (graph["target"], graph["landing"])
    assert facts["extra_paths"] == facts["merge_paths"] == [" edge ", '"raw\\udcff"']
    json.dumps(facts, ensure_ascii=False).encode("utf-8")
    reviewed = await repository.integration(
        "merge/topic",
        "merge/main",
        reviewed_base=graph["target"],
        reviewed_head=graph["landing"],
        expected_branch_head=graph["head"],
    )
    assert reviewed["landed"] and not reviewed["review_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleaned", [False, True])
async def test_real_git_reflog_recovers_the_pre_fast_forward_rebase_target(real_graph, cleaned):
    cwd, graph = real_graph
    facts = await GitRepository(cwd).integration(
        "removed/topic" if cleaned else "rebase/topic",
        "rebase/main",
        reviewed_base=graph["base"],
        reviewed_head=graph["head"],
        reported_reference=graph["rebased"][:12] if cleaned else None,
    )
    assert facts["landed"] and facts["review_required"]
    assert facts["head_sha"] == graph["rebased"]
    assert facts["branch_head_sha"] == (None if cleaned else graph["rebased"])
    assert facts["base_sha"] == graph["target"] and facts["base_source"] == "reflog"


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["--help", "@{-1}", "", "a\x00b"])
async def test_branch_expressions_and_options_never_reach_git(branch):
    repository = Repository()
    with pytest.raises(FactoryError) as caught:
        await repository.submission(branch, "main")
    assert caught.value.code == "invalid_ref" and not repository.commands


class Process:
    def __init__(self, output: bytes, *, running=False):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        if not running:
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = None if running else 0
        self.done = asyncio.Event()
        self.killed = False
        if not running:
            self.done.set()

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.stdout.feed_eof()
        self.done.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["normal", "paths", "output", "timeout", "inspection_timeout", "missing"]
)
async def test_git_process_is_fixed_argv_bounded_and_terminated_on_failure(
    monkeypatch, tmp_path, case
):
    process = Process(
        b"x" * (_git._OUTPUT_LIMIT + 1)
        if case == "output"
        else b" leading \x00raw\xff\x00raw\xfe\x00"
        if case == "paths"
        else b"result",
        running=case in {"timeout", "inspection_timeout", "output"},
    )
    calls = []

    async def launch(*arguments, **kwargs):
        calls.append((arguments, kwargs))
        if case == "missing":
            raise FileNotFoundError("git")
        return process

    monkeypatch.setenv("GIT_DIR", "wrong-repository")
    monkeypatch.setattr(_git.asyncio, "create_subprocess_exec", launch)
    if case in {"timeout", "inspection_timeout"}:
        monkeypatch.setattr(_git, "_TIMEOUT", 0)
    repository = GitRepository(tmp_path)
    if case in {"normal", "paths"}:
        assert await repository._run("rev-parse", "--show-toplevel") == (
            0,
            " leading \x00raw\udcff\x00raw\udcfe\x00" if case == "paths" else "result",
        )
    else:
        with pytest.raises(FactoryError) as caught:
            if case == "inspection_timeout":
                await repository.integration(
                    "topic", "main", reviewed_base=_BASE, reviewed_head=_HEAD
                )
            else:
                await repository._run("rev-parse", "--show-toplevel")
        assert (
            caught.value.code
            == {
                "output": "git_output_limit",
                "timeout": "git_timeout",
                "inspection_timeout": "git_timeout",
                "missing": "git_unavailable",
            }[case]
        )
        if case != "missing":
            assert process.killed
    assert calls[0][0][0] == "git" and "--no-optional-locks" in calls[0][0]
    assert "GIT_DIR" not in calls[0][1]["env"]
    assert calls[0][1]["cwd"] == tmp_path
