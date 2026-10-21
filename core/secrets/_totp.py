"""RFC 6238 time-based one-time codes from a login's stored seed."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


class TotpSeedError(ValueError):
    """A stored seed is neither a Base32 secret nor an ``otpauth://`` URI."""


def totp_code(seed: str, now: datetime) -> str:
    """The current code for ``seed``: a Base32 secret or an ``otpauth://totp`` URI."""
    secret, digits, period, algorithm = _parse_seed(seed)
    counter = int(now.timestamp()) // period
    digest = hmac.new(secret, struct.pack(">Q", counter), algorithm).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**digits)).zfill(digits)


def check_totp_seed(seed: str) -> None:
    _parse_seed(seed)


def _parse_seed(seed: str) -> tuple[bytes, int, int, Callable[..., Any]]:
    text = seed.strip()
    digits, period = 6, 30
    algorithm: Callable[..., Any] = hashlib.sha1
    if text.lower().startswith("otpauth://"):
        parsed = urlparse(text)
        if parsed.netloc.lower() != "totp":
            raise TotpSeedError("only otpauth://totp URIs are supported")
        query = {key.lower(): values[-1] for key, values in parse_qs(parsed.query).items()}
        text = unquote(query.get("secret", ""))
        try:
            digits = int(query.get("digits", 6))
            period = int(query.get("period", 30))
        except ValueError as exc:
            raise TotpSeedError("the otpauth URI has invalid digits or period") from exc
        algorithm = _ALGORITHMS.get(query.get("algorithm", "SHA1").upper(), hashlib.sha1)
    if not 6 <= digits <= 10 or not 1 <= period <= 300:
        raise TotpSeedError("unsupported digits or period")
    cleaned = text.replace(" ", "").replace("-", "").upper()
    if not cleaned:
        raise TotpSeedError("the seed is empty")
    try:
        secret = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except ValueError as exc:
        raise TotpSeedError("the seed is not Base32") from exc
    return secret, digits, period, algorithm
