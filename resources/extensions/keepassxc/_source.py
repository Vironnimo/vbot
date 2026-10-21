"""KeePassXC as a Secret Source: entries of a local database, read through keepassxc-cli.

Every call opens the database anew with the master password the user gave at
unlock; only this object holds it, in memory, until :meth:`KeePassXCSource.lock`.
References are entry paths (``Group/Sub/Title``) and containers are groups. The
settings are read live: a changed database or key file locks the source, since
the password belongs to the database it opened.

keepassxc-cli localizes its messages and exits with 1 on every failure, so no
decision reads its output text: a failed call is explained by checking whether
the database still opens and which entries it has.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from core.secrets.sources import (
    SOURCE_CAPABILITY_STORE,
    NewSourceEntry,
    SecretSourceError,
    SourceEntry,
    SourceFailure,
    SourceField,
    SourceStatus,
    UnlockField,
)

from ._cli import (
    CommandNotStartedError,
    CommandResult,
    CommandRunner,
    CommandTimeoutError,
    run_command,
)

SOURCE_NAME: Final = "keepassxc"
TIMEOUT_SECONDS: Final = 30.0
#: How long one entry listing serves later calls (a listing opens the database).
LISTING_SECONDS: Final = 15.0
_PROGRAM_SECONDS: Final = 30.0
#: ``keepassxc.cli`` is the command of the Snap package.
_PROGRAM_NAMES: Final = ("keepassxc-cli", "keepassxc.cli")
_SETTINGS_PLACE: Final = "the KeePassXC Extension settings (Settings -> Integrations -> Extensions)"

#: Source field -> (KeePassXC attribute, concealed). ``totp`` yields the stored
#: ``otpauth://`` URI; vBot derives the current code from it.
_FIELDS: Final[Mapping[str, tuple[str, bool]]] = {
    "username": ("UserName", False),
    "password": ("Password", True),
    "url": ("URL", False),
    "notes": ("Notes", True),
    "totp": ("otp", True),
}
#: Where entries set up before KeePassXC 2.6 keep their TOTP secret.
_LEGACY_TOTP_ATTRIBUTE: Final = "TOTP Seed"
_STORABLE_FIELDS: Final = ("username", "password", "url")


def standard_locations() -> tuple[str, ...]:
    """Where the KeePassXC installer puts keepassxc-cli outside PATH."""
    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
        return (str(Path(program_files) / "KeePassXC" / "keepassxc-cli.exe"),)
    return ()


@dataclass(frozen=True)
class _Unlocked:
    database: str
    key_file: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class _Listing:
    owner: _Unlocked
    entries: tuple[str, ...]
    groups: tuple[str, ...]
    expires: float


class KeePassXCSource:
    """The ``keepassxc`` Secret Source; safe to call from several worker threads."""

    def __init__(
        self,
        config: Callable[[], Mapping[str, Any]],
        *,
        logger: logging.Logger,
        runner: CommandRunner = run_command,
        which: Callable[[str], str | None] = shutil.which,
        locations: Sequence[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._logger = logger
        self._runner = runner
        self._which = which
        self._locations = tuple(standard_locations() if locations is None else locations)
        self._clock = clock
        self._lock = threading.Lock()
        # One keepassxc-cli process at a time: an added entry rewrites the file.
        self._cli_lock = threading.Lock()
        self._unlocked: _Unlocked | None = None
        self._lock_reason: str | None = None
        self._listing: _Listing | None = None
        self._program_cache: tuple[str, str | None, str | None, float] | None = None

    # -- SecretSource ----------------------------------------------------------

    @property
    def name(self) -> str:
        return SOURCE_NAME

    @property
    def label(self) -> str:
        return "KeePassXC"

    @property
    def unlock_fields(self) -> Sequence[UnlockField]:
        # A database opened by its key file alone needs no password.
        return (
            UnlockField(
                "password", "Master password", concealed=True, required=not self._path("key_file")
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({SOURCE_CAPABILITY_STORE})

    def status(self) -> SourceStatus:
        _program, problem = self._prerequisites()
        if problem is not None:
            return SourceStatus("unavailable", problem)
        if self._current() is None:
            with self._lock:
                return SourceStatus("locked", self._lock_reason)
        return SourceStatus("available")

    def unlock(self, inputs: Mapping[str, str]) -> SourceStatus:
        program, problem = self._prerequisites()
        if program is None or problem is not None:
            raise SecretSourceError("unavailable", problem or "keepassxc-cli was not found.")
        password = str(inputs.get("password") or "")
        key_file = self._path("key_file")
        if not password and not key_file:
            raise SecretSourceError("denied", "Enter the master password of the database.")
        if "\n" in password or "\r" in password:
            raise SecretSourceError(
                "denied", "keepassxc-cli cannot take a master password that contains a line break."
            )
        candidate = _Unlocked(self._path("database"), key_file, password)
        if not self._opens(program, candidate):
            what = "master password and key file" if key_file else "master password"
            raise SecretSourceError(
                "denied",
                f"KeePassXC did not open {Path(candidate.database).name} with this {what}. "
                f"Check the {what} and that the file is a KeePassXC database.",
            )
        with self._lock:
            self._unlocked = candidate
            self._lock_reason = None
            self._listing = None
        return SourceStatus("available")

    def lock(self) -> SourceStatus:
        with self._lock:
            self._drop(None)
        return self.status()

    def containers(self) -> Sequence[str]:
        return list(self._list().groups)

    def entries(self, container: str | None = None) -> Sequence[SourceEntry]:
        listing = self._list()
        group = (container or "").strip().strip("/")
        if group and group not in listing.groups:
            raise SecretSourceError("not_found", f"KeePassXC has no group {group} with entries.")
        fields = tuple(
            SourceField(name, concealed, optional=name != "password")
            for name, (_, concealed) in _FIELDS.items()
        )
        found = []
        for path in listing.entries:
            parent, _, title = path.rpartition("/")
            if group and parent != group:
                continue
            found.append(
                SourceEntry(
                    reference=path,
                    title=title,
                    kind="login",
                    fields=fields,
                    container=parent or None,
                )
            )
        return found

    def resolve(self, reference: str, field_name: str) -> str:
        program, unlocked = self._require_unlocked()
        path = reference.strip().strip("/")
        missing_code: SourceFailure = (
            "optional_field_missing"
            if field_name in _FIELDS and field_name != "password"
            else "not_found"
        )
        attributes = _attributes(field_name)
        for attribute in attributes:
            result = self._call(program, unlocked, "show", ["-s", "-a", attribute], [path])
            if result.returncode == 0:
                value = _single_value(result.stdout)
                if value:
                    return value
                if attribute != attributes[-1]:
                    continue
                raise SecretSourceError(
                    missing_code, f"The {field_name} field of the KeePassXC entry {path} is empty."
                )
        listing = self._list(refresh=True)
        if path not in listing.entries:
            raise SecretSourceError(
                "not_found",
                f"KeePassXC has no entry {path}. "
                "Bind the Secret to an existing entry in Settings -> Secrets.",
            )
        raise SecretSourceError(
            missing_code, f"The KeePassXC entry {path} has no {field_name} field."
        )

    def store(self, entry: NewSourceEntry) -> str:
        program, unlocked = self._require_unlocked()
        title = entry.title.strip()
        if not title or "/" in title or "\n" in title or "\r" in title:
            raise SecretSourceError(
                "unavailable", "A KeePassXC entry title needs text without a slash or line break."
            )
        values = {key.strip().lower(): value for key, value in entry.values.items() if value}
        unsupported = sorted(set(values) - set(_STORABLE_FIELDS))
        if unsupported:
            raise SecretSourceError(
                "unavailable",
                "vBot stores only a username, password and URL in a new KeePassXC entry, "
                f"not {', '.join(unsupported)}.",
            )
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise SecretSourceError(
                    "unavailable",
                    f"keepassxc-cli cannot store a {name} that contains a line break.",
                )
        group = (entry.container or "").strip().strip("/")
        path = f"{group}/{title}" if group else title
        if path in self._list(refresh=True).entries:
            raise SecretSourceError(
                "unavailable", f"KeePassXC already has an entry {path}; choose another title."
            )
        options = []
        if "username" in values:
            options.append(f"--username={values['username']}")
        if "url" in values:
            options.append(f"--url={values['url']}")
        new_password = ""
        if "password" in values:
            options.append("--password-prompt")
            new_password = values["password"] + "\n"
        try:
            result = self._call(program, unlocked, "add", options, [path], new_password)
        except SecretSourceError as error:
            if error.code != "timed_out":
                raise
            raise SecretSourceError(
                "timed_out",
                f"{error} KeePassXC may still have created the entry {path}; "
                "check the database before trying again.",
            ) from None
        finally:
            with self._lock:
                self._listing = None
        if result.returncode != 0:
            self._explain_failure(program, unlocked)
            where = f" Check that the group {group} exists." if group else ""
            raise SecretSourceError(
                "unavailable", f"KeePassXC did not create the entry {path}.{where}"
            )
        self._logger.info("Secret Source entry created (source=%s)", SOURCE_NAME)
        return path

    # -- internals -------------------------------------------------------------

    def _path(self, key: str) -> str:
        """A path setting as typed, without quotes, with ``~`` and variables expanded."""
        raw = str(self._config().get(key) or "").strip().strip('"').strip()
        return os.path.expandvars(os.path.expanduser(raw)) if raw else ""

    def _prerequisites(self) -> tuple[str | None, str | None]:
        """The keepassxc-cli program, or why the source cannot work now."""
        program, problem = self._program()
        if problem is not None:
            return None, problem
        database = self._path("database")
        if not database:
            return program, f"No database file is set. Set it in {_SETTINGS_PLACE}."
        if not Path(database).is_file():
            return program, (
                f"The database file {database} does not exist. Correct it in {_SETTINGS_PLACE}."
            )
        key_file = self._path("key_file")
        if key_file and not Path(key_file).is_file():
            return program, (
                f"The key file {key_file} does not exist. Correct it in {_SETTINGS_PLACE}."
            )
        return program, None

    def _program(self) -> tuple[str | None, str | None]:
        configured = self._path("cli_path")
        now = self._clock()
        with self._lock:
            cached = self._program_cache
            if cached is not None and cached[0] == configured and cached[3] > now:
                return cached[1], cached[2]
        program, problem = self._find_program(configured)
        with self._lock:
            self._program_cache = (configured, program, problem, now + _PROGRAM_SECONDS)
        return program, problem

    def _find_program(self, configured: str) -> tuple[str | None, str | None]:
        if configured:
            candidate = Path(configured)
            if candidate.is_dir():
                for name in ("keepassxc-cli.exe", "keepassxc-cli"):
                    if (candidate / name).is_file():
                        return str(candidate / name), None
            elif candidate.is_file():
                return configured, None
            return None, (
                f"keepassxc-cli was not found at {configured}. "
                f"Correct the keepassxc-cli path in {_SETTINGS_PLACE}."
            )
        for name in _PROGRAM_NAMES:
            found = self._which(name)
            if found:
                return found, None
        for location in self._locations:
            if Path(location).is_file():
                return location, None
        return None, (
            "keepassxc-cli was not found. Install KeePassXC, "
            f"or set the keepassxc-cli path in {_SETTINGS_PLACE}."
        )

    def _current(self) -> _Unlocked | None:
        """The unlock that still matches the database settings, if any."""
        database, key_file = self._path("database"), self._path("key_file")
        with self._lock:
            unlocked = self._unlocked
            if unlocked is None:
                return None
            if (unlocked.database, unlocked.key_file) != (database, key_file):
                self._drop("The database settings changed after it was unlocked.")
                return None
            return unlocked

    def _require_unlocked(self) -> tuple[str, _Unlocked]:
        program, problem = self._prerequisites()
        if program is None or problem is not None:
            raise SecretSourceError("unavailable", problem or "keepassxc-cli was not found.")
        unlocked = self._current()
        if unlocked is None:
            with self._lock:
                reason = self._lock_reason
            raise SecretSourceError(
                "locked", "The database is locked." + (f" {reason}" if reason else "")
            )
        return program, unlocked

    def _drop(self, reason: str | None) -> None:
        """Forget the master password (caller holds ``_lock``)."""
        self._unlocked = None
        self._listing = None
        self._lock_reason = reason

    def _list(self, *, refresh: bool = False) -> _Listing:
        program, unlocked = self._require_unlocked()
        now = self._clock()
        with self._lock:
            listing = self._listing
        if (
            not refresh
            and listing is not None
            and listing.owner is unlocked
            and listing.expires > now
        ):
            return listing
        # An empty search term matches every entry; each line is ``/Group/Title``.
        result = self._call(program, unlocked, "search", [], [""])
        if result.returncode == 0:
            paths = (line.removeprefix("/") for line in result.stdout.split("\n"))
            entries = tuple(dict.fromkeys(path for path in paths if path.strip()))
        else:
            # A search without results fails too; an empty database still opens.
            self._explain_failure(program, unlocked)
            entries = ()
        listing = _Listing(unlocked, entries, _groups(entries), now + LISTING_SECONDS)
        with self._lock:
            if self._unlocked is unlocked:
                self._listing = listing
        return listing

    def _explain_failure(self, program: str, unlocked: _Unlocked) -> None:
        """Raise when a failed call was the database not opening; return otherwise."""
        if self._opens(program, unlocked):
            return
        _program, problem = self._prerequisites()
        if problem is not None:
            raise SecretSourceError("unavailable", problem)
        reason = "It stopped opening with the master password it was unlocked with."
        with self._lock:
            if self._unlocked is unlocked:
                self._drop(reason)
        self._logger.warning(
            "Secret Source locked: the database stopped opening (source=%s)", SOURCE_NAME
        )
        raise SecretSourceError("locked", f"The database is locked. {reason}")

    def _opens(self, program: str, unlocked: _Unlocked) -> bool:
        return self._call(program, unlocked, "ls", [], []).returncode == 0

    def _call(
        self,
        program: str,
        unlocked: _Unlocked,
        command: str,
        options: Sequence[str],
        positional: Sequence[str],
        more_input: str = "",
    ) -> CommandResult:
        """One keepassxc-cli command on the database; the password goes on stdin."""
        argv = [program, command, "-q", *options]
        if unlocked.key_file:
            argv += ["-k", unlocked.key_file]
        if not unlocked.password:
            argv.append("--no-password")
        argv += ["--", unlocked.database, *positional]
        stdin = (f"{unlocked.password}\n" if unlocked.password else "") + more_input
        try:
            with self._cli_lock:
                return self._runner(argv, stdin=stdin, timeout=TIMEOUT_SECONDS)
        except CommandTimeoutError:
            self._logger.warning(
                "Secret Source call timed out (source=%s command=%s seconds=%d)",
                SOURCE_NAME,
                command,
                TIMEOUT_SECONDS,
            )
            raise SecretSourceError(
                "timed_out",
                f"keepassxc-cli did not answer within {int(TIMEOUT_SECONDS)} seconds. "
                "A database whose key derivation takes very long can cause this.",
            ) from None
        except CommandNotStartedError:
            self._logger.warning(
                "Secret Source program did not start (source=%s command=%s)", SOURCE_NAME, command
            )
            raise SecretSourceError(
                "unavailable",
                f"keepassxc-cli could not be started from {program}. "
                f"Check the keepassxc-cli path in {_SETTINGS_PLACE}.",
            ) from None


def _attributes(field_name: str) -> list[str]:
    """The KeePassXC attributes that hold ``field_name``, in the order to try."""
    wanted = field_name.strip()
    lowered = wanted.lower()
    if lowered in ("totp", "otp"):
        return ["otp", _LEGACY_TOTP_ATTRIBUTE]
    if lowered in _FIELDS:
        return [_FIELDS[lowered][0]]
    for attribute, _concealed in _FIELDS.values():
        if attribute.lower() == lowered:
            return [attribute]
    if lowered == "title":
        return ["Title"]
    return [wanted]


def _single_value(stdout: str) -> str:
    """The attribute value: the output without the line break keepassxc-cli appends."""
    return stdout.removesuffix("\n")


def _groups(entries: Sequence[str]) -> tuple[str, ...]:
    groups: set[str] = set()
    for path in entries:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            groups.add("/".join(parts[:depth]))
    return tuple(sorted(groups, key=str.casefold))
