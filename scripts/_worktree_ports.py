"""Selection of unclaimed server and fake-Provider port pairs."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

from scripts._worktree_records import (
    DATA_DIR_KEY,
    SERVER_PORT_KEY,
    WORKTREE_FILE_NAME,
    _read_worktree_marker,
)

MAIN_DEV_PORT = 8421


FIRST_WORKTREE_PORT = 8422


FAKE_PROVIDER_PORT_OFFSET = 10_000


def scan_used_ports(worktrees_dir: Path) -> set[int]:
    """Collect server and seeded fake-Provider ports declared by worktrees."""
    ports: set[int] = set()
    if not worktrees_dir.exists():
        return ports

    for candidate in worktrees_dir.iterdir():
        if not candidate.is_dir() or candidate.name.startswith("."):
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
            providers = settings.get("providers")
            if not isinstance(providers, dict):
                continue
            custom = providers.get("custom")
            if not isinstance(custom, dict):
                continue
            fake_provider = custom.get("fake")
            if not isinstance(fake_provider, dict):
                continue
            base_url = fake_provider.get("base_url")
            if not isinstance(base_url, str):
                continue
            parsed_port = urlsplit(base_url).port
            if parsed_port is not None:
                ports.add(parsed_port)
        except (OSError, ValueError, json.JSONDecodeError):
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
    """Find a free server port whose paired fake-Provider port is also free."""
    used_ports = scan_used_ports(worktrees_dir)
    candidate = start

    while True:
        provider_port = candidate + FAKE_PROVIDER_PORT_OFFSET
        if provider_port > 65_535:
            raise RuntimeError("no paired server and fake-Provider ports are available")
        unavailable = (
            candidate == MAIN_DEV_PORT
            or candidate in used_ports
            or provider_port in used_ports
            or is_port_bound(candidate)
            or is_port_bound(provider_port)
        )
        if not unavailable:
            return candidate
        candidate += 1
