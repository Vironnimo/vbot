"""Shared helpers for Secret Store tests."""

from __future__ import annotations

from pathlib import Path

from core.secrets import KeyFileProtection, SecretStore

TOKEN_TEXT = '{"access_token": "token-value", "format_version": 1, "refresh_token": "refresh"}\n'


def key_directory(tmp_path: Path) -> Path:
    """The key directory of the tests' stores, outside every data directory."""
    return tmp_path / "keys"


def data_directory(tmp_path: Path, name: str = "data") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def open_store(tmp_path: Path, data_dir: Path | None = None) -> SecretStore:
    """A store protected by a key file in ``tmp_path/keys``; each call is a new process."""
    return SecretStore(
        data_dir if data_dir is not None else tmp_path / "data",
        protections=(KeyFileProtection(directories=(key_directory(tmp_path),)),),
    )
