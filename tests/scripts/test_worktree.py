"""Unit tests for worktree script helper logic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

WORKTREE_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "worktree.py"
WORKTREE_SPEC = importlib.util.spec_from_file_location("worktree", WORKTREE_SCRIPT_PATH)
if WORKTREE_SPEC is None or WORKTREE_SPEC.loader is None:
    raise RuntimeError("Unable to load scripts/worktree.py for tests")
WORKTREE_MODULE: ModuleType = importlib.util.module_from_spec(WORKTREE_SPEC)
WORKTREE_SPEC.loader.exec_module(WORKTREE_MODULE)

find_free_port = WORKTREE_MODULE.find_free_port
is_port_bound = WORKTREE_MODULE.is_port_bound
merge_settings = WORKTREE_MODULE.merge_settings
scan_used_ports = WORKTREE_MODULE.scan_used_ports


def test_scan_used_ports_empty(tmp_path: Path) -> None:
    worktrees_dir = tmp_path / ".worktrees"

    assert scan_used_ports(worktrees_dir) == set()


def test_scan_used_ports_reads_port_line(tmp_path: Path) -> None:
    worktrees_dir = tmp_path / ".worktrees"
    doc_path = worktrees_dir / "feat" / ".vorch" / "WORKTREE.md"
    doc_path.parent.mkdir(parents=True)
    doc_path.write_text(
        "# Worktree: feat\n\nport: 8422\ndata-dir: /tmp/vbot\n",
        encoding="utf-8",
    )

    assert scan_used_ports(worktrees_dir) == {8422}


def test_scan_used_ports_skips_malformed(tmp_path: Path) -> None:
    worktrees_dir = tmp_path / ".worktrees"
    doc_path = worktrees_dir / "feat" / ".vorch" / "WORKTREE.md"
    doc_path.parent.mkdir(parents=True)
    doc_path.write_text("# Worktree: feat\n\nmissing-port-line\n", encoding="utf-8")

    assert scan_used_ports(worktrees_dir) == set()


def test_find_free_port_skips_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WORKTREE_MODULE, "scan_used_ports", lambda unused_dir: {8421, 8422})
    monkeypatch.setattr(WORKTREE_MODULE, "is_port_bound", lambda unused_port: False)

    assert find_free_port(tmp_path / ".worktrees") == 8423


def test_find_free_port_skips_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WORKTREE_MODULE, "scan_used_ports", lambda unused_dir: set())
    monkeypatch.setattr(WORKTREE_MODULE, "is_port_bound", lambda port: port == 8421)

    assert find_free_port(tmp_path / ".worktrees") == 8422


def test_is_port_bound_false_when_connection_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_os_error(*args: object, **kwargs: object) -> None:
        raise OSError("no listener")

    monkeypatch.setattr(WORKTREE_MODULE.socket, "create_connection", _raise_os_error)

    assert is_port_bound(8421) is False


def test_merge_settings_creates_new_file(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    merge_settings(settings_path, {"server_port": 8421})

    assert json.loads(settings_path.read_text(encoding="utf-8")) == {"server_port": 8421}


def test_merge_settings_updates_existing_key(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"server_port": 9999, "other": "keep"}),
        encoding="utf-8",
    )

    merge_settings(settings_path, {"server_port": 8421})

    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "server_port": 8421,
        "other": "keep",
    }


def test_merge_settings_adds_key(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"other": "value"}), encoding="utf-8")

    merge_settings(settings_path, {"server_port": 8421})

    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "other": "value",
        "server_port": 8421,
    }


def test_merge_settings_creates_parent_dirs(tmp_path: Path) -> None:
    settings_path = tmp_path / "nested" / "settings.json"

    merge_settings(settings_path, {"server_port": 8421})

    assert settings_path.exists() is True
    assert json.loads(settings_path.read_text(encoding="utf-8"))["server_port"] == 8421
