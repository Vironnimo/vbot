"""What one MCP connection retains about itself: sequenced events and redacted errors.

Events are bounded and numbered, so a reader that polls with a cursor learns how
many it missed. Every retained string passes credential redaction first: the
credentials the connection references and its stored OAuth tokens never enter
an event, an error message or the captured stderr of a local server.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from core.extensions.operations import ExtensionHost

from ._oauth import oauth_secrets

EVENT_HISTORY_LIMIT = 256
STDERR_CHUNK_SIZE = 4096
REDACTED = "[redacted]"


def dump(value: Any) -> dict[str, Any]:
    """An SDK model, dataclass or object as the JSON object the protocol carries."""
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return value
    raise TypeError("MCP payload must be an object")


class ConnectionEvents:
    """One connection's event history and the redaction of its credentials.

    *config* returns the connection's current configuration, whose credential
    references name what to redact.
    """

    def __init__(self, host: ExtensionHost, config: Callable[[], dict[str, Any]]) -> None:
        self._host = host
        self._config = config
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_HISTORY_LIMIT)
        self._sequence = 0

    def record(self, kind: str, payload: Any) -> None:
        """Keep one event; its strings and keys are redacted after JSON decoding."""
        secrets = sorted(self._secrets(), key=len, reverse=True)

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, REDACTED)
                return value
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {redact(key): redact(item) for key, item in value.items()}
            return value

        # Redact decoded strings: replacing inside serialized JSON misses escaped
        # credentials and can corrupt escape sequences for short credentials.
        safe_payload = redact(json.loads(json.dumps(payload, ensure_ascii=False, default=dump)))
        self._sequence += 1
        self._events.append({"sequence": self._sequence, "kind": kind, "payload": safe_payload})

    def read(self, after: int = 0) -> dict[str, Any]:
        """The events after cursor *after*, the current cursor, and how many were dropped."""
        first = self._events[0]["sequence"] if self._events else self._sequence + 1
        return {
            "events": [event for event in self._events if event["sequence"] > after],
            "cursor": self._sequence,
            "missed_events": max(0, first - after - 1),
        }

    def redact(self, message: str) -> str:
        for secret in sorted(self._secrets(), key=len, reverse=True):
            message = message.replace(secret, REDACTED)
        return message

    def safe_error(self, error: BaseException) -> str:
        """*error* as one redacted line naming its type; a group joins its members."""
        if isinstance(error, BaseExceptionGroup):
            message = "; ".join(self.safe_error(item) for item in error.exceptions)
        else:
            message = f"{type(error).__name__}: {error}"
        return self.redact(message)

    def drain_stderr(self, descriptor: int, loop: asyncio.AbstractEventLoop) -> None:
        """Record a local server's stderr as redacted ``stderr`` events; runs on its own thread.

        Draining continuously keeps a verbose server from blocking its protocol
        pipe. Text that could still be the start of a credential waits for the
        next chunk, so a credential split across reads is redacted too.
        """
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
            pending = ""
            while chunk := stream.read(STDERR_CHUNK_SIZE):
                pending += chunk
                tail = max((len(secret) for secret in self._secrets()), default=0)
                if len(pending) <= tail:
                    continue
                boundary = len(pending) - tail
                for secret in self._secrets():
                    start = pending.rfind(secret, 0, boundary + len(secret))
                    if start >= 0 and start < boundary < start + len(secret):
                        boundary = start
                output, pending = pending[:boundary], pending[boundary:]
                if output and not loop.is_closed():
                    loop.call_soon_threadsafe(self.record, "stderr", {"text": self.redact(output)})
            if pending and not loop.is_closed():
                loop.call_soon_threadsafe(self.record, "stderr", {"text": self.redact(pending)})

    def _secrets(self) -> list[str]:
        config = self._config()
        keys = set(config.get("credential_environment", {}).values())
        keys.update(config.get("credential_headers", {}).values())
        secrets = [self._host.resolve_credential(key) for key in keys]
        secrets.extend(oauth_secrets(self._host, config["id"]))
        return [value for value in secrets if value]
