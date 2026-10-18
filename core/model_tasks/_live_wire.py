"""Provider-neutral Live wire contract.

A Live wire is one provider call whose control channel is joined by the server.
It normalizes provider events into the small event set below and accepts the few
commands a Live call needs. Its ``media`` descriptor tells the accessor how
audio flows:

* ``{"type": "webrtc", "sdp": <answer>}``: the accessor connects audio directly
  to the provider; media never passes through the server.
* ``{"type": "relay", "audio": RELAY_AUDIO_FORMAT}``: audio is relayed through
  the server. The wire accepts microphone PCM through ``send_audio`` and emits
  assistant audio as :class:`WireAudio` and barge-in as
  :class:`WirePlaybackClear`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

JsonObject = dict[str, Any]

MEDIA_WEBRTC = "webrtc"
MEDIA_RELAY = "relay"
RELAY_SAMPLE_RATE = 24000
# 16-bit mono PCM: bytes per millisecond of relayed audio.
RELAY_BYTES_PER_MS = RELAY_SAMPLE_RATE * 2 // 1000
RELAY_AUDIO_FORMAT: JsonObject = {
    "encoding": "pcm16",
    "sample_rate": RELAY_SAMPLE_RATE,
    "channels": 1,
}


def relay_media() -> JsonObject:
    """Return a fresh relay media descriptor."""

    return {"type": MEDIA_RELAY, "audio": dict(RELAY_AUDIO_FORMAT)}


def websocket_url(base_url: str, path: str) -> str:
    """Return the WebSocket URL for *path* under an HTTP(S) Provider base URL."""

    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    return base + path


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
class WireToolCall:
    """A Model of the call called one of vBot's function Tools.

    The caller is the voice model, or OpenAI's hosted backend model. ``name``
    is the name as called, which the voice Agent's Session resolves.
    ``arguments`` is the decoded argument value, which may not be an object, or
    the raw text when it is not JSON. The result text returns through
    :meth:`LiveWire.deliver_result` with the same id.
    """

    call_id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class WireAudio:
    """PCM to render at a source-sample offset in one relay playback generation."""

    item_id: str
    pcm: bytes
    generation: int = 1
    start_samples: int = 0


@dataclass(frozen=True)
class WirePlaybackClear:
    """Discard previous relay audio and begin this generation at sample zero."""

    generation: int = 1


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


WireEvent = (
    WireStarted
    | WireCaption
    | WireDelegation
    | WireToolCall
    | WireAudio
    | WirePlaybackClear
    | WireUsage
    | WireProblem
    | WireClosed
)


@dataclass(frozen=True)
class PlaybackReceipt:
    """One accessor's confirmed rendered prefix in a relay generation.

    Generation zero reports initial readiness or owner loss. Retired-generation
    receipts can finish a clear, but cannot change current playback readiness.
    """

    generation: int
    played_samples: int
    enabled: bool
    cleared: bool = False

    def __post_init__(self) -> None:
        for value in (self.generation, self.played_samples):
            if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                raise ValueError("Playback positions must be uint32 integers")
        if not isinstance(self.enabled, bool) or not isinstance(self.cleared, bool):
            raise ValueError("Playback flags must be booleans")

    @classmethod
    def from_owner_frame(cls, frame: Mapping[str, Any]) -> PlaybackReceipt | None:
        """Parse the owner report; malformed frames remain silently ignored."""
        try:
            return cls(
                generation=frame["generation"],
                played_samples=frame["played_samples"],
                enabled=frame["enabled"],
                cleared=frame.get("cleared", False),
            )
        except KeyError, ValueError:
            return None

    def confirmed_prefix(self, produced_samples: int, previous: int) -> int:
        """Keep only actual produced samples, without regressing a confirmed prefix."""
        return max(previous, min(self.played_samples, produced_samples))

    def controls_generation(self, generation: int) -> bool:
        """Initial readiness and the current generation may change playback state."""
        return self.generation in {0, generation}


class LiveWire(Protocol):
    """One provider call with a joined control channel."""

    @property
    def call_id(self) -> str: ...

    @property
    def media(self) -> JsonObject:
        """How the accessor connects audio; see the module docstring."""
        ...

    def events(self) -> AsyncIterator[WireEvent]:
        """Yield normalized events until the control channel ends.

        The last event is always a :class:`WireClosed`.
        """
        ...

    async def deliver_result(self, delegation_id: str, text: str) -> None:
        """Return delegation output, or a Tool call's JSON result, to the voice model."""
        ...

    async def send_audio(self, pcm: bytes) -> None:
        """Forward microphone PCM (relay media only); dropped before the session runs."""
        ...

    async def update_playback(self, receipt: PlaybackReceipt) -> list[WireEvent]:
        """Apply the accessor's rendered audio prefix; return any playback reset."""
        ...

    @property
    def announces_as_user_input(self) -> bool:
        """Whether announcements reach the voice model like user input.

        The voice model then follows instructions quoted in them, so the call
        leaves untrusted text such as Run result excerpts out.
        """
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
