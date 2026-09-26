"""Build a versioned, source-readable Windows application payload."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.application.payload import (
    APP_FILES,
    SHAPES,
    PayloadError,
    app_paths,
    copy_application,
    native_source_digest,
)
from cli.application.runtime_sqlite import RuntimeSQLiteError, provision_runtime_sqlite
from core.utils.processes import subprocess_creation_flags

__all__ = ["APP_FILES", "BuildError", "app_paths", "copy_application"]

HOSTS = {
    "vBot.Server.exe": "server",
    "vBot.Desktop.exe": "desktop",
    "vBot.Update.exe": "update",
    "vBot.Python.exe": "python",
    "vBot.exe": "host",
    "vBot.GUI.exe": "gui",
}
INVENTORY_NAME = "vbot-runtime-inventory.json"
RUNTIME_DLL = "python313.dll"

BuildError = PayloadError


def _safe_version_id(version: str, revision: str) -> str:
    clean_version = re.sub(r"[^a-z0-9]+", "_", version.lower()).strip("_")
    clean_revision = re.sub(r"[^a-z0-9]", "", revision.lower())[:12]
    value = f"v{clean_version}_{clean_revision}"
    if not clean_version or not clean_revision or len(value) > 128:
        raise BuildError("version and revision must form a safe application version id")
    return value


def _copy_tree(
    source: Path, destination: Path, *, excluded_root_entries: frozenset[str] = frozenset()
) -> None:
    if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
        raise BuildError(f"runtime payload contains a link: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if child.name in excluded_root_entries:
            continue
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            raise BuildError(f"runtime payload contains a link: {child}")
        target = destination / child.name
        if child.is_dir():
            _copy_tree(child, target)
        elif child.is_file():
            shutil.copy2(child, target)


def _site_packages(runtime: Path) -> Path:
    return runtime / "Lib" / "site-packages"


def copy_runtime(
    source: Path, destination: Path, *, provision: bool, app_source: Path, shape: str
) -> None:
    if not source.is_dir():
        raise BuildError("runtime must be a prepared CPython directory")
    root_aliases = (
        frozenset({"python3.exe", "python3.13.exe"})
        if (source / "python.exe").is_file()
        else frozenset()
    )
    _copy_tree(source, destination, excluded_root_entries=root_aliases)
    _runtime_python(destination)
    if not all(
        (destination / "Lib" / module / "__init__.py").is_file() for module in ("venv", "ensurepip")
    ):
        raise BuildError("runtime must include venv and ensurepip for customization")
    if not (destination / RUNTIME_DLL).is_file():
        raise BuildError("runtime must be CPython 3.13 x64")
    try:
        provision_runtime_sqlite(destination, app_source)
    except (OSError, RuntimeSQLiteError) as error:
        raise BuildError(f"runtime SQLite provisioning failed: {error}") from error
    site = _site_packages(destination)
    source_site = _site_packages(source)
    has_packages = source_site.is_dir() and any(
        item.name not in {"pip", "setuptools", "wheel"}
        and not item.name.startswith(("pip-", "setuptools-", "wheel-"))
        for item in source_site.iterdir()
    )
    if has_packages and not (source / INVENTORY_NAME).is_file():
        if provision:
            shutil.rmtree(site, ignore_errors=True)
            has_packages = False
        else:
            raise BuildError(
                f"runtime site-packages has no {INVENTORY_NAME}; "
                "use a clean runtime and --provision-dependencies"
            )
    if provision:
        site.mkdir(parents=True, exist_ok=True)
        lock = app_source / "scripts" / "windows" / f"requirements-{shape}.lock"
        if not lock.is_file():
            raise BuildError(f"missing locked Windows runtime requirements: {lock}")
        python = _runtime_python(source)
        command = [
            sys.executable,
            "-B",
            "-m",
            "pip",
            "--python",
            str(python),
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--target",
            str(site),
            "--require-hashes",
            "--only-binary=:all:",
            "--no-binary=proxy-tools",
            "-r",
            str(lock),
        ]
        _run(command)
        for package in ("core", "server", "cli", "desktop"):
            shutil.rmtree(site / package, ignore_errors=True)
        for metadata in site.glob("vbot-*.dist-info"):
            shutil.rmtree(metadata)
    inventory = dependency_inventory(site)
    (destination / INVENTORY_NAME).write_text(
        json.dumps({"schema_version": 1, "packages": inventory}, indent=2) + "\n", encoding="utf-8"
    )


def _runtime_python(runtime: Path) -> Path:
    candidates = (runtime / "python.exe", runtime / "python3.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BuildError("runtime does not contain python.exe")


def dependency_inventory(site: Path) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    if not site.is_dir():
        return packages
    for metadata in sorted(site.glob("*.dist-info"), key=lambda item: item.name.casefold()):
        name = version = None
        metadata_file = metadata / "METADATA"
        if metadata_file.is_file():
            for line in metadata_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                if name and version:
                    break
        if name and version:
            packages.append({"name": name, "version": version})
    return packages


def _sign_with_packaged_runtime(
    executable: Path, archive: Path, signature: Path, key_environment: str
) -> str:
    script = (
        "import base64,hashlib,os,sys;"
        "from cryptography.hazmat.primitives import serialization;"
        "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey;"
        "key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ[sys.argv[3]],"
        "validate=True));digest=hashlib.sha256(open(sys.argv[1],'rb').read()).digest();"
        "open(sys.argv[2],'w',encoding='ascii').write(base64.b64encode(key.sign(digest)).decode()+'\\n');"
        "print(base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,"
        "serialization.PublicFormat.Raw)).decode())"
    )
    result = subprocess.run(
        [str(executable), "-c", script, str(archive), str(signature), key_environment],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=subprocess_creation_flags(),
    )
    if result.returncode != 0:
        raise BuildError(f"packaged release signer failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _remove_runtime_caches(runtime: Path) -> None:
    for directory in runtime.rglob("__pycache__"):
        if directory.is_dir():
            shutil.rmtree(directory)
    for suffix in ("*.pyc", "*.pyo"):
        for path in runtime.rglob(suffix):
            path.unlink()


def verify_release_source(source: Path, revision: str) -> None:
    """Bind a signed artifact to the exact clean tracked source revision."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=subprocess_creation_flags(),
    )
    if head.returncode or revision.lower() != head.stdout.strip().lower():
        raise BuildError("release revision must equal the source checkout HEAD")
    tracked_paths = [
        "core",
        "server",
        "cli",
        "desktop",
        "resources",
        "pyproject.toml",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "scripts/build_windows.py",
        "scripts/windows",
    ]
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *tracked_paths],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=subprocess_creation_flags(),
    )
    if status.returncode or status.stdout.strip():
        raise BuildError("release mode requires clean tracked application sources")


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BuildError(f"required Windows build tool is unavailable: {name}")
    return path


def _run(command: Sequence[str]) -> None:
    environment = os.environ.copy()
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    result = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        env=environment,
        creationflags=subprocess_creation_flags(),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise BuildError(f"command failed ({command[0]}): {detail}")


def _version_resource_values(version: str) -> tuple[str, str]:
    parts = [int(value) for value in re.findall(r"\d+", version)[:4]]
    parts.extend([0] * (4 - len(parts)))
    return ",".join(map(str, parts)), ".".join(map(str, parts))


def compile_host(
    source: Path, output: Path, *, role: str, version: str, stable: bool = False
) -> None:
    # The GUI companion always resolves the installation pointer, including
    # when an older source updater invokes this compiler without the new flag.
    stable = stable or role == "gui"
    windows = source / "scripts" / "windows"
    icon = (source / "desktop" / "icon.ico").resolve()
    manifest = (
        windows / ("desktop.manifest" if role == "desktop" else "launcher.manifest")
    ).resolve()
    numeric, display = _version_resource_values(version)
    # CREATE_NO_WINDOW only gives console applications an invisible console.
    # A GUI server makes pywinpty allocate and then hide a visible one on start.
    subsystem = "CONSOLE" if role in {"python", "host", "server"} else "WINDOWS"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vbot-native-") as temporary:
        resource = Path(temporary) / "launcher.res"
        resource_command = [
            _tool("llvm-rc"),
            "/nologo",
            f'/dVBOT_ICON_PATH="{icon}"',
            f'/dVBOT_MANIFEST_PATH="{manifest}"',
            f"/dVBOT_FILE_VERSION={numeric}",
            f'/dVBOT_FILE_VERSION_STRING="{display}"',
            f'/dVBOT_PRODUCT_NAME="{output.stem}"',
            f"/fo{resource}",
            str(windows / "launcher.rc"),
        ]
        _run(resource_command)
        object_path = Path(temporary) / "launcher.obj"
        compile_command = [
            _tool("clang-cl"),
            "/nologo",
            "/c",
            "/std:c11",
            "/O2",
            "/DUNICODE",
            "/D_UNICODE",
            f'/DVBOT_ROLE=L"{role}"',
            str(windows / "launcher.c"),
            f"/Fo:{object_path}",
        ]
        if stable:
            compile_command.insert(8, "/DVBOT_STABLE_BOOTSTRAP")
        _run(compile_command)
        link_command = [
            _tool("clang-cl"),
            "/nologo",
            str(object_path),
            str(resource),
            f"/Fe:{output}",
            "/link",
            f"/SUBSYSTEM:{subsystem}",
            "shell32.lib",
            "kernel32.lib",
            "user32.lib",
        ]
        _run(link_command)


def _hashes(version_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    seen: set[str] = set()
    for base in (version_root / "app", version_root / "runtime"):
        for path in sorted(base.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file():
                continue
            relative = path.relative_to(version_root).as_posix()
            folded = relative.casefold()
            if folded in seen:
                raise BuildError(f"case-colliding payload path: {relative}")
            seen.add(folded)
            values[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def build(args: argparse.Namespace) -> Path:
    source = Path(str(args.source)).resolve()
    runtime = Path(str(args.runtime)).resolve()
    output = Path(str(args.output)).resolve()
    if args.release_mode:
        if not os.environ.get(args.signing_key_env):
            raise BuildError(
                f"--release-mode requires signing key environment {args.signing_key_env}"
            )
        verify_release_source(source, str(args.revision))
    version_id = _safe_version_id(args.version, args.revision)
    package: Path = output / "windows-x86_64" / str(args.shape)
    if package.exists():
        shutil.rmtree(package)
    version_root = package / "versions" / version_id
    copy_application(source, version_root / "app", args.shape)
    copy_runtime(
        runtime,
        version_root / "runtime",
        provision=args.provision_dependencies,
        app_source=source,
        shape=args.shape,
    )
    _remove_runtime_caches(version_root / "runtime")
    for filename, role in HOSTS.items():
        compile_host(
            source,
            version_root / "runtime" / filename,
            role=role,
            version=args.version,
            stable=role in {"host", "gui"},
        )
    for filename in ("vBot.exe", "vBot.GUI.exe"):
        shutil.copy2(version_root / "runtime" / filename, package / filename)
    manifest = {
        "schema_version": 1,
        "bootstrap_protocol": 1,
        "version_id": version_id,
        "version": args.version,
        "revision": args.revision,
        "platform": "windows-x86_64",
        "install_shape": args.shape,
        "native_source_digest": native_source_digest(source),
        "files": _hashes(version_root),
    }
    (version_root / "release.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    if args.authenticode_command:
        for executable in [*package.glob("*.exe"), *version_root.glob("runtime/*.exe")]:
            _run([part.replace("{file}", str(executable)) for part in args.authenticode_command])
        manifest["files"] = _hashes(version_root)
        (version_root / "release.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        version_root / "runtime" / INVENTORY_NAME,
        artifacts / f"vbot-windows-x86_64-{args.shape}-runtime-inventory.json",
    )
    archive = artifacts / f"vbot-windows-x86_64-{args.shape}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(version_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_file():
                bundle.write(path, path.relative_to(version_root).as_posix())
    public_key = ""
    if args.release_mode:
        encoded_key = os.environ.get(args.signing_key_env)
        assert encoded_key is not None
        signature = archive.with_suffix(archive.suffix + ".sig")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            public_key = _sign_with_packaged_runtime(
                version_root / "runtime" / "vBot.Python.exe",
                archive,
                signature,
                args.signing_key_env,
            )
        else:
            try:
                key = Ed25519PrivateKey.from_private_bytes(
                    base64.b64decode(encoded_key, validate=True)
                )
            except ValueError as exc:
                raise BuildError(
                    "release signing key must be a base64 raw Ed25519 private key"
                ) from exc
            digest = hashlib.sha256(archive.read_bytes()).digest()
            signature.write_text(
                base64.b64encode(key.sign(digest)).decode("ascii") + "\n", encoding="ascii"
            )
            public_key = base64.b64encode(
                key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode("ascii")
        if not public_key:
            raise BuildError("release signing key must be a base64 raw Ed25519 private key")
    (artifacts / "release-public-key.txt").write_text(public_key + "\n", encoding="ascii")
    return package


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", required=True)
    value.add_argument("--runtime", required=True)
    value.add_argument("--output", required=True)
    value.add_argument("--shape", required=True, choices=SHAPES)
    value.add_argument("--version", required=True)
    value.add_argument("--revision", required=True)
    value.add_argument("--provision-dependencies", action="store_true")
    value.add_argument("--release-mode", action="store_true")
    value.add_argument("--signing-key-env", default="VBOT_RELEASE_SIGNING_KEY")
    value.add_argument("--authenticode-command", nargs="+", metavar="ARG")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    try:
        package = build(parser().parse_args(argv))
    except BuildError as exc:
        print(f"Windows package build failed: {exc}", file=sys.stderr)
        return 1
    print(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
