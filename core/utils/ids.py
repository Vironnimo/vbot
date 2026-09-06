"""Compact, typed object identities shared by kernel domains.

These are references, never secrets. Owners retain their ordinary authorization
checks. A 12-character lowercase base32 suffix carries 60 random bits; prefixes
identify the object kind without a second, session-local alias namespace.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from pathlib import Path

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_OPAQUE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_WINDOWS_DEVICE_ID = re.compile(r"con|prn|aux|nul|com[1-9]|lpt[1-9]")


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
        and _WINDOWS_DEVICE_ID.fullmatch(value) is None
    )


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
