"""Tests for the checkout-local installation manifest contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cli.install_state import (
    DESKTOP_CLIENT_SHAPE,
    INSTALL_STATE_FILE,
    INSTALL_STATE_SCHEMA_VERSION,
    InstallState,
    InstallStateError,
    build_install_state,
    dependency_digest,
    infer_legacy_install_state,
    read_install_state,
    write_install_state,
)


def _state(**changes: object) -> InstallState:
    values: dict[str, object] = {
        "schema_version": INSTALL_STATE_SCHEMA_VERSION,
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
        "schema_version": INSTALL_STATE_SCHEMA_VERSION,
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


def test_read_legacy_manifest_upgrades_schema_and_keeps_target_optional(
    tmp_path: Path,
) -> None:
    state = _state()
    payload = {
        key: value
        for key, value in state.__dict__.items()
        if key not in {"server_host", "server_port", "server_data_directory"}
    }
    payload["schema_version"] = 1
    payload["dependency_groups"] = list(state.dependency_groups)
    (tmp_path / INSTALL_STATE_FILE).write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_install_state(tmp_path)

    assert loaded is not None
    assert loaded.schema_version == INSTALL_STATE_SCHEMA_VERSION
    assert loaded.server_host is None
    assert loaded.server_port is None
    assert loaded.server_data_directory is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param(
            {"install_shape": DESKTOP_CLIENT_SHAPE, "webui_revision": "abc"},
            "desktop-client must not own a WebUI revision",
            id="desktop-client-with-local-webui",
        ),
        pytest.param(
            {
                "install_shape": DESKTOP_CLIENT_SHAPE,
                "webui_revision": None,
                "server_host": "127.0.0.1",
                "server_port": 8420,
                "server_data_directory": "data",
            },
            "desktop-client must not own a server target",
            id="desktop-client-with-server-target",
        ),
        pytest.param(
            {"server_host": "127.0.0.1"},
            "server target must be complete",
            id="incomplete-server-target",
        ),
    ],
)
def test_write_install_state_refuses_an_inconsistent_manifest(
    tmp_path: Path, changes: dict[str, object], reason: str
) -> None:
    with pytest.raises(InstallStateError, match=reason):
        write_install_state(tmp_path, _state(**changes))

    assert not (tmp_path / INSTALL_STATE_FILE).exists()


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
        server_host="127.0.0.1",
        server_port=18420,
        server_data_directory=str(tmp_path / "data"),
    )

    assert state.dependency_groups == ("server", "cli", "desktop")
    assert state.source_track == "dev"
    assert state.applied_revision == "revision"
    assert state.webui_revision == "revision"
    assert len(state.dependency_digest) == 64
    assert state.server_host == "127.0.0.1"
    assert state.server_port == 18420
    assert state.server_data_directory == str((tmp_path / "data").absolute())


_PYPROJECT = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "vbot"
version = "1.0.0"
dependencies = ["httpx"]

[project.optional-dependencies]
server = ["fastapi"]

[project.scripts]
vbot = "cli.main:main"

[tool.hatch.build.targets.wheel]
packages = ["core"]

[tool.ruff]
line-length = 100
"""


@pytest.mark.parametrize(
    ("old", "new", "consumed"),
    [
        pytest.param('version = "1.0.0"', 'version = "1.0.1"', False, id="version"),
        pytest.param("line-length = 100", "line-length = 120", False, id="tool-setting"),
        pytest.param(
            'dependencies = ["httpx"]',
            'dependencies = [ "httpx" ]  # reformatted',
            False,
            id="formatting",
        ),
        pytest.param(
            'dependencies = ["httpx"]', 'dependencies = ["httpx", "pyyaml"]', True, id="dependency"
        ),
        pytest.param('server = ["fastapi"]', 'server = ["fastapi", "uvicorn"]', True, id="extra"),
        pytest.param('vbot = "cli.main:main"', 'vbot = "cli.other:main"', True, id="script"),
        pytest.param('packages = ["core"]', 'packages = ["core", "server"]', True, id="packages"),
        pytest.param(
            'requires = ["hatchling"]', 'requires = ["hatchling>=1.27"]', True, id="build-system"
        ),
    ],
)
def test_dependency_digest_tracks_only_what_an_install_consumes(
    tmp_path: Path, old: str, new: str, consumed: bool
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_bytes(_PYPROJECT.encode())
    before = dependency_digest(tmp_path)

    project.write_bytes(_PYPROJECT.replace(old, new).encode())

    assert (dependency_digest(tmp_path) != before) is consumed


def test_dependency_digest_falls_back_to_bytes_for_an_unparseable_file(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    assert dependency_digest(tmp_path) == ""

    project.write_bytes(b"[project\nversion = 1")
    first = dependency_digest(tmp_path)
    project.write_bytes(b"[project\nversion = 2")

    assert first
    assert dependency_digest(tmp_path) != first


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
