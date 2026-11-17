"""The Gemini Live session state, sans IO.

:class:`GoogleSession` builds the ``setup`` message, normalizes the
``BidiGenerateContent`` server messages into wire events, keeps the
announcement gate and the accessor's rendered audio prefix, and returns the
client messages to send; :class:`core.model_tasks._live_google.GoogleLiveWire`
drives it with socket frames and a reconnect. Server VAD takes turns and
interrupts a model turn the user talks over.

Wire facts verified live (2026-11-17, ``gemini-3.8-live``,
``gemini-3.1-flash-live-preview``, ``gemini-2.5-flash-native-audio-latest``):
``setupComplete`` answers ``setup``; input audio at
``audio/pcm;rate=24000`` needs no resampling and output is 24 kHz PCM16 in
``modelTurn`` ``inlineData`` parts; transcripts are deltas, and 2.5 sends them
in messages of their own; ``voiceActivity`` reports the user's speech start
and end; a barge-in sends ``interrupted`` and then ``turnComplete`` even after
``generationComplete``; ``usageMetadata`` comes with each ``turnComplete`` and
counts that turn only, the whole context as its prompt; a function call ends
its turn (3.1: without ``turnComplete``) and the answer follows the
``toolResponse``; ``sessionResumptionUpdate`` handles arrive without asking;
setup errors and an invalid key close the socket (1007, 1008) after a 101
handshake; an unknown voice name is accepted silently. Extended Thinking sends
``turnComplete`` with ``interactionStatus: "IN_PROGRESS"`` after a spoken
filler and ``"IDLE"`` once the whole interaction ended.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.model_tasks._live_wire import (
    RELAY_SAMPLE_RATE,
    JsonObject,
    PlaybackReceipt,
    WireAudio,
    WireCaption,
    WireClosed,
    WireConnection,
    WireEvent,
    WirePlaybackClear,
    WireProblem,
    WireStarted,
    WireToolCall,
    WireUsage,
)
from core.providers.tool_schema import render_tool_definitions

# Microphone PCM goes out at the relay rate, which Gemini accepts as declared.
INPUT_MIME_TYPE = f"audio/pcm;rate={RELAY_SAMPLE_RATE}"
# A model turn without messages for this long no longer blocks announcements.
# Extended Thinking reasons in the background for 15-20 s (live 2026-11-17).
_TURN_STALL_SECONDS = 60.0
# Resume before the server drops a connection it announced with goAway.
_GO_AWAY_MARGIN_SECONDS = 5.0
_REMEMBERED_IDS = 256
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)s$")
_AUDIO_MIME = re.compile(r"^audio/(?:pcm|l16)\b", re.IGNORECASE)
_AUDIO_RATE = re.compile(r"\brate=(\d+)")

Clock = Callable[[], float]


@dataclass
class _Step:
    """The result of one input: normalized events and client messages.

    ``resume`` asks the driver to move the session to a new connection now.
    """

    events: list[WireEvent] = field(default_factory=list)
    commands: list[JsonObject] = field(default_factory=list)
    resume: bool = False


@dataclass
class _Playback:
    samples: int = 0
    played: int = 0


class _Recent:
    """The most recent ids, bounded."""

    def __init__(self, limit: int = _REMEMBERED_IDS) -> None:
        self._limit = limit
        self._items: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def put(self, key: str) -> None:
        self._items[key] = None
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)


class GoogleSession:
    """Sans-IO state of one Gemini Live session across its connections.

    Announcement gate: vBot's own text reaches the model as a user turn, which
    interrupts a running model turn, so it is sent only while the model is
    idle, the user is not speaking, no function call waits for its result,
    and the accessor has confirmed playback drained. One turn carries every
    pending announcement.

    Gemini cannot truncate what the model said; a barge-in or a playback
    reset starts a new playback generation and drops the rest of the turn.
    """

    def __init__(
        self,
        *,
        model_id: str,
        instructions: str,
        voice: str | None,
        tools: list[JsonObject],
        thinking_level: str | None = None,
        non_blocking_tools: bool = False,
        clock: Clock = time.monotonic,
    ) -> None:
        self._model_id = model_id
        self._instructions = instructions
        self._voice = voice
        self._tools = [dict(tool) for tool in tools]
        self._thinking_level = thinking_level
        self._non_blocking_tools = non_blocking_tools
        self._clock = clock
        self._started = False
        self._attached = False
        self._close_reason: str | None = None
        self._handle: str | None = None
        # Set by goAway: the server ends this connection soon.
        self._go_away = False
        self._resume_by: float | None = None
        self._usage: dict[str, int] = {}
        # Announcement gate.
        self._turn_active = False
        self._turn_touched = 0.0
        self._user_speaking = False
        self._announcements: list[str] = []
        self._awaiting: dict[str, str] = {}
        self._handled_calls = _Recent()
        # Messages for the next connection, held while none is attached.
        self._held: list[JsonObject] = []
        # Only the accessor knows what its AudioWorklet actually rendered.
        self._turns = 0
        self._playback_generation = 1
        self._playback_enabled = False
        self._playback: dict[int, _Playback] = {1: _Playback()}
        # Captions.
        self._user_text = ""
        self._assistant_text = ""

    @property
    def started(self) -> bool:
        return self._started

    @property
    def attached(self) -> bool:
        return self._attached

    @property
    def resumable(self) -> bool:
        """Whether a new connection can continue this session."""
        return self._started and self._handle is not None and self._close_reason is None

    # -- commands ---------------------------------------------------------

    def setup(self) -> JsonObject:
        """Return the ``setup`` message for a new connection, resuming when possible."""

        generation: JsonObject = {"responseModalities": ["AUDIO"]}
        if self._voice:
            generation["speechConfig"] = {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self._voice}}
            }
        if self._thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": self._thinking_level}
        setup: JsonObject = {
            "model": f"models/{self._model_id}",
            "generationConfig": generation,
            "systemInstruction": {"parts": [{"text": self._instructions}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            # Lifts the 15-minute audio session limit.
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {"handle": self._handle} if self._handle else {},
        }
        declarations = [
            _declaration(tool, non_blocking=self._non_blocking_tools)
            for tool in render_tool_definitions(
                [dict(tool) for tool in self._tools], profile="omit_strict"
            )
        ]
        if declarations:
            setup["tools"] = [{"functionDeclarations": declarations}]
        return {"setup": setup}

    def deliver(self, call_id: str, text: str) -> list[JsonObject]:
        """Answer one function call; unknown, cancelled or answered calls are ignored."""

        name = self._awaiting.pop(call_id, None)
        if name is None:
            return []
        self._touch_turn()
        response = {
            "toolResponse": {
                "functionResponses": [{"id": call_id, "name": name, "response": {"output": text}}]
            }
        }
        return [*self._send([response]), *self._flush()]

    def announce(self, text: str) -> list[JsonObject]:
        """Add application context the voice model should speak about.

        Gemini Live takes no system message during the conversation, so the
        context arrives as a user text turn.
        """

        if not self._started or self._close_reason is not None or not text:
            return []
        self._announcements.append(text)
        return self._flush()

    def audio(self, pcm: bytes) -> list[JsonObject]:
        """Forward microphone audio; dropped while no connection is set up."""

        if not self._attached or self._close_reason is not None or not pcm:
            return []
        return [
            {
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(pcm).decode("ascii"),
                        "mimeType": INPUT_MIME_TYPE,
                    }
                }
            }
        ]

    def playback(self, receipt: PlaybackReceipt) -> _Step:
        """Keep a monotonically increasing rendered prefix, never a delivery estimate."""

        step = _Step()
        progress = self._playback.get(receipt.generation)
        if progress is not None:
            progress.played = receipt.confirmed_prefix(progress.samples, progress.played)
        if receipt.controls_generation(self._playback_generation):
            changed = receipt.enabled != self._playback_enabled
            self._playback_enabled = receipt.enabled
            if receipt.cleared or (changed and not receipt.enabled):
                self._clear_playback(step)
        step.commands.extend(self._flush())
        step.resume = self._wants_resume()
        return step

    def tick(self) -> _Step:
        """Release a stalled turn, and resume before an announced disconnect."""

        step = _Step()
        now = self._clock()
        if self._turn_active and now >= self._turn_touched + _TURN_STALL_SECONDS:
            self._turn_active = False
            self._finish_captions(step)
        step.commands.extend(self._flush())
        step.resume = self._wants_resume()
        return step

    def next_deadline(self) -> float | None:
        """Return the next time :meth:`tick` can change anything, if any."""

        deadlines: list[float] = []
        if self._turn_active:
            deadlines.append(self._turn_touched + _TURN_STALL_SECONDS)
        if self._resume_by is not None and self.resumable:
            deadlines.append(self._resume_by)
        return min(deadlines) if deadlines else None

    def start(self) -> list[WireEvent]:
        """The first connection completed its setup."""

        self._attached = True
        self._started = True
        # Compression and resumption leave the session without a limit.
        return [WireStarted(expires_at=None)]

    def detach(self) -> list[WireEvent]:
        """The connection dropped; the driver tries to resume on a new one."""

        step = _Step()
        self._attached = False
        self._end_turn(step)
        self._user_speaking = False
        step.events.append(WireConnection(reconnecting=True))
        return step.events

    def attach(self) -> _Step:
        """A new connection continues the session; the turn in progress is lost."""

        step = _Step()
        self._end_turn(step)
        self._user_speaking = False
        self._go_away = False
        self._resume_by = None
        if not self._attached:
            self._attached = True
            step.events.append(WireConnection(reconnecting=False))
        held, self._held = self._held, []
        step.commands.extend(held)
        step.commands.extend(self._flush())
        return step

    def resume_failed(self) -> None:
        """A planned resume failed; the connection serves until the server drops it."""
        self._resume_by = None

    def resumes_after(self, code: int | None) -> bool:
        """Whether a connection that ended with close *code* is worth resuming."""
        if not self.resumable or code in _SETUP_CLOSE_CODES:
            return False
        return self._go_away or code in _RESUMABLE_CLOSE_CODES

    def finish(self, *, code: int | None = None, reason: str = "") -> list[WireEvent]:
        """Return the final events once the socket ended; the last is :class:`WireClosed`.

        *code* and *reason* are the WebSocket close the server sent, if any.
        """

        step = _Step()
        self._finish_captions(step)
        if self._close_reason is None and code in _FATAL_CLOSE_CODES:
            step.events.append(
                WireProblem(code=f"close_{code}", message=reason or str(code), fatal=True)
            )
            self._close_reason = "provider_error"
        elif self._close_reason is None and self._go_away:
            # The connection ended after goAway and could not be resumed.
            self._close_reason = "max_duration"
        step.events.append(
            WireClosed(
                reason=self._close_reason,
                usage=dict(self._usage) or None,
                confirmed=self._close_reason is not None,
            )
        )
        return step.events

    # -- server messages --------------------------------------------------

    def receive(self, message: JsonObject) -> _Step:
        """Apply one server message."""

        step = _Step()
        content = message.get("serverContent")
        if isinstance(content, dict):
            self._on_content(step, content)
        calls = message.get("toolCall")
        if isinstance(calls, dict):
            self._on_tool_call(step, calls)
        cancelled = message.get("toolCallCancellation")
        if isinstance(cancelled, dict):
            ids = cancelled.get("ids")
            for call_id in ids if isinstance(ids, list) else []:
                # The model dropped the call; its late result is not sent.
                if isinstance(call_id, str):
                    self._awaiting.pop(call_id, None)
        activity = message.get("voiceActivity")
        if isinstance(activity, dict):
            self._on_voice_activity(step, activity)
        update = message.get("sessionResumptionUpdate")
        if isinstance(update, dict):
            handle = update.get("newHandle")
            if update.get("resumable") and isinstance(handle, str) and handle:
                self._handle = handle
        go_away = message.get("goAway")
        if isinstance(go_away, dict):
            left = _seconds(go_away.get("timeLeft"))
            self._go_away = True
            self._resume_by = self._clock() + max(0.0, (left or 0.0) - _GO_AWAY_MARGIN_SECONDS)
        self._add_usage(step, message.get("usageMetadata"))
        step.commands.extend(self._flush())
        step.resume = self._wants_resume()
        return step

    def _on_content(self, step: _Step, content: JsonObject) -> None:
        if content.get("interrupted"):
            # Server VAD cancelled the model turn; its rest never plays.
            self._end_turn(step)
            self._clear_playback(step)
        user = content.get("inputTranscription")
        if isinstance(user, dict):
            self._on_user_text(step, user.get("text"))
        turn = content.get("modelTurn")
        parts = turn.get("parts") if isinstance(turn, dict) else None
        output = content.get("outputTranscription")
        output_text = output.get("text") if isinstance(output, dict) else None
        if parts or output_text:
            if not self._turn_active:
                self._turns += 1
            # The model answers, so the user's turn is over.
            self._finish_user(step)
            self._touch_turn()
            for part in parts if isinstance(parts, list) else []:
                self._on_part(step, part)
            if isinstance(output_text, str) and output_text:
                self._assistant_text += output_text
                step.events.append(
                    WireCaption(role="assistant", text=self._assistant_text.strip(), final=False)
                )
        if content.get("turnComplete"):
            if content.get("interactionStatus") == "IN_PROGRESS":
                # Extended Thinking keeps working after a spoken filler.
                self._finish_assistant(step)
                self._touch_turn()
            else:
                self._end_turn(step)

    def _on_part(self, step: _Step, part: Any) -> None:
        if not isinstance(part, dict):
            return
        inline = part.get("inlineData")
        if not isinstance(inline, dict):
            return
        mime_type = inline.get("mimeType")
        data = inline.get("data")
        if not isinstance(mime_type, str) or not isinstance(data, str):
            return
        rate = _AUDIO_RATE.search(mime_type)
        if not _AUDIO_MIME.match(mime_type) or (rate and int(rate.group(1)) != RELAY_SAMPLE_RATE):
            return
        try:
            pcm = base64.b64decode(data, validate=True)
        except binascii.Error, ValueError:
            return
        pcm = pcm[: len(pcm) - len(pcm) % 2]
        if not pcm:
            return
        generation = self._playback_generation
        progress = self._playback[generation]
        start = progress.samples
        progress.samples += len(pcm) // 2
        if self._playback_enabled:
            step.events.append(
                WireAudio(
                    item_id=f"turn_{self._turns}",
                    pcm=pcm,
                    generation=generation,
                    start_samples=start,
                )
            )

    def _on_user_text(self, step: _Step, text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        self._user_text += text
        step.events.append(WireCaption(role="user", text=self._user_text.strip(), final=False))

    def _on_tool_call(self, step: _Step, calls: JsonObject) -> None:
        functions = calls.get("functionCalls")
        for call in functions if isinstance(functions, list) else []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            name = call.get("name")
            if not isinstance(call_id, str) or not call_id or call_id in self._handled_calls:
                continue
            name = name if isinstance(name, str) else ""
            self._handled_calls.put(call_id)
            self._awaiting[call_id] = name
            self._touch_turn()
            # The call prepares every name and argument spelling itself.
            step.events.append(
                WireToolCall(call_id=call_id, name=name, arguments=_arguments(call.get("args")))
            )

    def _on_voice_activity(self, step: _Step, activity: JsonObject) -> None:
        kind = activity.get("type")
        if kind == "ACTIVITY_START":
            self._user_speaking = True
            self._finish_user(step)
        elif kind == "ACTIVITY_END":
            self._user_speaking = False

    # -- helpers ----------------------------------------------------------

    def _send(self, commands: list[JsonObject]) -> list[JsonObject]:
        """Send now, or hold the messages for the next connection."""
        if self._attached:
            return commands
        self._held.extend(commands)
        return []

    def _flush(self) -> list[JsonObject]:
        if (
            not self._attached
            or self._close_reason is not None
            or not self._announcements
            or self._turn_active
            or self._user_speaking
            or self._awaiting
            or (
                self._playback_enabled
                and self._playback[self._playback_generation].played
                < self._playback[self._playback_generation].samples
            )
        ):
            return []
        parts = [{"text": text} for text in self._announcements]
        self._announcements = []
        self._touch_turn()
        turn = {"role": "user", "parts": parts}
        return [{"clientContent": {"turns": [turn], "turnComplete": True}}]

    def _wants_resume(self) -> bool:
        """Resume after goAway once the model is idle, or when time runs out."""
        if self._resume_by is None or not self.resumable or not self._attached:
            return False
        idle = not (self._turn_active or self._user_speaking or self._awaiting)
        return idle or self._clock() >= self._resume_by

    def _touch_turn(self) -> None:
        self._turn_active = True
        self._turn_touched = self._clock()

    def _end_turn(self, step: _Step) -> None:
        self._turn_active = False
        self._finish_captions(step)

    def _clear_playback(self, step: _Step) -> None:
        self._playback_generation += 1
        self._playback = {self._playback_generation: _Playback()}
        step.events.append(WirePlaybackClear(self._playback_generation))

    def _finish_captions(self, step: _Step) -> None:
        self._finish_user(step)
        self._finish_assistant(step)

    def _finish_user(self, step: _Step) -> None:
        if self._user_text:
            step.events.append(WireCaption(role="user", text=self._user_text.strip(), final=True))
            self._user_text = ""

    def _finish_assistant(self, step: _Step) -> None:
        if self._assistant_text:
            step.events.append(
                WireCaption(role="assistant", text=self._assistant_text.strip(), final=True)
            )
            self._assistant_text = ""

    def _add_usage(self, step: _Step, metadata: Any) -> None:
        """Sum each turn's counts; every turn bills its whole prompt again."""
        if not isinstance(metadata, Mapping):
            return
        prompt = _count(metadata, "promptTokenCount") + _count(metadata, "toolUsePromptTokenCount")
        thoughts = _count(metadata, "thoughtsTokenCount")
        counts = {
            "input_tokens": prompt,
            "output_tokens": _count(metadata, "responseTokenCount") + thoughts,
            "reasoning_tokens": thoughts,
            "cache_read_tokens": _count(metadata, "cachedContentTokenCount"),
        }
        if not any(counts.values()):
            return
        for key, value in counts.items():
            if value:
                self._usage[key] = self._usage.get(key, 0) + value
        step.events.append(WireUsage(usage=dict(self._usage)))


# Closes that report an error rather than an ended connection: invalid
# argument (1007), policy or not found (1008), and an internal error (1011).
_FATAL_CLOSE_CODES = frozenset({1007, 1008, 1011})
# Errors in the session's own setup or key, which a new connection repeats.
_SETUP_CLOSE_CODES = frozenset({1007, 1008})
# Ends worth a resume: a dropped transport, going away, and the transient
# internal error the server sends now and then (live 2026-11-17).
_RESUMABLE_CLOSE_CODES = frozenset({None, 1001, 1006, 1011})


def _declaration(tool: JsonObject, *, non_blocking: bool) -> JsonObject:
    # The typed ``parameters`` field takes only an OpenAPI subset and closes the
    # setup with 1007 for keywords such as ``uniqueItems``; the JSON Schema
    # field keeps the canonical schema (live 2026-11-17, 3.8, 3.1 and 2.5).
    declaration: JsonObject = {"name": tool["name"], "description": tool["description"]}
    declaration["parametersJsonSchema"] = tool["parameters"]
    if non_blocking:
        declaration["behavior"] = "NON_BLOCKING"
    return declaration


def _arguments(arguments: Any) -> Any:
    """Gemini sends arguments as an object; text that is JSON is decoded too."""
    if arguments is None:
        return {}
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return arguments


def _count(metadata: Mapping[str, Any], name: str) -> int:
    value = metadata.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _seconds(value: Any) -> float | None:
    """Parse a protobuf JSON duration such as ``"50s"`` or ``"1.5s"``."""
    if isinstance(value, str) and (match := _DURATION.match(value.strip())):
        return float(match.group(1))
    return None
