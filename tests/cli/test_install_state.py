"""Tests for the checkout-local installation manifest contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cli.install_state import (
    DESKTOP_CLIENT_SHAPE,
    INSTALL_STATE_FILE,
    InstallState,
    InstallStateError,
    build_install_state,
    infer_legacy_install_state,
    read_install_state,
    write_install_state,
)


def _state(**changes: object) -> InstallState:
    values: dict[str, object] = {
        "schema_version": 1,
        "install_shape": "server",
        "dependency_groups": ("server", "cli"),
        "python_executable": sys.executable,
        "source_track": "release",
        "applied_revision": "abc123",
        "dependency_digest": "digest",
        "webui_revision": "abc123",
    }
    values.update(changes)
    return InstallState(**values)  # type: ignore[arg-type]


def test_write_and_read_install_state_round_trip(tmp_path: Path) -> None:
    state = _state(dependency_groups=("server", "cli", "desktop"))

    write_install_state(tmp_path, state)

    assert read_install_state(tmp_path) == state
    assert not (tmp_path / f"{INSTALL_STATE_FILE}.tmp").exists()


def test_read_install_state_rejects_invalid_shape(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "install_shape": "mystery",
        "dependency_groups": ["cli"],
        "python_executable": sys.executable,
        "source_track": "release",
        "applied_revision": "abc",
        "dependency_digest": "digest",
        "webui_revision": None,
    }
    (tmp_path / INSTALL_STATE_FILE).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InstallStateError, match="install_shape"):
        read_install_state(tmp_path)


def test_desktop_client_cannot_claim_local_webui(tmp_path: Path) -> None:
    with pytest.raises(InstallStateError, match="must not own"):
        write_install_state(
            tmp_path,
            _state(install_shape=DESKTOP_CLIENT_SHAPE, webui_revision="abc"),
        )


def test_build_install_state_records_exact_groups_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='vbot'\n", encoding="utf-8")
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("cli.install_state.detect_source_track", lambda _root: "dev")
    monkeypatch.setattr("cli.install_state.git_revision", lambda _root: "revision")

    state = build_install_state(
        tmp_path,
        install_shape="server-desktop",
        dependency_groups=("server", "cli", "desktop"),
        python_executable=sys.executable,
    )

    assert state.dependency_groups == ("server", "cli", "desktop")
    assert state.source_track == "dev"
    assert state.applied_revision == "revision"
    assert state.webui_revision == "revision"
    assert len(state.dependency_digest) == 64


def test_build_install_state_preserves_symlinked_environment_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_python = tmp_path / "base" / "python3"
    base_python.parent.mkdir()
    base_python.write_text("", encoding="utf-8")
    environment_python = tmp_path / "venv" / "bin" / "python3"
    environment_python.parent.mkdir(parents=True)
    try:
        environment_python.symlink_to(base_python)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation not permitted on this host")
    monkeypatch.setattr("cli.install_state.detect_source_track", lambda _root: "dev")
    monkeypatch.setattr("cli.install_state.git_revision", lambda _root: "revision")

    state = build_install_state(
        tmp_path,
        install_shape="server",
        dependency_groups=("server", "cli"),
        python_executable=str(environment_python),
    )

    assert state.python_executable == str(environment_python.absolute())
    assert Path(state.python_executable).resolve() == base_python


def test_infer_legacy_desktop_client_when_server_stack_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = {"webview": True, "fastapi": False, "pytest": False, "ruff": False, "mypy": False}
    monkeypatch.setattr(
        "cli.install_state._module_installed", lambda name: installed.get(name, False)
    )

    state = infer_legacy_install_state(tmp_path, track="release", revision="abc")

    assert state.install_shape == DESKTOP_CLIENT_SHAPE
    assert state.dependency_groups == ("cli", "desktop")
    assert state.webui_revision is None
