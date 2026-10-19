"""Paired devices: device tokens and the device store.

Every credential of the ``authenticated`` access mode belongs to a device.
A device token is ``vbt_<device id>_<32 random bytes, base64url>``; the store
keeps only the SHA-256 hash of its secret part and compares in constant time.

The store is the Generation 1 JSON document ``<data-dir>/access/devices.json``,
an owner-only file outside data snapshots and configuration backups: a restore
must never revive a revoked device. It is loaded once; a file that fails to load
makes the store unavailable (no device authenticates, management is refused)
and is never overwritten. Deleting the file revokes every device.

The server looks devices up in memory on the Event Loop. Writes block on disk
and run in worker threads; they are serialized, and a removal takes effect in
memory before it is written. A device not used for 90 days expires. Last use is
kept in memory and written at most once an hour per device, and on close.

A device records how it was created (``via``): by claiming an approved pairing
or by redeeming a login ticket. Records written before ``via`` existed have none.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast, get_args

from core.access.config import parse_ip
from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    format_report_diagnostics,
    read_json_file,
    validate_json_file,
)
from core.json_documents import (
    DURABLE_DOCUMENTS,
    JsonDocumentFormat,
    JsonDocumentWriteError,
    json_document,
    json_list,
    json_object,
    strip_unknown_fields,
    validate_collection_root,
    warn_unknown_fields,
    write_json_document,
)
from core.utils.ids import new_id
from core.utils.timestamps import format_canonical_timestamp, parse_canonical_timestamp

DeviceKind = Literal["browser", "desktop", "cli"]
DEVICE_KINDS: tuple[DeviceKind, ...] = get_args(DeviceKind)
#: How a device came to be: an approved pairing it claimed, or a login ticket.
DeviceOrigin = Literal["pairing", "ticket"]
DEVICE_ORIGINS: tuple[DeviceOrigin, ...] = get_args(DeviceOrigin)
DEVICE_NAME_MAX_CHARS = 80
DEVICE_IDLE_EXPIRY = timedelta(days=90)
LAST_USE_WRITE_INTERVAL = timedelta(hours=1)
#: The principal of the host itself: the local access token and the host CLI.
HOST_PRINCIPAL = "host"

DEVICES_FORMAT_VERSION = 1
DEVICE_STORE_PATH = DURABLE_DOCUMENTS["access_devices"]

_DEVICE_ID = r"dev_[0-9a-z]{16}"
_DEVICE_ID_PATTERN = re.compile(_DEVICE_ID)
_TOKEN_PATTERN = re.compile(rf"vbt_({_DEVICE_ID})_([A-Za-z0-9_-]{{43}})")
_TOKEN_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_APPROVER_PATTERN = re.compile(rf"{HOST_PRINCIPAL}|{_DEVICE_ID}")
#: What a stored name must not carry; names written before the stricter rule
#: below keep loading.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
#: What a new name must not carry: controls, formatting, unpaired surrogates,
#: line and paragraph separators.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

_DEVICE_FIELDS = frozenset(
    (
        "id",
        "name",
        "kind",
        "token_hash",
        "created_at",
        "last_used_at",
        "last_client",
        "approved_by",
    )
)
#: Fields a record may lack: they came after the first records were written.
_OPTIONAL_DEVICE_FIELDS = frozenset(("via",))
DEVICES_SHAPE = json_document(
    {"devices"},
    {"devices": json_list(json_object(_DEVICE_FIELDS | _OPTIONAL_DEVICE_FIELDS), key="id")},
)

LookupOutcome = Literal["valid", "expired", "invalid"]


class DeviceStoreUnavailableError(RuntimeError):
    """The device store could not be read; repair and reload it before it changes."""


class DeviceNotFoundError(LookupError):
    """No device has the given id."""


@dataclass(frozen=True)
class Device:
    """One paired device as the store keeps it."""

    id: str
    name: str
    kind: DeviceKind
    token_hash: str
    created_at: datetime
    last_used_at: datetime
    last_client: str | None
    approved_by: str
    #: ``None`` for a device stored before its origin was recorded.
    via: DeviceOrigin | None = None

    def describe(self) -> dict[str, Any]:
        """The device as clients see it: everything except its token hash."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "created_at": format_canonical_timestamp(self.created_at),
            "last_used_at": format_canonical_timestamp(self.last_used_at),
            "last_client": self.last_client,
            "approved_by": self.approved_by,
            "via": self.via,
        }


def new_device(
    *,
    name: str,
    kind: str,
    approved_by: str,
    client: str | None,
    now: datetime,
    via: DeviceOrigin | None = None,
) -> tuple[Device, str]:
    """Create a device and its token; the token is returned once and never stored."""
    if kind not in DEVICE_KINDS:
        raise ValueError(f"device kind must be one of {', '.join(DEVICE_KINDS)}")
    if via is not None and via not in DEVICE_ORIGINS:
        raise ValueError(f"via must be one of {', '.join(DEVICE_ORIGINS)}")
    if not _APPROVER_PATTERN.fullmatch(approved_by):
        raise ValueError("approved_by must name the host or a device")
    device_id = new_id("dev")
    secret = secrets.token_urlsafe(32)
    device = Device(
        id=device_id,
        name=normalize_device_name(name),
        kind=cast(DeviceKind, kind),
        token_hash=hash_token_secret(secret),
        created_at=now,
        last_used_at=now,
        last_client=client,
        approved_by=approved_by,
        via=via,
    )
    return device, f"vbt_{device_id}_{secret}"


def normalize_device_name(value: Any) -> str:
    """A new display name: 1 to 80 characters, whitespace runs collapsed to one space.

    Names reach terminals, approval dialogs and logs, so characters that are
    invisible, steer the display or cannot encode as UTF-8 are refused.
    """
    name = _collapsed_name(value)
    if any(unicodedata.category(character) in _HIDDEN_CATEGORIES for character in name):
        raise ValueError(
            "device name must not contain control, formatting or invalid Unicode characters"
        )
    return name


def _collapsed_name(value: Any) -> str:
    """A name with whitespace collapsed, 1 to 80 characters, without ASCII controls."""
    if not isinstance(value, str):
        raise ValueError("device name must be a string")
    name = " ".join(value.split())
    if not name:
        raise ValueError("device name must not be empty")
    if len(name) > DEVICE_NAME_MAX_CHARS:
        raise ValueError(f"device name must be at most {DEVICE_NAME_MAX_CHARS} characters")
    if _CONTROL_CHARACTERS.search(name):
        raise ValueError("device name must not contain control characters")
    return name


def parse_device_token(token: str) -> tuple[str, str] | None:
    """Split a device token into device id and secret, or ``None`` when malformed."""
    match = _TOKEN_PATTERN.fullmatch(token)
    return (match.group(1), match.group(2)) if match else None


def hash_token_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def is_device_id(value: Any) -> bool:
    return isinstance(value, str) and _DEVICE_ID_PATTERN.fullmatch(value) is not None


class DeviceStore:
    """The devices of one data directory, held in memory and persisted on change."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).expanduser() / DEVICE_STORE_PATH
        # Writers hold the write lock across disk I/O; the state lock is short and
        # also taken on the Event Loop.
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._devices: Mapping[str, Device] = MappingProxyType({})
        self._usage: dict[str, tuple[datetime, str | None]] = {}
        self._error: str | None = "The device store is not loaded"
        self._pending_removals: dict[str, Device] = {}

    # Reading -----------------------------------------------------------------

    def load(self) -> None:
        """Read the store from disk (blocking). A failure leaves it unavailable."""
        with self._write_lock:
            report, data = read_json_file(self.path, _validate_document, missing_ok=True)
            if not report.exists:
                devices: dict[str, Device] = {}
            elif not report.ok:
                with self._state_lock:
                    self._devices = MappingProxyType({})
                    self._usage.clear()
                    self._error = "; ".join(format_report_diagnostics(report))
                return
            else:
                document = strip_unknown_fields(data, DEVICES_SHAPE)
                devices = {
                    device.id: device
                    for device in (_parse_device(entry) for entry in document["devices"])
                }
            with self._state_lock:
                self._devices = MappingProxyType(devices)
                self._usage.clear()
                self._error = None
                self._pending_removals.clear()

    @property
    def available(self) -> bool:
        return self._error is None

    @property
    def error(self) -> str | None:
        """Why the store is unavailable, or ``None``."""
        return self._error

    def devices(self) -> list[Device]:
        """Every device with its latest use, oldest first."""
        with self._state_lock:
            current = [self._with_usage(device) for device in self._devices.values()]
        return sorted(current, key=lambda device: (device.created_at, device.id))

    def get(self, device_id: str) -> Device | None:
        with self._state_lock:
            device = self._devices.get(device_id)
            return None if device is None else self._with_usage(device)

    def removal_pending(self, device_id: str) -> bool:
        """Whether this process still owes the durable removal of this device."""
        with self._state_lock:
            return device_id in self._pending_removals

    def lookup(self, token: str, *, now: datetime) -> tuple[LookupOutcome, Device | None]:
        """Check a device token in memory: ``valid``, ``expired`` or ``invalid``.

        An expired device is returned with its outcome so that the caller can
        remove it; it never authenticates.
        """
        parsed = parse_device_token(token)
        if parsed is None or not self.available:
            return "invalid", None
        device_id, secret = parsed
        device = self.get(device_id)
        if device is None or not secrets.compare_digest(
            hash_token_secret(secret), device.token_hash
        ):
            return "invalid", None
        if now - device.last_used_at > DEVICE_IDLE_EXPIRY:
            return "expired", device
        return "valid", device

    def expired_devices(self, *, now: datetime) -> list[Device]:
        return [
            device for device in self.devices() if now - device.last_used_at > DEVICE_IDLE_EXPIRY
        ]

    def record_use(self, device_id: str, client: str | None, *, now: datetime) -> bool:
        """Remember activity, including ticket issuers; ``True`` when a write is due."""
        with self._state_lock:
            device = self._devices.get(device_id)
            seen: set[str] = set()
            due = False
            while device is not None and device.id not in seen:
                seen.add(device.id)
                self._usage[device.id] = (now, client)
                due |= now - device.last_used_at >= LAST_USE_WRITE_INTERVAL
                device = self._devices.get(device.approved_by) if device.via == "ticket" else None
            return due

    # Writing (blocking) --------------------------------------------------------

    def add(self, device: Device) -> None:
        """Persist a new device; it authenticates once the write succeeded.

        Raises :class:`DeviceNotFoundError` when the approver is a device that is
        no longer paired: revoking a device withdraws what it approved.
        """
        with self._write_lock:
            current = self._require_available()
            if device.id in current:
                raise ValueError("device id already exists")
            if device.approved_by != HOST_PRINCIPAL and device.approved_by not in current:
                raise DeviceNotFoundError(device.approved_by)
            self._write({**current, device.id: device})
            with self._state_lock:
                self._devices = MappingProxyType({**self._devices, device.id: device})

    def rename(self, device_id: str, name: str) -> Device:
        normalized = normalize_device_name(name)
        with self._write_lock:
            current = self._require_available()
            if device_id not in current:
                raise DeviceNotFoundError(device_id)
            renamed = replace(current[device_id], name=normalized)
            self._write({**current, device_id: renamed})
            with self._state_lock:
                return self._with_usage(self._devices[device_id])

    def rebind_ticket_browser(self, device_id: str, approved_by: str) -> Device:
        """Move an existing ticket browser under a fresh grant, without a cycle.

        Validation and persistence share the ordered write with revocation, so
        neither a removed browser nor a removed issuer can be resurrected.
        Independently paired browsers and self-issued tickets keep their grant.
        """
        with self._write_lock:
            current = self._require_available()
            device = current.get(device_id)
            if device is None or (approved_by != HOST_PRINCIPAL and approved_by not in current):
                raise DeviceNotFoundError(device_id)
            if device.via != "ticket" or approved_by in {device.id, device.approved_by}:
                return self.get(device_id) or device
            ancestor = current.get(approved_by)
            seen: set[str] = set()
            while ancestor is not None:
                if ancestor.id == device_id or ancestor.id in seen:
                    raise ValueError("a ticket browser cannot be delegated to its descendant")
                seen.add(ancestor.id)
                ancestor = current.get(ancestor.approved_by) if ancestor.via == "ticket" else None
            rebound = replace(device, approved_by=approved_by)
            self._write({**current, device_id: rebound})
            with self._state_lock:
                return self._with_usage(self._devices[device_id])

    def remove(self, device_ids: Iterable[str]) -> list[Device]:
        """Remove devices: at once in memory, then on disk.

        Raises ``OSError`` when the write fails; the devices stay removed for this
        process. Their removals remain pending for a retry, any later successful
        write, or the shutdown flush. A crash before recovery can read them back.
        """
        with self._write_lock:
            self._require_available()
            with self._state_lock:
                removed = [
                    self._with_usage(device)
                    for device_id in dict.fromkeys(device_ids)
                    if (
                        device := self._devices.get(device_id)
                        or self._pending_removals.get(device_id)
                    )
                    is not None
                ]
                if not removed and not self._pending_removals:
                    return []
                gone = {device.id for device in removed}
                remaining = {
                    key: device for key, device in self._devices.items() if key not in gone
                }
                self._devices = MappingProxyType(remaining)
                self._pending_removals.update((device.id, device) for device in removed)
                for device_id in gone:
                    self._usage.pop(device_id, None)
            self._write(remaining)
            return removed

    def flush_usage(self) -> None:
        """Persist pending removals and remembered uses newer than stored ones."""
        with self._write_lock:
            if not self.available:
                return
            with self._state_lock:
                if not self._pending_removals and not any(
                    device_id in self._devices and used > self._devices[device_id].last_used_at
                    for device_id, (used, _client) in self._usage.items()
                ):
                    return
            self._write(dict(self._devices))

    def _write(self, devices: Mapping[str, Device]) -> None:
        """Write ``devices`` with the remembered uses folded in, then adopt those uses."""
        with self._state_lock:
            folded = {key: self._with_usage(device) for key, device in devices.items()}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            write_json_document(
                self.path,
                {"devices": [_device_record(device) for device in folded.values()]},
                DEVICES_FORMAT,
                owner_only=True,
            )
        except JsonDocumentWriteError as exc:
            with self._state_lock:
                self._error = str(exc)
            raise DeviceStoreUnavailableError(str(exc)) from exc
        with self._state_lock:
            self._pending_removals.clear()
            self._devices = MappingProxyType(
                {key: folded.get(key, device) for key, device in self._devices.items()}
            )
            for key, device in folded.items():
                used = self._usage.get(key)
                if used is not None and used[0] <= device.last_used_at:
                    self._usage.pop(key, None)

    def _require_available(self) -> Mapping[str, Device]:
        if self._error is not None:
            raise DeviceStoreUnavailableError(self._error)
        return self._devices

    def _with_usage(self, device: Device) -> Device:
        used = self._usage.get(device.id)
        if used is None or used[0] <= device.last_used_at:
            return device
        return replace(device, last_used_at=used[0], last_client=used[1])


def validate_device_store_file(path: str | Path) -> JsonValidationReport:
    """Validate the persisted device store without loading it."""
    return validate_json_file(path, _validate_document, missing_ok=True)


def _validate_document(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    entries = validate_collection_root(
        diagnostics,
        data,
        version=DEVICES_FORMAT_VERSION,
        shape=DEVICES_SHAPE,
        collection="devices",
    )
    entry_shape = DEVICES_SHAPE.nested["devices"].items
    assert entry_shape is not None
    seen: set[str] = set()
    for index, entry in enumerate(entries or []):
        path = f"$.devices[{index}]"
        warn_unknown_fields(diagnostics, path, entry, entry_shape)
        try:
            device = _parse_device(entry)
        except ValueError as error:
            add_error(diagnostics, path, str(error))
            continue
        if device.id in seen:
            add_error(diagnostics, path, f"repeats device id {device.id}")
        seen.add(device.id)
    return diagnostics


def _parse_device(entry: Any) -> Device:
    if not isinstance(entry, dict):
        raise ValueError("must be an object")
    missing = sorted(_DEVICE_FIELDS - set(entry))
    if missing:
        raise ValueError(f"is missing {', '.join(missing)}")
    if not is_device_id(entry["id"]):
        raise ValueError("id must be a device id")
    try:
        # The stored form keeps the rule it was written under (``_collapsed_name``).
        name = _collapsed_name(entry["name"])
    except ValueError as error:
        raise ValueError(str(error)) from None
    if name != entry["name"]:
        raise ValueError("name must be normalized")
    if entry["kind"] not in DEVICE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(DEVICE_KINDS)}")
    token_hash = entry["token_hash"]
    if not isinstance(token_hash, str) or not _TOKEN_HASH_PATTERN.fullmatch(token_hash):
        raise ValueError("token_hash must be a SHA-256 hex digest")
    try:
        created_at = parse_canonical_timestamp(entry["created_at"])
        last_used_at = parse_canonical_timestamp(entry["last_used_at"])
    except ValueError as error:
        raise ValueError(str(error)) from None
    client = entry["last_client"]
    if client is not None and (not isinstance(client, str) or parse_ip(client) is None):
        raise ValueError("last_client must be an IP address or null")
    approver = entry["approved_by"]
    if not isinstance(approver, str) or not _APPROVER_PATTERN.fullmatch(approver):
        raise ValueError("approved_by must name the host or a device")
    via = entry.get("via")
    if via is not None and via not in DEVICE_ORIGINS:
        raise ValueError(f"via must be one of {', '.join(DEVICE_ORIGINS)}")
    return Device(
        id=entry["id"],
        name=name,
        kind=entry["kind"],
        token_hash=token_hash,
        created_at=created_at,
        last_used_at=last_used_at,
        last_client=client,
        approved_by=approver,
        via=via,
    )


def _device_record(device: Device) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": device.id,
        "name": device.name,
        "kind": device.kind,
        "token_hash": device.token_hash,
        "created_at": format_canonical_timestamp(device.created_at),
        "last_used_at": format_canonical_timestamp(device.last_used_at),
        "last_client": device.last_client,
        "approved_by": device.approved_by,
    }
    if device.via is not None:
        record["via"] = device.via
    return record


# The whole document must load: an entry this vBot cannot read refuses every write.
DEVICES_FORMAT = JsonDocumentFormat(
    name="Access devices",
    version=DEVICES_FORMAT_VERSION,
    shape=DEVICES_SHAPE,
    validate=_validate_document,
)
