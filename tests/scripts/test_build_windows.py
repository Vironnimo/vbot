from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cli.application.payload import NATIVE_SOURCE_FILES
from scripts import build_windows


def test_build_command_preserves_failure_output() -> None:
    with pytest.raises(build_windows.BuildError, match="build-tool-error"):
        build_windows._run(
            [sys.executable, "-c", "import sys; sys.stderr.write('build-tool-error'); sys.exit(7)"]
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console allocation")
def test_build_tool_has_no_console_when_builder_has_none(tmp_path: Path) -> None:
    probe = tmp_path / "console_probe.py"
    probe.write_text(
        "import ctypes, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from scripts import build_windows\n"
        "assert not ctypes.windll.kernel32.GetConsoleWindow()\n"
        "child = ('import ctypes, sys; '"
        "'sys.exit(23 if ctypes.windll.kernel32.GetConsoleWindow() else 0)')\n"
        "build_windows._run([sys.executable, '-c', child])\n"
        "print('windowless-build-completed')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(probe), str(Path(build_windows.__file__).parent.parent)],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "windowless-build-completed"


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    for directory in ("core", "cli", "server", "desktop", "resources", "webui/dist"):
        path = source / directory
        path.mkdir(parents=True)
        (path / "included.txt").write_text(directory, encoding="utf-8")
    (source / "desktop" / "icon.ico").write_bytes(b"icon")
    for relative in NATIVE_SOURCE_FILES:
        native_path = source / relative
        if not native_path.exists():
            native_path.parent.mkdir(parents=True, exist_ok=True)
            native_path.write_text(relative, encoding="utf-8")
    (source / "desktop" / "windows.config").write_text("dpi-config", encoding="utf-8")
    (source / "core" / "__pycache__").mkdir()
    (source / "core" / "__pycache__" / "bad.pyc").write_bytes(b"bad")
    (source / "tests").mkdir()
    (source / "docs").mkdir()
    for filename in build_windows.APP_FILES:
        (source / filename).write_text(filename, encoding="utf-8")
    native = source / "resources/native/ripgrep/fixture/x86_64-pc-windows-msvc/rg.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"verified fixture executable")
    artifact = {
        "binary_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
        "sha256": "0" * 64,
        "url": "https://example.invalid/never-download.zip",
    }
    (source / "resources/ripgrep.lock.json").write_text(
        json.dumps({"version": "fixture", "artifacts": {"x86_64-pc-windows-msvc": artifact}})
    )
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


def test_native_source_fingerprint_ignores_checkout_line_endings_but_detects_changes(tmp_path):
    source = _source(tmp_path)
    launcher = source / "scripts/windows/launcher.c"
    launcher.write_bytes(b"int main(void) {\n return 0;\n}\n")
    expected = build_windows.native_source_digest(source)
    launcher.write_bytes(launcher.read_bytes().replace(b"\n", b"\r\n"))
    assert build_windows.native_source_digest(source) == expected
    launcher.write_bytes(b"int main(void) { return 1; }\n")
    assert build_windows.native_source_digest(source) != expected


@pytest.mark.parametrize(
    ("shape", "present", "absent"),
    [
        (
            "server",
            ("server", "resources", "webui/dist", "desktop/icon.ico"),
            ("desktop/included.txt",),
        ),
        ("server-desktop", ("server", "resources", "webui/dist", "desktop/windows.config"), ()),
        ("desktop-client", ("desktop/windows.config",), ("server", "resources", "webui")),
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


def test_reused_assets_replace_generated_pages_without_overwriting_extension_source(tmp_path):
    from cli.application.payload import copy_application

    source = _source(tmp_path)
    assets = tmp_path / "verified-app"
    page = "resources/extensions/demo"
    for relative, value in {
        "webui/dist/index.html": "verified UI",
        f"{page}/web/page.html": "verified Extension page",
    }.items():
        target = assets / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    for relative, value in {
        f"{page}/ui/page.html": "unchanged UI source",
        f"{page}/backend.py": "new backend",
        f"{page}/web/stale.js": "stale generated file",
    }.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    shutil.rmtree(source / "webui" / "dist")
    destination = tmp_path / "candidate"
    copy_application(source, destination, "server", assets=assets)
    assert (destination / "webui/dist/index.html").read_text(encoding="utf-8") == "verified UI"
    assert (destination / page / "web/page.html").read_text(
        encoding="utf-8"
    ) == "verified Extension page"
    assert not (destination / page / "web/stale.js").exists()
    assert (destination / page / "backend.py").read_text(encoding="utf-8") == "new backend"


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


def test_runtime_copy_installs_the_pinned_sqlite_without_touching_the_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "DLLs").mkdir()
    (runtime / "DLLs" / "sqlite3.dll").write_bytes(b"cpython sqlite")
    source = _source(tmp_path)
    pinned = b"pinned sqlite"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("sqlite3.dll", pinned)
    archive = buffer.getvalue()
    (source / "scripts/windows/sqlite.lock.json").write_text(
        json.dumps(
            {
                "version": "3.53.4",
                "url": "https://sqlite.org/fixture.zip",
                "archive_sha3_256": hashlib.sha3_256(archive).hexdigest(),
                "library_sha256": hashlib.sha256(pinned).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("cli.application.runtime_sqlite._download", lambda _url: archive)

    destination = tmp_path / "copy"
    build_windows.copy_runtime(
        runtime, destination, provision=False, app_source=source, shape="server"
    )

    assert (destination / "DLLs" / "sqlite3.dll").read_bytes() == pinned
    assert (runtime / "DLLs" / "sqlite3.dll").read_bytes() == b"cpython sqlite"


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
    lock.parent.mkdir(parents=True, exist_ok=True)
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
    assert (package / "vBot.GUI.exe").read_bytes() == (
        version_root / "runtime/vBot.GUI.exe"
    ).read_bytes()
    assert "runtime/vBot.GUI.exe" in names
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

    assert (
        'Name: "{group}\\vBot Desktop"; Filename: "{app}\\vBot.GUI.exe"; Parameters: "desktop"'
        in script
    )
    assert "application install --root" in script
    assert "DefaultDirName={localappdata}\\Programs\\vBot" in script
    assert "{param:VBOTDATA|{%USERPROFILE}\\.vbot}" in script
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


@pytest.mark.parametrize("role", ["desktop", "server", "python"])
def test_native_host_manifest_matches_role(tmp_path: Path, monkeypatch, role: str) -> None:
    commands = []
    source = Path(build_windows.__file__).parent.parent
    monkeypatch.setattr(build_windows, "_tool", lambda name: name)
    monkeypatch.setattr(build_windows, "_run", lambda command: commands.append(list(command)))
    build_windows.compile_host(source, tmp_path / "host.exe", role=role, version="1.0.0")
    expected = "desktop.manifest" if role == "desktop" else "launcher.manifest"
    manifest_path = (source / "scripts" / "windows" / expected).resolve()
    assert f'/dVBOT_MANIFEST_PATH="{manifest_path}"' in commands[0]
    manifest = ElementTree.parse(manifest_path)
    settings = "{http://schemas.microsoft.com/SMI/2016/WindowsSettings}"
    long_paths = manifest.find(f".//{settings}longPathAware")
    assert long_paths is not None and long_paths.text == "true"
    dpi = manifest.find(f".//{settings}dpiAwareness")
    assert dpi is not None and dpi.text == "PerMonitorV2"
    compatibility = "{urn:schemas-microsoft-com:compatibility.v1}"
    os_entry = manifest.find(f".//{compatibility}supportedOS")
    assert os_entry is not None
    assert os_entry.attrib["Id"] == "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"


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
    assert "/SUBSYSTEM:CONSOLE" in commands[2]

    commands.clear()
    build_windows.compile_host(source, tmp_path / "vBot.Python.exe", role="python", version="2.3.4")
    assert "/SUBSYSTEM:CONSOLE" in commands[2]


@pytest.mark.skipif(
    sys.platform != "win32" or not shutil.which("clang-cl") or not shutil.which("llvm-rc"),
    reason="Windows native compiler required",
)
@pytest.mark.parametrize("filename,role", build_windows.HOSTS.items())
def test_source_update_compiles_host_without_application_dependencies(tmp_path, filename, role):
    # Keep each native build independent instead of fitting all six compilers
    # into one normal per-test timeout on slower Windows runners.
    source = Path(build_windows.__file__).parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-E",
            "-S",
            "-c",
            "import sys\n"
            "from pathlib import Path\n"
            "from scripts.build_windows import compile_host\n"
            "filename, role = sys.argv[2:4]\n"
            "compile_host(Path.cwd(), Path(sys.argv[1]) / filename, "
            "role=role, version='0.4.3', stable=filename == 'vBot.exe')\n",
            str(tmp_path / "native hosts"),
            filename,
            role,
        ],
        cwd=source,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert {path.name for path in (tmp_path / "native hosts").glob("*.exe")} == {filename}


@pytest.mark.skipif(
    sys.platform != "win32" or not shutil.which("clang-cl") or not shutil.which("llvm-rc"),
    reason="Windows native compiler required",
)
@pytest.mark.parametrize("role, stable", [("host", True), ("update", False), ("server", False)])
def test_native_startup_failure_exits_and_reports_stderr_without_a_dialog(tmp_path, role, stable):
    root = tmp_path / "native-failure"
    output = (
        root / "vBot.exe"
        if stable
        else root / "versions" / "rel_test" / "runtime" / f"vBot.{role.title()}.exe"
    )
    source = Path(build_windows.__file__).parent.parent
    build_windows.compile_host(source, output, role=role, version="0.4.3", stable=stable)
    if stable:
        (root / "active-version").write_text("rel_test\n", encoding="ascii")
    process = subprocess.Popen(
        [str(output), "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert process.returncode == 111
    assert not stdout
    assert b"[ERROR]" in stderr
    assert str(output).encode("utf-8") in stderr


@pytest.mark.skipif(
    sys.platform != "win32" or not shutil.which("clang-cl") or not shutil.which("llvm-rc"),
    reason="Windows native compiler required",
)
@pytest.mark.parametrize(
    "role, stable", [("host", True), ("update", False), ("gui", True), ("server", False)]
)
def test_native_redirected_output_preserves_unicode(tmp_path, role, stable):
    import sysconfig

    root = tmp_path / "native-encoding"
    runtime = root / "versions" / "rel_test" / "runtime"
    runtime.mkdir(parents=True)
    python_root = Path(sys.base_prefix)
    dll_name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    shutil.copy2(python_root / dll_name, runtime / dll_name)
    for dependency in python_root.glob("vcruntime*.dll"):
        shutil.copy2(dependency, runtime / dependency.name)
    shutil.copytree(
        Path(sysconfig.get_path("stdlib")),
        runtime / "Lib",
        ignore=shutil.ignore_patterns(
            "__pycache__", "site-packages", "test", "tests", "ensurepip", "idlelib", "tkinter"
        ),
    )
    output = root / "vBot.exe" if stable else runtime / f"vBot.{role.title()}.exe"
    (root / "active-version").write_text("rel_test\n", encoding="ascii")
    build_windows.compile_host(
        Path(build_windows.__file__).parent.parent,
        output,
        role=role,
        version="0.4.3",
        stable=stable and role != "gui",
    )
    expected = "caf\u00e9 \u2014 \u65e5\u672c"
    result = subprocess.run(
        [str(output), "-c", f"import sys; print(sys.flags.utf8_mode); print({expected!r})"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").splitlines() == ["1", expected]

    if role in {"gui", "server"}:
        shutil.copytree(python_root / "DLLs", runtime / "DLLs")
    if role == "server":
        _assert_native_server_pseudoterminals_are_windowless(output)

    if role == "gui":
        import struct

        executable = output.read_bytes()
        pe = struct.unpack_from("<I", executable, 0x3C)[0]
        assert struct.unpack_from("<H", executable, pe + 24 + 68)[0] == 2
        app = runtime.parent / "app" / "cli"
        app.mkdir(parents=True)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "main.py").write_text(
            "import ctypes, json, sys\n"
            "from pathlib import Path\n"
            "Path(sys.argv[-1]).write_text(json.dumps({"
            "'args': sys.argv[1:-1], 'console': ctypes.windll.kernel32.GetConsoleWindow(), "
            "'module': __file__}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        for version in ("rel_test", "rel_next"):
            if version == "rel_next":
                runtime.parent.rename(root / "versions" / version)
                (root / "active-version").write_text(version + "\n", encoding="ascii")
            report = tmp_path / f"desktop report {version}.json"
            result = subprocess.run(
                [str(output), "desktop", "--host", "example.test", str(report)],
                cwd=tmp_path,
                capture_output=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            observed = json.loads(report.read_text(encoding="utf-8"))
            assert observed["args"] == ["desktop", "--host", "example.test"]
            assert observed["console"] == 0
            assert version in Path(observed["module"]).parts


def _assert_native_server_pseudoterminals_are_windowless(executable: Path) -> None:
    import winpty

    # The isolated native fixture loads only its copied standard library. Make
    # this interpreter's installed PTY dependency available explicitly.
    dependency_root = str(Path(winpty.__file__).parent.parent)
    probe = (
        "import ctypes, gc, os, sys\n"
        "from ctypes import wintypes\n"
        "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "kernel32.GetConsoleWindow.argtypes = []\n"
        "kernel32.GetConsoleWindow.restype = wintypes.HWND\n"
        "kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), "
        "wintypes.DWORD]\n"
        "kernel32.GetConsoleProcessList.restype = wintypes.DWORD\n"
        "def assert_windowless_console():\n"
        "    processes = (wintypes.DWORD * 16)()\n"
        "    count = kernel32.GetConsoleProcessList(processes, len(processes))\n"
        "    assert count > 0, 'Native server must already have an attached console'\n"
        "    assert not kernel32.GetConsoleWindow(), 'Native server must have no console window'\n"
        # This precondition fails for a GUI-subsystem regression before pywinpty
        # can allocate a visible console on the developer's desktop.
        "assert_windowless_console()\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from winpty import PtyProcess\n"
        "for index in range(2):\n"
        "    marker = f'native-pty-{index}'\n"
        "    process = PtyProcess.spawn([os.environ['COMSPEC'], '/d', '/q', '/c', "
        "'echo ' + marker])\n"
        "    try:\n"
        "        assert_windowless_console()\n"
        "        process.fileobj.settimeout(5)\n"
        "        output = ''\n"
        "        while marker not in output:\n"
        "            output += process.read()\n"
        "        assert marker in output, output\n"
        "    finally:\n"
        "        process.close(force=True)\n"
        "    del process\n"
        "    gc.collect()\n"
        "    assert_windowless_console()\n"
        "print('windowless-pty-completed')\n"
    )
    result = subprocess.run(
        [str(executable), "-c", probe, dependency_root],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "windowless-pty-completed"
