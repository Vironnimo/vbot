"""Personal Antigravity login, CLI discovery and admitted request lifetimes."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any

from core.providers._usage_types import UsageWindow
from core.providers.errors import ProviderAuthError
from core.storage import TemporaryFileManager
from core.utils.errors import ProviderError
from core.utils.file_status import is_file_strict
from core.utils.processes import subprocess_creation_flags
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

_PROBES = BoundedWorkerPool(name="antigravity-probe", max_workers=4)
SUBSCRIPTION_SETTINGS: dict[str, object] = {"modelProvider": "", "useG1Credits": False}
_LOGIN = "Sign in with a personal Google AI subscription by running agy on the vBot server."
_ENV_KEYS = frozenset(
    [
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PROGRAMDATA",
        "USERNAME",
        "USER",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ]
)


def subscription_environment(home: Path) -> dict[str, str]:
    """Expose the OS keyring and network settings, without API/ADC credentials or personal hooks."""
    environment = {key: value for key, value in os.environ.items() if key.upper() in _ENV_KEYS}
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        GEMINI_HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        AGY_ADC_AUTH="false",
        # A disposable profile has no updater debounce marker. Its detached
        # version check otherwise opens Windows Terminal on every invocation.
        AGY_CLI_DISABLE_AUTO_UPDATE="true",
    )
    if os.name == "nt":
        environment.update(APPDATA=str(home), LOCALAPPDATA=str(home))
    return environment


class _LoginError(ProviderAuthError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _executable() -> Path:
    explicit = os.environ.get("VBOT_ANTIGRAVITY_PATH")
    command = "agy.exe" if os.name == "nt" else "agy"
    if explicit:
        candidate = Path(explicit).expanduser()
    elif found := shutil.which(command):
        candidate = Path(found)
    elif os.name == "nt" and (local := os.environ.get("LOCALAPPDATA")):
        candidate = Path(local) / "agy" / "bin" / command
    else:
        candidate = Path.home() / ".local" / "bin" / command
    if (os.name == "nt" and candidate.suffix.lower() != ".exe") or not is_file_strict(candidate):
        raise _LoginError(
            "missing",
            "Install the native Antigravity CLI on the vBot server or set VBOT_ANTIGRAVITY_PATH "
            "to its executable.",
        )
    return candidate.resolve()


def _command(executable: Path, name: str) -> dict[str, Any]:
    # Both documented read-only commands produce a zero-turn JSON envelope.
    arguments = ["models"] if name == "models" else ["-p", "/usage"]
    settings = json.dumps({**SUBSCRIPTION_SETTINGS, "disableSlashCommands": False})
    with tempfile.TemporaryDirectory(prefix="vbot-agy-probe-") as directory:
        try:
            result = subprocess.run(
                [str(executable), "--settings", settings, "--output-format", "json", *arguments],
                cwd=directory,
                env=subscription_environment(Path(directory)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=subprocess_creation_flags(),
            )
        except subprocess.TimeoutExpired:
            raise _LoginError(
                "unavailable", f"Antigravity did not answer the {name} check. {_LOGIN}"
            ) from None
    if result.returncode:
        # The CLI can include account data and authorization URLs in diagnostics.
        reason = (result.stderr + result.stdout).lower()
        if any(
            word in reason
            for word in (b"sign in", b"log in", b"not logged", b"authenticat", b"oauth")
        ):
            raise _LoginError("logged_out", _LOGIN)
        if b"flag" in reason and b"settings" in reason:
            raise _LoginError(
                "incompatible", "Update Antigravity to a version with --settings support."
            )
        raise _LoginError("unavailable", f"Antigravity failed its {name} check. {_LOGIN}")
    try:
        payload = json.loads(result.stdout)
    except ValueError, UnicodeError:
        payload = None
    if (
        isinstance(payload, dict)
        and payload.get("status") == "SUCCESS"
        and payload.get("num_turns") == 0
        and isinstance(command := payload.get("command"), dict)
        and command.get("name") == name
        and isinstance(data := command.get("data"), dict)
    ):
        return data
    raise _LoginError("incompatible", f"Antigravity returned an unrecognized {name} response.")


def _windows(executable: Path) -> tuple[UsageWindow, ...]:
    groups = _command(executable, "usage").get("groups")
    result: list[UsageWindow] = []
    for group in groups if isinstance(groups, list) else ():
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        buckets = group.get("buckets")
        for bucket in buckets if isinstance(buckets, list) else ():
            if not isinstance(bucket, dict):
                continue
            window, remaining = bucket.get("window"), bucket.get("remaining_fraction")
            if (
                window not in ("5h", "weekly")
                or not isinstance(remaining, (int, float))
                or isinstance(remaining, bool)
                or not 0 <= remaining <= 1
            ):
                continue
            reset = bucket.get("reset_time")
            result.append(
                UsageWindow(
                    label=f"{group['name']} ({window})",
                    used_percent=round(100 * (1 - remaining), 6),
                    window_seconds=18_000 if window == "5h" else 604_800,
                    reset_at=reset if isinstance(reset, str) else None,
                )
            )
    # The official Free plan also has a weekly limit. Only paid personal plans
    # report both a five-hour allowance and a weekly allowance.
    if {window.window_seconds for window in result} != {18_000, 604_800}:
        raise _LoginError(
            "subscription_unconfirmed",
            "Antigravity did not report personal five-hour and weekly subscription quotas. "
            + _LOGIN,
        )
    return tuple(result)


class AntigravityNative:
    """An enrolled CLI's probes and stateless turns, retired as a unit on disconnect."""

    def __init__(
        self,
        *,
        temporary_files: TemporaryFileManager | None,
        on_auth_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self.temporary_files = temporary_files
        self._on_auth_error = on_auth_error
        self._closed = False
        self._capacity = asyncio.Semaphore(16)
        self._leases: set[asyncio.Future[None]] = set()
        self._turns: set[asyncio.Task[None]] = set()

    def _check_open(self) -> None:
        if self._closed:
            raise _LoginError("disabled", "The Antigravity Connection was closed.")

    @asynccontextmanager
    async def _lease(self) -> AsyncIterator[None]:
        self._check_open()
        done = asyncio.get_running_loop().create_future()
        self._leases.add(done)
        try:
            async with self._capacity:
                self._check_open()
                yield
                self._check_open()
        finally:
            self._leases.discard(done)
            done.set_result(None)

    async def probe(self) -> dict[str, Any]:
        try:
            async with self.turn() as executable:
                catalog = await _PROBES.run(_command, executable, "models")
                if not isinstance(catalog.get("models"), list):
                    raise _LoginError("incompatible", "Antigravity did not return a Model catalog.")
                return {
                    "connected": True,
                    "subscription": True,
                    "status": "ready",
                    "models": catalog["models"],
                }
        except _LoginError as exc:
            return {"connected": False, "status": exc.status, "message": str(exc)}
        except OSError:
            return {
                "connected": False,
                "status": "unavailable",
                "message": "Cannot start the Antigravity CLI on the vBot server.",
            }

    async def usage(self) -> tuple[UsageWindow, ...]:
        async with self._lease():
            return await _PROBES.run(_windows, await _PROBES.run(_executable))

    async def stream[T](self, start: Callable[[Path], AsyncGenerator[T]]) -> AsyncGenerator[T]:
        """Keep a stable producer across Chat's independently timed reads."""
        self._check_open()
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=1)

        async def produce() -> None:
            try:
                async with self.turn() as executable, aclosing(start(executable)) as stream:
                    async for event in stream:
                        await queue.put(event)
            except ProviderAuthError as exc:
                if not self._closed and self._on_auth_error is not None:
                    status = exc.status if isinstance(exc, _LoginError) else "logged_out"
                    self._on_auth_error(status, str(exc))
                raise

        producer = asyncio.create_task(produce(), name="antigravity-turn")
        self._turns.add(producer)
        try:
            while not producer.done() or not queue.empty():
                if self._closed:
                    raise ProviderError("The Antigravity Connection was closed.")
                if not queue.empty():
                    yield queue.get_nowait()
                    continue
                read = asyncio.create_task(queue.get())
                try:
                    await asyncio.wait((producer, read), return_when=asyncio.FIRST_COMPLETED)
                    self._check_open()
                    if read.done():
                        yield read.result()
                finally:
                    if not read.done():
                        read.cancel()
                        await settle_before_cancelling(asyncio.gather(read, return_exceptions=True))
            if producer.cancelled() and self._closed:
                raise ProviderError("The Antigravity Connection was closed.")
            producer.result()
        finally:
            producer.cancel()
            try:
                await settle_before_cancelling(asyncio.gather(producer, return_exceptions=True))
            finally:
                self._turns.discard(producer)

    @asynccontextmanager
    async def turn(self) -> AsyncIterator[Path]:
        async with self._lease():
            try:
                executable = await _PROBES.run(_executable)
                await _PROBES.run(_windows, executable)
            except OSError as exc:
                raise _LoginError(
                    "unavailable", "Cannot start the Antigravity CLI on the vBot server."
                ) from exc
            yield executable

    def abort(self) -> None:
        self._closed = True
        for producer in self._turns:
            producer.cancel()

    async def aclose(self) -> None:
        self.abort()
        if self._leases or self._turns:
            await settle_before_cancelling(
                asyncio.gather(*self._leases, *self._turns, return_exceptions=True)
            )
