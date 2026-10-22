"""Keep backend verification on the dependency versions shipped in packages.

This internal development helper refreshes/checks the package locks and their
test-only overlays, and installs them into an explicitly named virtual environment.
It never installs into the invoking interpreter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_PREFIX = "# vbot-dependency-input: "
PINS = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)", re.MULTILINE)
SERVER_EXTRAS = ("server", "cli", "application")


@dataclass(frozen=True)
class Lock:
    path: str
    target: str
    extras: tuple[str, ...]
    runtime: str = ""


RUNTIME_LOCKS = (
    Lock("scripts/windows/requirements-server.lock", "x86_64-pc-windows-msvc", SERVER_EXTRAS),
    Lock(
        "scripts/windows/requirements-server-desktop.lock",
        "x86_64-pc-windows-msvc",
        (*SERVER_EXTRAS, "desktop"),
    ),
    Lock(
        "scripts/windows/requirements-desktop-client.lock",
        "x86_64-pc-windows-msvc",
        ("cli", "application", "desktop"),
    ),
    *(
        Lock(
            f"scripts/linux/requirements-server-linux-{arch}.lock",
            f"{arch}-unknown-linux-gnu",
            SERVER_EXTRAS,
        )
        for arch in ("x86_64", "aarch64")
    ),
)
TEST_LOCKS = tuple(
    Lock(
        f"scripts/requirements-tests-{name}.lock",
        runtime.target,
        (*SERVER_EXTRAS, "dev"),
        runtime.path,
    )
    for name, runtime in zip(
        ("windows-x86_64", "linux-x86_64", "linux-aarch64"),
        (RUNTIME_LOCKS[0], *RUNTIME_LOCKS[3:]),
        strict=True,
    )
)


def _pins(text: str) -> dict[str, str]:
    return {re.sub(r"[-_.]+", "-", name).lower(): version for name, version in PINS.findall(text)}


def _constraints(lock: Lock) -> tuple[Lock, ...]:
    return tuple(runtime for runtime in RUNTIME_LOCKS if runtime.target == lock.target)


def _input_digest(root: Path, lock: Lock) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = project["project"]
    inputs = {
        "python": metadata["requires-python"],
        "target": lock.target,
        "dependencies": metadata["dependencies"],
        "extras": {extra: metadata["optional-dependencies"][extra] for extra in lock.extras},
    }
    if lock.runtime:
        inputs["build"] = project["build-system"]["requires"]
        inputs["runtime"] = {
            runtime.path: (root / runtime.path).read_text(encoding="utf-8")
            for runtime in _constraints(lock)
        }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def check(root: Path = ROOT) -> None:
    """Refuse stale inputs and disagreement between shared runtime/test pins."""
    pins: dict[str, dict[str, str]] = {}
    for lock in (*RUNTIME_LOCKS, *TEST_LOCKS):
        text = (root / lock.path).read_text(encoding="utf-8")
        if INPUT_PREFIX + _input_digest(root, lock) not in text.splitlines():
            raise ValueError(
                f"{lock.path} is stale; run python scripts/test_dependencies.py refresh"
            )
        pins[lock.path] = _pins(text)
        if not pins[lock.path]:
            raise ValueError(f"{lock.path} contains no pinned dependencies")
    server = pins[RUNTIME_LOCKS[0].path]
    for lock in RUNTIME_LOCKS[1:]:
        for name, version in pins[lock.path].items():
            if name in server and version != server[name]:
                raise ValueError(f"{lock.path}: {name} differs from the tested server runtime")
    for lock in TEST_LOCKS:
        overlap = pins[lock.path].keys() & pins[lock.runtime].keys()
        if overlap:
            raise ValueError(
                f"{lock.path} must not replace runtime dependencies: {sorted(overlap)}"
            )
        for runtime in _constraints(lock):
            for name, version in pins[lock.path].items():
                if name in pins[runtime.path] and version != pins[runtime.path][name]:
                    raise ValueError(f"{lock.path}: {name} differs from {runtime.path}")


def current_lock() -> Lock:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"} and sys.maxsize > 2**32:
        target = "windows-x86_64"
    elif sys.platform == "linux" and machine in {"x86_64", "aarch64"}:
        target = f"linux-{machine}"
    else:
        raise ValueError(f"no backend test lock for {sys.platform}/{machine}")
    for lock in TEST_LOCKS:
        if lock.path.endswith(f"-{target}.lock"):
            return lock
    raise ValueError(f"no backend test lock for {sys.platform}/{machine}")


def environment_key(root: Path = ROOT) -> str:
    check(root)
    lock = current_lock()
    digest = hashlib.sha256()
    for path in (lock.runtime, lock.path, "scripts/test_dependencies.py"):
        digest.update((root / path).read_bytes())
    digest.update(str(Path(sys.base_prefix).resolve()).encode())
    digest.update(sys.version.encode())
    return digest.hexdigest()[:20]


def environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def install(environment: Path, root: Path = ROOT) -> Path:
    """Install the complete shipped server closure and fixed test overlay in a venv."""
    check(root)
    lock = current_lock()
    environment = environment.resolve()
    if environment in (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()):
        raise ValueError("test dependencies require their own virtual environment")
    python = environment_python(environment)
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, cwd=root)
    if not (environment / "pyvenv.cfg").is_file():
        raise ValueError(f"not a virtual environment: {environment}")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            lock.runtime,
            "-r",
            lock.path,
        ],
        check=True,
        cwd=root,
    )
    # A cached WSL environment outlives its checkout; always rebind the editable source.
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "-e",
            ".",
        ],
        check=True,
        cwd=root,
    )
    subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
    return python


def refresh(root: Path = ROOT) -> None:
    """Resolve with the existing pinned uv, retaining current versions where possible."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    python = project["tool"]["mypy"]["python_version"]
    uv = next(
        dependency.removeprefix("uv==")
        for dependency in project["project"]["optional-dependencies"]["application"]
        if dependency.startswith("uv==")
    )
    installed = subprocess.run(
        [sys.executable, "-m", "uv", "--version"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    ).stdout.split()
    if len(installed) < 2 or installed[1] != uv:
        raise ValueError(f"lock refresh requires the declared uv=={uv}")
    for lock in (*RUNTIME_LOCKS, *TEST_LOCKS):
        command = [sys.executable, "-m", "uv", "pip", "compile", "pyproject.toml"]
        for extra in lock.extras:
            command.extend(("--extra", extra))
        command.extend(
            (
                "--python-version",
                python,
                "--python-platform",
                lock.target,
                "--generate-hashes",
                "--no-emit-package",
                "vbot",
            )
        )
        if lock.runtime:
            command.extend(("--only-binary", ":all:"))
            for runtime in _constraints(lock):
                command.extend(("--constraint", runtime.path))
            for name in _pins((root / lock.runtime).read_text(encoding="utf-8")):
                command.extend(("--no-emit-package", name))
        elif lock != RUNTIME_LOCKS[0]:
            command.extend(("--constraint", RUNTIME_LOCKS[0].path))
        command.extend(("--output-file", lock.path))
        subprocess.run(command, check=True, cwd=root, stdout=subprocess.DEVNULL)
        path = root / lock.path
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(INPUT_PREFIX)
        ]
        lines.insert(2, INPUT_PREFIX + _input_digest(root, lock))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    check(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "refresh", "install", "key"))
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "install":
            if args.environment is None:
                parser.error("install requires --environment")
            print(install(args.environment))
        elif args.action == "refresh":
            refresh()
        elif args.action == "key":
            print(environment_key())
        else:
            check()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"test dependencies: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
