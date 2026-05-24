#!/usr/bin/env python
"""Manage vBot git worktrees for parallel development."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path


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

MAIN_PORT = 8420
FIRST_WORKTREE_PORT = 8421
WORKTREES_DIR = PROJECT_ROOT / ".worktrees"
DATA_DIR_TEMPLATE_DIR = PROJECT_ROOT / ".data-dir-base"
WORKTREE_FILE_NAME = ".vbot-worktree"
DATA_DIR_KEY = "data_dir"
MANAGED_BRANCH_KEY = "managed_branch"
SERVER_PORT_KEY = "server_port"
MAIN_AGENT_ID = "main"
UNKNOWN_VALUE = "unknown"
VALID_WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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


def _read_worktree_marker(marker_path: Path) -> dict[str, object] | None:
    """Read a worktree marker JSON object."""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    return data


def _expected_data_dir(name: str) -> Path:
    """Return the managed data-dir path for a worktree name."""
    return Path.home() / f".vbot-{name}"


def _main_agent_config_path(data_dir: Path) -> Path:
    """Return the main-agent config path inside a data dir."""
    return data_dir / "agents" / MAIN_AGENT_ID / "agent.json"


def _main_agent_workspace_path(data_dir: Path) -> Path:
    """Return the dedicated workspace path for the main agent."""
    return data_dir / f"workspace-{MAIN_AGENT_ID}"


def seed_data_dir(data_dir: Path) -> None:
    """Copy the default data-dir template and rewrite main-agent workspace."""
    if not DATA_DIR_TEMPLATE_DIR.is_dir():
        raise FileNotFoundError(f"data-dir template not found: {DATA_DIR_TEMPLATE_DIR}")

    shutil.copytree(DATA_DIR_TEMPLATE_DIR, data_dir, dirs_exist_ok=True)

    agent_config_path = _main_agent_config_path(data_dir)
    agent_config = json.loads(agent_config_path.read_text(encoding="utf-8"))
    if not isinstance(agent_config, dict):
        raise ValueError("main agent config must be a JSON object")

    agent_config["workspace"] = str(_main_agent_workspace_path(data_dir))
    agent_config_path.write_text(f"{json.dumps(agent_config, indent=2)}\n", encoding="utf-8")


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


def _read_worktree_branch_name(worktree_path: Path) -> str | None:
    """Read the currently checked-out branch in a worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def scan_used_ports(worktrees_dir: Path) -> set[int]:
    """Collect ports declared in per-worktree settings.json files."""
    ports: set[int] = set()
    if not worktrees_dir.exists():
        return ports

    for candidate in worktrees_dir.iterdir():
        if not candidate.is_dir():
            continue

        marker = candidate / WORKTREE_FILE_NAME
        if not marker.exists():
            continue

        data = _read_worktree_marker(marker)
        if data is None:
            continue

        try:
            raw_data_dir = data.get(DATA_DIR_KEY, "")
            if not isinstance(raw_data_dir, str) or not raw_data_dir:
                continue
            settings_path = Path(raw_data_dir).expanduser() / "settings.json"
            if not settings_path.exists():
                continue
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                continue
            port = settings.get(SERVER_PORT_KEY)
            if isinstance(port, int):
                ports.add(port)
        except (OSError, json.JSONDecodeError):
            continue

    return ports


def is_port_bound(port: int) -> bool:
    """Return True when localhost accepts a TCP connection on the port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def find_free_port(worktrees_dir: Path, start: int = FIRST_WORKTREE_PORT) -> int:
    """Find the first unassigned and unbound worktree port."""
    used_ports = scan_used_ports(worktrees_dir)
    candidate = start

    while candidate == MAIN_PORT or candidate in used_ports or is_port_bound(candidate):
        candidate += 1

    return candidate


def merge_settings(settings_path: Path, updates: dict[str, object]) -> None:
    """Merge updates into settings JSON, creating file and parents as needed."""
    settings: dict[str, object] = {}
    if settings_path.exists():
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings = loaded

    settings.update(updates)
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


def _read_settings_port(data_dir: Path | None) -> int | None:
    """Read the configured server port from a data directory."""
    if data_dir is None:
        return None

    settings_path = data_dir / "settings.json"
    if not settings_path.exists():
        return None

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(settings, dict):
        return None

    port = settings.get(SERVER_PORT_KEY)
    if isinstance(port, int):
        return port
    return None


def _marker_data_dir(marker_data: dict[str, object] | None) -> tuple[str, Path | None]:
    """Return display and resolved data-dir values from marker data."""
    if marker_data is None:
        return UNKNOWN_VALUE, None

    raw_data_dir = marker_data.get(DATA_DIR_KEY)
    if not isinstance(raw_data_dir, str) or not raw_data_dir:
        return UNKNOWN_VALUE, None

    return raw_data_dir, Path(raw_data_dir).expanduser()


def _marker_managed_branch(marker_data: dict[str, object] | None) -> str:
    """Return a stable display value for marker managed-branch state."""
    if marker_data is None:
        return UNKNOWN_VALUE

    managed_branch = marker_data.get(MANAGED_BRANCH_KEY)
    if isinstance(managed_branch, bool):
        return str(managed_branch).lower()
    return UNKNOWN_VALUE


def iter_worktree_entries(worktrees_dir: Path) -> list[dict[str, str | int | Path]]:
    """Collect script-managed worktree entries sorted by name."""
    if not worktrees_dir.exists():
        return []

    entries: list[dict[str, str | int | Path]] = []
    for worktree_path in sorted(worktrees_dir.iterdir(), key=lambda path: path.name):
        if not worktree_path.is_dir():
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
    _run_command(["git", "worktree", "remove", "--force", str(worktree_path)])

    if remove_data_dir:
        shutil.rmtree(data_dir, ignore_errors=True)

    if managed_branch:
        _run_command(["git", "branch", "-D", name])


def cmd_create(args: argparse.Namespace) -> int:
    """Create a new worktree with dedicated port and data directory."""
    name: str = args.name
    validation_error = validate_worktree_name(name)
    if validation_error is not None:
        print_error(validation_error)
        return 1

    worktree_path = WORKTREES_DIR / name
    managed_branch = args.from_branch is None

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

    port = find_free_port(WORKTREES_DIR)

    data_dir_tilde = f"~/.vbot-{name}"
    data_dir = Path.home() / f".vbot-{name}"
    data_dir_preexisting = data_dir.exists()

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        cleanup_failed_create(
            name,
            worktree_path,
            data_dir,
            managed_branch=managed_branch,
            remove_data_dir=not data_dir_preexisting,
        )
        print_error(str(exc))
        return 1

    try:
        seed_data_dir(data_dir)
        merge_settings(data_dir / "settings.json", {SERVER_PORT_KEY: port})
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

    marker = worktree_path / WORKTREE_FILE_NAME
    marker_data = {
        DATA_DIR_KEY: data_dir_tilde,
        MANAGED_BRANCH_KEY: managed_branch,
    }
    try:
        marker.write_text(
            json.dumps(marker_data, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
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

    print_ok(
        name=name,
        port=port,
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

    if not worktree_path.exists():
        print_error(f"worktree '{name}' does not exist")
        return 1

    worktree_branch = _read_worktree_branch_name(worktree_path)

    marker = worktree_path / WORKTREE_FILE_NAME
    marker_data = _read_worktree_marker(marker)
    data_dir = _resolve_remove_data_dir(name, marker_data)

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
    if return_code != 0:
        if marker_text is not None and not marker.exists():
            with suppress(OSError):
                marker.write_text(marker_text, encoding="utf-8")

        reason = stderr or "git worktree remove failed"
        if not args.force:
            lowered = reason.lower()
            if "dirty" in lowered or "modified" in lowered:
                reason = "worktree has uncommitted changes, use --force to override"
        print_error(reason)
        return 1

    shutil.rmtree(data_dir, ignore_errors=True)

    if delete_branch:
        branch_delete_flag = "-D" if args.force else "-d"
        branch_return_code, branch_stderr = _run_command(
            ["git", "branch", branch_delete_flag, name]
        )
        if branch_return_code != 0:
            reason = branch_stderr or f"git branch {branch_delete_flag} {name} failed"
            print_error(reason)

    print_ok(name=name, path=worktree_path, **{"data-dir": data_dir}, status="deleted")
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Manage vBot git worktrees")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a worktree")
    create_parser.add_argument("name")
    create_parser.add_argument("--from", dest="from_branch", metavar="BRANCH")

    delete_parser = subparsers.add_parser("delete", help="Delete a worktree")
    delete_parser.add_argument("name")
    delete_parser.add_argument("--force", action="store_true")

    subparsers.add_parser("list", help="List worktrees")

    return parser.parse_args(argv)


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    if args.command == "create":
        return cmd_create(args)
    if args.command == "delete":
        return cmd_delete(args)
    if args.command == "list":
        return cmd_list(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
