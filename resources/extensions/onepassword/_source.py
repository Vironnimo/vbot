"""1Password as a Secret Source: items of the vaults a service account can read.

Every call runs the 1Password CLI (``op``) with a service account token in the
environment of its process only. The token is either saved in the Extension
settings (Secret Store credential ``OP_SERVICE_ACCOUNT_TOKEN``), which makes the
source available without an unlock, or given at unlock and then held only in
memory until :meth:`OnePasswordSource.lock`. A lock also pauses a saved token
until the next unlock.

References are ``<vault id>/<item id>``; containers are vault names. Vault and
item listings are kept in memory for a minute, because service accounts have
hourly and daily request limits; values are read per call and never kept.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from core.secrets.agent_secrets import SecretKind
from core.secrets.sources import (
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

SOURCE_NAME: Final = "onepassword"
TOKEN_ENV_KEY: Final = "OP_SERVICE_ACCOUNT_TOKEN"
TIMEOUT_SECONDS: Final = 30.0
#: How long vault and item listings serve later calls.
LISTING_SECONDS: Final = 60.0
_PROGRAM_SECONDS: Final = 30.0
_SETTINGS_PLACE: Final = "the 1Password Extension settings (Settings -> Integrations -> Extensions)"
_EXCERPT_CHARACTERS: Final = 160

#: Item category -> (Secret kind, source fields offered for binding).
_CATEGORIES: Final[Mapping[str, tuple[SecretKind, tuple[str, ...]]]] = {
    "LOGIN": ("login", ("username", "password", "url", "totp", "notes")),
    "PASSWORD": ("token", ("password", "notes")),
    "API_CREDENTIAL": ("token", ("credential", "username", "notes")),
    "SECURE_NOTE": ("text", ("notes",)),
    "DATABASE": ("login", ("username", "password", "notes")),
    "SERVER": ("login", ("username", "password", "url", "notes")),
}
_OTHER_CATEGORY: Final[tuple[SecretKind, tuple[str, ...]]] = ("text", ("notes",))
_VISIBLE_FIELDS: Final = frozenset({"username", "url"})
_PURPOSES: Final = {"username": "USERNAME", "password": "PASSWORD", "notes": "NOTES"}

_ERROR_PREFIX = re.compile(r"^\[ERROR\]\s+\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s+", re.MULTILINE)
_TOKEN_LIKE = re.compile(r"\bops_[A-Za-z0-9+/=_-]+|\beyJ[A-Za-z0-9+/=_-]{16,}")
_RATE_LIMITED = re.compile(r"rate limit|too many requests|\b429\b", re.IGNORECASE)
_NOT_FOUND = re.compile(
    r"isn't an item|isn't a vault|not found|no item|could not find", re.IGNORECASE
)
_REFUSED = re.compile(
    r"unauthori[sz]ed|\b40[13]\b|invalid token|token is invalid|authenticat|signed in|expired"
    r"|forbidden|access denied|permission",
    re.IGNORECASE,
)
_UNREACHABLE = re.compile(
    r"dial tcp|connection refused|no such host|network is unreachable|i/o timeout|tls handshake",
    re.IGNORECASE,
)


def standard_locations() -> tuple[str, ...]:
    """Where 1Password's installers put ``op`` when PATH does not name it."""
    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (
            str(Path(program_files) / "1Password CLI" / "op.exe"),
            str(Path(local) / "Microsoft" / "WinGet" / "Links" / "op.exe"),
        )
    return ("/usr/local/bin/op", "/usr/bin/op", "/opt/homebrew/bin/op")


@dataclass(frozen=True)
class _Vault:
    id: str
    name: str


@dataclass(frozen=True)
class _Cached[T]:
    token: str
    value: T
    expires: float


class OnePasswordSource:
    """The ``onepassword`` Secret Source; safe to call from several worker threads."""

    def __init__(
        self,
        config: Callable[[], Mapping[str, Any]],
        credential: Callable[[str], str],
        *,
        logger: logging.Logger,
        runner: CommandRunner = run_command,
        which: Callable[[str], str | None] = shutil.which,
        locations: Sequence[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._credential = credential
        self._logger = logger
        self._runner = runner
        self._which = which
        self._locations = tuple(standard_locations() if locations is None else locations)
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._locked = False
        self._rejected: str | None = None
        self._vaults: _Cached[tuple[_Vault, ...]] | None = None
        self._items: dict[str, _Cached[tuple[SourceEntry, ...]]] = {}
        self._program_cache: tuple[str, str | None, str | None, float] | None = None

    # -- SecretSource ----------------------------------------------------------

    @property
    def name(self) -> str:
        return SOURCE_NAME

    @property
    def label(self) -> str:
        return "1Password"

    @property
    def unlock_fields(self) -> Sequence[UnlockField]:
        # With a saved token, unlocking without input uses it.
        return (
            UnlockField(
                "token",
                "Service account token",
                concealed=True,
                required=not self._saved_token(),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset()

    def status(self) -> SourceStatus:
        _program, problem = self._program()
        if problem is not None:
            return SourceStatus("unavailable", problem)
        token, reason = self._current_token()
        if token is None:
            return SourceStatus("locked", reason)
        return SourceStatus("available")

    def unlock(self, inputs: Mapping[str, str]) -> SourceStatus:
        program = self._require_program()
        given = str(inputs.get("token") or "").strip()
        token = given or self._saved_token()
        if not token:
            raise SecretSourceError("denied", "Enter the service account token.")
        vaults = self._read_vaults(program, token)
        with self._lock:
            self._token = given or None
            self._locked = False
            self._rejected = None
            self._items = {}
            self._vaults = _Cached(token, vaults, self._clock() + LISTING_SECONDS)
        return SourceStatus("available")

    def lock(self) -> SourceStatus:
        with self._lock:
            self._token = None
            self._locked = True
            self._vaults = None
            self._items = {}
        return self.status()

    def containers(self) -> Sequence[str]:
        return list(_labels(self._vault_list()))

    def entries(self, container: str | None = None) -> Sequence[SourceEntry]:
        vaults = self._vault_list()
        labels = _labels(vaults)
        if container is not None and container.strip():
            wanted = container.strip()
            chosen = [
                vault
                for vault, label in zip(vaults, labels, strict=True)
                if wanted in (label, vault.id)
            ]
            if not chosen:
                raise SecretSourceError(
                    "not_found",
                    f"The service account cannot read a 1Password vault named {wanted}. "
                    f"Its vaults: {', '.join(labels) or 'none'}.",
                )
        else:
            chosen = list(vaults)
        found: list[SourceEntry] = []
        for vault in chosen:
            label = labels[vaults.index(vault)]
            found.extend(self._vault_items(vault, label))
        return found

    def resolve(self, reference: str, field_name: str) -> str:
        program, token = self._require_token()
        parts = reference.strip().removeprefix("op://").strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            raise SecretSourceError(
                "not_found",
                f"{reference} is not a 1Password item reference. "
                "Bind the Secret to an item in Settings -> Secrets.",
            )
        vault_id, item_id = parts
        result = self._call(
            program,
            token,
            ["item", "get", item_id, "--vault", vault_id, "--format", "json", "--reveal"],
        )
        if result.returncode != 0:
            raise self._failure(result, token, f"the item {reference}")
        item = _json(result.stdout)
        if not isinstance(item, dict):
            raise SecretSourceError("unavailable", "op did not answer with an item.")
        title = str(item.get("title") or reference)
        found, value = _field_value(item, field_name)
        if not found:
            raise SecretSourceError(
                "not_found", f"The 1Password item {title} has no {field_name} field."
            )
        if not value:
            raise SecretSourceError(
                "not_found", f"The {field_name} field of the 1Password item {title} is empty."
            )
        return value

    def store(self, entry: NewSourceEntry) -> str:
        raise SecretSourceError("unavailable", "vBot does not create 1Password items.")

    # -- internals -------------------------------------------------------------

    def _saved_token(self) -> str:
        return str(self._credential(TOKEN_ENV_KEY) or "").strip()

    def _current_token(self) -> tuple[str | None, str | None]:
        saved = self._saved_token()
        with self._lock:
            if self._locked:
                return None, None
            token = self._token or saved
            if not token:
                return None, (
                    f"Enter a service account token to unlock, or save one in {_SETTINGS_PLACE}."
                )
            if token == self._rejected:
                return None, "1Password refused the service account token."
            return token, None

    def _require_program(self) -> str:
        program, problem = self._program()
        if program is None or problem is not None:
            raise SecretSourceError("unavailable", problem or "op was not found.")
        return program

    def _require_token(self) -> tuple[str, str]:
        program = self._require_program()
        token, reason = self._current_token()
        if token is None:
            raise SecretSourceError(
                "locked", "1Password is locked." + (f" {reason}" if reason else "")
            )
        return program, token

    def _program(self) -> tuple[str | None, str | None]:
        raw = str(self._config().get("cli_path") or "").strip().strip('"').strip()
        configured = os.path.expandvars(os.path.expanduser(raw)) if raw else ""
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
                for name in ("op.exe", "op"):
                    if (candidate / name).is_file():
                        return str(candidate / name), None
            elif candidate.is_file():
                return configured, None
            return None, (
                f"The 1Password CLI was not found at {configured}. "
                f"Correct the op path in {_SETTINGS_PLACE}."
            )
        found = self._which("op")
        if found:
            return found, None
        for location in self._locations:
            if Path(location).is_file():
                return location, None
        return None, (
            "The 1Password CLI (op) was not found. Install it, "
            f"or set the op path in {_SETTINGS_PLACE}."
        )

    def _vault_list(self) -> tuple[_Vault, ...]:
        program, token = self._require_token()
        with self._lock:
            cached = self._vaults
        if cached is not None and cached.token == token and cached.expires > self._clock():
            return cached.value
        vaults = self._read_vaults(program, token)
        with self._lock:
            self._vaults = _Cached(token, vaults, self._clock() + LISTING_SECONDS)
        return vaults

    def _read_vaults(self, program: str, token: str) -> tuple[_Vault, ...]:
        result = self._call(program, token, ["vault", "list", "--format", "json"])
        if result.returncode != 0:
            error = self._failure(result, token, "its vaults")
            if error.code == "denied":
                # Listing vaults needs nothing but a valid token.
                with self._lock:
                    self._rejected = token
                    if self._token == token:
                        self._token = None
            raise error
        vaults = []
        for raw in _json_list(result.stdout):
            vault_id, name = raw.get("id"), raw.get("name")
            if isinstance(vault_id, str) and vault_id:
                vaults.append(
                    _Vault(vault_id, name if isinstance(name, str) and name else vault_id)
                )
        return tuple(sorted(vaults, key=lambda vault: vault.name.casefold()))

    def _vault_items(self, vault: _Vault, label: str) -> tuple[SourceEntry, ...]:
        program, token = self._require_token()
        with self._lock:
            cached = self._items.get(vault.id)
        if cached is not None and cached.token == token and cached.expires > self._clock():
            return cached.value
        result = self._call(
            program, token, ["item", "list", "--vault", vault.id, "--format", "json"]
        )
        if result.returncode != 0:
            raise self._failure(result, token, f"the items of the vault {label}")
        items = tuple(
            entry
            for raw in _json_list(result.stdout)
            if (entry := _entry(raw, vault, label)) is not None
        )
        with self._lock:
            self._items[vault.id] = _Cached(token, items, self._clock() + LISTING_SECONDS)
        return items

    def _call(self, program: str, token: str, arguments: Sequence[str]) -> CommandResult:
        environment = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("OP_")
        }
        # Connect variables would take precedence over the service account.
        environment[TOKEN_ENV_KEY] = token
        environment["OP_CACHE"] = "false"
        command = " ".join(arguments[:2])
        try:
            return self._runner([program, *arguments], env=environment, timeout=TIMEOUT_SECONDS)
        except CommandTimeoutError:
            self._logger.warning(
                "Secret Source call timed out (source=%s command=%s seconds=%d)",
                SOURCE_NAME,
                command,
                TIMEOUT_SECONDS,
            )
            raise SecretSourceError(
                "timed_out",
                f"1Password did not answer within {int(TIMEOUT_SECONDS)} seconds. "
                "Check the server's internet connection.",
            ) from None
        except CommandNotStartedError:
            self._logger.warning(
                "Secret Source program did not start (source=%s command=%s)", SOURCE_NAME, command
            )
            raise SecretSourceError(
                "unavailable",
                f"The 1Password CLI could not be started from {program}. "
                f"Check the op path in {_SETTINGS_PLACE}.",
            ) from None

    def _failure(self, result: CommandResult, token: str, subject: str) -> SecretSourceError:
        """The error for a failed ``op`` call, without anything that could be a value."""
        excerpt = _excerpt(result.stderr, token)
        said = f' op said: "{excerpt}"' if excerpt else ""
        code: SourceFailure
        if _RATE_LIMITED.search(excerpt):
            code, message = (
                "unavailable",
                ("The service account reached 1Password's request limit; try again later."),
            )
        elif _NOT_FOUND.search(excerpt):
            code, message = (
                "not_found",
                (
                    f"1Password did not find {subject}. Check that it still exists and that the "
                    "service account can read its vault."
                ),
            )
        elif _REFUSED.search(excerpt):
            code, message = (
                "denied",
                (
                    f"1Password refused access to {subject}. Check that the service account token "
                    "is valid and that its service account can read the vault."
                ),
            )
        elif _UNREACHABLE.search(excerpt):
            code, message = (
                "unavailable",
                ("1Password could not be reached. Check the server's internet connection."),
            )
        else:
            code, message = "unavailable", f"op could not read {subject}."
        self._logger.debug(
            "Secret Source call failed (source=%s code=%s exit=%d)",
            SOURCE_NAME,
            code,
            result.returncode,
        )
        return SecretSourceError(code, message + said)


def _labels(vaults: Sequence[_Vault]) -> list[str]:
    """Vault names, with the id added to a name that several vaults share."""
    counts: dict[str, int] = {}
    for vault in vaults:
        counts[vault.name] = counts.get(vault.name, 0) + 1
    return [
        vault.name if counts[vault.name] == 1 else f"{vault.name} ({vault.id})" for vault in vaults
    ]


def _entry(raw: Mapping[str, Any], vault: _Vault, label: str) -> SourceEntry | None:
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    category = str(raw.get("category") or "").upper()
    kind, names = _CATEGORIES.get(category, _OTHER_CATEGORY)
    visible = {
        "username": _text(raw.get("additional_information")) if category == "LOGIN" else None,
        "url": _primary_url(raw),
    }
    fields = tuple(
        SourceField(name, name not in _VISIBLE_FIELDS, visible.get(name) or None) for name in names
    )
    return SourceEntry(
        reference=f"{vault.id}/{item_id}",
        title=_text(raw.get("title")) or item_id,
        kind=kind,
        fields=fields,
        container=label,
    )


def _field_value(item: Mapping[str, Any], field_name: str) -> tuple[bool, str]:
    """Whether the item has the field, and its value (``""`` when empty)."""
    wanted = field_name.strip()
    lowered = wanted.casefold()
    fields = [field for field in item.get("fields") or () if isinstance(field, dict)]
    if lowered in ("url", "website"):
        url = _primary_url(item)
        if url:
            return True, url
    if lowered in ("totp", "otp", "one-time password"):
        # The otpauth:// URI; vBot derives the current code from it.
        otp = next((field for field in fields if str(field.get("type")).upper() == "OTP"), None)
        return (otp is not None), _text(otp.get("value")) if otp else ""
    purpose = _PURPOSES.get(lowered)
    candidates = [
        *(field for field in fields if purpose and field.get("purpose") == purpose),
        *(field for field in fields if field.get("id") == wanted),
        *(field for field in fields if str(field.get("id") or "").casefold() == lowered),
        *(field for field in fields if str(field.get("label") or "").casefold() == lowered),
    ]
    if not candidates:
        return False, ""
    return True, _text(candidates[0].get("value"))


def _primary_url(raw: Mapping[str, Any]) -> str:
    urls = [url for url in raw.get("urls") or () if isinstance(url, dict) and url.get("href")]
    primary = next((url for url in urls if url.get("primary")), urls[0] if urls else None)
    return _text(primary.get("href")) if primary else ""


def _text(value: Any) -> str:
    if value is None or isinstance(value, dict | list):
        return ""
    return str(value)


def _json(text: str) -> Any:
    try:
        return json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return None


def _json_list(text: str) -> list[dict[str, Any]]:
    data = _json(text)
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _excerpt(stderr: str, token: str) -> str:
    """op's own message, without timestamps or anything shaped like a token."""
    text = _ERROR_PREFIX.sub("", stderr)
    if token:
        text = text.replace(token, "[token]")
    text = _TOKEN_LIKE.sub("[token]", text)
    text = " ".join(text.split())
    if len(text) > _EXCERPT_CHARACTERS:
        text = text[: _EXCERPT_CHARACTERS - 3].rstrip() + "..."
    return text
