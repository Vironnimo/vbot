"""This computer's Device sign-ins: the token a server issued when it approved the CLI.

One file per OS user holds, per server origin (``https://host[:port]``), the
Device token with the Device's id and name, and a pairing that still waits for
approval (its request id, poll secret and code), so that running ``vbot devices
pair`` again continues it. The CLI, the TUI and the tray read it through
``cli/server_client.py``; only ``vbot devices pair`` and ``vbot devices logout``
change it.

On Windows the file is ``%LOCALAPPDATA%\\vbot\\sign-ins.json`` (not roaming: a
Device is one computer), and every secret in it is wrapped with DPAPI for the
current user, bound to its origin. Elsewhere it is
``$XDG_CONFIG_HOME/vbot/sign-ins.json`` (default ``~/.config/vbot``) and holds
the secrets in plain text. On both, the file is readable by its owner only.
Nothing here prints or logs a secret.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from core.utils import dpapi
from core.utils.atomic import atomic_write_text
from core.utils.file_lock import acquire_file_lock, release_file_lock

FILE_NAME = "sign-ins.json"
FORMAT_VERSION = 1
_DPAPI = "dpapi"
_PLAIN = "none"
_DEVICE_TOKEN = re.compile(r"vbt_[A-Za-z0-9_-]{1,256}")
_SECRET = re.compile(r"[\x21-\x7e]{1,256}")


class SignInStoreError(Exception):
    """The sign-in file cannot be read or written; the message names the file, never a secret."""


@dataclass(frozen=True)
class SignIn:
    """The Device a server approved for this computer's CLI, with its token."""

    origin: str
    token: str
    device_id: str
    device_name: str
    signed_in_at: str

    @override
    def __repr__(self) -> str:
        return (
            f"SignIn(origin={self.origin!r}, device_id={self.device_id!r}, "
            f"device_name={self.device_name!r})"
        )


@dataclass(frozen=True)
class PendingPairing:
    """A pairing request that waits for approval; ``expires_at`` is Unix time in seconds."""

    origin: str
    request_id: str
    poll_secret: str
    code: str
    name: str
    expires_at: float
    poll_interval: float

    @override
    def __repr__(self) -> str:
        return f"PendingPairing(origin={self.origin!r}, code={self.code!r})"


def default_path() -> Path:
    """Where this OS user's sign-ins live (see the module docstring)."""

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = Path(local) if local and Path(local).is_absolute() else None
        return (base or Path.home() / "AppData" / "Local") / "vbot" / FILE_NAME
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    # The XDG specification ignores a relative value.
    base = Path(configured) if configured and Path(configured).is_absolute() else None
    return (base or Path.home() / ".config") / "vbot" / FILE_NAME


class SignInStore:
    """Read and change the sign-in file; every change is one locked, atomic rewrite."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else default_path()

    def sign_in(self, origin: str) -> SignIn | None:
        """The usable sign-in for ``origin``; ``None`` when there is none or it cannot be opened."""

        record = self._servers().get(origin, {}).get("sign_in")
        if not isinstance(record, Mapping):
            return None
        token = _reveal(record.get("token"), origin, "token")
        device_id, device_name = record.get("device_id"), record.get("device_name")
        if token is None or not _DEVICE_TOKEN.fullmatch(token):
            return None
        return SignIn(
            origin=origin,
            token=token,
            device_id=device_id if isinstance(device_id, str) else "",
            device_name=device_name if isinstance(device_name, str) else "",
            signed_in_at=str(record.get("signed_in_at") or ""),
        )

    def pending(self, origin: str) -> PendingPairing | None:
        """The pairing that waits for approval at ``origin``, if one was stored."""

        record = self._servers().get(origin, {}).get("pairing")
        if not isinstance(record, Mapping):
            return None
        secret = _reveal(record.get("poll_secret"), origin, "poll_secret")
        fields = (record.get(name) for name in ("request_id", "code", "name"))
        request_id, code, name = fields
        expires_at, interval = record.get("expires_at"), record.get("poll_interval")
        if (
            secret is None
            or not all(isinstance(value, str) and value for value in (request_id, code, name))
            or not isinstance(expires_at, (int, float))
            or not isinstance(interval, (int, float))
        ):
            return None
        return PendingPairing(
            origin=origin,
            request_id=str(request_id),
            poll_secret=secret,
            code=str(code),
            name=str(name),
            expires_at=float(expires_at),
            poll_interval=float(interval),
        )

    def save_sign_in(self, sign_in: SignIn) -> None:
        """Store the sign-in for its origin, replacing an earlier one and its pairing."""

        if not _DEVICE_TOKEN.fullmatch(sign_in.token):
            raise SignInStoreError("The server returned a Device token of an unknown form")
        with self._changing() as servers:
            servers[sign_in.origin] = {
                "sign_in": {
                    "device_id": sign_in.device_id,
                    "device_name": sign_in.device_name,
                    "signed_in_at": sign_in.signed_in_at,
                    "token": _conceal(sign_in.token, sign_in.origin, "token"),
                }
            }

    def save_pending(self, pairing: PendingPairing) -> None:
        """Store a started pairing so that a later run continues it."""

        if not _SECRET.fullmatch(pairing.poll_secret):
            raise SignInStoreError("The server returned a pairing secret of an unknown form")
        with self._changing() as servers:
            entry = dict(servers.get(pairing.origin, {}))
            entry["pairing"] = {
                "request_id": pairing.request_id,
                "code": pairing.code,
                "name": pairing.name,
                "expires_at": pairing.expires_at,
                "poll_interval": pairing.poll_interval,
                "poll_secret": _conceal(pairing.poll_secret, pairing.origin, "poll_secret"),
            }
            servers[pairing.origin] = entry

    def drop_pending(self, origin: str) -> None:
        """Forget the stored pairing of ``origin``; its sign-in stays."""

        if "pairing" not in self._servers().get(origin, {}):
            return
        with self._changing() as servers:
            entry = dict(servers.get(origin, {}))
            entry.pop("pairing", None)
            if entry:
                servers[origin] = entry
            else:
                servers.pop(origin, None)

    def forget(self, origin: str) -> bool:
        """Delete the sign-in and pairing of ``origin``; whether there was one."""

        if origin not in self._servers():
            return False
        with self._changing() as servers:
            return servers.pop(origin, None) is not None

    # File access -------------------------------------------------------------

    def _servers(self) -> dict[str, dict[str, Any]]:
        servers: dict[str, dict[str, Any]] = self._read()["servers"]
        return servers

    def _read(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"format_version": FORMAT_VERSION, "servers": {}}
        except OSError as exc:
            raise SignInStoreError(
                f"The sign-in file {self.path} cannot be read: {exc.strerror or exc}"
            ) from exc
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        if (
            not isinstance(document, dict)
            or document.get("format_version") != FORMAT_VERSION
            or not isinstance(document.get("servers"), dict)
            or not all(isinstance(entry, dict) for entry in document["servers"].values())
        ):
            raise SignInStoreError(
                f"The sign-in file {self.path} is not a vBot sign-in file of a format this "
                "version reads"
            )
        return document

    @contextmanager
    def _changing(self) -> Iterator[dict[str, dict[str, Any]]]:
        """Yield the servers map under the file lock and write it back atomically.

        A file that cannot be read is never overwritten: the change fails and the
        file stays for the user to inspect.
        """

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with _locked(self.path.with_name(f"{self.path.name}.lock")):
                document = self._read()
                servers = document["servers"]
                yield servers
                document["servers"] = servers
                atomic_write_text(
                    self.path,
                    json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    owner_only=True,
                )
        except OSError as exc:
            raise SignInStoreError(
                f"The sign-in file {self.path} cannot be written: {exc.strerror or exc}"
            ) from exc


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` so that concurrent CLI runs change the file in turn."""

    with path.open("a+b") as handle:
        acquire_file_lock(handle.fileno(), blocking=True)
        try:
            yield
        finally:
            release_file_lock(handle.fileno())


def _entropy(origin: str, purpose: str) -> bytes:
    return f"vbot-device-sign-in:{origin}:{purpose}".encode()


def _conceal(value: str, origin: str, purpose: str) -> dict[str, str]:
    if not dpapi.available():
        return {"protection": _PLAIN, "value": value}
    try:
        blob = dpapi.protect(value.encode("ascii"), _entropy(origin, purpose))
    except dpapi.DpapiError as exc:
        raise SignInStoreError(f"Windows could not protect the sign-in: {exc}") from exc
    return {"protection": _DPAPI, "value": base64.b64encode(blob).decode("ascii")}


def _reveal(record: object, origin: str, purpose: str) -> str | None:
    """The secret in ``record``; ``None`` when this account cannot open it."""

    if not isinstance(record, Mapping) or not isinstance(record.get("value"), str):
        return None
    protection, value = record.get("protection"), record["value"]
    if protection == _PLAIN:
        return value if _SECRET.fullmatch(value) else None
    if protection != _DPAPI or not dpapi.available():
        return None
    try:
        secret = dpapi.unprotect(base64.b64decode(value, validate=True), _entropy(origin, purpose))
        text = secret.decode("ascii")
    except ValueError, dpapi.DpapiError:
        # Not valid base64 or ASCII, or wrapped for another account or computer.
        return None
    return text if _SECRET.fullmatch(text) else None
