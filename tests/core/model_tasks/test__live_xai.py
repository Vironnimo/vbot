"""Offline verification of the xAI Grok Voice wire against the verified wire facts."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from core.model_tasks._live_wire import (
    WireAudio,
    WireCaption,
    WireClosed,
    WirePlaybackClear,
    WireProblem,
    WireSendError,
    WireStarted,
    WireToolCall,
    WireUsage,
    relay_media,
)
from core.model_tasks._live_xai import XaiLiveWire, open_xai_live_wire
from core.model_tasks._live_xai_session import XaiSession
from core.model_tasks.model_tasks import parse_task_model_target_id
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import StaticTokenGetter

TARGET = "xai/grok-voice-think-fast-2.0::subscription"
INSTRUCTIONS = "Talk with the user."


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


REQUEST_TOOL = {
    "name": "vbot_request",
    "description": "Ask vBot.",
    "parameters": _object({"request": {"type": "string"}}),
}
TOOLS = [
    {"name": "overview", "description": "What runs.", "parameters": _object({})},
    {
        "name": "open",
        "description": "Show something.",
        "parameters": _object({"target": {"type": "string"}}),
    },
]
ONE_SECOND = 48_000  # bytes of PCM16 mono 24 kHz
_SPEECH_STARTED = {"type": "input_audio_buffer.speech_started", "item_id": "u1"}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _session(*, clock: Clock | None = None) -> XaiSession:
    session = XaiSession(
        tools=[*TOOLS, REQUEST_TOOL], clock=clock or Clock(), wall_clock=lambda: 1000.0
    )
    session.receive({"type": "session.updated", "session": {}})
    return session


def _audio(item: str, response: str, size: int, kind: str = "response.output_audio.delta") -> dict:
    delta = base64.b64encode(bytes(size)).decode("ascii")
    return {"type": kind, "item_id": item, "response_id": response, "delta": delta}


def _created(response: str) -> dict:
    return {"type": "response.created", "response": {"id": response, "status": "in_progress"}}


def _done(response: str, status: str = "completed", **extra: Any) -> dict:
    return {"type": "response.done", "response": {"id": response, "status": status, **extra}}


def _call(response: str, call_id: str, name: str = "vbot_request", arguments: Any = None) -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "response_id": response,
        "item_id": f"item_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps({"request": "Open the terminals"})
        if arguments is None
        else arguments,
    }


def _types(commands: list[dict]) -> list[str]:
    return [command["type"] for command in commands]


def _creates(commands: list[dict]) -> list[dict]:
    return [command for command in commands if command["type"] == "response.create"]


def _outputs(commands: list[dict]) -> dict[str, Any]:
    """Function call outputs by call id: decoded JSON, or the plain text."""
    return {
        command["item"]["call_id"]: _decoded(command["item"]["output"])
        for command in commands
        if command["type"] == "conversation.item.create"
        and command["item"]["type"] == "function_call_output"
    }


def _decoded(output: str) -> Any:
    try:
        return json.loads(output)
    except ValueError:
        return output


# -- session setup -----------------------------------------------------------


def test_session_update_configures_voice_vad_audio_transcription_and_flat_tools():
    update = XaiSession(tools=[*TOOLS, REQUEST_TOOL]).configure(INSTRUCTIONS, "eve")

    assert update == {
        "type": "session.update",
        "session": {
            "instructions": INSTRUCTIONS,
            "voice": "eve",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.85,
                "prefix_padding_ms": 333,
                "silence_duration_ms": 500,
            },
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "grok-transcribe"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}},
            },
            "tools": [{"type": "function", **tool} for tool in [*TOOLS, REQUEST_TOOL]],
        },
    }
    assert all("strict" not in tool for tool in update["session"]["tools"])
    assert "voice" not in XaiSession(tools=TOOLS).configure(INSTRUCTIONS, None)["session"]


def test_session_starts_on_session_updated_and_drops_audio_before():
    session = XaiSession(tools=[REQUEST_TOOL], clock=Clock(), wall_clock=lambda: 1000.0)

    assert session.audio(b"\x01\x00") == []
    step = session.receive({"type": "session.updated", "session": {}})
    again = session.receive({"type": "session.updated", "session": {}})

    assert step.events == [WireStarted(expires_at=1000.0 + 7200)]
    assert again.events == []
    assert session.audio(b"\x01\x00") == [
        {"type": "input_audio_buffer.append", "audio": base64.b64encode(b"\x01\x00").decode()}
    ]
    assert session.audio(b"") == []


# -- captions ----------------------------------------------------------------


def test_assistant_captions_stream_and_finish_once_across_duplicate_event_names():
    session = _session()
    session.receive(_created("r1"))
    events: list[Any] = []
    for event in (
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "r1",
            "item_id": "a1",
            "delta": "Opening",
        },
        {"type": "response.text.delta", "response_id": "r1", "item_id": "a1", "delta": " ignored"},
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "r1",
            "item_id": "a1",
            "delta": " now.",
        },
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "r1",
            "item_id": "a1",
            "transcript": "Opening now.",
        },
        {"type": "response.text.done", "response_id": "r1", "item_id": "a1", "text": "late"},
    ):
        events.extend(session.receive(event).events)

    assert events == [
        WireCaption("assistant", "Opening", final=False),
        WireCaption("assistant", "Opening now.", final=False),
        WireCaption("assistant", "Opening now.", final=True),
    ]


def test_user_snapshots_replace_each_other_and_finish_once_on_the_completed_status():
    session = _session()
    events: list[Any] = []
    for event in (
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "item_id": "u1",
            "transcript": "Please open the terminal.",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u1",
            "transcript": "Please open the terminal.",
            "status": "in_progress",
        },
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "item_id": "u1",
            "transcript": "Please open the terminals view.",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u1",
            "transcript": "Please open the terminals view.",
            "status": "completed",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u1",
            "transcript": "Please open the terminals view.",
            "status": "completed",
        },
    ):
        events.extend(session.receive(event).events)

    assert events == [
        WireCaption("user", "Please open the terminal.", final=False),
        WireCaption("user", "Please open the terminals view.", final=False),
        WireCaption("user", "Please open the terminals view.", final=True),
    ]


def test_open_user_turns_finish_at_a_new_user_item_the_response_end_or_the_call_end():
    session = _session()
    snapshot = "conversation.item.input_audio_transcription.updated"
    session.receive({"type": snapshot, "item_id": "u1", "transcript": "First"})
    added = session.receive(
        {"type": "conversation.item.added", "item": {"id": "u2", "type": "message", "role": "user"}}
    )
    session.receive({"type": snapshot, "item_id": "u2", "transcript": "Second"})
    session.receive(_created("r1"))
    done = session.receive(_done("r1"))
    session.receive({"type": snapshot, "item_id": "u3", "transcript": "Third"})

    assert added.events == [WireCaption("user", "First", final=True)]
    assert WireCaption("user", "Second", final=True) in done.events
    assert session.finish() == [
        WireCaption("user", "Third", final=True),
        WireClosed(reason=None, usage=None, confirmed=False),
    ]


def test_an_announcement_item_does_not_finish_the_open_user_turn():
    session = _session()
    session.receive(
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "item_id": "u1",
            "transcript": "Open the",
        }
    )
    session.announce("vBot update: {}")

    added = session.receive(
        {
            "type": "conversation.item.added",
            "item": {
                "id": "n1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "vBot update: {}"}],
            },
        }
    )

    assert added.events == []
    assert session.finish() == [
        WireCaption("user", "Open the", final=True),
        WireClosed(reason=None, usage=None, confirmed=False),
    ]


# -- Tool calls -------------------------------------------------


def test_completed_calls_run_once_and_results_return_as_tool_output():
    session = _session()
    session.receive(_created("r1"))
    session.receive(_call("r1", "c1"))
    session.receive(
        {
            "type": "response.output_item.done",
            "response_id": "r1",
            "item": {
                "type": "function_call",
                "call_id": "c1",
                "name": "vbot_request",
                "arguments": json.dumps({"request": "Open the terminals"}),
            },
        }
    )
    done = session.receive(
        _done(
            "r1",
            output=[
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "vbot_request",
                    "arguments": json.dumps({"request": "Open the terminals"}),
                }
            ],
        )
    )

    assert done.events == [
        WireToolCall(call_id="c1", name="vbot_request", arguments={"request": "Open the terminals"})
    ]
    assert _creates(done.commands) == []
    commands = session.deliver("c1", "The terminals view is open.")
    assert _outputs(commands) == {"c1": "The terminals view is open."}
    assert _types(commands) == ["conversation.item.create", "response.create"]
    assert session.deliver("c1", "again") == []


@pytest.mark.parametrize(
    ("events", "status"),
    [
        ([_call("r1", "c1")], "cancelled"),
        # The user talked over the response; a late completion keeps it interrupted.
        ([_audio("a1", "r1", 100), _call("r1", "c1"), _SPEECH_STARTED], "completed"),
        ([_audio("a1", "r1", 100), _SPEECH_STARTED, _call("r1", "c1")], "completed"),
    ],
    ids=["cancelled", "call_before_barge_in", "call_after_barge_in"],
)
def test_calls_of_an_interrupted_response_never_run_and_get_an_output(events, status):
    session = _session()
    session.receive(_created("r1"))
    for event in events:
        session.receive(event)
    done = session.receive(_done("r1", status=status))

    assert done.events == []
    assert _outputs(done.commands)["c1"] == (
        "Error (interrupted): The call was interrupted before it started; nothing was done. "
        "Call it again if the user still wants it."
    )
    assert _creates(done.commands) == []
    assert session.deliver("c1", "late") == []


def test_every_call_goes_to_the_call_as_written_and_returns_its_text():
    session = _session()
    session.receive(_created("r1"))
    session.receive(_call("r1", "c1", name="overview", arguments=json.dumps({})))
    session.receive(_call("r1", "c2", name="status", arguments="{broken"))
    session.receive(_call("r1", "c3", name="functions.vbot_request"))
    done = session.receive(_done("r1"))

    # The call prepares names and arguments; undecodable text stays as it came.
    assert [(event.call_id, event.name, event.arguments) for event in done.events] == [
        ("c1", "overview", {}),
        ("c2", "status", "{broken"),
        ("c3", "functions.vbot_request", {"request": "Open the terminals"}),
    ]
    session.deliver("c2", "Error (invalid_arguments): The arguments are not one JSON object.")
    session.deliver("c3", "Error (unknown_tool): There is no Tool called status.")
    commands = session.deliver("c1", "Agents: Main, Coder.")
    assert _outputs(commands) == {"c1": "Agents: Main, Coder."}


# -- response gate -------------------------------------------------------------


def test_results_wait_for_the_active_response_and_coalesce_into_one_create():
    session = _session()
    session.receive(_created("r1"))
    session.receive(_call("r1", "c1"))
    session.receive(_done("r1"))
    session.receive(_created("r2"))

    first = session.deliver("c1", "Done.")
    announced = session.announce('vBot update: {"run": "completed"}')
    finished = session.receive(_done("r2"))

    assert _creates(first) == [] and _creates(announced) == []
    assert announced[0]["item"] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": 'vBot update: {"run": "completed"}'}],
    }
    assert _creates(finished.commands) == [{"type": "response.create", "event_id": "vbot_rc_1"}]


def test_a_response_created_after_an_addition_covers_it():
    session = _session()
    session.receive({"type": "input_audio_buffer.speech_started", "item_id": "u1"})
    session.announce("vBot update: {}")
    session.receive({"type": "input_audio_buffer.speech_stopped", "item_id": "u1"})
    session.receive(_created("r1"))
    done = session.receive(_done("r1"))

    assert _creates(done.commands) == []
    assert session.tick() == []


def test_user_speech_blocks_speaking_until_it_stops():
    session = _session()
    session.receive({"type": "input_audio_buffer.speech_started", "item_id": "u1"})

    assert _creates(session.announce("vBot update: {}")) == []
    stopped = session.receive({"type": "input_audio_buffer.speech_stopped", "item_id": "u1"})
    assert _creates(stopped.commands) == [{"type": "response.create", "event_id": "vbot_rc_1"}]


def test_every_call_of_the_last_completed_response_needs_its_output_first():
    session = _session()
    session.receive(_created("r1"))
    session.receive(_call("r1", "c1"))
    session.receive(_call("r1", "c2"))
    session.receive(_done("r1"))

    assert _creates(session.deliver("c1", "one")) == []
    assert _creates(session.announce("vBot update: {}")) == []
    assert _creates(session.deliver("c2", "two")) == [
        {"type": "response.create", "event_id": "vbot_rc_1"}
    ]


def test_speaking_waits_for_the_estimated_playback_to_drain():
    clock = Clock()
    session = _session(clock=clock)
    session.receive(_created("r1"))
    session.receive(_audio("a1", "r1", 2 * ONE_SECOND))
    session.receive(_call("r1", "c1"))
    session.receive(_done("r1"))

    assert _creates(session.deliver("c1", "Done.")) == []
    assert session.next_deadline() == pytest.approx(clock.now + 2.0)
    clock.now += 1.9
    assert session.tick() == []
    clock.now += 0.1
    assert session.tick() == [{"type": "response.create", "event_id": "vbot_rc_1"}]


def test_an_unanswered_create_is_retried_once_then_dropped():
    clock = Clock()
    session = _session(clock=clock)

    assert _creates(session.announce("vBot update: {}")) != []
    assert _creates(session.announce("vBot update: {}")) == []
    clock.now += 5.0
    assert session.tick() == [{"type": "response.create", "event_id": "vbot_rc_2"}]
    clock.now += 5.0
    assert session.tick() == []
    assert session.next_deadline() is None


def test_active_response_errors_wait_for_that_response_and_benign_errors_stay_silent():
    clock = Clock()
    session = _session(clock=clock)
    session.announce("vBot update: {}")

    busy = session.receive(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Conversation already has an active response",
            },
        }
    )
    benign = [
        session.receive(
            {
                "type": "error",
                "error": {
                    "code": "response_cancel_not_active",
                    "message": "no active response found",
                },
            }
        ),
        session.receive(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Audio truncation failed"},
            }
        ),
    ]

    assert busy.events == [] and _creates(busy.commands) == []
    assert all(step.events == [] for step in benign)
    assert session.tick() == []
    # The gate opens again with the events of the response vBot had not seen.
    session.receive(_created("resp_unseen"))
    session.receive(_done("resp_unseen"))
    assert _creates(session.announce("vBot update: {}")) == [
        {"type": "response.create", "event_id": "vbot_rc_2"}
    ]


def test_a_refused_create_is_reported_and_not_repeated():
    clock = Clock()
    session = _session(clock=clock)
    session.announce("vBot update: {}")

    refused = session.receive(
        {
            "type": "error",
            "error": {
                "type": "internal_error",
                "code": "server_error",
                "message": "failed",
                "event_id": "vbot_rc_1",
            },
        }
    )

    assert refused.events == [WireProblem(code="server_error", message="failed")]
    clock.now += 10
    assert session.tick() == []


def test_a_stalled_response_stops_blocking_the_gate():
    clock = Clock()
    session = _session(clock=clock)
    session.receive(_created("r1"))
    session.receive(_audio("a1", "r1", 100))

    assert _creates(session.announce("vBot update: {}")) == []
    clock.now += 30.0
    assert session.tick() == [{"type": "response.create", "event_id": "vbot_rc_1"}]


# -- audio and barge-in --------------------------------------------------------


def test_audio_is_forwarded_once_per_item_across_duplicate_event_names():
    session = _session()
    session.receive(_created("r1"))

    first = session.receive(_audio("a1", "r1", 4))
    duplicate = session.receive(_audio("a1", "r1", 4, kind="response.audio.delta"))
    invalid = session.receive(
        {
            "type": "response.output_audio.delta",
            "item_id": "a1",
            "response_id": "r1",
            "delta": "***",
        }
    )

    assert first.events == [WireAudio(item_id="a1", pcm=bytes(4))]
    assert duplicate.events == [] and invalid.events == []


def test_barge_in_clears_playback_truncates_at_the_played_audio_and_fences_the_response():
    clock = Clock()
    session = _session(clock=clock)
    session.receive(_created("r1"))
    session.receive(
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "r1",
            "item_id": "a1",
            "delta": "The terminals view is open. Two Codex terminals",
        }
    )
    session.receive(_audio("a1", "r1", 3 * ONE_SECOND))
    clock.now += 1.25

    step = session.receive({"type": "input_audio_buffer.speech_started", "item_id": "u2"})
    late = [
        session.receive(_audio("a1", "r1", 100)),
        session.receive(
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "r1",
                "item_id": "a1",
                "transcript": "whole text",
            }
        ),
    ]

    assert step.events == [
        WirePlaybackClear(),
        WireCaption("assistant", "The terminals view is open. Two Codex terminals", final=True),
    ]
    assert step.commands == [
        {
            "type": "conversation.item.truncate",
            "item_id": "a1",
            "content_index": 0,
            "audio_end_ms": 1250,
        }
    ]
    assert all(result.events == [] for result in late)


def test_barge_in_skips_truncation_when_nothing_or_everything_was_heard():
    clock = Clock()
    session = _session(clock=clock)
    session.receive(_created("r1"))
    session.receive(_audio("a1", "r1", ONE_SECOND))
    immediately = session.receive({"type": "input_audio_buffer.speech_started", "item_id": "u1"})
    session.receive({"type": "input_audio_buffer.speech_stopped", "item_id": "u1"})
    session.receive(_created("r2"))
    session.receive(_audio("a2", "r2", ONE_SECOND))
    session.receive({"type": "response.output_audio.done", "response_id": "r2", "item_id": "a2"})
    session.receive(_done("r2"))
    clock.now += 1.5
    after_playback = session.receive({"type": "input_audio_buffer.speech_started", "item_id": "u2"})

    assert immediately.commands == []
    assert after_playback.commands == []
    assert after_playback.events == [WirePlaybackClear()]


def test_barge_in_abandons_a_silent_response_so_the_gate_never_waits_for_it():
    session = _session()
    session.receive(_created("r1"))
    session.receive({"type": "input_audio_buffer.speech_started", "item_id": "u1"})
    session.receive({"type": "input_audio_buffer.speech_stopped", "item_id": "u1"})

    assert _creates(session.announce("vBot update: {}")) == [
        {"type": "response.create", "event_id": "vbot_rc_1"}
    ]


# -- close and usage -----------------------------------------------------------


def test_max_duration_closes_the_socket_and_confirms_the_close():
    session = _session()
    step = session.receive({"type": "error", "error": {"type": "max_duration", "message": "x"}})

    assert step.close and step.events == []
    assert session.finish()[-1] == WireClosed(reason="max_duration", usage=None, confirmed=True)


def test_a_rejected_setup_is_a_problem_that_ends_the_call():
    session = XaiSession(tools=[REQUEST_TOOL])
    step = session.receive(
        {"type": "error", "error": {"type": "invalid_request_error", "message": "bad voice"}}
    )

    assert step.close
    assert step.events == [WireProblem(code="invalid_request_error", message="bad voice")]


def test_usage_accumulates_numeric_counts_and_ignores_empty_usage():
    session = _session()
    session.receive(_created("r1"))
    empty = session.receive(_done("r1", usage={}))
    session.receive(_created("r2"))
    counted = session.receive(_done("r2", usage={"input_tokens": 3, "output_tokens": 5}))
    duplicate = session.receive(_done("r2", usage={"input_tokens": 3, "output_tokens": 5}))
    assert duplicate.events == []
    assert (
        session.receive(
            {
                "type": "response.done",
                "response_id": "r2",
                "response": {
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                },
            }
        ).events
        == []
    )
    assert session.finish()[-1].usage == {"input_tokens": 3, "output_tokens": 5}
    session.receive(_created("r3"))
    again = session.receive(_done("r3", usage={"input_tokens": 1, "details": {"x": 1}}))

    assert empty.events == []
    assert counted.events == [WireUsage({"input_tokens": 3, "output_tokens": 5})]
    assert again.events == [WireUsage({"input_tokens": 4, "output_tokens": 5})]
    # Remember counted responses for the whole call, beyond the recent-event cache.
    for index in range(300):
        session.receive(_done(f"empty-{index}", usage={}))
    assert session.receive(_done("r2", usage={"input_tokens": 3, "output_tokens": 5})).events == []
    assert session.finish()[-1].usage == {"input_tokens": 4, "output_tokens": 5}


# -- socket wire ---------------------------------------------------------------


class FakeSocket:
    """A realtime WebSocket fed from a queue of frames; ``None`` ends it."""

    def __init__(self, frames: list[Any] | None = None) -> None:
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        for frame in frames or []:
            self.push(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def push(self, frame: Any) -> None:
        self.frames.put_nowait(frame)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> Any:
        frame = await self.frames.get()
        if frame is None or self.closed:
            raise ConnectionClosedError(None, None)
        return frame if isinstance(frame, str | bytes) else json.dumps(frame)

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedError(None, None)
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True
        self.frames.put_nowait(None)


class FakeConnect:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes) or [FakeSocket()]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **options: Any) -> FakeSocket:
        self.calls.append((url, options))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, FakeSocket)
        return outcome


class RefreshingTokenGetter:
    def __init__(self) -> None:
        self.token = "old-token"
        self.rejections: list[tuple[str, int]] = []

    async def __call__(self) -> str:
        return self.token

    async def refresh_after_rejection(
        self, rejected_access_token: str, *, status_code: int, response_body: str
    ) -> str | None:
        self.rejections.append((rejected_access_token, status_code))
        self.token = "new-token"
        return self.token


def _runtime(token_getter: Any = None) -> SimpleNamespace:
    provider = ProviderConfig(
        id="xai",
        name="xAI",
        adapter="xai",
        base_url="https://api.x.ai/v1",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="SuperGrok",
                auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key=""),
            )
        ],
    )
    return SimpleNamespace(
        providers=SimpleNamespace(get=lambda provider_id: provider),
        get_connection_token_getter=lambda connection: (
            token_getter or StaticTokenGetter("xai-token")
        ),
    )


def _rejected(status: int, body: bytes = b'{"error":"denied"}') -> InvalidStatus:
    return InvalidStatus(Response(status, "Rejected", Headers(), body))


async def _open(connect: FakeConnect, token_getter: Any = None, **kwargs: Any) -> XaiLiveWire:
    return await open_xai_live_wire(
        _runtime(token_getter),
        parse_task_model_target_id(TARGET),
        instructions=kwargs.pop("instructions", INSTRUCTIONS),
        voice=kwargs.pop("voice", "eve"),
        tools=kwargs.pop("tools", [REQUEST_TOOL]),
        connect=connect,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_open_joins_the_pinned_model_with_connection_auth_and_configures_the_session():
    socket = FakeSocket()
    connect = FakeConnect(socket)

    wire = await _open(connect, tools=TOOLS)

    url, options = connect.calls[0]
    assert url == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-2.0"
    assert options["additional_headers"]["Authorization"] == "Bearer xai-token"
    assert options["max_size"] == 16 * 1024 * 1024
    assert options["ssl"] is not None
    assert socket.sent[0]["type"] == "session.update"
    assert socket.sent[0]["session"]["instructions"] == INSTRUCTIONS
    assert [tool["name"] for tool in socket.sent[0]["session"]["tools"]] == ["overview", "open"]
    assert re.fullmatch(r"live_[a-z0-9]{16}", wire.call_id)
    assert wire.media == relay_media()
    assert wire.announces_as_user_input is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (_rejected(401), ProviderAuthError),
        (_rejected(403), ProviderAuthError),
        (_rejected(429), ProviderRateLimitError),
        (_rejected(500), ProviderError),
        (OSError("refused"), NetworkError),
        (TimeoutError(), NetworkError),
    ],
    ids=["401", "403", "429", "500", "refused", "timeout"],
)
async def test_handshake_failures_map_to_provider_errors(failure, error):
    with pytest.raises(error):
        await _open(FakeConnect(failure))


@pytest.mark.asyncio
async def test_a_rejected_oauth_token_is_refreshed_once_and_the_join_retried():
    token_getter = RefreshingTokenGetter()
    socket = FakeSocket()
    connect = FakeConnect(_rejected(401), socket)

    await _open(connect, token_getter)

    assert token_getter.rejections == [("old-token", 401)]
    assert [options["additional_headers"]["Authorization"] for _url, options in connect.calls] == [
        "Bearer old-token",
        "Bearer new-token",
    ]


@pytest.mark.asyncio
async def test_socket_events_are_normalized_and_the_wire_answers_on_the_socket():
    socket = FakeSocket()
    wire = await _open(FakeConnect(socket))
    events: list[Any] = []

    async def read() -> None:
        async for event in wire.events():
            events.append(event)

    reader = asyncio.create_task(read())
    socket.push({"type": "ping"})
    socket.push(b"\x00\x01")
    socket.push("not json")
    socket.push({"type": "session.updated", "session": {}})
    socket.push(_created("r1"))
    socket.push(_call("r1", "c1"))
    socket.push(_done("r1"))
    async with asyncio.timeout(2):
        while not any(isinstance(event, WireToolCall) for event in events):
            await asyncio.sleep(0.005)
    await wire.send_audio(b"\x01\x00")
    await wire.deliver_result("c1", "Done.")
    await wire.request_close()
    async with asyncio.timeout(2):
        await reader

    assert isinstance(events[0], WireStarted)
    assert events[1] == WireToolCall(
        call_id="c1", name="vbot_request", arguments={"request": "Open the terminals"}
    )
    assert events[-1] == WireClosed(reason=None, usage=None, confirmed=False)
    assert _types(socket.sent[1:]) == [
        "input_audio_buffer.append",
        "conversation.item.create",
        "response.create",
    ]
    with pytest.raises(WireSendError):
        await wire.announce("vBot update: {}")


@pytest.mark.asyncio
async def test_the_wire_closes_on_max_duration_and_speaks_after_playback_drains():
    socket = FakeSocket()
    wire = await _open(FakeConnect(socket))
    events: list[Any] = []

    async def read() -> None:
        async for event in wire.events():
            events.append(event)

    reader = asyncio.create_task(read())
    socket.push({"type": "session.updated", "session": {}})
    socket.push(_created("r1"))
    socket.push(_audio("a1", "r1", ONE_SECOND // 10))
    socket.push(_done("r1"))
    async with asyncio.timeout(2):
        while not any(isinstance(event, WireAudio) for event in events):
            await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)
    await wire.announce("vBot update: {}")
    assert _creates(socket.sent) == []
    async with asyncio.timeout(2):
        while not _creates(socket.sent):
            await asyncio.sleep(0.01)
    socket.push({"type": "error", "error": {"type": "max_duration", "message": "120 minutes"}})
    async with asyncio.timeout(2):
        await reader

    assert socket.closed
    assert events[-1] == WireClosed(reason="max_duration", usage=None, confirmed=True)
