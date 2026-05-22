#!/usr/bin/env python
"""Manage vBot git worktrees for parallel development."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
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
WORKTREE_FILE_NAME = ".vbot-worktree"
WORKTREE_DOC_PATH = Path(".vorch") / "WORKTREE.md"


def print_ok(**fields: str | int | Path) -> None:
    """Print structured success output as key-value lines."""
    for key, value in fields.items():
        rendered = str(value) if isinstance(value, Path) else value
        print(f"{key}: {rendered}")


def print_error(reason: str) -> None:
    """Print structured error output."""
    print(f"error: {reason}")


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


def _resolve_remove_data_dir(name: str, marker_data: dict[str, object] | None) -> Path:
    """Resolve the data dir to remove with strict safety checks."""
    expected = _expected_data_dir(name)
    if marker_data is None:
        return expected

    raw_data_dir = marker_data.get("data_dir")
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
            raw_data_dir = data.get("data_dir", "")
            if not isinstance(raw_data_dir, str) or not raw_data_dir:
                continue
            settings_path = Path(raw_data_dir).expanduser() / "settings.json"
            if not settings_path.exists():
                continue
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                continue
            port = settings.get("server_port")
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


def worktree_doc_content(name: str, port: int, data_dir_tilde: str) -> str:
    """Render the per-worktree WORKTREE.md with no flags in commands."""
    return (
        f"# Worktree: {name}\n\n"
        f"port: {port}\n"
        f"data-dir: {data_dir_tilde}\n\n"
        f"You are in the '{name}' worktree. All vBot commands recognise this context\n"
        "automatically - no --port or --data-dir flags needed.\n\n"
        "Start server: python scripts/test-env.py start\n"
        "Stop server:  python scripts/test-env.py stop\n"
        "CLI:          python cli/main.py server <command>\n"
        f"URL:          http://localhost:{port}\n\n"
        "When finished: tell the Orchestrator to merge and clean up.\n"
    )


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Run a command and return returncode and stderr text."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, cwd=cwd, check=False)
    except OSError as exc:
        return 1, str(exc)
    return result.returncode, result.stderr.strip()


def cmd_create(args: argparse.Namespace) -> int:
    """Create a new worktree with dedicated port and data directory."""
    name: str = args.name
    worktree_path = WORKTREES_DIR / name

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

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_error(str(exc))
        return 1

    try:
        merge_settings(data_dir / "settings.json", {"server_port": port})
    except (OSError, json.JSONDecodeError) as exc:
        print_error(str(exc))
        return 1

    marker = worktree_path / WORKTREE_FILE_NAME
    marker_data = {
        "data_dir": data_dir_tilde,
        "managed_branch": args.from_branch is None,
    }
    try:
        marker.write_text(
            json.dumps(marker_data, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print_error(str(exc))
        return 1

    npm_command = shutil.which("npm") or "npm"
    return_code, stderr = _run_command([npm_command, "install"], cwd=worktree_path / "webui")
    if return_code != 0:
        print_error(f"npm install failed: {stderr}" if stderr else "npm install failed")
        return 1

    doc_path = worktree_path / WORKTREE_DOC_PATH
    try:
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(worktree_doc_content(name, port, data_dir_tilde), encoding="utf-8")
    except OSError as exc:
        print_error(str(exc))
        return 1

    print_ok(
        name=name,
        port=port,
        **{"data-dir": data_dir_tilde},
        path=worktree_path,
        url=f"http://localhost:{port}",
    )
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove a worktree and its dedicated data directory."""
    name: str = args.name
    worktree_path = WORKTREES_DIR / name

    if not worktree_path.exists():
        print_error(f"worktree '{name}' does not exist")
        return 1

    worktree_branch = _read_worktree_branch_name(worktree_path)

    marker = worktree_path / WORKTREE_FILE_NAME
    marker_data = _read_worktree_marker(marker)
    data_dir = _resolve_remove_data_dir(name, marker_data)

    delete_branch = worktree_branch == name
    if marker_data is not None:
        managed_branch = marker_data.get("managed_branch")
        if isinstance(managed_branch, bool):
            delete_branch = managed_branch and worktree_branch == name

    if args.force:
        git_command = ["git", "worktree", "remove", "--force", str(worktree_path)]
    else:
        git_command = ["git", "worktree", "remove", str(worktree_path)]

    return_code, stderr = _run_command(git_command)
    if return_code != 0:
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

    print_ok(name=name, path=worktree_path, **{"data-dir": data_dir}, status="removed")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Manage vBot git worktrees")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a worktree")
    create_parser.add_argument("name")
    create_parser.add_argument("--from", dest="from_branch", metavar="BRANCH")

    remove_parser = subparsers.add_parser("remove", help="Remove a worktree")
    remove_parser.add_argument("name")
    remove_parser.add_argument("--force", action="store_true")

    return parser.parse_args(argv)


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    if args.command == "create":
        return cmd_create(args)
    if args.command == "remove":
        return cmd_remove(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
