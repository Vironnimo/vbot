#!/usr/bin/env python
"""Manage vBot git worktrees for parallel development."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

# Direct execution loads helper modules from this checkout.
_checkout_root = Path(__file__).resolve().parents[1]
if str(_checkout_root) not in sys.path:
    sys.path.insert(0, str(_checkout_root))

from scripts._worktree_args import parse_args  # noqa: E402
from scripts._worktree_lock import (  # noqa: E402
    KIND_MERGE,
    KIND_REPAIR,
    MergeLockBusyError,
    _holder_record_is_current,
    _merge_exclusive_lock,
    _own_repair_window_is_active,
    _probe_lock_is_busy,
    _read_holder_record,
    _request_window_release,
    cmd_keeper_hold,
)
from scripts._worktree_ports import (  # noqa: E402
    FAKE_PROVIDER_PORT_OFFSET,
    find_free_port,
)
from scripts._worktree_records import (  # noqa: E402
    DATA_DIR_KEY,
    MANAGED_BRANCH_KEY,
    SERVER_PORT_KEY,
    UNKNOWN_VALUE,
    WORKTREE_FILE_NAME,
    _list_uncommitted_paths,
    _marker_data_dir,
    _marker_managed_branch,
    _read_settings_port,
    _read_worktree_branch_name,
    _read_worktree_marker,
)


def _resolve_project_root() -> Path:
    """Resolve the canonical repository root across linked git worktrees."""
    script_root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=script_root,
            check=False,
        )
    except OSError:
        return script_root

    if result.returncode != 0:
        return script_root

    git_common_dir = result.stdout.strip()
    if not git_common_dir:
        return script_root

    return Path(git_common_dir).resolve().parent


PROJECT_ROOT = _resolve_project_root()

WORKTREES_DIR = PROJECT_ROOT / ".worktrees"
FAKE_PROVIDER_SETTINGS_PATH = PROJECT_ROOT / "tests" / "e2e" / "fake-provider-settings.json"
VALID_WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TRASH_DIR_PREFIX = ".trash-"
PORT_ALLOCATION_LOCK_NAME = "vbot-worktree-port.lock"
PRIMARY_BRANCH = "main"
MERGE_LOCK_FILE_NAME = "vbot-merge.lock"
MERGE_HOLDER_FILE_NAME = "vbot-merge.lock.holder.json"
MERGE_RELEASE_FILE_NAME = "vbot-merge.lock.release"
REPAIR_LOG_FILE_NAME = "vbot-merge-repair.log"
MERGE_CONFLICT_EXIT_CODE = 2
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — keeps the repair keeper alive
# after the spawning CLI exits and outside the caller's Ctrl+C group.
WINDOWS_DETACHED_CREATION_FLAGS = 0x00000008 | 0x00000200


def print_ok(**fields: str | int | bool | Path) -> None:
    """Print structured success output as key-value lines."""
    for key, value in fields.items():
        rendered = str(value) if isinstance(value, Path) else value
        print(f"{key}: {rendered}")


def print_error(reason: str) -> None:
    """Print structured error output."""
    print(f"error: {reason}")


def validate_worktree_name(name: str) -> str | None:
    """Return an error message when a worktree name is unsafe."""
    if VALID_WORKTREE_NAME_PATTERN.fullmatch(name):
        return None

    return (
        "worktree name must start with a letter or number and contain only "
        "letters, numbers, dots, underscores, and hyphens"
    )


def _expected_data_dir(name: str) -> Path:
    """Return the managed data-dir path for a worktree name."""
    return Path.home() / f".vbot-{name}"


def initialize_data_dir(data_dir: Path) -> None:
    """Run the pure-standard-library canonical initializer without package imports."""

    # Load the layout from this checkout, not PROJECT_ROOT: a worktree may carry a
    # newer canonical layout than the linked main repository, and the checkout's
    # own code defines the layout its server expects. The seed resources
    # (.env.example and friends) come from the same checkout, otherwise a branch
    # that changes a resource would seed the main copy and diverge from it.
    checkout_root = Path(__file__).resolve().parent.parent
    layout_module = runpy.run_path(
        str(checkout_root / "core" / "storage" / "layout.py"),
        run_name="vbot_data_directory_layout",
    )
    initializer = layout_module["initialize_data_directory"]
    initializer(data_dir, resources_dir=checkout_root / "resources")


def _resolve_remove_data_dir(name: str, marker_data: dict[str, object] | None) -> Path:
    """Resolve the data dir to remove with strict safety checks."""
    expected = _expected_data_dir(name)
    if marker_data is None:
        return expected

    raw_data_dir = marker_data.get(DATA_DIR_KEY)
    if not isinstance(raw_data_dir, str) or not raw_data_dir:
        return expected

    candidate = Path(raw_data_dir).expanduser()
    if candidate == expected:
        return candidate

    return expected


def _clear_readonly_and_retry(func: Callable[[str], object], path: str, _excinfo: object) -> None:
    """rmtree error handler: clear the read-only attribute and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_directory_tree(tree_path: Path) -> str | None:
    """Delete a directory tree, tolerating read-only files and transient locks.

    Returns None on success, otherwise the last error text.
    """
    last_error: str | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(0.5)
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(tree_path, onexc=_clear_readonly_and_retry)
            else:
                shutil.rmtree(tree_path, onerror=_clear_readonly_and_retry)
            return None
        except OSError as exc:
            last_error = str(exc)
        if not tree_path.exists():
            return None
    return last_error


def _terminate_worktree_processes(worktree_path: Path) -> list[str]:
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
        "ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; $_.Path }"
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


def _move_to_trash(worktree_path: Path) -> Path | None:
    """Rename a stuck worktree directory aside so its name becomes reusable.

    A rename succeeds even when an external process (e.g. an editor language
    server) still holds files inside the tree mapped. Trash directories are
    swept on later create/delete runs once the locks are gone.
    """
    trash_path = worktree_path.parent / f"{TRASH_DIR_PREFIX}{worktree_path.name}-{time.time_ns()}"
    try:
        worktree_path.rename(trash_path)
    except OSError:
        return None
    return trash_path


def sweep_trash_directories(worktrees_dir: Path) -> None:
    """Best-effort removal of trash directories left by earlier deletes."""
    if not worktrees_dir.exists():
        return

    for candidate in worktrees_dir.iterdir():
        if candidate.is_dir() and candidate.name.startswith(TRASH_DIR_PREFIX):
            _remove_directory_tree(candidate)


def _worktree_registration_state(worktree_path: Path) -> bool | None:
    """Return whether Git still registers a worktree, or ``None`` if unknown."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    target = os.path.normcase(str(worktree_path.resolve()))
    for field in result.stdout.split("\0"):
        if not field.startswith("worktree "):
            continue
        registered = Path(field.removeprefix("worktree ")).resolve()
        if os.path.normcase(str(registered)) == target:
            return True
    return False


@contextmanager
def _port_allocation_lock() -> Iterator[None]:
    """Serialize port selection until the owning marker is durable."""
    lock_path = _git_common_dir() / PORT_ALLOCATION_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        fcntl = importlib.import_module("fcntl")

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _git_common_dir() -> Path:
    """Resolve the shared Git directory from either the main tree or a worktree."""
    dot_git = PROJECT_ROOT / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return dot_git
    prefix = "gitdir:"
    if not marker.lower().startswith(prefix):
        return dot_git
    git_dir = Path(marker[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (PROJECT_ROOT / git_dir).resolve()
    if git_dir.parent.name == "worktrees":
        return git_dir.parent.parent
    return git_dir


def _merge_lock_paths() -> tuple[Path, Path, Path]:
    """Resolve merge lock, holder record, and release signal paths."""
    git_dir = _git_common_dir()
    return (
        git_dir / MERGE_LOCK_FILE_NAME,
        git_dir / MERGE_HOLDER_FILE_NAME,
        git_dir / MERGE_RELEASE_FILE_NAME,
    )


def seed_worktree_settings(settings_path: Path, *, server_port: int) -> None:
    """Seed the free local Provider and Models without replacing existing settings."""

    settings: dict[str, object] = {}
    if settings_path.exists():
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings = loaded

    fixture = json.loads(FAKE_PROVIDER_SETTINGS_PATH.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise ValueError("fake Provider settings fixture must be a JSON object")
    providers = fixture["providers"]
    custom = providers["custom"]
    fake_provider = custom["fake"]
    fake_provider["base_url"] = f"http://127.0.0.1:{server_port + FAKE_PROVIDER_PORT_OFFSET}/v1"

    def merge_missing(target: dict[str, object], source: dict[str, object]) -> None:
        for key, value in source.items():
            current = target.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merge_missing(current, value)
            elif key not in target:
                target[key] = value

    merge_missing(settings, fixture)
    settings[SERVER_PORT_KEY] = server_port
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(f"{json.dumps(settings, indent=2)}\n", encoding="utf-8")


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Run a command and return returncode and stderr text."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=cwd or PROJECT_ROOT,
            check=False,
        )
    except OSError as exc:
        return 1, str(exc)
    return result.returncode, result.stderr.strip()


def iter_worktree_entries(worktrees_dir: Path) -> list[dict[str, str | int | Path]]:
    """Collect script-managed worktree entries sorted by name."""
    if not worktrees_dir.exists():
        return []

    entries: list[dict[str, str | int | Path]] = []
    for worktree_path in sorted(worktrees_dir.iterdir(), key=lambda path: path.name):
        if not worktree_path.is_dir() or worktree_path.name.startswith("."):
            continue

        marker_path = worktree_path / WORKTREE_FILE_NAME
        if not marker_path.exists():
            continue

        marker_data = _read_worktree_marker(marker_path)
        data_dir_display, data_dir = _marker_data_dir(marker_data)
        port = _read_settings_port(data_dir)
        branch = _read_worktree_branch_name(worktree_path) or UNKNOWN_VALUE

        entries.append(
            {
                "name": worktree_path.name,
                "path": worktree_path,
                "branch": branch,
                "data-dir": data_dir_display,
                "port": port if port is not None else UNKNOWN_VALUE,
                "managed-branch": _marker_managed_branch(marker_data),
            }
        )

    return entries


def cleanup_failed_create(
    name: str,
    worktree_path: Path,
    data_dir: Path,
    *,
    managed_branch: bool,
    remove_data_dir: bool,
) -> None:
    """Remove artifacts created before a failed create operation."""
    return_code, _ = _run_command(["git", "worktree", "remove", "--force", str(worktree_path)])
    if return_code != 0 and worktree_path.exists():
        # The npm steps may leave processes (e.g. esbuild) locking files that
        # break git's directory deletion on Windows — finish it ourselves.
        _terminate_worktree_processes(worktree_path)
        if _remove_directory_tree(worktree_path) is not None and worktree_path.exists():
            _move_to_trash(worktree_path)
        _run_command(["git", "worktree", "prune"])

    if remove_data_dir:
        shutil.rmtree(data_dir, ignore_errors=True)

    if managed_branch:
        _run_command(["git", "branch", "-D", name])


def _stop_worktree_services(worktree_path: Path, data_dir: Path) -> str | None:
    """Stop the exact managed server and fake Provider before deletion."""
    settings_path = data_dir / "settings.json"
    if not settings_path.exists():
        return None
    test_env_script = worktree_path / "scripts" / "test-env.py"
    if not test_env_script.is_file():
        return f"test environment stop script is missing: {test_env_script}"
    port = _read_settings_port(data_dir)
    command = [
        sys.executable,
        str(test_env_script),
        "stop",
        "--host",
        "127.0.0.1",
        "--data-dir",
        str(data_dir),
    ]
    if port is not None:
        command.extend(["--port", str(port)])
    return_code, stderr = _run_command(command, cwd=worktree_path)
    if return_code == 0:
        return None
    return stderr or "managed worktree services could not be stopped"


def cmd_create(args: argparse.Namespace) -> int:
    """Create a new worktree with dedicated port and data directory."""
    name: str = args.name
    validation_error = validate_worktree_name(name)
    if validation_error is not None:
        print_error(validation_error)
        return 1

    worktree_path = WORKTREES_DIR / name
    managed_branch = args.from_branch is None

    sweep_trash_directories(WORKTREES_DIR)

    if worktree_path.exists():
        print_error(f"worktree '{name}' already exists")
        return 1

    if args.from_branch:
        git_command = ["git", "worktree", "add", str(worktree_path), args.from_branch]
    else:
        branch_check_code, _ = _run_command(["git", "rev-parse", "--verify", f"refs/heads/{name}"])
        if branch_check_code == 0:
            print_error(f"branch '{name}' already exists; use --from to specify an existing branch")
            return 1
        git_command = ["git", "worktree", "add", "-b", name, str(worktree_path)]

    return_code, stderr = _run_command(git_command)
    if return_code != 0:
        print_error(stderr or "git worktree add failed")
        return 1

    data_dir_tilde = f"~/.vbot-{name}"
    data_dir = Path.home() / f".vbot-{name}"
    data_dir_preexisting = data_dir.exists()
    try:
        with _port_allocation_lock():
            port = find_free_port(WORKTREES_DIR)
            initialize_data_dir(data_dir)
            seed_worktree_settings(data_dir / "settings.json", server_port=port)
            marker = worktree_path / WORKTREE_FILE_NAME
            marker.write_text(
                json.dumps(
                    {
                        DATA_DIR_KEY: data_dir_tilde,
                        MANAGED_BRANCH_KEY: managed_branch,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        cleanup_failed_create(
            name,
            worktree_path,
            data_dir,
            managed_branch=managed_branch,
            remove_data_dir=not data_dir_preexisting,
        )
        print_error(str(exc))
        return 1

    npm_command = shutil.which("npm") or "npm"
    print(
        "installing webui dependencies (cold npm cache: several minutes, no output until done)...",
        flush=True,
    )
    return_code, stderr = _run_command([npm_command, "install"], cwd=worktree_path / "webui")
    if return_code != 0:
        cleanup_failed_create(
            name,
            worktree_path,
            data_dir,
            managed_branch=managed_branch,
            remove_data_dir=not data_dir_preexisting,
        )
        print_error(f"npm install failed: {stderr}" if stderr else "npm install failed")
        return 1

    print("building webui (a few minutes, no output until done)...", flush=True)
    return_code, stderr = _run_command([npm_command, "run", "build"], cwd=worktree_path / "webui")
    if return_code != 0:
        cleanup_failed_create(
            name,
            worktree_path,
            data_dir,
            managed_branch=managed_branch,
            remove_data_dir=not data_dir_preexisting,
        )
        print_error(f"npm run build failed: {stderr}" if stderr else "npm run build failed")
        return 1

    branch = name if managed_branch else args.from_branch
    print_ok(
        name=name,
        branch=branch,
        port=port,
        **{"provider-port": port + FAKE_PROVIDER_PORT_OFFSET},
        **{"data-dir": data_dir_tilde},
        path=worktree_path,
        url=f"http://localhost:{port}",
    )
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a worktree and its dedicated data directory."""
    name: str = args.name
    validation_error = validate_worktree_name(name)
    if validation_error is not None:
        print_error(validation_error)
        return 1

    worktree_path = WORKTREES_DIR / name

    sweep_trash_directories(WORKTREES_DIR)

    if not worktree_path.exists():
        print_error(f"worktree '{name}' does not exist")
        return 1

    worktree_branch = _read_worktree_branch_name(worktree_path)

    marker = worktree_path / WORKTREE_FILE_NAME
    marker_data = _read_worktree_marker(marker)
    data_dir = _resolve_remove_data_dir(name, marker_data)

    stop_error = _stop_worktree_services(worktree_path, data_dir)
    if stop_error is not None:
        print_error(f"worktree services could not be stopped: {stop_error}")
        return 1

    marker_text: str | None = None
    if marker.exists() and not args.force:
        try:
            marker_text = marker.read_text(encoding="utf-8")
        except OSError:
            marker_text = None
        # Remove only the script-managed marker so legacy branches without the
        # ignore rule do not fail the non-force dirty-worktree guard.
        _run_command(["git", "-C", str(worktree_path), "clean", "-f", "--", WORKTREE_FILE_NAME])

    delete_branch = False
    if marker_data is not None:
        managed_branch = marker_data.get(MANAGED_BRANCH_KEY)
        if isinstance(managed_branch, bool) and managed_branch:
            delete_branch = worktree_branch == name

    if args.force:
        git_command = ["git", "worktree", "remove", "--force", str(worktree_path)]
    else:
        git_command = ["git", "worktree", "remove", str(worktree_path)]

    return_code, stderr = _run_command(git_command)
    terminated_paths: list[str] = []
    leftover_path: Path | None = None
    if return_code != 0:
        reason = stderr or "git worktree remove failed"
        uncommitted_paths = _list_uncommitted_paths(worktree_path) if not args.force else []
        registration_state = (
            _worktree_registration_state(worktree_path) if not args.force else False
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
        terminated_paths = _terminate_worktree_processes(worktree_path)
        if worktree_path.exists():
            removal_error = _remove_directory_tree(worktree_path)
            if removal_error is not None and worktree_path.exists():
                leftover_path = _move_to_trash(worktree_path)
                if leftover_path is None:
                    if marker_text is not None and not marker.exists():
                        with suppress(OSError):
                            marker.write_text(marker_text, encoding="utf-8")
                    print_error(f"worktree directory could not be removed: {removal_error}")
                    return 1
        _run_command(["git", "worktree", "prune"])

    data_removal_error = _remove_directory_tree(data_dir) if data_dir.exists() else None
    if data_removal_error is not None and data_dir.exists():
        print_error(f"data directory could not be removed: {data_removal_error}")
        return 1

    if delete_branch:
        branch_delete_flag = "-D" if args.force else "-d"
        branch_return_code, branch_stderr = _run_command(
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
    if leftover_path is not None:
        # Still held by an external process (e.g. an editor); swept later.
        fields["leftover"] = leftover_path
    print_ok(**fields)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """List script-managed worktrees."""
    entries = iter_worktree_entries(WORKTREES_DIR)
    if not entries:
        print_ok(status="empty")
        return 0

    for index, entry in enumerate(entries):
        if index:
            print()
        print_ok(**entry)

    return 0


def _read_primary_branch() -> str | None:
    """Read the currently checked-out branch of the primary checkout."""
    return _read_worktree_branch_name(PROJECT_ROOT)


def _list_conflicted_paths(repo_path: Path) -> list[str]:
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


def _print_merge_conflict_hints(name: str, *, window_open: bool) -> None:
    """Print the agent-facing recovery hints after a conflicted merge."""
    print(f"hint: freeze main first: python scripts/worktree.py repair-start {name}")
    print(
        "hint: bring main into your branch (git rebase main), resolve the "
        "conflicts, commit, and rerun the quality gates"
    )
    print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
    if window_open:
        print("note: your protected repair window stays open while you fix this")


def cmd_merge(args: argparse.Namespace) -> int:
    """Merge a finished worktree branch into main and remove the worktree.

    Concurrency contract: only one merge or protected repair window may touch
    the primary checkout at a time. A task with an active repair window merges
    under its own window; every other task waits for the lock.
    """
    name: str = args.name
    validation_error = validate_worktree_name(name)
    if validation_error is not None:
        print_error(validation_error)
        return 1

    worktree_path = WORKTREES_DIR / name
    marker = worktree_path / WORKTREE_FILE_NAME
    if not worktree_path.exists():
        print_error(f"worktree '{name}' does not exist")
        return 1
    if not marker.exists():
        print_error(f"worktree '{name}' is not script-managed (missing {WORKTREE_FILE_NAME})")
        return 1

    branch = _read_worktree_branch_name(worktree_path)
    if branch is None:
        print_error(f"worktree '{name}' has no checked-out branch")
        return 1

    primary_branch = _read_primary_branch()
    if primary_branch != PRIMARY_BRANCH:
        print_error(
            f"primary checkout is on '{primary_branch}', not '{PRIMARY_BRANCH}'; "
            "switch it back before merging"
        )
        return 1

    # A leftover mid-merge state (crash during an earlier merge) is not user
    # dirt; the locked recovery step below aborts it before anything runs.
    merge_leftover = (PROJECT_ROOT / ".git" / "MERGE_HEAD").exists()
    uncommitted = [] if merge_leftover else _list_uncommitted_paths(PROJECT_ROOT)
    if uncommitted:
        print_error("primary checkout has uncommitted changes; commit or clean it first")
        for line in uncommitted:
            print(f"uncommitted: {line}")
        return 1

    lock_path, holder_path, release_path = _merge_lock_paths()
    message = args.message or f"merge: {name}"

    window_active = _own_repair_window_is_active(holder_path, name)
    protected = False
    if window_active and _probe_lock_is_busy(lock_path):
        # The keeper process holds the lock on our behalf; verify its
        # heartbeat again so a dead keeper can never open a race window.
        if _own_repair_window_is_active(holder_path, name):
            protected = True
        else:
            window_active = False

    if not protected:
        try:
            with _merge_exclusive_lock(
                task=name,
                kind=KIND_MERGE,
                timeout_seconds=args.wait_timeout,
                lock_path=lock_path,
                holder_path=holder_path,
            ):
                return _merge_and_cleanup(args, name, branch, message, window_open=False)
        except MergeLockBusyError as exc:
            print_error(str(exc))
            return 1

    outcome = _merge_and_cleanup(args, name, branch, message, window_open=True)
    # Success closes the window; a conflict keeps it open for the retry.
    if outcome == 0 and not _request_window_release(release_path, holder_path, lock_path):
        print_error("repair keeper did not shut down; it expires at its deadline")
    return outcome


def _merge_and_cleanup(
    args: argparse.Namespace,
    name: str,
    branch: str,
    message: str,
    *,
    window_open: bool,
) -> int:
    """Run the mechanical merge into main and remove the merged worktree."""
    merge_head_path = PROJECT_ROOT / ".git" / "MERGE_HEAD"
    if merge_head_path.exists():
        _run_command(["git", "-C", str(PROJECT_ROOT), "merge", "--abort"])
        print("recovered: aborted an unfinished merge left in the primary checkout")

    return_code, stderr = _run_command(
        ["git", "-C", str(PROJECT_ROOT), "merge", branch, "--no-ff", "-m", message]
    )
    if return_code != 0:
        for conflict_path in _list_conflicted_paths(PROJECT_ROOT):
            print(f"conflicted: {conflict_path}")
        _run_command(["git", "-C", str(PROJECT_ROOT), "merge", "--abort"])
        remaining = _list_uncommitted_paths(PROJECT_ROOT)
        for line in remaining:
            print(f"uncommitted-after-abort: {line}")
        detail = stderr or "git merge failed"
        print_error(detail)
        _print_merge_conflict_hints(name, window_open=window_open)
        return MERGE_CONFLICT_EXIT_CODE

    head_result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = head_result.stdout.strip() or UNKNOWN_VALUE

    print_ok(name=name, status="merged", commit=head, branch=branch)
    cleanup_code = cmd_delete(argparse.Namespace(name=name, force=False))
    if cleanup_code != 0:
        print_error(f"worktree cleanup failed; run 'python scripts/worktree.py delete {name}'")
        return 1
    return 0


def cmd_repair_start(args: argparse.Namespace) -> int:
    """Open a protected repair window that freezes main for this task."""
    name: str = args.name
    validation_error = validate_worktree_name(name)
    if validation_error is not None:
        print_error(validation_error)
        return 1

    worktree_path = WORKTREES_DIR / name
    if not worktree_path.exists():
        print_error(f"worktree '{name}' does not exist")
        return 1

    lock_path, holder_path, release_path = _merge_lock_paths()
    with suppress(OSError):
        release_path.unlink()

    deadline = time.time() + args.window
    log_path = _git_common_dir() / REPAIR_LOG_FILE_NAME
    log_handle = log_path.open("ab")
    keeper_command = [
        sys.executable,
        str(Path(__file__).resolve()),
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
        if os.name == "nt":
            subprocess.Popen(
                keeper_command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                creationflags=WINDOWS_DETACHED_CREATION_FLAGS,
            )
        else:
            subprocess.Popen(
                keeper_command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
    finally:
        log_handle.close()

    started = time.monotonic()
    while time.monotonic() - started < args.wait_timeout:
        if _own_repair_window_is_active(holder_path, name):
            print_ok(status="repair-window-open", task=name, window_seconds=int(args.window))
            return 0
        time.sleep(0.25)

    print_error(
        f"repair window did not open within {int(args.wait_timeout)}s; see keeper log: {log_path}"
    )
    return 1


def cmd_repair_finish(args: argparse.Namespace) -> int:
    """Close this task's protected repair window."""
    name: str = args.name
    validation_error = validate_worktree_name(name)
    if validation_error is not None:
        print_error(validation_error)
        return 1

    lock_path, holder_path, release_path = _merge_lock_paths()
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


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    if args.command == "create":
        return cmd_create(args)
    if args.command == "delete":
        return cmd_delete(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "merge":
        return cmd_merge(args)
    if args.command == "repair-start":
        return cmd_repair_start(args)
    if args.command == "repair-finish":
        return cmd_repair_finish(args)
    if args.command == "keeper-hold":
        return cmd_keeper_hold(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
