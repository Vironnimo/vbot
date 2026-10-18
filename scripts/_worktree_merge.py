"""Merge landing and repair transactions; main moves only after a checked commit."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from scripts import _worktree_records as worktree_records
from scripts._webui_packages import installed_differences
from scripts._worktree_context import (
    WorktreeContext,
    _npm_command,
    print_error,
    print_ok,
    validate_worktree_name,
)
from scripts._worktree_lock import (
    KIND_MERGE,
    KIND_REPAIR,
    MergeLockBusyError,
    _extended_repair_window,
    _holder_record_is_current,
    _lease_path,
    _merge_exclusive_lock,
    _own_repair_window_is_active,
    _probe_lock_is_busy,
    _read_holder_record,
    _repair_window_is_extended,
    _repair_window_stays_open,
    _request_window_release,
)
from scripts._worktree_records import (
    UNKNOWN_VALUE,
    WORKTREE_FILE_NAME,
)
from scripts._worktree_removal import LANDING_DIR_PREFIX, WorktreeRemoval
from scripts._worktree_seed import (
    seed_type_check_cache,
    seed_webui_packages,
)

PRIMARY_BRANCH = "main"
MERGE_LOCK_FILE_NAME = "vbot-merge.lock"
MERGE_HOLDER_FILE_NAME = "vbot-merge.lock.holder.json"
MERGE_RELEASE_FILE_NAME = "vbot-merge.lock.release"

# A merge whose base main left behind while its commit was checked merges again
# onto the new main, up to this many checks in all.
LANDING_ATTEMPTS = 3
# git refuses to fast-forward main while another git command holds its index lock.
FAST_FORWARD_ATTEMPTS = 5
FAST_FORWARD_RETRY_SECONDS = 0.5
REPAIR_LOG_FILE_NAME = "vbot-merge-repair.log"
MERGE_CONFLICT_EXIT_CODE = 2
# Locks the WebUI packages; a merge that changes it replaces main's node_modules.
WEBUI_LOCK_FILE = "webui/package-lock.json"


class WorktreeMerge:
    """Own the protected merge transaction and its repair-window lifecycle."""

    def __init__(self, context: WorktreeContext, removal: WorktreeRemoval) -> None:
        self.context = context
        self.removal = removal

    def _merge_lock_paths(self) -> tuple[Path, Path, Path]:
        """Resolve merge lock, holder record, and release signal paths."""
        git_dir = self.context.git_common_dir()
        return (
            git_dir / MERGE_LOCK_FILE_NAME,
            git_dir / MERGE_HOLDER_FILE_NAME,
            git_dir / MERGE_RELEASE_FILE_NAME,
        )

    def _read_primary_branch(self) -> str | None:
        """Read the currently checked-out branch of the primary checkout."""
        return worktree_records._read_worktree_branch_name(self.context.project_root)

    def _list_conflicted_paths(self, repo_path: Path) -> list[str]:
        """List unmerged paths during an unresolved merge in a repository."""
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "diff",
                    "--name-only",
                    "--diff-filter=U",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []

        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _merge_conflicts(self, branch: str) -> list[str] | None:
        """Return the paths merging *branch* into main would conflict on, without touching main.

        None when git cannot tell; the merge itself then finds any conflict.
        """
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.context.project_root),
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                ]
                + ["--no-messages", PRIMARY_BRANCH, branch],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if result.returncode != 1:
            return [] if result.returncode == 0 else None
        # The first line names the merged tree; the conflicted paths follow.
        return [line for line in result.stdout.splitlines()[1:] if line.strip()]

    def _print_merge_conflict_hints(self, name: str, *, window_open: bool) -> None:
        """Print the agent-facing recovery hints after a conflicted merge."""
        print(f"hint: freeze main first: python scripts/worktree.py repair-start {name}")
        print(
            "hint: bring main into your branch (git rebase main), resolve the conflicts, and commit"
        )
        print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
        if window_open:
            print("note: your protected repair window stays open while you fix this")

    def _print_merge_check_hints(self, name: str, *, window_open: bool) -> None:
        """Print the agent-facing recovery hints after the merge commit failed its check."""
        print("hint: the commit check rejected the merged result (report above); main is unchanged")
        print(
            "hint: bring main into your branch (git rebase main), fix the reported "
            "problems, and commit"
        )
        print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
        if window_open:
            print("note: your protected repair window stays open while you fix this")

    def _git_stdout(self, repo: Path, *arguments: str) -> str | None:
        """Return the output of a git command in *repo*, or None when it fails."""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None

    def _nul_separated(self, output: str) -> set[str]:
        """Return the paths of a ``-z`` git listing."""
        return {path for path in output.split("\0") if path}

    def _main_head(self) -> str | None:
        """Return the commit the primary checkout has checked out."""
        head = self._git_stdout(self.context.project_root, "rev-parse", "-q", "--verify", "HEAD")
        return head.strip() if head else None

    def _merge_changes_since(self, tree: str, merge_paths: set[str]) -> list[str] | None:
        """Return the paths of a merge staged in main that changed after it was staged.

        *tree* is the merge's result and *merge_paths* the paths it changed. None when
        git cannot tell.
        """
        staged = self._git_stdout(
            self.context.project_root, "diff", "--cached", "--name-only", "--no-renames", "-z", tree
        )
        unstaged = self._git_stdout(
            self.context.project_root, "diff", "--name-only", "--no-renames", "-z"
        )
        deleted = self._git_stdout(
            self.context.project_root,
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=D",
            "-z",
            "HEAD",
            tree,
        )
        if staged is None or unstaged is None or deleted is None:
            return None
        changed = merge_paths & (self._nul_separated(staged) | self._nul_separated(unstaged))
        # A file the merge deleted and someone created again is untracked.
        changed |= {
            path
            for path in self._nul_separated(deleted)
            if os.path.lexists(self.context.project_root / path)
        }
        return sorted(changed)

    def _roll_back_unfinished_merge(self) -> bool:
        """Undo a merge an interrupted earlier merge left staged in main; return whether none is
        left.

        Merges used to be staged in main for their commit check, so a killed merge of
        such a version leaves one behind. Only the paths that merge changed are
        restored, and only while each is exactly as the merge left it: other sessions'
        staged and unstaged work stays. Otherwise the merge stays and the reason is
        printed.
        """
        merge_head_path = self.context.project_root / ".git" / "MERGE_HEAD"
        if not merge_head_path.exists():
            return True
        try:
            merge_heads = merge_head_path.read_text(encoding="utf-8").split()
        except OSError:
            merge_heads = []
        head = self._main_head()
        merge_paths: set[str] = set()
        changed: list[str] | None = []
        reason = None
        if head is None or len(merge_heads) != 1:
            reason = "it does not merge a single branch"
        else:
            merged = self._git_stdout(
                self.context.project_root,
                "merge-tree",
                "--write-tree",
                "--no-messages",
                head,
                merge_heads[0],
            )
            tree = merged.split("\n", 1)[0].strip() if merged else ""
            paths = self._git_stdout(
                self.context.project_root, "diff", "--name-only", "--no-renames", "-z", head, tree
            )
            if not tree or paths is None:
                reason = "its result cannot be computed without conflicts"
            else:
                merge_paths = self._nul_separated(paths)
                changed = self._merge_changes_since(tree, merge_paths)
                if changed is None:
                    reason = "git cannot compare it with the primary checkout"
                elif changed:
                    reason = "files it changed were changed again since"
        if reason is not None:
            print_error(
                f"primary checkout has an unfinished merge that cannot be rolled back "
                f"safely: {reason}"
            )
            for path in changed or []:
                print(f"changed-since-merge: {path}")
            print(
                "hint: conclude or undo that merge in the primary checkout by hand; "
                "`git merge --abort` also discards other sessions' staged changes"
            )
            return False

        # Without MERGE_HEAD, a commit concluding that merge can no longer record it.
        self.context.run_command(["git", "-C", str(self.context.project_root), "merge", "--quit"])
        if self._main_head() != head:
            # A commit concluded it meanwhile, such as a killed merge's commit check.
            return True
        if merge_paths:
            try:
                result = subprocess.run(
                    ["git", "-C", str(self.context.project_root), "--literal-pathspecs", "restore"]
                    + ["--source=HEAD", "--staged", "--worktree"]
                    + ["--pathspec-from-file=-", "--pathspec-file-nul"],
                    input="\0".join(sorted(merge_paths)) + "\0",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                failure = result.stderr.strip() if result.returncode != 0 else None
            except OSError as exc:
                failure = str(exc)
            if failure is not None:
                print_error(
                    f"the unfinished merge in the primary checkout was not rolled back: {failure}"
                )
                return False
        print(
            "recovered: rolled back an unfinished merge left in the primary checkout "
            f"({len(merge_paths)} files)"
        )
        return True

    def _create_landing_checkout(self, landing: Path, worktree_path: Path) -> str | None:
        """Check main out detached in *landing*; return why that failed, or None.

        The type checker's cache comes from the task's worktree, else from the primary
        checkout.
        """
        return_code, stderr = self.context.run_command(
            ["git", "worktree", "add", "--detach", str(landing), PRIMARY_BRANCH]
        )
        if return_code != 0:
            return stderr or "git worktree add failed"
        for source in (worktree_path, self.context.project_root):
            if seed_type_check_cache(source, landing):
                break
        return None

    def _provide_webui_packages(self, landing: Path, worktree_path: Path) -> str | None:
        """Install the merged WebUI packages in *landing* when its commit check needs them.

        The commit check formats and lints the merge's WebUI sources on packages
        matching the merged lock, when main has installed packages. A matching
        installation of main or of the task's worktree is copied; otherwise ``npm ci``
        installs one. Returns why that failed, or None.
        """
        from scripts import commit_check

        if not (self.context.project_root / "webui" / "node_modules").is_dir():
            return None
        staged = self._git_stdout(
            landing, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"
        )
        scope = commit_check.webui_scope(self._nul_separated(staged or ""))
        if not (scope.sources or scope.all_styles):
            return None
        webui = landing / "webui"
        node_modules = webui / "node_modules"
        if node_modules.is_dir():
            if not installed_differences(node_modules, webui / "package-lock.json"):
                return None
            self.removal.remove_directory_tree(node_modules)
        for source in (self.context.project_root, worktree_path):
            if seed_webui_packages(source, landing):
                return None
        print(
            "installing the merged webui dependencies (npm ci, no output until done)...", flush=True
        )
        return_code, stderr = self.context.run_command([_npm_command(), "ci"], cwd=webui)
        if return_code == 0:
            return None
        return stderr or f"npm ci exited with code {return_code}"

    def _commit_landing(self, landing: Path, message: str) -> tuple[int, str]:
        """Commit the merge staged in *landing* through the commit check."""
        try:
            result = subprocess.run(
                ["git", "-C", str(landing), "commit", "-m", message],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            return 1, str(exc)
        return result.returncode, result.stderr.strip()

    def _fast_forward_main(self, commit: str) -> tuple[int, str]:
        """Move main to *commit*; git refuses when main moved or local changes overlap."""
        return_code, stderr = 1, ""
        for attempt in range(FAST_FORWARD_ATTEMPTS):
            if attempt:
                time.sleep(FAST_FORWARD_RETRY_SECONDS)
            return_code, stderr = self.context.run_command(
                ["git", "-C", str(self.context.project_root), "merge", "--ff-only", "-q", commit]
            )
            if return_code == 0 or "index.lock" not in stderr:
                break
        return return_code, stderr

    def _install_webui_packages(self, action: str) -> str | None:
        """Install the packages main's WebUI lock names; return why that failed, or None."""
        print(f"{action} (npm ci, no output until done)...", flush=True)
        return_code, stderr = self.context.run_command(
            [_npm_command(), "ci"], cwd=self.context.project_root / "webui"
        )
        if return_code == 0:
            return None
        return stderr or f"npm ci exited with code {return_code}"

    @contextmanager
    def _merge_protection(self, name: str, wait_timeout: float) -> Iterator[bool]:
        """Keep every other merge and repair window off main until this landing is done.

        Yields whether this task's own repair window does that: its keeper holds the
        merge lock, past the window's deadline if need be, while this merge holds the
        window's lease. Otherwise the merge holds the merge lock itself. Raises
        MergeLockBusyError when that stays busy for *wait_timeout* seconds.
        """
        lock_path, holder_path, release_path = self._merge_lock_paths()
        if _own_repair_window_is_active(holder_path, name):
            with _extended_repair_window(lock_path, holder_path, name) as extended:
                if extended:
                    yield True
                    return
            # A keeper that cannot extend the window holds the lock until its deadline.
            # It is closed so that this merge takes the lock at once, unless another
            # merge of this task holds the lease.
            if _own_repair_window_is_active(holder_path, name) and not _probe_lock_is_busy(
                _lease_path(lock_path)
            ):
                _request_window_release(release_path, holder_path, lock_path)
        with _merge_exclusive_lock(
            task=name,
            kind=KIND_MERGE,
            timeout_seconds=wait_timeout,
            lock_path=lock_path,
            holder_path=holder_path,
        ):
            yield False

    def merge(self, args: argparse.Namespace) -> int:
        """Merge a finished worktree branch into main and remove the worktree.

        A conflict with main is reported first. The merge commit is then made and
        checked in a private landing checkout, and main fast-forwards to it only once
        it passed: an interrupted merge leaves main as it was.
        Concurrency contract: only one merge or protected repair window may touch
        the primary checkout at a time. A task with an active repair window merges
        under its own window, which stays open until the merge is done; every other
        task waits for the lock.
        """
        name: str = args.name
        validation_error = validate_worktree_name(name)
        if validation_error is not None:
            print_error(validation_error)
            return 1

        worktree_path = self.context.worktrees_dir / name
        marker = worktree_path / WORKTREE_FILE_NAME
        if not worktree_path.exists():
            print_error(f"worktree '{name}' does not exist")
            return 1
        if not marker.exists():
            print_error(f"worktree '{name}' is not script-managed (missing {WORKTREE_FILE_NAME})")
            return 1

        branch = worktree_records._read_worktree_branch_name(worktree_path)
        if branch is None:
            print_error(f"worktree '{name}' has no checked-out branch")
            return 1

        primary_branch = self._read_primary_branch()
        if primary_branch != PRIMARY_BRANCH:
            print_error(
                f"primary checkout is on '{primary_branch}', not '{PRIMARY_BRANCH}'; "
                "switch it back before merging"
            )
            return 1

        lock_path, holder_path, release_path = self._merge_lock_paths()
        message = args.message or f"merge: {name}"

        # A conflict needs a repair first; the merge would only roll back.
        conflicted = self._merge_conflicts(branch)
        if conflicted:
            for conflict_path in conflicted:
                print(f"conflicted: {conflict_path}")
            print_error(f"merging '{branch}' into {PRIMARY_BRANCH} conflicts")
            self._print_merge_conflict_hints(
                name, window_open=_repair_window_stays_open(holder_path, name)
            )
            return MERGE_CONFLICT_EXIT_CODE

        self.removal.sweep_private_checkouts(self.context.worktrees_dir)
        with self.removal.private_checkout(LANDING_DIR_PREFIX, name) as landing:
            failure = self._create_landing_checkout(landing, worktree_path)
            if failure is not None:
                print_error(f"the landing checkout could not be created: {failure}")
                return 1
            try:
                with self._merge_protection(name, args.wait_timeout) as window:
                    outcome, landed = self._land_merge(
                        name, branch, message, worktree_path, landing, window=window
                    )
                    # Success closes the window; a failure keeps it open for the retry.
                    if (
                        landed
                        and window
                        and not _request_window_release(release_path, holder_path, lock_path)
                    ):
                        print_error("repair keeper did not shut down; it expires at its deadline")
            except MergeLockBusyError as exc:
                print_error(str(exc))
                return 1

        if not landed:
            return outcome
        cleanup_code = self.removal.delete(argparse.Namespace(name=name, force=False))
        if cleanup_code != 0:
            print_error(f"worktree cleanup failed; run 'python scripts/worktree.py delete {name}'")
            return 1
        return outcome

    def _land_merge(
        self,
        name: str,
        branch: str,
        message: str,
        worktree_path: Path,
        landing: Path,
        *,
        window: bool,
    ) -> tuple[int, bool]:
        """Land the branch on main through *landing*; return the exit code and whether it landed.

        Runs under the merge lock or this task's extended repair window. The merge
        commit is made and checked in *landing*, which starts from main; main only
        fast-forwards to it. When main moved meanwhile, the merge is made and checked
        again onto the new main.
        """
        lock_path, holder_path, _ = self._merge_lock_paths()

        def window_open() -> bool:
            return window and _repair_window_stays_open(holder_path, name)

        if not self._roll_back_unfinished_merge():
            return 1, False

        primary_branch = self._read_primary_branch()
        if primary_branch != PRIMARY_BRANCH:
            print_error(
                f"primary checkout is on '{primary_branch}', not '{PRIMARY_BRANCH}'; "
                "switch it back before merging"
            )
            return 1, False
        uncommitted = worktree_records._list_uncommitted_paths(self.context.project_root)
        if uncommitted:
            print_error("primary checkout has uncommitted changes; commit or clean it first")
            for line in uncommitted:
                print(f"uncommitted: {line}")
            return 1, False

        base = self._main_head()
        landed: str | None = None
        for _attempt in range(LANDING_ATTEMPTS):
            if base is None:
                print_error("the primary checkout's HEAD cannot be read")
                return 1, False
            return_code, stderr = self.context.run_command(
                ["git", "-C", str(landing), "reset", "-q", "--hard", base]
            )
            if return_code == 0:
                return_code, stderr = self.context.run_command(
                    ["git", "-C", str(landing), "merge", "--no-ff", "--no-commit", branch]
                )
            if return_code != 0:
                conflicted = self._list_conflicted_paths(landing)
                for conflict_path in conflicted:
                    print(f"conflicted: {conflict_path}")
                print_error(stderr or "git merge failed")
                if conflicted:
                    self._print_merge_conflict_hints(name, window_open=window_open())
                else:
                    self._print_merge_check_hints(name, window_open=window_open())
                return MERGE_CONFLICT_EXIT_CODE, False
            # Without MERGE_HEAD, main already contains the branch: nothing to commit.
            if self._git_stdout(landing, "rev-parse", "-q", "--verify", "MERGE_HEAD") is None:
                landed = base
                break

            failure = self._provide_webui_packages(landing, worktree_path)
            if failure is not None:
                print_error(f"npm ci failed for the merged webui; main is unchanged: {failure}")
                print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
                if window_open():
                    print("note: your protected repair window stays open while you fix this")
                return 1, False
            print("checking the merge commit (no output until done)...", flush=True)
            return_code, stderr = self._commit_landing(landing, message)
            commit = self._git_stdout(landing, "rev-parse", "-q", "--verify", "HEAD")
            if return_code != 0 or commit is None or commit.strip() == base:
                print_error(stderr or "git commit failed")
                self._print_merge_check_hints(name, window_open=window_open())
                return MERGE_CONFLICT_EXIT_CODE, False
            commit = commit.strip()

            if window and not _repair_window_is_extended(lock_path, holder_path, name):
                print_error(
                    "your repair window ended while the merge commit was checked; main is unchanged"
                )
                print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
                return 1, False
            return_code, stderr = self._fast_forward_main(commit)
            if return_code == 0:
                landed = commit
                break
            head = self._main_head()
            if head == base:
                print_error(f"main could not be fast-forwarded to the merge commit: {stderr}")
                print("hint: main is unchanged; commit or clean main's changes to the files listed")
                print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
                return 1, False
            print(
                "main moved while the merge commit was checked; merging onto it again...",
                flush=True,
            )
            base = head

        if landed is None or base is None:
            print_error(
                f"main moved during each of {LANDING_ATTEMPTS} checks of the merge commit; "
                "main is unchanged by this merge"
            )
            print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
            return 1, False

        short = (
            self._git_stdout(self.context.project_root, "rev-parse", "--short", landed) or ""
        ).strip()
        print_ok(name=name, status="merged", commit=short or UNKNOWN_VALUE, branch=branch)
        # The WebUI checks refuse installed packages that differ from the lock.
        lock_changed, _ = self.context.run_command(
            [
                "git",
                "-C",
                str(self.context.project_root),
                "diff",
                "--quiet",
                base,
                landed,
                "--",
                WEBUI_LOCK_FILE,
            ]
        )
        if lock_changed == 1 and (self.context.project_root / "webui" / "node_modules").is_dir():
            failure = self._install_webui_packages(
                "installing the merged webui dependencies in main"
            )
            if failure is not None:
                print_error(
                    "main's webui/node_modules does not match its package-lock.json; run `npm ci` "
                    f"in {self.context.project_root / 'webui'} before its next WebUI "
                    f"commit: {failure}"
                )
                return 1, True
        return 0, True

    def repair_start(self, args: argparse.Namespace) -> int:
        """Open a protected repair window that freezes main for this task."""
        name: str = args.name
        validation_error = validate_worktree_name(name)
        if validation_error is not None:
            print_error(validation_error)
            return 1

        worktree_path = self.context.worktrees_dir / name
        if not worktree_path.exists():
            print_error(f"worktree '{name}' does not exist")
            return 1

        lock_path, holder_path, release_path = self._merge_lock_paths()
        with suppress(OSError):
            release_path.unlink()

        deadline = time.time() + args.window
        log_path = self.context.git_common_dir() / REPAIR_LOG_FILE_NAME
        log_handle = log_path.open("ab")
        keeper_command = [
            sys.executable,
            str(self.context.checkout_root / "scripts" / "worktree.py"),
            "keeper-hold",
            "--task",
            name,
            "--deadline",
            str(deadline),
            "--lock-path",
            str(lock_path),
            "--holder-path",
            str(holder_path),
            "--release-path",
            str(release_path),
        ]
        try:
            self.context.spawn_detached(keeper_command, log_handle)
        finally:
            log_handle.close()

        started = time.monotonic()
        while time.monotonic() - started < args.wait_timeout:
            if _own_repair_window_is_active(holder_path, name):
                print_ok(status="repair-window-open", task=name, window_seconds=int(args.window))
                return 0
            time.sleep(0.25)

        print_error(
            f"repair window did not open within {int(args.wait_timeout)}s; see keeper log: "
            f"{log_path}"
        )
        return 1

    def repair_finish(self, args: argparse.Namespace) -> int:
        """Close this task's protected repair window."""
        name: str = args.name
        validation_error = validate_worktree_name(name)
        if validation_error is not None:
            print_error(validation_error)
            return 1

        lock_path, holder_path, release_path = self._merge_lock_paths()
        record = _read_holder_record(holder_path)
        if record is None or not _holder_record_is_current(record):
            print_ok(status="already-closed", task=name)
            return 0
        if record.get("task") != name or record.get("kind") != KIND_REPAIR:
            holder_task = record.get("task") or UNKNOWN_VALUE
            print_error(f"the active window or merge belongs to task '{holder_task}', not '{name}'")
            return 1

        if not _request_window_release(release_path, holder_path, lock_path):
            print_error("repair keeper did not shut down; it expires at its deadline")
            return 1
        print_ok(status="repair-window-closed", task=name)
        return 0
