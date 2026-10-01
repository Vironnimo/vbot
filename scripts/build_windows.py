"""Build a versioned, source-readable Windows application payload."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.application.packages import package_name
from cli.application.payload import APP_FILES, SHAPES, app_paths, copy_application
from cli.application.runtime_sqlite import RuntimeSQLiteError, provision_runtime_sqlite
from core.utils.processes import subprocess_creation_flags
from scripts.package_build import (
    CHANNELS,
    INVENTORY_NAME,
    BuildError,
    sign_archive,
    verify_release_source,
    write_archive,
    write_inventory,
    write_manifest,
    write_release_identity,
)
from scripts.package_build import version_id as safe_version_id
from scripts.windows.native_hosts import compile_hosts, run_tool

__all__ = ["APP_FILES", "BuildError", "app_paths", "copy_application"]

PLATFORM = "windows-x86_64"
SEARCH_TARGET = "x86_64-pc-windows-msvc"
RUNTIME_DLL = "python313.dll"


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
        raise BuildError("runtime must include venv and ensurepip for managed environments")
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
        run_tool(command)
        for package in ("core", "server", "cli", "desktop"):
            shutil.rmtree(site / package, ignore_errors=True)
        for metadata in site.glob("vbot-*.dist-info"):
            shutil.rmtree(metadata)
    write_inventory(destination, site)


def _runtime_python(runtime: Path) -> Path:
    candidates = (runtime / "python.exe", runtime / "python3.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BuildError("runtime does not contain python.exe")


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
    package: Path = output / PLATFORM / str(args.shape)
    if package.exists():
        shutil.rmtree(package)
    version_root = package / "versions" / safe_version_id(args.version, args.revision)
    copy_application(source, version_root / "app", args.shape, search_target=SEARCH_TARGET)
    copy_runtime(
        runtime,
        version_root / "runtime",
        provision=args.provision_dependencies,
        app_source=source,
        shape=args.shape,
    )
    _remove_runtime_caches(version_root / "runtime")
    compile_hosts(source, version_root / "runtime", version=args.version)
    for filename in ("vBot.exe", "vBot.GUI.exe"):
        shutil.copy2(version_root / "runtime" / filename, package / filename)
    if args.authenticode_command:
        for executable in [*package.glob("*.exe"), *version_root.glob("runtime/*.exe")]:
            run_tool(
                [part.replace("{file}", str(executable)) for part in args.authenticode_command]
            )
    manifest = write_manifest(
        version_root,
        version=args.version,
        revision=args.revision,
        platform=PLATFORM,
        shape=args.shape,
        channel=args.channel,
    )
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        version_root / "runtime" / INVENTORY_NAME,
        artifacts / f"vbot-{PLATFORM}-{args.shape}-runtime-inventory.json",
    )
    archive = artifacts / package_name(args.shape, PLATFORM)
    write_archive(version_root, archive)
    write_release_identity(artifacts, manifest)
    public_key = ""
    if args.release_mode:
        public_key = sign_archive(
            archive,
            args.signing_key_env,
            fallback=lambda archive, signature, key_environment: _sign_with_packaged_runtime(
                version_root / "runtime" / "vBot.Python.exe", archive, signature, key_environment
            ),
        )
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
    value.add_argument("--channel", choices=CHANNELS, default="release")
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
