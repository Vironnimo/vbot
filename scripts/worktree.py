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
from typing import IO
from uuid import uuid4

# Direct execution loads helper modules from this checkout.
_checkout_root = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(_checkout_root)]:
    sys.path.insert(0, str(_checkout_root))

from scripts._webui_packages import installed_differences  # noqa: E402
from scripts._worktree_args import parse_args  # noqa: E402
from scripts._worktree_lock import (  # noqa: E402
    KIND_MERGE,
    KIND_REPAIR,
    MergeLockBusyError,
    _acquire_file_lock,
    _extended_repair_window,
    _held_file_lock,
    _holder_record_is_current,
    _lease_path,
    _merge_exclusive_lock,
    _own_repair_window_is_active,
    _probe_lock_is_busy,
    _read_holder_record,
    _release_file_lock,
    _repair_window_is_extended,
    _repair_window_stays_open,
    _request_window_release,
    cmd_keeper_hold,
)
from scripts._worktree_ports import (  # noqa: E402
    FAKE_PROVIDER_PORT_OFFSET,
    find_free_port,
)
from scripts._worktree_records import (  # noqa: E402
    DATA_DIR_KEY,
    DATA_OWNER_FILE_NAME,
    DATA_OWNER_KEY,
    MANAGED_BRANCH_KEY,
    SERVER_PORT_KEY,
    UNKNOWN_VALUE,
    WORKTREE_FILE_NAME,
    _find_worktree_registration,
    _list_uncommitted_paths,
    _marker_data_dir,
    _marker_managed_branch,
    _read_registered_branch_name,
    _read_worktree_branch_name,
    _read_worktree_marker,
    _read_worktree_registrations,
    _worktree_server_port,
)
from scripts._worktree_seed import (  # noqa: E402
    seed_native_resources,
    seed_type_check_cache,
    seed_webui_packages,
)


def _script_checkout_root() -> Path:
    """Return the checkout that holds the running script.

    Resolved at call time: a worktree checkout runs its own code and fixtures,
    which may be newer than the linked main repository's (``PROJECT_ROOT``).
    """
    return Path(__file__).resolve().parent.parent


def _resolve_project_root() -> Path:
    """Resolve the canonical repository root across linked git worktrees."""
    script_root = _script_checkout_root()
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
# Relative to the running script's checkout (see ``_script_checkout_root``).
FAKE_PROVIDER_SETTINGS_RELATIVE_PATH = Path("tests") / "e2e" / "fake-provider-settings.json"
VALID_WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TRASH_DIR_PREFIX = ".trash-"
# A merge prepares its merge commit, and scripts/push.py checks the commit it
# pushes, in a private detached checkout named with one of these prefixes.
LANDING_DIR_PREFIX = ".landing-"
PUSH_DIR_PREFIX = ".push-"
PRIVATE_CHECKOUT_PREFIXES = (LANDING_DIR_PREFIX, PUSH_DIR_PREFIX)
PRIVATE_CHECKOUT_LOCK_SUFFIX = ".lock"
# A merge whose base main left behind while its commit was checked merges again
# onto the new main, up to this many checks in all.
LANDING_ATTEMPTS = 3
# git refuses to fast-forward main while another git command holds its index lock.
FAST_FORWARD_ATTEMPTS = 5
FAST_FORWARD_RETRY_SECONDS = 0.5
PORT_ALLOCATION_LOCK_NAME = "vbot-worktree-port.lock"
PRIMARY_BRANCH = "main"
MERGE_LOCK_FILE_NAME = "vbot-merge.lock"
MERGE_HOLDER_FILE_NAME = "vbot-merge.lock.holder.json"
MERGE_RELEASE_FILE_NAME = "vbot-merge.lock.release"
REPAIR_LOG_FILE_NAME = "vbot-merge-repair.log"
MERGE_CONFLICT_EXIT_CODE = 2
# Locks the WebUI packages; a merge that changes it replaces main's node_modules.
WEBUI_LOCK_FILE = "webui/package-lock.json"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — keeps the repair keeper and the
# released-directory remover alive after the spawning CLI exits and outside the
# caller's Ctrl+C group.
WINDOWS_DETACHED_CREATION_FLAGS = 0x00000008 | 0x00000200
# How long the remover of an emptied worktree directory waits for the processes
# whose working directory it is, typically the shell that ran the delete, to exit.
RELEASE_WAIT_SECONDS = 300.0
RELEASE_POLL_SECONDS = 0.5


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
    if name.rstrip(".").casefold() == "dev":
        return "worktree name 'dev' is reserved for the primary checkout's data directory"
    if VALID_WORKTREE_NAME_PATTERN.fullmatch(name) and not name.endswith("."):
        return None

    return (
        "worktree name must start with a letter or number and contain only "
        "letters, numbers, dots, underscores, and hyphens; it must not end with a dot"
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
    checkout_root = _script_checkout_root()
    layout_module = runpy.run_path(
        str(checkout_root / "core" / "storage" / "layout.py"),
        run_name="vbot_data_directory_layout",
    )
    initializer = layout_module["initialize_data_directory"]
    initializer(data_dir, resources_dir=checkout_root / "resources", require_new=True)


def _canonical_path(path: Path) -> str:
    """Return a stable identity for a repository or worktree path."""
    return os.path.normcase(str(path.resolve()))


def _data_owner_record(worktree_path: Path, token: str) -> dict[str, object]:
    """Bind a newly created data directory to its repository and checkout."""
    return {
        "format_version": 1,
        DATA_OWNER_KEY: token,
        "repository": _canonical_path(PROJECT_ROOT),
        "worktree": _canonical_path(worktree_path),
    }


def _owns_data_dir(
    worktree_path: Path, data_dir: Path, marker_data: dict[str, object] | None
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
            if path.is_symlink() or _canonical_path(path) != os.path.normcase(
                str(path.parent.resolve() / path.name)
            ):
                return False
        owner_path = data_dir / DATA_OWNER_FILE_NAME
        if owner_path.is_symlink():
            return False
        return _read_worktree_marker(owner_path) == _data_owner_record(worktree_path, token)
    except (OSError, RuntimeError, ValueError):
        return False


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


def _remove_directory_tree(tree_path: Path) -> str | None:
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


def _spawn_detached(command: list[str], output: int | IO[bytes]) -> None:
    """Start *command* in the primary checkout so it outlives this CLI and its Ctrl+C group."""
    if os.name == "nt":
        subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            creationflags=WINDOWS_DETACHED_CREATION_FLAGS,
        )
    else:
        subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )


def _remove_when_released(name: str) -> bool:
    """Hand an emptied worktree directory to a background remover; return whether it started.

    Windows neither removes nor renames a directory that is a running process's
    working directory. A delete or merge started from inside the worktree, often
    through the worktree's own copy of this script, holds it until the command
    ends; the shell that ran it keeps it even after a `cd` inside the command.
    The remover runs the primary checkout's script, as the worktree's is gone.
    """
    script = PROJECT_ROOT / "scripts" / "worktree.py"
    command = [sys.executable, str(script), "remove-released", name]
    try:
        _spawn_detached(command, subprocess.DEVNULL)
    except OSError:
        return False
    return True


def cmd_remove_released(args: argparse.Namespace) -> int:
    """Remove an emptied worktree directory once no process uses it any more.

    Leaves a directory that holds a checkout again, or that stays in use for
    RELEASE_WAIT_SECONDS; `delete <name>` removes such a leftover later.
    """
    name: str = args.name
    if validate_worktree_name(name) is not None:
        return 1
    worktree_path = WORKTREES_DIR / name
    deadline = time.monotonic() + RELEASE_WAIT_SECONDS
    while worktree_path.exists():
        if (worktree_path / ".git").exists():
            return 1
        if _remove_directory_tree(worktree_path) is None:
            break
        if time.monotonic() >= deadline:
            return 1
        time.sleep(RELEASE_POLL_SECONDS)
    return 0


def _remove_checkout(checkout: Path) -> None:
    """Remove a linked checkout and its registration, even while processes hold files in it."""
    return_code, _ = _run_command(["git", "worktree", "remove", "--force", str(checkout)])
    if return_code == 0:
        return
    if checkout.exists():
        # The npm steps may leave processes (e.g. esbuild) locking files that
        # break git's directory deletion on Windows — finish it ourselves.
        _terminate_worktree_processes(checkout)
        if _remove_directory_tree(checkout) is not None and checkout.exists():
            _move_to_trash(checkout)
    _run_command(["git", "worktree", "prune"])


def _private_checkout_lock_path(checkout: Path) -> Path:
    """Return the lock a merge or push holds while its private checkout is in use."""
    return checkout.with_name(checkout.name + PRIVATE_CHECKOUT_LOCK_SUFFIX)


def sweep_private_checkouts(worktrees_dir: Path) -> None:
    """Remove the private checkouts of merges and pushes that ended without removing them.

    A running merge or push holds its checkout's lock; the OS frees it when the
    process dies, however it ends.
    """
    if not worktrees_dir.exists():
        return

    for candidate in list(worktrees_dir.iterdir()):
        if not candidate.name.startswith(PRIVATE_CHECKOUT_PREFIXES) or not candidate.is_dir():
            continue
        lock_path = _private_checkout_lock_path(candidate)
        with lock_path.open("a+b") as lock_file:
            if not _acquire_file_lock(lock_file):
                continue
            try:
                _remove_checkout(candidate)
            finally:
                _release_file_lock(lock_file)
        with suppress(OSError):
            lock_path.unlink()


def sweep_trash_directories(worktrees_dir: Path) -> None:
    """Best-effort removal of trash directories left by earlier deletes."""
    if not worktrees_dir.exists():
        return

    for candidate in worktrees_dir.iterdir():
        if candidate.is_dir() and candidate.name.startswith(TRASH_DIR_PREFIX):
            _remove_directory_tree(candidate)


def _worktree_registration_state(worktree_path: Path) -> bool | None:
    """Return whether Git still registers a worktree, or ``None`` if unknown."""
    registrations = _read_worktree_registrations(PROJECT_ROOT)
    if registrations is None:
        return None
    return _find_worktree_registration(registrations, worktree_path) is not None


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
        if sys.platform == "win32":
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

    # The fixture comes from the running checkout, like the data-dir layout in
    # ``initialize_data_dir``: a branch that changes it seeds its own version.
    fixture_path = _script_checkout_root() / FAKE_PROVIDER_SETTINGS_RELATIVE_PATH
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
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


def _npm_command() -> str:
    return shutil.which("npm") or "npm"


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
        port = _worktree_server_port(marker_data, data_dir)
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
    marker_data: dict[str, object] | None,
) -> None:
    """Remove artifacts created before a failed create operation."""
    _remove_checkout(worktree_path)

    if _owns_data_dir(worktree_path, data_dir, marker_data):
        shutil.rmtree(data_dir, ignore_errors=True)

    if managed_branch:
        _run_command(["git", "branch", "-D", name])


def _stop_worktree_services(
    worktree_path: Path, data_dir: Path, marker_data: dict[str, object] | None
) -> str | None:
    """Stop the exact managed server and fake Provider before deletion.

    The worktree's own ``test-env.py`` runs when present. A leftover whose
    checkout is gone (for example after a merge whose directory removal
    failed) falls back to this checkout's copy: ``stop`` targets the recorded
    data dir and port, and still refuses to kill a fake Provider it cannot
    verify as its own. Without a known port nothing is stopped: ``stop``
    would otherwise target vBot's default port.
    """
    settings_path = data_dir / "settings.json"
    port = _worktree_server_port(marker_data, data_dir)
    if not settings_path.exists() or port is None:
        return None
    script_cwd = worktree_path
    test_env_script = worktree_path / "scripts" / "test-env.py"
    if not test_env_script.is_file():
        script_cwd = _script_checkout_root()
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
    return_code, stderr = _run_command(command, cwd=script_cwd)
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
    data_dir = _expected_data_dir(name)
    managed_branch = args.from_branch is None

    sweep_private_checkouts(WORKTREES_DIR)
    sweep_trash_directories(WORKTREES_DIR)

    if worktree_path.exists():
        print_error(f"worktree '{name}' already exists")
        return 1

    if os.path.lexists(data_dir):
        print_error(f"data directory already exists; choose another worktree name: {data_dir}")
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
    marker_data: dict[str, object] | None = None
    try:
        with _port_allocation_lock():
            if os.path.lexists(data_dir):
                raise FileExistsError(f"data directory already exists: {data_dir}")
            port = find_free_port(WORKTREES_DIR)
            initialize_data_dir(data_dir)
            token = uuid4().hex
            marker_data = {
                DATA_DIR_KEY: data_dir_tilde,
                MANAGED_BRANCH_KEY: managed_branch,
                DATA_OWNER_KEY: token,
                SERVER_PORT_KEY: port,
            }
            with (data_dir / DATA_OWNER_FILE_NAME).open("x", encoding="utf-8") as owner:
                owner.write(json.dumps(_data_owner_record(worktree_path, token), indent=2) + "\n")
            seed_worktree_settings(data_dir / "settings.json", server_port=port)
            marker = worktree_path / WORKTREE_FILE_NAME
            marker.write_text(
                json.dumps(marker_data, indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        cleanup_failed_create(
            name,
            worktree_path,
            data_dir,
            managed_branch=managed_branch,
            marker_data=marker_data,
        )
        print_error(str(exc))
        return 1

    seed_native_resources(PROJECT_ROOT, worktree_path)
    print("installing the verified search engine...", flush=True)
    return_code, stderr = _run_command(
        [sys.executable, "-m", "cli.search_runtime"], cwd=worktree_path
    )
    if return_code != 0:
        cleanup_failed_create(
            name,
            worktree_path,
            data_dir,
            managed_branch=managed_branch,
            marker_data=marker_data,
        )
        print_error(f"search engine installation failed: {stderr}")
        return 1
    # The WebUI is not built here: `test-env.py start` builds it before every start.
    print("copying webui dependencies from the primary checkout...", flush=True)
    if not seed_webui_packages(PROJECT_ROOT, worktree_path):
        print(
            "installing webui dependencies instead: the primary checkout's differ "
            "(cold npm cache: several minutes, no output until done)...",
            flush=True,
        )
        return_code, stderr = _run_command([_npm_command(), "install"], cwd=worktree_path / "webui")
        if return_code != 0:
            cleanup_failed_create(
                name,
                worktree_path,
                data_dir,
                managed_branch=managed_branch,
                marker_data=marker_data,
            )
            print_error(f"npm install failed: {stderr}" if stderr else "npm install failed")
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

    sweep_private_checkouts(WORKTREES_DIR)
    sweep_trash_directories(WORKTREES_DIR)

    if not worktree_path.exists():
        print_error(f"worktree '{name}' does not exist")
        return 1

    # A directory without `.git` is a leftover whose checkout is gone, typically
    # a merge whose directory removal failed and restored the marker. `git -C`
    # would resolve such a directory through the enclosing repository, so its
    # branch comes only from Git's worktree registration.
    checkout_present = (worktree_path / ".git").exists()
    if checkout_present:
        worktree_branch = _read_worktree_branch_name(worktree_path)
    else:
        worktree_branch = _read_registered_branch_name(PROJECT_ROOT, worktree_path)

    marker = worktree_path / WORKTREE_FILE_NAME
    marker_data = _read_worktree_marker(marker)
    data_dir = _expected_data_dir(name)

    data_owned = _owns_data_dir(worktree_path, data_dir, marker_data)
    if data_owned:
        stop_error = _stop_worktree_services(worktree_path, data_dir, marker_data)
        if stop_error is not None:
            print_error(f"worktree services could not be stopped: {stop_error}")
            return 1

    marker_text: str | None = None
    if marker.exists():
        try:
            marker_text = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            marker_text = None
    if marker.exists() and checkout_present and not args.force:
        # Remove only the script-managed marker so legacy branches without the
        # ignore rule do not fail the non-force dirty-worktree guard.
        _run_command(["git", "-C", str(worktree_path), "clean", "-f", "--", WORKTREE_FILE_NAME])

    delete_branch = False
    if marker_data is not None:
        managed_branch = marker_data.get(MANAGED_BRANCH_KEY)
        if isinstance(managed_branch, bool) and managed_branch:
            delete_branch = worktree_branch == name

    terminated_paths: list[str] = []
    leftover_path: Path | None = None
    removal_deferred = False
    # Without a checkout there is no work to lose in either mode, and
    # `git worktree remove` refuses a registered directory lacking `.git`.
    finish_removal = not checkout_present
    if checkout_present:
        if args.force:
            git_command = ["git", "worktree", "remove", "--force", str(worktree_path)]
        else:
            git_command = ["git", "worktree", "remove", str(worktree_path)]

        return_code, stderr = _run_command(git_command)
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
            finish_removal = True

    if finish_removal:
        terminated_paths = _terminate_worktree_processes(worktree_path)
        if worktree_path.exists():
            removal_error = _remove_directory_tree(worktree_path)
            if removal_error is not None and worktree_path.exists():
                leftover_path = _move_to_trash(worktree_path)
                # Only the directory is left once its checkout is gone, typically
                # held as the working directory of this command or its shell.
                removal_deferred = (
                    leftover_path is None
                    and not (worktree_path / ".git").exists()
                    and _remove_when_released(name)
                )
                if leftover_path is None and not removal_deferred:
                    if marker_text is not None and not marker.exists():
                        with suppress(OSError):
                            marker.write_text(marker_text, encoding="utf-8")
                    print_error(f"worktree directory could not be removed: {removal_error}")
                    return 1
        _run_command(["git", "worktree", "prune"])

    data_preserved = os.path.lexists(data_dir)
    if data_owned and _owns_data_dir(worktree_path, data_dir, marker_data):
        data_removal_error = _remove_directory_tree(data_dir)
        if data_removal_error is not None and data_dir.exists():
            print_error(f"data directory could not be removed: {data_removal_error}")
            return 1
        data_preserved = False

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
    if data_preserved:
        fields["data-status"] = "preserved (ownership unverified)"
    if leftover_path is not None:
        # Still held by an external process (e.g. an editor); swept later.
        fields["leftover"] = leftover_path
    print_ok(**fields)
    if removal_deferred:
        print(
            "note: a running process, typically this command or its shell, still uses the "
            "emptied worktree directory; a background process removes it once that process exits"
        )
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


def _merge_conflicts(branch: str) -> list[str] | None:
    """Return the paths merging *branch* into main would conflict on, without touching main.

    None when git cannot tell; the merge itself then finds any conflict.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "merge-tree", "--write-tree", "--name-only"]
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


def _print_merge_conflict_hints(name: str, *, window_open: bool) -> None:
    """Print the agent-facing recovery hints after a conflicted merge."""
    print(f"hint: freeze main first: python scripts/worktree.py repair-start {name}")
    print("hint: bring main into your branch (git rebase main), resolve the conflicts, and commit")
    print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
    if window_open:
        print("note: your protected repair window stays open while you fix this")


def _print_merge_check_hints(name: str, *, window_open: bool) -> None:
    """Print the agent-facing recovery hints after the merge commit failed its check."""
    print("hint: the commit check rejected the merged result (report above); main is unchanged")
    print(
        "hint: bring main into your branch (git rebase main), fix the reported problems, and commit"
    )
    print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
    if window_open:
        print("note: your protected repair window stays open while you fix this")


def _git_stdout(repo: Path, *arguments: str) -> str | None:
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


def _nul_separated(output: str) -> set[str]:
    """Return the paths of a ``-z`` git listing."""
    return {path for path in output.split("\0") if path}


def _main_head() -> str | None:
    """Return the commit the primary checkout has checked out."""
    head = _git_stdout(PROJECT_ROOT, "rev-parse", "-q", "--verify", "HEAD")
    return head.strip() if head else None


def _merge_changes_since(tree: str, merge_paths: set[str]) -> list[str] | None:
    """Return the paths of a merge staged in main that changed after it was staged.

    *tree* is the merge's result and *merge_paths* the paths it changed. None when
    git cannot tell.
    """
    staged = _git_stdout(
        PROJECT_ROOT, "diff", "--cached", "--name-only", "--no-renames", "-z", tree
    )
    unstaged = _git_stdout(PROJECT_ROOT, "diff", "--name-only", "--no-renames", "-z")
    deleted = _git_stdout(
        PROJECT_ROOT, "diff", "--name-only", "--no-renames", "--diff-filter=D", "-z", "HEAD", tree
    )
    if staged is None or unstaged is None or deleted is None:
        return None
    changed = merge_paths & (_nul_separated(staged) | _nul_separated(unstaged))
    # A file the merge deleted and someone created again is untracked.
    changed |= {path for path in _nul_separated(deleted) if os.path.lexists(PROJECT_ROOT / path)}
    return sorted(changed)


def _roll_back_unfinished_merge() -> bool:
    """Undo a merge an interrupted earlier merge left staged in main; return whether none is left.

    Merges used to be staged in main for their commit check, so a killed merge of
    such a version leaves one behind. Only the paths that merge changed are
    restored, and only while each is exactly as the merge left it: other sessions'
    staged and unstaged work stays. Otherwise the merge stays and the reason is
    printed.
    """
    merge_head_path = PROJECT_ROOT / ".git" / "MERGE_HEAD"
    if not merge_head_path.exists():
        return True
    try:
        merge_heads = merge_head_path.read_text(encoding="utf-8").split()
    except OSError:
        merge_heads = []
    head = _main_head()
    merge_paths: set[str] = set()
    changed: list[str] | None = []
    reason = None
    if head is None or len(merge_heads) != 1:
        reason = "it does not merge a single branch"
    else:
        merged = _git_stdout(
            PROJECT_ROOT, "merge-tree", "--write-tree", "--no-messages", head, merge_heads[0]
        )
        tree = merged.split("\n", 1)[0].strip() if merged else ""
        paths = _git_stdout(PROJECT_ROOT, "diff", "--name-only", "--no-renames", "-z", head, tree)
        if not tree or paths is None:
            reason = "its result cannot be computed without conflicts"
        else:
            merge_paths = _nul_separated(paths)
            changed = _merge_changes_since(tree, merge_paths)
            if changed is None:
                reason = "git cannot compare it with the primary checkout"
            elif changed:
                reason = "files it changed were changed again since"
    if reason is not None:
        print_error(
            f"primary checkout has an unfinished merge that cannot be rolled back safely: {reason}"
        )
        for path in changed or []:
            print(f"changed-since-merge: {path}")
        print(
            "hint: conclude or undo that merge in the primary checkout by hand; "
            "`git merge --abort` also discards other sessions' staged changes"
        )
        return False

    # Without MERGE_HEAD, a commit concluding that merge can no longer record it.
    _run_command(["git", "-C", str(PROJECT_ROOT), "merge", "--quit"])
    if _main_head() != head:
        # A commit concluded it meanwhile, such as a killed merge's commit check.
        return True
    if merge_paths:
        try:
            result = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "--literal-pathspecs", "restore"]
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


@contextmanager
def private_checkout(prefix: str, name: str) -> Iterator[Path]:
    """Name a private checkout under ``.worktrees`` and hold its lock until it is removed again.

    The caller creates the checkout at the yielded path; it is removed when the
    context ends, however it ends. A checkout whose process died is removed by the
    next ``sweep_private_checkouts``.
    """
    checkout = WORKTREES_DIR / f"{prefix}{name}-{uuid4().hex[:8]}"
    lock_path = _private_checkout_lock_path(checkout)
    try:
        with _held_file_lock(lock_path):
            try:
                yield checkout
            finally:
                if checkout.exists():
                    _remove_checkout(checkout)
    finally:
        with suppress(OSError):
            lock_path.unlink()


def _create_landing_checkout(landing: Path, worktree_path: Path) -> str | None:
    """Check main out detached in *landing*; return why that failed, or None.

    The type checker's cache comes from the task's worktree, else from the primary
    checkout.
    """
    return_code, stderr = _run_command(
        ["git", "worktree", "add", "--detach", str(landing), PRIMARY_BRANCH]
    )
    if return_code != 0:
        return stderr or "git worktree add failed"
    for source in (worktree_path, PROJECT_ROOT):
        if seed_type_check_cache(source, landing):
            break
    return None


def _provide_webui_packages(landing: Path, worktree_path: Path) -> str | None:
    """Install the merged WebUI packages in *landing* when its commit check needs them.

    The commit check formats and lints the merge's WebUI sources on packages
    matching the merged lock, when main has installed packages. A matching
    installation of main or of the task's worktree is copied; otherwise ``npm ci``
    installs one. Returns why that failed, or None.
    """
    from scripts import commit_check

    if not (PROJECT_ROOT / "webui" / "node_modules").is_dir():
        return None
    staged = _git_stdout(landing, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD")
    scope = commit_check.webui_scope(_nul_separated(staged or ""))
    if not (scope.sources or scope.all_styles):
        return None
    webui = landing / "webui"
    node_modules = webui / "node_modules"
    if node_modules.is_dir():
        if not installed_differences(node_modules, webui / "package-lock.json"):
            return None
        _remove_directory_tree(node_modules)
    for source in (PROJECT_ROOT, worktree_path):
        if seed_webui_packages(source, landing):
            return None
    print("installing the merged webui dependencies (npm ci, no output until done)...", flush=True)
    return_code, stderr = _run_command([_npm_command(), "ci"], cwd=webui)
    if return_code == 0:
        return None
    return stderr or f"npm ci exited with code {return_code}"


def _commit_landing(landing: Path, message: str) -> tuple[int, str]:
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


def _fast_forward_main(commit: str) -> tuple[int, str]:
    """Move main to *commit*; git refuses when main moved or local changes overlap."""
    return_code, stderr = 1, ""
    for attempt in range(FAST_FORWARD_ATTEMPTS):
        if attempt:
            time.sleep(FAST_FORWARD_RETRY_SECONDS)
        return_code, stderr = _run_command(
            ["git", "-C", str(PROJECT_ROOT), "merge", "--ff-only", "-q", commit]
        )
        if return_code == 0 or "index.lock" not in stderr:
            break
    return return_code, stderr


def _install_webui_packages(action: str) -> str | None:
    """Install the packages main's WebUI lock names; return why that failed, or None."""
    print(f"{action} (npm ci, no output until done)...", flush=True)
    return_code, stderr = _run_command([_npm_command(), "ci"], cwd=PROJECT_ROOT / "webui")
    if return_code == 0:
        return None
    return stderr or f"npm ci exited with code {return_code}"


@contextmanager
def _merge_protection(name: str, wait_timeout: float) -> Iterator[bool]:
    """Keep every other merge and repair window off main until this landing is done.

    Yields whether this task's own repair window does that: its keeper holds the
    merge lock, past the window's deadline if need be, while this merge holds the
    window's lease. Otherwise the merge holds the merge lock itself. Raises
    MergeLockBusyError when that stays busy for *wait_timeout* seconds.
    """
    lock_path, holder_path, release_path = _merge_lock_paths()
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


def cmd_merge(args: argparse.Namespace) -> int:
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

    lock_path, holder_path, release_path = _merge_lock_paths()
    message = args.message or f"merge: {name}"

    # A conflict needs a repair first; the merge would only roll back.
    conflicted = _merge_conflicts(branch)
    if conflicted:
        for conflict_path in conflicted:
            print(f"conflicted: {conflict_path}")
        print_error(f"merging '{branch}' into {PRIMARY_BRANCH} conflicts")
        _print_merge_conflict_hints(name, window_open=_repair_window_stays_open(holder_path, name))
        return MERGE_CONFLICT_EXIT_CODE

    sweep_private_checkouts(WORKTREES_DIR)
    with private_checkout(LANDING_DIR_PREFIX, name) as landing:
        failure = _create_landing_checkout(landing, worktree_path)
        if failure is not None:
            print_error(f"the landing checkout could not be created: {failure}")
            return 1
        try:
            with _merge_protection(name, args.wait_timeout) as window:
                outcome, landed = _land_merge(
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
    cleanup_code = cmd_delete(argparse.Namespace(name=name, force=False))
    if cleanup_code != 0:
        print_error(f"worktree cleanup failed; run 'python scripts/worktree.py delete {name}'")
        return 1
    return outcome


def _land_merge(
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
    lock_path, holder_path, _ = _merge_lock_paths()

    def window_open() -> bool:
        return window and _repair_window_stays_open(holder_path, name)

    if not _roll_back_unfinished_merge():
        return 1, False

    primary_branch = _read_primary_branch()
    if primary_branch != PRIMARY_BRANCH:
        print_error(
            f"primary checkout is on '{primary_branch}', not '{PRIMARY_BRANCH}'; "
            "switch it back before merging"
        )
        return 1, False
    uncommitted = _list_uncommitted_paths(PROJECT_ROOT)
    if uncommitted:
        print_error("primary checkout has uncommitted changes; commit or clean it first")
        for line in uncommitted:
            print(f"uncommitted: {line}")
        return 1, False

    base = _main_head()
    landed: str | None = None
    for _attempt in range(LANDING_ATTEMPTS):
        if base is None:
            print_error("the primary checkout's HEAD cannot be read")
            return 1, False
        return_code, stderr = _run_command(
            ["git", "-C", str(landing), "reset", "-q", "--hard", base]
        )
        if return_code == 0:
            return_code, stderr = _run_command(
                ["git", "-C", str(landing), "merge", "--no-ff", "--no-commit", branch]
            )
        if return_code != 0:
            conflicted = _list_conflicted_paths(landing)
            for conflict_path in conflicted:
                print(f"conflicted: {conflict_path}")
            print_error(stderr or "git merge failed")
            if conflicted:
                _print_merge_conflict_hints(name, window_open=window_open())
            else:
                _print_merge_check_hints(name, window_open=window_open())
            return MERGE_CONFLICT_EXIT_CODE, False
        # Without MERGE_HEAD, main already contains the branch: nothing to commit.
        if _git_stdout(landing, "rev-parse", "-q", "--verify", "MERGE_HEAD") is None:
            landed = base
            break

        failure = _provide_webui_packages(landing, worktree_path)
        if failure is not None:
            print_error(f"npm ci failed for the merged webui; main is unchanged: {failure}")
            print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
            if window_open():
                print("note: your protected repair window stays open while you fix this")
            return 1, False
        print("checking the merge commit (no output until done)...", flush=True)
        return_code, stderr = _commit_landing(landing, message)
        commit = _git_stdout(landing, "rev-parse", "-q", "--verify", "HEAD")
        if return_code != 0 or commit is None or commit.strip() == base:
            print_error(stderr or "git commit failed")
            _print_merge_check_hints(name, window_open=window_open())
            return MERGE_CONFLICT_EXIT_CODE, False
        commit = commit.strip()

        if window and not _repair_window_is_extended(lock_path, holder_path, name):
            print_error(
                "your repair window ended while the merge commit was checked; main is unchanged"
            )
            print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
            return 1, False
        return_code, stderr = _fast_forward_main(commit)
        if return_code == 0:
            landed = commit
            break
        head = _main_head()
        if head == base:
            print_error(f"main could not be fast-forwarded to the merge commit: {stderr}")
            print("hint: main is unchanged; commit or clean main's changes to the files listed")
            print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
            return 1, False
        print("main moved while the merge commit was checked; merging onto it again...", flush=True)
        base = head

    if landed is None or base is None:
        print_error(
            f"main moved during each of {LANDING_ATTEMPTS} checks of the merge commit; "
            "main is unchanged by this merge"
        )
        print(f"hint: retry the merge: python scripts/worktree.py merge {name}")
        return 1, False

    short = (_git_stdout(PROJECT_ROOT, "rev-parse", "--short", landed) or "").strip()
    print_ok(name=name, status="merged", commit=short or UNKNOWN_VALUE, branch=branch)
    # The WebUI checks refuse installed packages that differ from the lock.
    lock_changed, _ = _run_command(
        ["git", "-C", str(PROJECT_ROOT), "diff", "--quiet", base, landed, "--", WEBUI_LOCK_FILE]
    )
    if lock_changed == 1 and (PROJECT_ROOT / "webui" / "node_modules").is_dir():
        failure = _install_webui_packages("installing the merged webui dependencies in main")
        if failure is not None:
            print_error(
                "main's webui/node_modules does not match its package-lock.json; run `npm ci` "
                f"in {PROJECT_ROOT / 'webui'} before its next WebUI commit: {failure}"
            )
            return 1, True
    return 0, True


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
        _spawn_detached(keeper_command, log_handle)
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
    if args.command == "remove-released":
        return cmd_remove_released(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
