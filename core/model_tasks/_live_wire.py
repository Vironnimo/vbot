"""Provider-neutral Live wire contract.

A Live wire is one provider call whose control channel is joined by the server.
It normalizes provider events into the small event set below and accepts the few
commands a Live call needs. Media never passes through a WebRTC wire.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

JsonObject = dict[str, Any]


class WireSendError(Exception):
    """A command could not be sent because the control channel is gone."""


@dataclass(frozen=True)
class WireStarted:
    """The provider session is running; ``expires_at`` is a Unix timestamp when known."""

    expires_at: float | None


@dataclass(frozen=True)
class WireCaption:
    """Transcript text of the current turn; ``final`` marks a finished turn."""

    role: str
    text: str
    final: bool


@dataclass(frozen=True)
class WireDelegation:
    """The voice model handed work to the client.

    ``request`` is the delegated request text when the provider supplies it;
    otherwise the request must be inferred from the transcript.
    """

    delegation_id: str
    request: str | None


@dataclass(frozen=True)
class WireUsage:
    """Cumulative provider usage for the call."""

    usage: JsonObject


@dataclass(frozen=True)
class WireProblem:
    """A provider-reported error event that did not end the call."""

    code: str
    message: str


@dataclass(frozen=True)
class WireClosed:
    """The provider finished the call, or the control channel ended.

    ``confirmed`` is true only when the provider reported the close itself.
    """

    reason: str | None
    usage: JsonObject | None
    confirmed: bool


WireEvent = WireStarted | WireCaption | WireDelegation | WireUsage | WireProblem | WireClosed


class LiveWire(Protocol):
    """One provider call with a joined control channel."""

    @property
    def call_id(self) -> str: ...

    @property
    def answer_sdp(self) -> str: ...

    def events(self) -> AsyncIterator[WireEvent]:
        """Yield normalized events until the control channel ends.

        The last event is always a :class:`WireClosed`.
        """
        ...

    async def deliver_result(self, delegation_id: str, text: str) -> None:
        """Return speakable delegation output to the voice model."""
        ...

    async def announce(self, text: str) -> None:
        """Add speakable context not tied to a delegation."""
        ...

    async def request_close(self) -> None:
        """Ask the provider to finish the call; the close arrives as an event."""
        ...

    async def aclose(self) -> None:
        """Drop the control channel immediately; idempotent."""
        ...
