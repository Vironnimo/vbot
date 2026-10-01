"""Build a versioned, source-readable Linux server package.

The runtime is the pinned python-build-standalone CPython of
``scripts/linux/python.lock.json``; its locked dependencies install from wheels
only. Build each platform on a host of that platform: the builder runs the
runtime it packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.application.payload import copy_application
from cli.application.state import package_name
from scripts.package_build import (
    CHANNELS,
    INVENTORY_NAME,
    PYTHON_VERSION,
    BuildError,
    remove_bytecode_caches,
    sign_archive,
    verify_release_source,
    write_archive,
    write_inventory,
    write_manifest,
    write_release_identity,
)
from scripts.package_build import version_id as safe_version_id

SHAPE = "server"
PLATFORMS = {
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "linux-x86_64": "x86_64-unknown-linux-musl",
}
PYTHON = f"python{PYTHON_VERSION}"
#: Lets every runtime invocation, also in isolated mode, import the application.
APPLICATION_PATH_FILE = "vbot-application.pth"
# Runtime parts a headless server never loads: headers, manuals, Tcl/Tk, the
# embedding library (the interpreter links CPython statically) and tests.
_PRUNED = frozenset(
    {
        "include",
        "share",
        "lib/pkgconfig",
        "lib/libpython3.so",
        f"lib/lib{PYTHON}.so",
        f"lib/lib{PYTHON}.so.1.0",
        f"lib/{PYTHON}/test",
        f"lib/{PYTHON}/idlelib",
        f"lib/{PYTHON}/tkinter",
        f"lib/{PYTHON}/turtledemo",
    }
)
_PRUNED_PREFIXES = ("lib/tcl", "lib/tk", "lib/itcl", "lib/thread", "lib/libtcl", "lib/libtk")


def _runtime_lock(source: Path, platform: str) -> dict[str, str]:
    lock = json.loads((source / "scripts" / "linux" / "python.lock.json").read_text("utf-8"))
    spec = lock["runtimes"].get(platform)
    if not isinstance(spec, dict):
        raise BuildError(f"no locked Python runtime for {platform}")
    return {"url": str(spec["url"]), "sha256": str(spec["sha256"])}


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch_runtime(spec: dict[str, str], cache: Path) -> Path:
    """Return the locked runtime archive, downloading it into *cache* when absent."""
    archive = cache / f"{spec['sha256']}.tar.gz"
    if archive.is_file() and _sha256(archive) == spec["sha256"]:
        return archive
    import httpx

    cache.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(".partial")
    with (
        httpx.stream("GET", spec["url"], follow_redirects=True, timeout=120) as response,
        partial.open("wb") as output,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            output.write(chunk)
    if _sha256(partial) != spec["sha256"]:
        partial.unlink()
        raise BuildError(f"the downloaded Python runtime does not match its lock: {spec['url']}")
    partial.replace(archive)
    return archive


def _pruned(relative: str) -> bool:
    return relative in _PRUNED or relative.startswith(_PRUNED_PREFIXES)


def _copy_runtime_tree(source: Path, destination: Path, top: Path) -> None:
    """Copy without links: a linked file becomes a copy of its target."""
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        relative = child.relative_to(top).as_posix()
        if _pruned(relative) or child.name == "__pycache__":
            continue
        target = destination / child.name
        if child.is_symlink():
            resolved = child.resolve()
            if not resolved.is_relative_to(top.resolve()) or not resolved.is_file():
                raise BuildError(f"runtime link does not name a runtime file: {relative}")
            shutil.copy2(resolved, target)
        elif child.is_dir():
            _copy_runtime_tree(child, target, top)
        elif child.is_file():
            shutil.copy2(child, target)


def extract_runtime(archive: Path, destination: Path) -> None:
    """Unpack the standalone runtime as a link-free tree with one ``bin/python3``."""
    with tempfile.TemporaryDirectory() as temporary:
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        top = Path(temporary) / "python"
        if not (top / "bin" / PYTHON).is_file():
            raise BuildError(f"the runtime archive has no bin/{PYTHON}")
        _copy_runtime_tree(top, destination, top)
    # One interpreter name: aliases would be byte copies, and only bin/python3
    # is the identity every vBot process runs as. Its tool scripts carry the
    # build machine's paths.
    binaries = destination / "bin"
    interpreter = binaries / PYTHON
    for path in binaries.iterdir():
        if path != interpreter:
            path.unlink()
    interpreter.rename(binaries / "python3")


def site_packages(runtime: Path) -> Path:
    return runtime / "lib" / PYTHON / "site-packages"


def provision_dependencies(runtime: Path, lock: Path) -> None:
    if not lock.is_file():
        raise BuildError(f"missing locked Linux runtime requirements: {lock}")
    site = site_packages(runtime)
    command = [
        str(runtime / "bin" / "python3"),
        "-I",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-compile",
        "--target",
        str(site),
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        str(lock),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise BuildError(f"dependency installation failed:\n{result.stdout}\n{result.stderr}")


def check_runtime(runtime: Path) -> None:
    """Run the packaged interpreter: it must import the app and use a WAL-safe SQLite."""
    from core.database._connections import is_wal_reset_vulnerable

    result = subprocess.run(
        [
            str(runtime / "bin" / "python3"),
            "-I",
            "-B",
            "-c",
            "import sqlite3, cli.main, server.main; print(sqlite3.sqlite_version)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise BuildError(f"the packaged runtime cannot start vBot:\n{result.stderr}")
    version = tuple(int(part) for part in result.stdout.strip().split("."))
    if is_wal_reset_vulnerable(version):
        raise BuildError(f"the packaged runtime's SQLite {result.stdout.strip()} is not WAL-safe")


def build(args: argparse.Namespace) -> Path:
    source = Path(str(args.source)).resolve()
    output = Path(str(args.output)).resolve()
    platform = str(args.platform)
    if args.release_mode:
        if not os.environ.get(args.signing_key_env):
            raise BuildError(
                f"--release-mode requires signing key environment {args.signing_key_env}"
            )
        verify_release_source(source, str(args.revision))
    package = output / platform / SHAPE
    if package.exists():
        shutil.rmtree(package)
    version_root = package / "versions" / safe_version_id(args.version, args.revision)
    copy_application(source, version_root / "app", SHAPE, search_target=PLATFORMS[platform])
    runtime = version_root / "runtime"
    archive = fetch_runtime(_runtime_lock(source, platform), Path(str(args.cache)).resolve())
    extract_runtime(archive, runtime)
    provision_dependencies(
        runtime, source / "scripts" / "linux" / f"requirements-{SHAPE}-{platform}.lock"
    )
    site = site_packages(runtime)
    # site-packages -> python3.X -> lib -> runtime -> the version's app.
    (site / APPLICATION_PATH_FILE).write_text("../../../../app\n", encoding="utf-8")
    write_inventory(runtime, site)
    bootstrap = runtime / "vbot"
    shutil.copyfile(source / "scripts" / "linux" / "vbot", bootstrap)
    bootstrap.chmod(0o755)
    check_runtime(runtime)
    remove_bytecode_caches(version_root)
    shutil.copy2(bootstrap, package / "vbot")
    manifest = write_manifest(
        version_root,
        version=args.version,
        revision=args.revision,
        platform=platform,
        shape=SHAPE,
        channel=args.channel,
    )
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        runtime / INVENTORY_NAME, artifacts / f"vbot-{platform}-{SHAPE}-runtime-inventory.json"
    )
    bundle = artifacts / package_name(SHAPE, platform)
    write_archive(version_root, bundle)
    write_release_identity(artifacts, manifest)
    public_key = sign_archive(bundle, args.signing_key_env) if args.release_mode else ""
    (artifacts / "release-public-key.txt").write_text(public_key + "\n", encoding="ascii")
    return package


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", required=True)
    value.add_argument("--output", required=True)
    value.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    value.add_argument("--version", required=True)
    value.add_argument("--revision", required=True)
    value.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "vbot-runtime-cache"))
    value.add_argument("--release-mode", action="store_true")
    value.add_argument("--channel", choices=CHANNELS, default="release")
    value.add_argument("--signing-key-env", default="VBOT_RELEASE_SIGNING_KEY")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    try:
        package = build(parser().parse_args(argv))
    except BuildError as exc:
        print(f"Linux package build failed: {exc}", file=sys.stderr)
        return 1
    print(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
