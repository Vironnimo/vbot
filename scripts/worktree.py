#!/usr/bin/env python
"""Manage vBot git worktrees for parallel development."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path
from uuid import uuid4

# Direct execution loads helper modules from this checkout.
_checkout_root = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(_checkout_root)]:
    sys.path.insert(0, str(_checkout_root))

from scripts import _worktree_records as worktree_records  # noqa: E402
from scripts._worktree_args import parse_args  # noqa: E402
from scripts._worktree_context import (  # noqa: E402
    WorktreeContext,
    _npm_command,
    print_error,
    print_ok,
    validate_worktree_name,
)
from scripts._worktree_lock import (  # noqa: E402
    cmd_keeper_hold,
)
from scripts._worktree_merge import WorktreeMerge  # noqa: E402
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
    _marker_data_dir,
    _marker_managed_branch,
    _read_worktree_marker,
    _worktree_server_port,
)
from scripts._worktree_removal import WorktreeRemoval  # noqa: E402
from scripts._worktree_seed import (  # noqa: E402
    seed_native_resources,
    seed_webui_packages,
)

FAKE_PROVIDER_SETTINGS_RELATIVE_PATH = Path("scripts") / "fake-provider-settings.json"


class WorktreeManager:
    """Create and list managed task checkouts, assembling lifecycle owners for dispatch."""

    def __init__(self, context: WorktreeContext) -> None:
        self.context = context
        self.removal = WorktreeRemoval(context)
        self.merges = WorktreeMerge(context, self.removal)

    def initialize_data_dir(self, data_dir: Path) -> None:
        """Run the pure-standard-library canonical initializer without package imports."""

        # Load the layout from this checkout, not PROJECT_ROOT: a worktree may carry a
        # newer canonical layout than the linked main repository, and the checkout's
        # own code defines the layout its server expects. Credentials are never seeded:
        # a worktree's data directory starts without a Secret Store.
        checkout_root = self.context.checkout_root
        layout_module = runpy.run_path(
            str(checkout_root / "core" / "storage" / "layout.py"),
            run_name="vbot_data_directory_layout",
        )
        initializer = layout_module["initialize_data_directory"]
        initializer(data_dir, require_new=True)

    def seed_worktree_settings(self, settings_path: Path, *, server_port: int) -> None:
        """Seed the free local Provider and Models without replacing existing settings."""

        settings: dict[str, object] = {}
        if settings_path.exists():
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded

        # The fixture comes from the running checkout, like the data-dir layout in
        # ``initialize_data_dir``: a branch that changes it seeds its own version.
        fixture_path = self.context.checkout_root / FAKE_PROVIDER_SETTINGS_RELATIVE_PATH
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

    def iter_worktree_entries(self, worktrees_dir: Path) -> list[dict[str, str | int | Path]]:
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
            branch = worktree_records._read_worktree_branch_name(worktree_path) or UNKNOWN_VALUE

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

    def create(self, args: argparse.Namespace) -> int:
        """Create a new worktree with dedicated port and data directory."""
        name: str = args.name
        validation_error = validate_worktree_name(name)
        if validation_error is not None:
            print_error(validation_error)
            return 1

        worktree_path = self.context.worktrees_dir / name
        data_dir = self.removal.expected_data_dir(name)
        managed_branch = args.from_branch is None

        self.removal.sweep_private_checkouts(self.context.worktrees_dir)
        self.removal.sweep_trash_directories(self.context.worktrees_dir)

        if worktree_path.exists():
            print_error(f"worktree '{name}' already exists")
            return 1

        if os.path.lexists(data_dir):
            print_error(f"data directory already exists; choose another worktree name: {data_dir}")
            return 1

        if args.from_branch:
            git_command = ["git", "worktree", "add", str(worktree_path), args.from_branch]
        else:
            branch_check_code, _ = self.context.run_command(
                ["git", "rev-parse", "--verify", f"refs/heads/{name}"]
            )
            if branch_check_code == 0:
                print_error(
                    f"branch '{name}' already exists; use --from to specify an existing branch"
                )
                return 1
            git_command = ["git", "worktree", "add", "-b", name, str(worktree_path)]

        return_code, stderr = self.context.run_command(git_command)
        if return_code != 0:
            print_error(stderr or "git worktree add failed")
            return 1

        marker_data: dict[str, object] | None = None
        try:
            with self.context.port_allocation_lock():
                if os.path.lexists(data_dir):
                    raise FileExistsError(f"data directory already exists: {data_dir}")
                port = find_free_port(self.context.worktrees_dir)
                data_dir.parent.mkdir(parents=True, exist_ok=True)
                self.initialize_data_dir(data_dir)
                token = uuid4().hex
                marker_data = {
                    DATA_DIR_KEY: str(data_dir),
                    MANAGED_BRANCH_KEY: managed_branch,
                    DATA_OWNER_KEY: token,
                    SERVER_PORT_KEY: port,
                }
                with (data_dir / DATA_OWNER_FILE_NAME).open("x", encoding="utf-8") as owner:
                    owner.write(
                        json.dumps(self.removal.data_owner_record(worktree_path, token), indent=2)
                        + "\n"
                    )
                self.seed_worktree_settings(data_dir / "settings.json", server_port=port)
                marker = worktree_path / WORKTREE_FILE_NAME
                marker.write_text(
                    json.dumps(marker_data, indent=2) + "\n",
                    encoding="utf-8",
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.removal.cleanup_failed_create(
                name,
                worktree_path,
                data_dir,
                managed_branch=managed_branch,
                marker_data=marker_data,
            )
            print_error(str(exc))
            return 1

        seed_native_resources(self.context.project_root, worktree_path)
        print("installing the verified search engine...", flush=True)
        return_code, stderr = self.context.run_command(
            [sys.executable, "-m", "cli.search_runtime"], cwd=worktree_path
        )
        if return_code != 0:
            self.removal.cleanup_failed_create(
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
        if not seed_webui_packages(self.context.project_root, worktree_path):
            print(
                "installing webui dependencies instead: the primary checkout's differ "
                "(cold npm cache: several minutes, no output until done)...",
                flush=True,
            )
            return_code, stderr = self.context.run_command(
                [_npm_command(), "install"], cwd=worktree_path / "webui"
            )
            if return_code != 0:
                self.removal.cleanup_failed_create(
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
            **{"data-dir": data_dir},
            path=worktree_path,
            url=f"http://localhost:{port}",
        )
        return 0

    def list(self, _args: argparse.Namespace) -> int:
        """List script-managed worktrees."""
        entries = self.iter_worktree_entries(self.context.worktrees_dir)
        if not entries:
            print_ok(status="empty")
            return 0

        for index, entry in enumerate(entries):
            if index:
                print()
            print_ok(**entry)

        return 0


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    manager = WorktreeManager(WorktreeContext.from_checkout(_checkout_root))
    commands = {
        "create": manager.create,
        "delete": manager.removal.delete,
        "list": manager.list,
        "merge": manager.merges.merge,
        "repair-start": manager.merges.repair_start,
        "repair-finish": manager.merges.repair_finish,
        "keeper-hold": cmd_keeper_hold,
        "remove-released": manager.removal.remove_released,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
