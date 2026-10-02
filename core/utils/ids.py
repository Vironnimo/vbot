"""Compact, typed object identities and the filesystem boundary for names.

Generated ids are references, never secrets. Owners retain their ordinary
authorization checks. A 12-character lowercase base32 suffix carries 60 random
bits; prefixes identify the object kind without a second, session-local alias
namespace. User-chosen names that become one file or folder name pass
:func:`is_reserved_name` when they are created or renamed.
"""

from __future__ import annotations

import ntpath
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_OPAQUE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
# ``ntpath.isreserved`` omits COM0 and LPT0, which Windows documents as reserved too.
_WINDOWS_DEVICES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"{port}{digit}" for port in ("COM", "LPT") for digit in "0123456789¹²³"),
    }
)


def new_id(prefix: str, *, claim: Callable[[str], bool] | None = None) -> str:
    """Generate and claim an unused identity, retrying collisions.

    The owner must atomically reserve the candidate in ``claim`` or prevent
    concurrent creation until it has published the returned id. Returning False
    rejects a collision; other failures propagate without retrying side effects.
    Without a claim, use 80 random bits (16 characters) for identities created
    before they have a storage owner, such as Messages. No global registry or
    lifetime state is kept here.
    """
    bit_count = 60 if claim is not None else 80
    while True:
        bits = secrets.randbits(bit_count)
        suffix = "".join(_ALPHABET[(bits >> shift) & 31] for shift in range(bit_count - 5, -1, -5))
        candidate = f"{prefix}_{suffix}"
        if claim is None or claim(candidate):
            return candidate


def is_safe_id(value: object) -> bool:
    """Accept opaque, bounded lowercase ids usable as one filesystem basename."""
    return (
        isinstance(value, str)
        and _OPAQUE_ID.fullmatch(value) is not None
        and not is_reserved_name(value)
    )


def is_reserved_name(name: str) -> bool:
    """Return whether Windows cannot use ``name`` as one file or folder name.

    Reserved are device names such as ``CON``, ``NUL``, ``COM0`` or ``LPT1``, also
    with an extension (``aux.json``); names ending in a dot or space; and names
    containing a control character or any of ``< > : " / \\ | ? *``. This is
    ``ntpath.isreserved`` for one component, stricter by ``COM0``/``LPT0`` and by
    a drive prefix (``a:b``) or separator that ``ntpath`` splits off first.
    Owners refuse such names on every platform when a user creates or renames
    something stored under that name, so data stays portable to Windows; readers
    keep accepting existing names. ``"."`` and ``".."`` are not reserved here and
    stay the caller's check.
    """
    return (
        any(char in name for char in ":/\\")
        or ntpath.isreserved(name)
        or _windows_device(name) is not None
    )


def reserved_name_message(subject: str, name: str, *, advice: str | None = None) -> str:
    """Explain why ``name`` was refused, for a ``name`` where :func:`is_reserved_name` holds.

    ``subject`` names what the user chose, such as ``"Agent id"``; it appears as
    ``The <subject> '<name>' ...``. ``advice`` replaces the closing
    ``choose a different <subject>`` where the name is derived from other input.
    """
    device = _windows_device(name)
    if device is not None:
        reason = (
            f"{device} is a device name there (like CON, PRN, AUX, NUL, COM1 or LPT1, "
            "also with an extension such as .txt), so it cannot name a file or folder"
        )
    else:
        reason = (
            "a file or folder name there cannot contain a control character or any of "
            '< > : " / \\ | ? *, and cannot end in a dot or space'
        )
    return (
        f"The {subject} {name!r} is reserved on Windows: {reason}. vBot refuses it on "
        "every system so the data stays usable on Windows; "
        f"{advice or f'choose a different {subject}'}."
    )


def _windows_device(name: str) -> str | None:
    stem = name.partition(".")[0].rstrip(" ").upper()
    return stem if stem in _WINDOWS_DEVICES else None


def has_id_entry(directory: Path, identifier: str) -> bool:
    """Return whether ``directory`` holds an entry spelled exactly ``identifier``.

    Ids are exact, but a case-insensitive filesystem (Windows) opens a stored
    ``vbot`` entry for ``directory / "VBOT"``. Path-backed owners use this check
    before treating a requested id as present, so a case variant names no object
    on every platform. A missing directory holds no entries; other filesystem
    errors propagate to the owner.
    """
    try:
        with os.scandir(directory) as entries:
            return any(entry.name == identifier for entry in entries)
    except FileNotFoundError, NotADirectoryError:
        return False


def write_id_file(directory: Path, prefix: str, suffix: str, data: bytes) -> Path:
    """Write a uniquely named file exclusively; a collision never replaces data.

    Callers own allowed suffixes, error translation, retention, and publication.
    Interrupted writes may leave a partial file, whose name stays occupied.
    """
    directory.mkdir(parents=True, exist_ok=True)

    def claim(candidate: str) -> bool:
        try:
            with (directory / f"{candidate}{suffix}").open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            return False
        return True

    return directory / f"{new_id(prefix, claim=claim)}{suffix}"
