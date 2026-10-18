"""Owned worktree removal, service shutdown and private-checkout cleanup."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from core.utils.file_status import exists_strict, is_file_strict
from core.utils.server_control import live_server_ports
from scripts import _worktree_records as worktree_records
from scripts._worktree_context import (
    WorktreeContext,
    canonical_path,
    print_error,
    print_ok,
    validate_worktree_name,
)
from scripts._worktree_lock import (
    _acquire_file_lock,
    _held_file_lock,
    _release_file_lock,
)
from scripts._worktree_records import (
    DATA_DIR_KEY,
    DATA_OWNER_FILE_NAME,
    DATA_OWNER_KEY,
    MANAGED_BRANCH_KEY,
    WORKTREE_FILE_NAME,
    _find_worktree_registration,
    _read_worktree_marker,
    _read_worktree_registrations,
    _worktree_server_port,
)

TRASH_DIR_PREFIX = ".trash-"
# A merge prepares its merge commit, and scripts/push.py checks the commit it
# pushes, in a private detached checkout named with one of these prefixes.
LANDING_DIR_PREFIX = ".landing-"
PUSH_DIR_PREFIX = ".push-"
PRIVATE_CHECKOUT_PREFIXES = (LANDING_DIR_PREFIX, PUSH_DIR_PREFIX)
PRIVATE_CHECKOUT_LOCK_SUFFIX = ".lock"
# How long the remover of an emptied worktree directory waits for the processes
# whose working directory it is, typically the shell that ran the delete, to exit.
RELEASE_WAIT_SECONDS = 300.0
RELEASE_POLL_SECONDS = 0.5


def _clear_readonly_and_retry(func: Callable[[str], object], path: str, _excinfo: object) -> None:
    """rmtree error handler: clear the read-only attribute of ``path`` itself and retry.

    A link, Windows junctions included, never passes the change on to what it points
    at. Where ``os.chmod`` cannot leave links unfollowed (Linux), a link stays as it is.
    """
    mode = os.lstat(path).st_mode | stat.S_IWRITE
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, mode, follow_symlinks=False)
    elif not os.path.islink(path):
        os.chmod(path, mode)
    func(path)


class WorktreeRemoval:
    """Remove only the checkouts, services and data this repository demonstrably owns."""

    def __init__(self, context: WorktreeContext) -> None:
        self.context = context

    def expected_data_dir(self, name: str) -> Path:
        """Return the managed data-dir path for a new worktree."""
        return self.context.data_root / name

    def managed_data_dir(self, name: str, marker_data: dict[str, object] | None) -> Path:
        """Return the data dir a worktree was created with.

        Worktrees created before ``DATA_ROOT`` keep ``~/.vbot-<name>``; any other
        recorded path gets the current location, which ownership checks then reject.
        """
        legacy = Path.home() / f".vbot-{name}"
        raw_data_dir = marker_data.get(DATA_DIR_KEY) if marker_data is not None else None
        if isinstance(raw_data_dir, str) and os.path.normcase(
            os.path.abspath(Path(raw_data_dir).expanduser())
        ) == os.path.normcase(os.path.abspath(legacy)):
            return legacy
        return self.expected_data_dir(name)

    def data_owner_record(self, worktree_path: Path, token: str) -> dict[str, object]:
        """Bind a newly created data directory to its repository and checkout."""
        return {
            "format_version": 1,
            DATA_OWNER_KEY: token,
            "repository": canonical_path(self.context.project_root),
            "worktree": canonical_path(worktree_path),
        }

    def owns_data_dir(
        self, worktree_path: Path, data_dir: Path, marker_data: dict[str, object] | None
    ) -> bool:
        """Require matching ownership records before stopping services or deleting data.

        Legacy markers prove only which directory a checkout uses, not who created
        it. A renamed, redirected or replaced directory must not inherit ownership.
        """
        if marker_data is None:
            return False
        token = marker_data.get(DATA_OWNER_KEY)
        raw_data_dir = marker_data.get(DATA_DIR_KEY)
        if not isinstance(token, str) or not token or not isinstance(raw_data_dir, str):
            return False
        try:
            candidate = Path(raw_data_dir).expanduser()
            if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(
                os.path.abspath(data_dir)
            ):
                return False
            if not data_dir.is_dir():
                return False
            # resolve() also detects Windows junctions; is_symlink() covers dangling links.
            for path in (data_dir, worktree_path):
                if path.is_symlink() or canonical_path(path) != os.path.normcase(
                    str(path.parent.resolve() / path.name)
                ):
                    return False
            owner_path = data_dir / DATA_OWNER_FILE_NAME
            if owner_path.is_symlink():
                return False
            return _read_worktree_marker(owner_path) == self.data_owner_record(worktree_path, token)
        except OSError, RuntimeError, ValueError:
            return False

    def remove_directory_tree(self, tree_path: Path) -> str | None:
        """Delete a directory tree, tolerating read-only files and transient locks.

        Returns None on success, otherwise the last error text.
        """
        last_error: str | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(0.5)
            try:
                shutil.rmtree(tree_path, onexc=_clear_readonly_and_retry)
                return None
            except OSError as exc:
                last_error = str(exc)
            if not tree_path.exists():
                return None
        return last_error

    def _terminate_worktree_processes(self, worktree_path: Path) -> list[str]:
        """Terminate processes whose executable image lives inside the worktree.

        Windows cannot delete files whose executable is loaded by a running
        process — orphaned esbuild service processes from Vite builds are the
        common case. Such processes are disposable: their binary lives in a
        worktree that is being deleted. POSIX can unlink running executables,
        so this is a no-op there. Returns the terminated executable paths.
        """
        if os.name != "nt":
            return []

        pattern = f"{worktree_path}\\*".replace("'", "''")
        script = (
            f"Get-Process | Where-Object {{ $_.Path -and $_.Path -like '{pattern}' }} | "
            "ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; "
            "$_.Path }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []

        if result.returncode != 0:
            return []

        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _move_to_trash(self, worktree_path: Path) -> Path | None:
        """Rename a stuck worktree directory aside so its name becomes reusable.

        A rename succeeds even when an external process (e.g. an editor language
        server) still holds files inside the tree mapped. Trash directories are
        swept on later create/delete runs once the locks are gone.
        """
        trash_path = (
            worktree_path.parent / f"{TRASH_DIR_PREFIX}{worktree_path.name}-{time.time_ns()}"
        )
        try:
            worktree_path.rename(trash_path)
        except OSError:
            return None
        return trash_path

    def _remove_when_released(self, name: str) -> bool:
        """Hand an emptied worktree directory to a background remover; return whether it started.

        Windows neither removes nor renames a directory that is a running process's
        working directory. A delete or merge started from inside the worktree, often
        through the worktree's own copy of this script, holds it until the command
        ends; the shell that ran it keeps it even after a `cd` inside the command.
        The remover runs the primary checkout's script, as the worktree's is gone.
        """
        script = self.context.project_root / "scripts" / "worktree.py"
        command = [sys.executable, str(script), "remove-released", name]
        try:
            self.context.spawn_detached(command, subprocess.DEVNULL)
        except OSError:
            return False
        return True

    def remove_released(self, args: argparse.Namespace) -> int:
        """Remove an emptied worktree directory once no process uses it any more.

        Leaves a directory that holds a checkout again, or that stays in use for
        RELEASE_WAIT_SECONDS; `delete <name>` removes such a leftover later.
        """
        name: str = args.name
        if validate_worktree_name(name) is not None:
            return 1
        worktree_path = self.context.worktrees_dir / name
        deadline = time.monotonic() + RELEASE_WAIT_SECONDS
        while worktree_path.exists():
            if (worktree_path / ".git").exists():
                return 1
            if self.remove_directory_tree(worktree_path) is None:
                break
            if time.monotonic() >= deadline:
                return 1
            time.sleep(RELEASE_POLL_SECONDS)
        return 0

    def _remove_checkout(self, checkout: Path) -> None:
        """Remove a linked checkout and its registration, even while processes hold files in it."""
        return_code, _ = self.context.run_command(
            ["git", "worktree", "remove", "--force", str(checkout)]
        )
        if return_code == 0:
            return
        if checkout.exists():
            # The npm steps may leave processes (e.g. esbuild) locking files that
            # break git's directory deletion on Windows — finish it ourselves.
            self._terminate_worktree_processes(checkout)
            if self.remove_directory_tree(checkout) is not None and checkout.exists():
                self._move_to_trash(checkout)
        self.context.run_command(["git", "worktree", "prune"])

    def _private_checkout_lock_path(self, checkout: Path) -> Path:
        """Return the lock a merge or push holds while its private checkout is in use."""
        return checkout.with_name(checkout.name + PRIVATE_CHECKOUT_LOCK_SUFFIX)

    def sweep_private_checkouts(self, worktrees_dir: Path) -> None:
        """Remove the private checkouts of merges and pushes that ended without removing them.

        A running merge or push holds its checkout's lock; the OS frees it when the
        process dies, however it ends.
        """
        if not worktrees_dir.exists():
            return

        for candidate in list(worktrees_dir.iterdir()):
            if not candidate.name.startswith(PRIVATE_CHECKOUT_PREFIXES) or not candidate.is_dir():
                continue
            lock_path = self._private_checkout_lock_path(candidate)
            with lock_path.open("a+b") as lock_file:
                if not _acquire_file_lock(lock_file):
                    continue
                try:
                    self._remove_checkout(candidate)
                finally:
                    _release_file_lock(lock_file)
            with suppress(OSError):
                lock_path.unlink()

    def sweep_trash_directories(self, worktrees_dir: Path) -> None:
        """Best-effort removal of trash directories left by earlier deletes."""
        if not worktrees_dir.exists():
            return

        for candidate in worktrees_dir.iterdir():
            if candidate.is_dir() and candidate.name.startswith(TRASH_DIR_PREFIX):
                self.remove_directory_tree(candidate)

    def _worktree_registration_state(self, worktree_path: Path) -> bool | None:
        """Return whether Git still registers a worktree, or ``None`` if unknown."""
        registrations = _read_worktree_registrations(self.context.project_root)
        if registrations is None:
            return None
        return _find_worktree_registration(registrations, worktree_path) is not None

    def cleanup_failed_create(
        self,
        name: str,
        worktree_path: Path,
        data_dir: Path,
        *,
        managed_branch: bool,
        marker_data: dict[str, object] | None,
    ) -> None:
        """Remove artifacts created before a failed create operation."""
        self._remove_checkout(worktree_path)

        if self.owns_data_dir(worktree_path, data_dir, marker_data):
            shutil.rmtree(data_dir, ignore_errors=True)

        if managed_branch:
            self.context.run_command(["git", "branch", "-D", name])

    def stop_worktree_services(
        self, worktree_path: Path, data_dir: Path, marker_data: dict[str, object] | None
    ) -> str | None:
        """Stop the exact managed server and fake Provider before deletion.

        The worktree's own ``test-env.py`` runs when present. A leftover whose
        checkout is gone (for example after a merge whose directory removal
        failed) falls back to this checkout's copy: ``stop`` targets the recorded
        data dir and port, and still refuses to kill a fake Provider it cannot
        verify as its own. Without a known port deletion refuses: ``stop``
        would otherwise target vBot's default port.
        """
        port = _worktree_server_port(marker_data, data_dir)
        if port is None or not 1 <= port <= 65535:
            return (
                "the worktree server port is unknown; restore its settings or marker "
                "before deleting"
            )
        script_cwd = worktree_path
        test_env_script = worktree_path / "scripts" / "test-env.py"
        if not test_env_script.is_file():
            script_cwd = self.context.checkout_root
            test_env_script = script_cwd / "scripts" / "test-env.py"
            if not test_env_script.is_file():
                return f"test environment stop script is missing: {test_env_script}"
        command = [
            sys.executable,
            str(test_env_script),
            "stop",
            "--host",
            "127.0.0.1",
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
        ]
        return_code, stderr = self.context.run_command(command, cwd=script_cwd)
        if return_code == 0:
            try:
                claims = live_server_ports(data_dir)
            except OSError as exc:
                return f"server claims on the worktree data directory could not be checked: {exc}"
            if claims:
                return f"the worktree data directory is still in use on ports: {claims}"
            return None
        return stderr or "managed worktree services could not be stopped"

    def delete(self, args: argparse.Namespace) -> int:
        """Delete a worktree and its dedicated data directory."""
        name: str = args.name
        validation_error = validate_worktree_name(name)
        if validation_error is not None:
            print_error(validation_error)
            return 1

        worktree_path = self.context.worktrees_dir / name

        self.sweep_private_checkouts(self.context.worktrees_dir)
        self.sweep_trash_directories(self.context.worktrees_dir)

        try:
            if not exists_strict(worktree_path):
                print_error(f"worktree '{name}' does not exist")
                return 1
            checkout_present = exists_strict(worktree_path / ".git", follow_symlinks=False)
            if not checkout_present and not args.force:
                leftovers = [
                    path.name
                    for path in worktree_path.iterdir()
                    if path.name != WORKTREE_FILE_NAME or not is_file_strict(path)
                ]
                if leftovers:
                    print_error(
                        "worktree Git metadata is missing; restore it or use --force to "
                        "discard files"
                    )
                    return 1
        except OSError as exc:
            print_error(f"worktree contents could not be checked: {exc}")
            return 1

        # Without `.git`, `git -C` would resolve the enclosing repository. Prefer
        # Git's registration; the managed marker below can recover a pruned one.
        if checkout_present:
            worktree_branch = worktree_records._read_worktree_branch_name(worktree_path)
        else:
            worktree_branch = worktree_records._read_registered_branch_name(
                self.context.project_root, worktree_path
            )

        marker = worktree_path / WORKTREE_FILE_NAME
        marker_data = _read_worktree_marker(marker)
        data_dir = self.managed_data_dir(name, marker_data)

        data_owned = self.owns_data_dir(worktree_path, data_dir, marker_data)
        if data_owned:
            stop_error = self.stop_worktree_services(worktree_path, data_dir, marker_data)
            if stop_error is not None:
                print_error(f"worktree services could not be stopped: {stop_error}")
                return 1

        marker_text: str | None = None
        if marker.exists():
            try:
                marker_text = marker.read_text(encoding="utf-8")
            except OSError, UnicodeError:
                marker_text = None
        if marker.exists() and checkout_present and not args.force:
            # Remove only the script-managed marker so legacy branches without the
            # ignore rule do not fail the non-force dirty-worktree guard.
            self.context.run_command(
                ["git", "-C", str(worktree_path), "clean", "-f", "--", WORKTREE_FILE_NAME]
            )

        delete_branch = False
        if marker_data is not None:
            managed_branch = marker_data.get(MANAGED_BRANCH_KEY)
            if isinstance(managed_branch, bool) and managed_branch:
                delete_branch = worktree_branch == name
                if not checkout_present and worktree_branch is None:
                    branch_status, _ = self.context.run_command(
                        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]
                    )
                    delete_branch = branch_status == 0

        terminated_paths: list[str] = []
        leftover_path: Path | None = None
        removal_deferred = False
        # A metadata-less directory was checked for remaining files above, or the
        # caller explicitly chose to discard them. Git cannot remove it itself.
        finish_removal = not checkout_present
        if checkout_present:
            if args.force:
                git_command = ["git", "worktree", "remove", "--force", str(worktree_path)]
            else:
                git_command = ["git", "worktree", "remove", str(worktree_path)]

            return_code, stderr = self.context.run_command(git_command)
            if return_code != 0:
                reason = stderr or "git worktree remove failed"
                uncommitted_paths = (
                    worktree_records._list_uncommitted_paths(worktree_path)
                    if not args.force
                    else []
                )
                registration_state = (
                    self._worktree_registration_state(worktree_path) if not args.force else False
                )
                if not args.force and registration_state is not False:
                    if marker_text is not None and not marker.exists():
                        with suppress(OSError):
                            marker.write_text(marker_text, encoding="utf-8")
                    if uncommitted_paths:
                        print_error("worktree has uncommitted changes, use --force to override")
                    else:
                        print_error(reason)
                    for line in uncommitted_paths:
                        print(f"uncommitted: {line}")
                    return 1
                # git may have deregistered the worktree but failed to delete files
                # locked by running processes (Windows) — finish the removal ourselves.
                finish_removal = True

        if finish_removal:
            terminated_paths = self._terminate_worktree_processes(worktree_path)
            if worktree_path.exists():
                removal_error = self.remove_directory_tree(worktree_path)
                if removal_error is not None and worktree_path.exists():
                    leftover_path = self._move_to_trash(worktree_path)
                    # Only the directory is left once its checkout is gone, typically
                    # held as the working directory of this command or its shell.
                    removal_deferred = (
                        leftover_path is None
                        and not (worktree_path / ".git").exists()
                        and self._remove_when_released(name)
                    )
                    if leftover_path is None and not removal_deferred:
                        if marker_text is not None and not marker.exists():
                            with suppress(OSError):
                                marker.write_text(marker_text, encoding="utf-8")
                        print_error(f"worktree directory could not be removed: {removal_error}")
                        return 1
            self.context.run_command(["git", "worktree", "prune"])

        data_preserved = os.path.lexists(data_dir)
        if data_owned and self.owns_data_dir(worktree_path, data_dir, marker_data):
            data_removal_error = self.remove_directory_tree(data_dir)
            if data_removal_error is not None and data_dir.exists():
                print_error(f"data directory could not be removed: {data_removal_error}")
                return 1
            data_preserved = False

        if delete_branch:
            branch_delete_flag = "-D" if args.force else "-d"
            branch_return_code, branch_stderr = self.context.run_command(
                ["git", "branch", branch_delete_flag, name]
            )
            if branch_return_code != 0:
                reason = branch_stderr or f"git branch {branch_delete_flag} {name} failed"
                print_error(reason)
                return 1

        for terminated in terminated_paths:
            print(f"terminated: {terminated}")
        fields: dict[str, str | int | bool | Path] = {
            "name": name,
            "path": worktree_path,
            "data-dir": data_dir,
            "status": "deleted",
        }
        if data_preserved:
            fields["data-status"] = "preserved (ownership unverified)"
        if leftover_path is not None:
            # Still held by an external process (e.g. an editor); swept later.
            fields["leftover"] = leftover_path
        print_ok(**fields)
        if removal_deferred:
            print(
                "note: a running process, typically this command or its shell, still uses the "
                "emptied worktree directory; a background process removes it once that "
                "process exits"
            )
        return 0

    @contextmanager
    def private_checkout(self, prefix: str, name: str) -> Iterator[Path]:
        """Name a private checkout under ``.worktrees`` and hold its lock until it is removed again.

        The caller creates the checkout at the yielded path; it is removed when the
        context ends, however it ends. A checkout whose process died is removed by the
        next ``sweep_private_checkouts``.
        """
        checkout = self.context.worktrees_dir / f"{prefix}{name}-{uuid4().hex[:8]}"
        lock_path = self._private_checkout_lock_path(checkout)
        try:
            with _held_file_lock(lock_path):
                try:
                    yield checkout
                finally:
                    if checkout.exists():
                        self._remove_checkout(checkout)
        finally:
            with suppress(OSError):
                lock_path.unlink()
