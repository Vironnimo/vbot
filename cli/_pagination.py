"""Shell-safe CLI cursors; server paging state stays an opaque JSON object."""

from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Mapping


def encode_cursor(cursor: Mapping[str, object]) -> str:
    """Keep every server cursor field in a token shells can pass without quoting."""
    payload = json.dumps(
        dict(cursor), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "v1." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> dict[str, object]:
    """Accept a printed token or the CLI's existing JSON-object cursor syntax."""
    try:
        if raw.startswith("v1."):
            encoded = raw[3:]
            if not encoded or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in encoded
            ):
                raise ValueError
            payload = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            value = json.loads(payload)
        else:
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Use the exact cursor from the previous Session list; a JSON object is also accepted"
        ) from error
