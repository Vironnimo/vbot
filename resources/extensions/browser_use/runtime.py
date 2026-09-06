"""Prepare the owned native client and browser without Node or shell shims."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx

VERSION = "0.36.0"
ASSETS = {
    "darwin-arm64": "b2106ab39db0838e7b1772f7f26f760518de56d09053150c56f9dddf15af997d",
    "darwin-x64": "45d9ac061a7d72e61eaff905326e2e19365f4dadb12142ea2f2d76d84689c708",
    "linux-arm64": "aeb556addca3903601a433de1acad3ace1c9c61d170084bf58d875884599a990",
    "linux-x64": "56d15181e51e00213f907fcf39707cfc76bfa804ff20f5a9373661c73f96de5e",
    "linux-musl-arm64": "1ca7e003c9cb185f174fc81e51a609db27c77e3bfe00a0edff60688f8cd14f88",
    "linux-musl-x64": "a20cc2a5202a48f5820372803dedbcd5f556dff7a89421f1b0f2612962b10718",
    "win32-x64.exe": "412ff72737a109e93f5304b0ff76c988fb6f1f451d0fc7e010577922bcc20ff3",
}


class SetupError(Exception):
    """Only a fixed setup stage crosses the Tool boundary, never subprocess output."""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(stage)


def asset_name() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    system = sys.platform
    if system == "win32" and arch == "arm64":
        arch = "x64"  # Windows ARM64 supports the upstream x64 executable.
    if system == "linux" and (
        platform.libc_ver()[0] == "musl" or Path("/etc/alpine-release").exists()
    ):
        system = "linux-musl"
    key = f"{system}-{arch}" + (".exe" if system == "win32" else "")
    if key not in ASSETS:
        raise SetupError("platform")
    return key


class BrowserRuntime:
    """One persistent installation per data directory; fresh Runs reuse it."""

    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve() / "artifacts" / "browser-use"
        if not self.root.resolve().is_relative_to(data_dir.resolve()):
            raise SetupError("directory")
        self._lock = threading.Lock()
        self.browser_cache = Path.home() / ".agent-browser" / "browsers"
        self._prepared: dict[str, tuple[str, str]] = {}
        self._failures: dict[str, list[SetupError]] = {}

    def ensure(self, mode: str, check: Callable[[], None]) -> tuple[str, str]:
        check()
        while not self._lock.acquire(timeout=0.1):
            check()
        try:
            check()
            cached = self._prepared.get(mode)
            if cached and all(not item or Path(item).is_file() for item in cached):
                return cached
            failures = self._failures.setdefault(mode, [])
            if len(failures) >= 2:
                raise failures[-1]
            try:
                with self._installation_lock(check):
                    binary = self._client(check)
                    chrome = ""
                    if mode == "managed":
                        with self._installation_lock(check, self.browser_cache):
                            chrome = self._browser(binary, check)
                    check()
                    result = (str(binary), chrome)
                    self._prepared[mode] = result
                    failures.clear()
                    return result
            except SetupError as error:
                failures.append(error)
                raise
            except (OSError, httpx.HTTPError) as error:
                failure = SetupError(
                    "network" if isinstance(error, httpx.HTTPError) else "filesystem"
                )
                failures.append(failure)
                raise failure from error
        finally:
            self._lock.release()

    @contextmanager
    def _installation_lock(
        self, check: Callable[[], None], directory: Path | None = None
    ) -> Iterator[None]:
        directory = self.root if directory is None else directory
        directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 600
        with (directory / "install.lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            while True:
                check()
                lock.seek(0)
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise SetupError("lock_timeout") from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _client(self, check: Callable[[], None]) -> Path:
        key = asset_name()
        directory = self.root / VERSION
        directory.mkdir(exist_ok=True)
        target = directory / ("agent-browser-" + key)
        expected = ASSETS[key]
        if (
            not target.is_file()
            or target.stat().st_size > 64 * 1024 * 1024
            or hashlib.sha256(target.read_bytes()).hexdigest() != expected
        ):
            url = f"https://github.com/vercel-labs/agent-browser/releases/download/v{VERSION}/{target.name}"
            self._download(url, target, expected, check)
        output = self._run([str(target), "--version"], check, timeout=20)
        if output.strip() != f"agent-browser {VERSION}":
            raise SetupError("client_version")
        return target

    def _download(self, url: str, target: Path, expected: str, check: Callable[[], None]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as destination:
                temporary = Path(destination.name)
                digest = hashlib.sha256()
                size = 0
                deadline = time.monotonic() + 300
                with httpx.stream("GET", url, follow_redirects=True, timeout=20) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(65536):
                        check()
                        size += len(chunk)
                        if size > 64 * 1024 * 1024 or time.monotonic() > deadline:
                            raise SetupError("download_limit")
                        digest.update(chunk)
                        destination.write(chunk)
                if digest.hexdigest() != expected:
                    raise SetupError("client_integrity")
            check()
            temporary.chmod(0o755)
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _browser(self, binary: Path, check: Callable[[], None]) -> str:
        # Debian/Raspberry Pi OS provide Chromium and its shared libraries together.
        # Native Chrome for Testing has no Linux ARM64 build in this backend version.
        if sys.platform == "linux":
            for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
                found = shutil.which(name)
                if found:
                    return found
            if shutil.which("apt-get"):
                prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
                self._run([*prefix, "apt-get", "update", "-y"], check)
                self._run([*prefix, "apt-get", "install", "-y", "chromium"], check)
                found = shutil.which("chromium")
                if found:
                    return found
                raise SetupError("system_browser")
            if platform.machine().lower() in {"aarch64", "arm64"}:
                raise SetupError("system_browser")
        browsers = self.browser_cache
        marker = self.root / "installed.json"
        suffixes = (
            "chrome.exe",
            "chrome-win64/chrome.exe",
            "chrome",
            "chrome-linux64/chrome",
            "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
            "Google Chrome for Testing",
            "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        )

        def find() -> Path | None:
            for directory in sorted(browsers.glob("chrome-*"), reverse=True):
                for suffix in suffixes:
                    candidate = directory / suffix
                    if candidate.is_file():
                        return candidate
            return None

        chrome = None
        if marker.is_file():
            try:
                record = json.loads(marker.read_text(encoding="utf-8"))
                candidate = browsers / record["path"]
                if (
                    record["client"] == VERSION
                    and candidate.resolve().is_relative_to(browsers.resolve())
                    and candidate.is_file()
                ):
                    chrome = candidate
            except (ValueError, KeyError, TypeError):
                pass
        if chrome is None:
            self._run([str(binary), "install"], check)
            chrome = find()
        if chrome is None:
            raise SetupError("browser_missing")
        if sys.platform != "win32":
            self._run([str(chrome), "--version"], check, timeout=20)
        check()
        staged = self.root / "installed.json.tmp"
        staged.write_text(
            json.dumps({"client": VERSION, "path": chrome.relative_to(browsers).as_posix()}),
            encoding="utf-8",
        )
        staged.replace(marker)
        return str(chrome)

    def _run(self, command: list[str], check: Callable[[], None], *, timeout: int = 600) -> str:
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "LANG",
            "LC_ALL",
        }
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment["DEBIAN_FRONTEND"] = "noninteractive"
        check()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                env=environment,
                cwd=self.root,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    check()
                    if time.monotonic() > deadline or output.tell() > 8 * 1024 * 1024:
                        raise SetupError("process_timeout")
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=0.1)
                check()
                if process.returncode:
                    raise SetupError("system_packages" if "apt-get" in command else "process_exit")
                output.seek(0)
                return output.read(65536).decode("utf-8", errors="replace")
            finally:
                if process.poll() is None:
                    if sys.platform == "win32":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
