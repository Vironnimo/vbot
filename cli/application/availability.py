"""Whether the update channel publishes a version newer than the active one.

A check reads the channel's release identity
(``packages.newer_published_release``, at most 4 KiB from release downloads,
which have no request limit), optionally its verified changelog, and stores the answer in
``<install>/update-check.json``. The tray, the installation's server and
``vbot application status`` read that answer; whoever finds it due asks the
channel again, so an installation checks about once per ``CHECK_INTERVAL``
whether its tray, its server or both run. The record is a disposable cache: an
unreadable record, or one written for another channel or active version, only
makes the next check due. Nothing here starts an update.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cli.application.packages import newer_published_release, version_label
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    load_installation,
    read_json,
    write_json,
)
from core.utils import changelog
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp

if TYPE_CHECKING:
    import httpx

_LOGGER = logging.getLogger("vbot.application.availability")

RECORD_NAME = "update-check.json"
#: How old an answer may get before the next reader asks the channel again.
CHECK_INTERVAL = timedelta(hours=6)
#: After a failed check (offline, GitHub unavailable) the next one is due sooner.
RETRY_INTERVAL = timedelta(hours=1)
_TIMEOUT_SECONDS = 20.0
_RECORD_LIMIT = changelog.MAX_BYTES + 16 * 1024


@dataclass(frozen=True)
class AvailableVersion:
    """A published version an update would install."""

    version_id: str
    version: str
    #: The version for people, with the build's short revision (``version_label``).
    label: str


@dataclass(frozen=True)
class Availability:
    """The channel's newest answer for one active version.

    ``available`` is the newer version an update would install; ``None`` means
    the active version is the newest the channel publishes, or nothing is known
    yet (``checked_at`` empty). ``checked_at`` is the last successful check,
    ``attempted_at`` the last attempt (canonical UTC); ``error`` says why the
    last attempt failed and is empty after a success. A failed attempt keeps
    what the last successful one found.
    """

    channel: str
    active_version_id: str
    attempted_at: str
    checked_at: str = ""
    available: AvailableVersion | None = None
    error: str = ""
    notes: list[changelog.Release] | None = None

    @property
    def offered_label(self) -> str:
        if self.available is None:
            return ""
        return self.available.version if self.channel == "release" else self.available.label

    def projection(self) -> dict[str, Any]:
        """The answer as the server and ``vbot application status`` report it."""

        available = self.available
        return {
            "channel": self.channel,
            "checked_at": self.checked_at or None,
            "error": self.error or None,
            "notes": self.notes,
            "available": None
            if available is None
            else {
                "version_id": available.version_id,
                "version": available.version,
                "label": available.label,
            },
        }


def read(install: Installation) -> Availability | None:
    """The stored answer for the installation's channel and active version, if any."""

    try:
        install = _current(install)
        value = read_json(contained(install.root, RECORD_NAME), limit=_RECORD_LIMIT)
        stored = _from_record(value)
        active = install.version().name
    except ApplicationError, OSError, ValueError:
        return None
    if stored is None or stored.channel != install.channel or stored.active_version_id != active:
        return None
    return stored


def check(
    install: Installation,
    *,
    client: httpx.Client | None = None,
    now: Callable[[], datetime] | None = None,
) -> Availability:
    """Ask the channel now and store the answer; a failure is stored in ``error``."""

    install = _current(install)
    previous = read(install)
    attempted = format_canonical_timestamp((now or _utc_now)())
    active = install.version().name
    try:
        identity, notes = _check_channel(install, client)
    # Any failure (offline, an unexpected answer, an unreadable active version)
    # only becomes the stored reason; the next check is due after RETRY_INTERVAL.
    except Exception as error:
        result = Availability(
            install.channel,
            active,
            attempted,
            checked_at=previous.checked_at if previous else "",
            available=previous.available if previous else None,
            notes=previous.notes if previous else None,
            error=str(error) or type(error).__name__,
        )
        _LOGGER.info("Could not check for a newer vBot version (error=%s)", result.error)
    else:
        result = Availability(
            install.channel,
            active,
            attempted,
            checked_at=attempted,
            notes=notes,
            available=None
            if identity is None
            else AvailableVersion(
                str(identity["version_id"]),
                str(identity.get("version") or ""),
                version_label(identity),
            ),
        )
    try:
        write_json(contained(install.root, RECORD_NAME), _to_record(result))
    except (ApplicationError, OSError) as error:
        _LOGGER.warning("Could not store the update check (error=%s)", error)
    return result


def refresh(
    install: Installation,
    *,
    client: httpx.Client | None = None,
    now: Callable[[], datetime] | None = None,
) -> Availability:
    """The stored answer, after asking the channel when it is due."""

    clock = now or _utc_now
    stored = read(install)
    if stored is not None and not _due(stored, clock()):
        return stored
    return check(install, client=client, now=clock)


def review(
    install: Installation, *, selected: str | None = None, prepared: str | None = None
) -> tuple[list[changelog.Release], str | None]:
    """Pre-install notes and the channel identity the worker must still see."""
    if install.channel != "release" or not install.release_public_key:
        return [], None
    try:
        if prepared:
            root = install.version(prepared)
            manifest = read_json(root / "release.json", limit=32 * 1024**2)
            if manifest.get("channel") != "release":
                return [], None
            active = read_json(install.version() / "release.json", limit=32 * 1024**2)["version"]
            lower = changelog.version_key(active)
            upper = changelog.version_key(manifest["version"])
            releases = changelog.bundled(root / "app/changelog")
            return [
                r for r in releases if lower < changelog.version_key(r["version"]) <= upper
            ], None
        if selected:
            return _notes_for_version(install, selected), None
        # An operator requested an update: an earlier "up to date" answer cannot
        # identify the release the worker will install or its new warnings.
        cached = read(install)
        checked = (
            cached
            if cached and cached.available and not _due(cached, _utc_now())
            else check(install)
        )
        if checked.available:
            return checked.notes or [], checked.available.version_id
        return [], checked.active_version_id if checked.checked_at else None
    except Exception as error:
        _LOGGER.info("Could not read pre-update notices (error=%s)", error)
        return [], None


def _notes_for_version(install: Installation, version: str) -> list[changelog.Release]:
    """Notes through an explicitly chosen release, including older pinned targets."""
    cached = read(install)
    if (
        cached
        and cached.notes
        and not _due(cached, _utc_now())
        and any(r["version"] == version for r in cached.notes)
    ):
        return [
            r
            for r in cached.notes
            if changelog.version_key(r["version"]) <= changelog.version_key(version)
        ]
    _, notes = _check_channel(install, None, version=version)
    return notes or []


def _check_channel(
    install: Installation, client: httpx.Client | None, *, version: str | None = None
) -> tuple[dict[str, Any] | None, list[changelog.Release] | None]:
    if client is None:
        import httpx

        with httpx.Client(
            timeout=_TIMEOUT_SECONDS, follow_redirects=True, trust_env=False
        ) as owned:
            return _check_channel(install, owned, version=version)
    identity = newer_published_release(install, client, version=version)
    if install.channel != "release" or not identity:
        return identity, None
    try:
        digests = identity.get("assets") or {}
        expected = digests.get(changelog.ASSET)
        if not expected:
            return identity, None
        offered = identity["version"]
        url = f"{install.asset_base(offered)}/{changelog.ASSET}"
        data = bytearray()
        with client.stream("GET", url) as response:
            response.raise_for_status()
            if response.url.scheme != "https":
                raise ValueError("Insecure changelog redirect")
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(data) + len(chunk) > changelog.MAX_BYTES:
                    raise ValueError("Changelog asset exceeds 256 KiB")
                data.extend(chunk)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Changelog digest mismatch")
        asset = json.loads(data)
        notes = _read_notes(asset["releases"])
        if (
            asset["schema_version"] != 1
            or not notes
            or not any(r["version"] == offered for r in notes)
        ):
            raise ValueError("Unexpected changelog release or schema")
        active = read_json(install.version() / "release.json", limit=32 * 1024**2)["version"]
        lower, upper = changelog.version_key(active), changelog.version_key(offered)
        return identity, [r for r in notes if lower < changelog.version_key(r["version"]) <= upper]
    except Exception as error:
        # Notes are optional even when a verified, newer package is available.
        _LOGGER.info("Could not read release notes (error=%s)", error)
        return identity, None


def _read_notes(value: object) -> list[changelog.Release] | None:
    if not isinstance(value, list) or len(value) > changelog.MAX_RELEASES:
        return None
    releases: list[changelog.Release] = []
    for item in value:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) for k in ("version", "date", "markdown")
        ):
            return None
        before = item.get("before_update")
        if before is not None and not isinstance(before, str):
            return None
        try:
            changelog.version_key(item["version"])
            if date.fromisoformat(item["date"]).isoformat() != item["date"]:
                return None
        except ValueError:
            return None
        releases.append(
            changelog.Release(
                version=item["version"],
                date=item["date"],
                before_update=before,
                markdown=item["markdown"],
            )
        )
    if len({r["version"] for r in releases}) != len(releases):
        return None
    return sorted(releases, key=lambda r: changelog.version_key(r["version"]), reverse=True)


def _current(install: Installation) -> Installation:
    """The installation as recorded now: ``vbot application channel`` changes it."""

    try:
        return load_installation(install.root)
    except ApplicationError:
        return install


def _due(stored: Availability, now: datetime) -> bool:
    try:
        attempted = parse_timestamp(stored.attempted_at)
    except ValueError:
        return True
    interval = RETRY_INTERVAL if stored.error else CHECK_INTERVAL
    return not attempted <= now < attempted + interval


def _to_record(value: Availability) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "channel": value.channel,
        "active_version_id": value.active_version_id,
        "attempted_at": value.attempted_at,
        "checked_at": value.checked_at,
        "available": None
        if value.available is None
        else {
            "version_id": value.available.version_id,
            "version": value.available.version,
            "label": value.available.label,
        },
        "error": value.error,
        "notes": value.notes,
    }


def _from_record(value: dict[str, Any]) -> Availability | None:
    if value.get("schema_version") != 1:
        return None
    texts: dict[str, str] = {}
    for key in ("channel", "active_version_id", "attempted_at", "checked_at", "error"):
        text = value.get(key)
        if not isinstance(text, str):
            return None
        texts[key] = text
    available = value.get("available")
    version: AvailableVersion | None = None
    if available is not None:
        if not isinstance(available, dict):
            return None
        fields: list[str] = []
        for key in ("version_id", "version", "label"):
            field = available.get(key)
            if not isinstance(field, str) or not field:
                return None
            fields.append(field)
        version = AvailableVersion(*fields)
    return Availability(
        texts["channel"],
        texts["active_version_id"],
        texts["attempted_at"],
        checked_at=texts["checked_at"],
        available=version,
        error=texts["error"],
        notes=_read_notes(value.get("notes"))
        if version and texts["channel"] == "release"
        else None,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
