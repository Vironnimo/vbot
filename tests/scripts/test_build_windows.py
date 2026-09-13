from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import build_windows


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    for directory in ("core", "cli", "server", "desktop", "resources", "webui/dist"):
        path = source / directory
        path.mkdir(parents=True)
        (path / "included.txt").write_text(directory, encoding="utf-8")
    (source / "desktop" / "icon.ico").write_bytes(b"icon")
    (source / "core" / "__pycache__").mkdir()
    (source / "core" / "__pycache__" / "bad.pyc").write_bytes(b"bad")
    (source / "tests").mkdir()
    (source / "docs").mkdir()
    for filename in build_windows.APP_FILES:
        (source / filename).write_text(filename, encoding="utf-8")
    return source


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    (runtime / "Lib" / "venv").mkdir()
    (runtime / "Lib" / "venv" / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "Lib" / "ensurepip").mkdir()
    (runtime / "Lib" / "ensurepip" / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "python.exe").write_bytes(b"python")
    (runtime / "python313.dll").write_bytes(b"dll")
    return runtime


@pytest.mark.parametrize(
    ("shape", "present", "absent"),
    [
        (
            "server",
            ("server", "resources", "webui/dist", "desktop/icon.ico"),
            ("desktop/included.txt",),
        ),
        ("server-desktop", ("server", "resources", "webui/dist", "desktop"), ()),
        ("desktop-client", ("desktop",), ("server", "resources", "webui")),
    ],
)
def test_copy_application_respects_shape_and_exclusions(
    tmp_path: Path, shape: str, present: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    destination = tmp_path / "app"
    build_windows.copy_application(_source(tmp_path), destination, shape)

    for relative in present:
        assert (destination / relative).exists()
    for relative in absent:
        assert not (destination / relative).exists()
    assert not (destination / "tests").exists()
    assert not (destination / "docs").exists()
    assert not (destination / "core" / "__pycache__").exists()


def test_copy_application_rejects_source_links(tmp_path: Path) -> None:
    source = _source(tmp_path)
    link = source / "core" / "linked.py"
    try:
        link.symlink_to(source / "core" / "included.txt")
    except OSError:
        pytest.skip("creating symlinks is unavailable")

    with pytest.raises(build_windows.BuildError, match="contains a link"):
        build_windows.copy_application(source, tmp_path / "app", "server")


def test_runtime_with_unowned_site_packages_is_rejected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "Lib" / "site-packages" / "ambient_package").mkdir()

    with pytest.raises(build_windows.BuildError, match="no vbot-runtime-inventory.json"):
        build_windows.copy_runtime(
            runtime,
            tmp_path / "copy",
            provision=False,
            app_source=_source(tmp_path),
            shape="server",
        )


def test_runtime_rejects_a_different_cpython_minor(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "python313.dll").rename(runtime / "python314.dll")

    with pytest.raises(build_windows.BuildError, match="CPython 3.13 x64"):
        build_windows.copy_runtime(
            runtime,
            tmp_path / "copy",
            provision=False,
            app_source=_source(tmp_path),
            shape="server",
        )


def test_runtime_omits_root_python_alias_links(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    for alias in ("python3.exe", "python3.13.exe"):
        try:
            (runtime / alias).symlink_to(runtime / "python.exe")
        except OSError:
            pytest.skip("creating symlinks is unavailable")

    destination = tmp_path / "copy"
    build_windows.copy_runtime(
        runtime,
        destination,
        provision=False,
        app_source=_source(tmp_path),
        shape="server",
    )

    assert (destination / "python.exe").read_bytes() == b"python"
    assert not (destination / "python3.exe").exists()
    assert not (destination / "python3.13.exe").exists()
    assert all((runtime / alias).is_symlink() for alias in ("python3.exe", "python3.13.exe"))


@pytest.mark.parametrize("relative", ("unrelated.exe", "Lib/python3.exe"))
def test_runtime_rejects_unexcluded_links(tmp_path: Path, relative: str) -> None:
    runtime = _runtime(tmp_path)
    link = runtime / relative
    try:
        link.symlink_to(runtime / "python.exe")
    except OSError:
        pytest.skip("creating symlinks is unavailable")

    with pytest.raises(build_windows.BuildError, match="contains a link"):
        build_windows.copy_runtime(
            runtime,
            tmp_path / "copy",
            provision=False,
            app_source=_source(tmp_path),
            shape="server",
        )


def test_runtime_provisioning_uses_shape_lock_with_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    lock = source / "scripts" / "windows" / "requirements-server.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("example==1.0 --hash=sha256:abc\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(build_windows, "_run", lambda command: commands.append(command))
    runtime = _runtime(tmp_path)

    build_windows.copy_runtime(
        runtime,
        tmp_path / "copy",
        provision=True,
        app_source=source,
        shape="server",
    )

    assert commands
    command = commands[0]
    assert command[:6] == [
        sys.executable,
        "-B",
        "-m",
        "pip",
        "--python",
        str(runtime / "python.exe"),
    ]
    assert "--require-hashes" in command
    assert "--only-binary=:all:" in command
    assert "--no-binary=proxy-tools" in command
    assert command[-2:] == ["-r", str(lock)]


def test_build_writes_complete_hashed_manifest_and_rooted_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    runtime = _runtime(tmp_path)

    def fake_compile(
        source_path: Path, output: Path, *, role: str, version: str, stable: bool = False
    ) -> None:
        del source_path, version
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"{role}:{stable}".encode())

    monkeypatch.setattr(build_windows, "compile_host", fake_compile)
    args = argparse.Namespace(
        source=str(source),
        runtime=str(runtime),
        output=str(tmp_path / "build"),
        shape="server",
        version="0.4.0",
        revision="abcdef1234567890",
        provision_dependencies=False,
        release_mode=False,
        signing_key_env="UNUSED",
        authenticode_command=None,
    )
    package = build_windows.build(args)
    version_root = package / "versions" / "v0_4_0_abcdef123456"
    manifest = json.loads((version_root / "release.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["bootstrap_protocol"] == 1
    assert manifest["platform"] == "windows-x86_64"
    assert manifest["install_shape"] == "server"
    assert set(manifest["files"]) == {
        path.relative_to(version_root).as_posix()
        for base in (version_root / "app", version_root / "runtime")
        for path in base.rglob("*")
        if path.is_file()
    }
    for relative, digest in manifest["files"].items():
        assert digest == hashlib.sha256((version_root / relative).read_bytes()).hexdigest()
    assert (package / "vBot.exe").read_bytes() == (
        version_root / "runtime" / "vBot.exe"
    ).read_bytes()

    archive = tmp_path / "build" / "artifacts" / "vbot-windows-x86_64-server.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "release.json" in names
    assert "app/core/included.txt" in names
    assert "runtime/vBot.Server.exe" in names
    assert all(not name.startswith("versions/") for name in names)


def test_release_build_fails_closed_without_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build_windows, "compile_host", lambda *args, **kwargs: args[1].write_bytes(b"exe")
    )
    monkeypatch.delenv("MISSING_SIGNING_KEY", raising=False)
    args = argparse.Namespace(
        source=str(_source(tmp_path)),
        runtime=str(_runtime(tmp_path)),
        output=str(tmp_path / "build"),
        shape="desktop-client",
        version="1.0.0",
        revision="abcdef",
        provision_dependencies=False,
        release_mode=True,
        signing_key_env="MISSING_SIGNING_KEY",
        authenticode_command=None,
    )
    with pytest.raises(build_windows.BuildError, match="requires signing key environment"):
        build_windows.build(args)


def test_release_archive_signature_covers_raw_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build_windows, "compile_host", lambda *args, **kwargs: args[1].write_bytes(b"exe")
    )
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("TEST_SIGNING_KEY", base64.b64encode(key.private_bytes_raw()).decode())
    monkeypatch.setattr(build_windows, "verify_release_source", lambda source, revision: None)
    args = argparse.Namespace(
        source=str(_source(tmp_path)),
        runtime=str(_runtime(tmp_path)),
        output=str(tmp_path / "build"),
        shape="desktop-client",
        version="1.0.0",
        revision="abcdef",
        provision_dependencies=False,
        release_mode=True,
        signing_key_env="TEST_SIGNING_KEY",
        authenticode_command=None,
    )
    build_windows.build(args)

    archive = tmp_path / "build" / "artifacts" / "vbot-windows-x86_64-desktop-client.zip"
    signature = base64.b64decode(
        archive.with_suffix(".zip.sig").read_text(encoding="ascii").strip(), validate=True
    )
    key.public_key().verify(signature, hashlib.sha256(archive.read_bytes()).digest())


def test_installer_bootstraps_application_and_stops_before_uninstall() -> None:
    script = (Path(build_windows.__file__).parent / "windows" / "vbot.iss").read_text(
        encoding="utf-8"
    )

    assert "application install --root" in script
    assert "ExpandConstant('{tmp}\\vbot-payload')" in script
    assert "--shape {#InstallShape}" in script
    assert "function PrepareToInstall(var NeedsRestart: Boolean): String;" in script
    assert "FileExists(ExpandConstant('{app}\\application.json'))" in script
    assert "Use vbot update" in script
    assert "function DestinationHasEntries(): Boolean;" in script
    assert "FindFirst(AddBackslash(ExpandConstant('{app}')) + '*'" in script
    assert "Choose an empty directory" in script
    assert "function InitializeUninstall(): Boolean;" in script
    assert "'server stop'" in script
    assert "SuppressibleMsgBox(" in script
    assert "MsgBox(" not in script.replace("SuppressibleMsgBox(", "")


def test_compile_host_constructs_msvc_abi_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    source = _source(tmp_path)
    monkeypatch.setattr(build_windows, "_tool", lambda name: name)
    monkeypatch.setattr(build_windows, "_run", lambda command: commands.append(list(command)))

    build_windows.compile_host(source, tmp_path / "vBot.Server.exe", role="server", version="2.3.4")

    assert commands[0][0] == "llvm-rc"
    assert any("VBOT_ICON_PATH" in argument for argument in commands[0])
    assert any("VBOT_MANIFEST_PATH" in argument for argument in commands[0])
    manifest = (Path(build_windows.__file__).parent / "windows" / "launcher.manifest").read_text(
        encoding="utf-8"
    )
    assert "<longPathAware" in manifest
    assert ">true</longPathAware>" in manifest
    assert commands[1][0] == "clang-cl"
    assert '/DVBOT_ROLE=L"server"' in commands[1]
    assert "/SUBSYSTEM:WINDOWS" in commands[2]

    commands.clear()
    build_windows.compile_host(source, tmp_path / "vBot.Python.exe", role="python", version="2.3.4")
    assert "/SUBSYSTEM:CONSOLE" in commands[2]
