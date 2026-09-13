"""Application-managed Extension dependencies never modify release runtimes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import dependencies
from cli.application.state import ApplicationError, Installation
from core.extensions.dependencies import runtime_dependencies


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    install.save()
    (root / "active-version").write_text("rel_base\n", encoding="ascii")
    return install


def _release(install: Installation, version_id: str, *, extra: bool = False) -> None:
    version = install.version(version_id)
    runtime = version / "runtime"
    runtime.mkdir(parents=True)
    executable = runtime / ("vBot.Python.exe" if os.name == "nt" else "bin/python3")
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("", encoding="utf-8")
    if os.name == "nt":
        (runtime / "python.exe").write_text("", encoding="utf-8")
    packages = [{"name": "base_pkg", "version": "1.0"}]
    if extra:
        packages.append({"name": "new-base", "version": "2.0"})
    (runtime / "vbot-runtime-inventory.json").write_text(
        json.dumps({"schema_version": 1, "packages": packages}), encoding="utf-8"
    )
    (version / "release.json").write_text(
        json.dumps({"files": {"runtime/python314.dll": "a" * 64}}), encoding="utf-8"
    )


def _fake_uv(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
    site = Path(arguments[arguments.index("--target") + 1])
    site.mkdir(parents=True)
    (site / "base_pkg.py").write_text("base", encoding="utf-8")
    metadata = site / "base_pkg-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: base-pkg\nVersion: 1.0\n", encoding="utf-8")
    (metadata / "RECORD").write_text(
        "base_pkg.py,,\nbase_pkg-1.0.dist-info/RECORD,,\n", encoding="utf-8"
    )
    (site / "extension_pkg.py").write_text("extension", encoding="utf-8")
    return SimpleNamespace(returncode=0)


def test_install_uses_exact_runtime_uv_constraints_and_keeps_base_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "install")
    _release(install, "rel_base")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("extension-pkg==1\n", encoding="utf-8")
    calls: list[list[str]] = []
    constraint_sets: list[str] = []

    def fake_uv(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        constraint_sets.append(
            Path(arguments[arguments.index("--constraint") + 1]).read_text(encoding="utf-8")
        )
        return _fake_uv(arguments, **kwargs)

    monkeypatch.setattr(dependencies.subprocess, "run", fake_uv)

    result = dependencies.install_dependencies(install, requirements)

    runtime = runtime_dependencies(install.version())
    site = Path(result["site"])
    assert calls[0][:5] == [str(install.interpreter("rel_base")), "-m", "uv", "pip", "install"]
    assert "base-pkg==1.0" in constraint_sets[0]
    assert (site / "extension_pkg.py").is_file()
    assert not (site / "base_pkg.py").exists()
    active = json.loads(
        (
            install.root / "data" / "extension-dependencies" / runtime.fingerprint / "active.json"
        ).read_text(encoding="utf-8")
    )
    assert active["requirements"] == "extension-pkg==1\n"
    assert not any(
        path.name == "site-packages" for path in install.version().rglob("site-packages")
    )


def test_environment_creation_pins_private_python_and_sanitizes_ambient_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "install")
    _release(install, "rel_base")
    monkeypatch.setenv("VIRTUAL_ENV", "foreign")
    monkeypatch.setenv("VBOT_UPDATE_HANDOFF", "ticket")

    command, environment = dependencies.environment_creation_command(
        install, tmp_path / "managed environment"
    )

    assert command == [
        str(install.interpreter()),
        "-m",
        "uv",
        "venv",
        "--python",
        str(install.version() / "runtime" / ("python.exe" if os.name == "nt" else "bin/python3")),
        "--seed",
        str(tmp_path / "managed environment"),
    ]
    assert "VIRTUAL_ENV" not in environment
    assert "VBOT_UPDATE_HANDOFF" not in environment
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_prepare_reresolves_the_saved_recipe_for_new_runtime_before_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "install")
    _release(install, "rel_base")
    _release(install, "rel_new", extra=True)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("extension-pkg==1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_uv(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        return _fake_uv(arguments, **kwargs)

    monkeypatch.setattr(dependencies.subprocess, "run", fake_uv)
    dependencies.install_dependencies(install, requirements)
    dependencies.prepare_for_version(install, "rel_new")

    assert len(calls) == 2
    assert calls[1][0] == str(install.interpreter("rel_new"))
    assert dependencies.status(install)["available"] is True


def test_conflicting_runtime_distribution_rejects_recipe_without_active_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "install")
    _release(install, "rel_base")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("extension-pkg==1\n", encoding="utf-8")

    def conflicting(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        result = _fake_uv(arguments)
        site = Path(arguments[arguments.index("--target") + 1])
        (site / "base_pkg-1.0.dist-info" / "METADATA").write_text(
            "Name: base-pkg\nVersion: 2.0\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(dependencies.subprocess, "run", conflicting)

    with pytest.raises(ApplicationError, match="conflicts with runtime"):
        dependencies.install_dependencies(install, requirements)
    runtime = runtime_dependencies(install.version())
    assert not (
        install.root / "data" / "extension-dependencies" / runtime.fingerprint / "active.json"
    ).exists()


def test_client_installation_has_no_dependency_recipe(tmp_path: Path) -> None:
    install = Installation(tmp_path, "desktop-client", None, None, None)

    assert dependencies.status(install) == {"available": False, "reason": "desktop_client"}
    with pytest.raises(ApplicationError, match="Desktop Client"):
        dependencies.install_dependencies(install, tmp_path / "requirements.txt")
