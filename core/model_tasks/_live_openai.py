"""OpenAI GPT-Live wires: ChatGPT subscription (Codex route) and public API.

Both dialects create a WebRTC call over HTTP with client delegation and join
the call's control WebSocket before the SDP answer is returned, because the
control channel does not replay earlier events. The browser's data channel
duplicates control events; the server never relies on it.

* ``codex`` (Connection mode ``codex_responses``): ``POST
  /codex/realtime/calls`` returns the raw SDP answer and the call id in
  ``Location``; control is ``wss://api.openai.com/v1/live/<call_id>``. The
  delegation event carries the request text.
* ``public`` (API key): ``POST /live/sessions`` returns JSON; control is
  ``<base>/live/sessions/<id>/attach``. Delegations carry no text.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from core.model_tasks._live_wire import (
    MEDIA_WEBRTC,
    JsonObject,
    WireCaption,
    WireClosed,
    WireDelegation,
    WireEvent,
    WireProblem,
    WireSendError,
    WireStarted,
    WireUsage,
    websocket_url,
)
from core.providers.errors import ProviderAuthError
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
)
from core.utils.tls import shared_ssl_context

DIALECT_CODEX = "codex"
DIALECT_PUBLIC = "public"
CODEX_CONNECTION_MODE = "codex_responses"

CODEX_CALLS_ENDPOINT = "/codex/realtime/calls?intent=quicksilver&architecture=avas"
CODEX_CONTROL_URL = "wss://api.openai.com/v1/live/{call_id}"
CODEX_LIVE_HEADERS = {"OpenAI-Alpha": "quicksilver=v2", "originator": "vbot"}
PUBLIC_SESSIONS_ENDPOINT = "/live/sessions"

_CREATE_TIMEOUT_SECONDS = 30.0
_CONTROL_OPEN_TIMEOUT_SECONDS = 10.0
_CONTROL_MAX_FRAME_BYTES = 8 * 1024 * 1024
# Codex appends accept at most 500 bytes of text; the public API 500 tokens.
_CODEX_APPEND_MAX_BYTES = 500
_PUBLIC_APPEND_MAX_BYTES = 1500
_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# The sideband mirrors call audio (~128 KB/s). The event type leads each frame,
# so those frames are dropped before JSON decoding.
_AUDIO_FRAME_MARKERS = ('"session.input_audio.append"', '"session.output_audio.delta"')
_CAPTION_ROLES = frozenset({"user", "assistant"})

WebSocketConnector = Callable[..., Awaitable[Any]]


class ControlJoinError(Exception):
    """The call was created, but its control channel could not be joined."""

    def __init__(self, call_id: str, error_type: str) -> None:
        super().__init__(f"Live control channel join failed ({error_type})")
        self.call_id = call_id


def openai_live_dialect(connection_mode: str | None) -> str:
    """Return the GPT-Live dialect for an OpenAI Connection mode."""

    return DIALECT_CODEX if connection_mode == CODEX_CONNECTION_MODE else DIALECT_PUBLIC


async def open_openai_live_wire(
    runtime: Any,
    target_ref: Any,
    *,
    offer_sdp: str,
    instructions: str,
    voice: str | None,
    connect: WebSocketConnector | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> OpenAILiveWire:
    """Create the provider call and join its control channel.

    Provider errors from call creation propagate unchanged. A control join
    failure raises :class:`ControlJoinError`; the created call then ends when
    its media disconnects.
    """

    client = _OpenAILiveClient.from_runtime(runtime, target_ref)
    call_id, answer_sdp, control_url, headers = await client.create_call(
        offer_sdp=offer_sdp,
        instructions=instructions,
        voice=voice,
        http_client=http_client,
    )
    try:
        socket = await _join_control(connect or websocket_connect, control_url, headers)
    except Exception as exc:
        raise ControlJoinError(call_id, type(exc).__name__) from exc
    return OpenAILiveWire(
        call_id=call_id,
        answer_sdp=answer_sdp,
        socket=socket,
        dialect=client.dialect,
    )


class _OpenAILiveClient(ProviderTaskClient):
    """Creates GPT-Live calls on one resolved OpenAI Connection."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dialect = openai_live_dialect(getattr(self._connection, "mode", None))
        self._control_headers: dict[str, str] = {}
        self._call_headers = {
            "session-id": str(uuid4()),
            "thread-id": str(uuid4()),
            "x-session-id": str(uuid4()),
        }

    async def create_call(
        self,
        *,
        offer_sdp: str,
        instructions: str,
        voice: str | None,
        http_client: httpx.AsyncClient | None,
    ) -> tuple[str, str, str, dict[str, str]]:
        session: JsonObject = {
            "model": self._model_id,
            "instructions": instructions,
            "delegation": {"type": "client"},
        }
        if voice:
            session["audio"] = {"output": {"voice": voice}}
        if self.dialect == DIALECT_CODEX:
            call_id, answer_sdp = await self.post_and_parse(
                CODEX_CALLS_ENDPOINT,
                timeout=_CREATE_TIMEOUT_SECONDS,
                parse=_parse_codex_call,
                json={"sdp": offer_sdp, "session": session},
                headers=self._codex_headers,
                retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
                http_client=http_client,
            )
            control_url = CODEX_CONTROL_URL.format(call_id=call_id)
        else:
            # The page's data channel may neither send commands nor see events.
            session["client"] = {
                "data_channel": {"allowed_client_events": [], "allowed_server_events": []}
            }
            call_id, answer_sdp = await self.post_and_parse(
                PUBLIC_SESSIONS_ENDPOINT,
                timeout=_CREATE_TIMEOUT_SECONDS,
                parse=_parse_public_session,
                json={"session": session, "transport": {"type": "webrtc", "sdp": offer_sdp}},
                headers=self._public_headers,
                retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
                http_client=http_client,
            )
            control_url = websocket_url(self._base_url, f"/live/sessions/{call_id}/attach")
        return call_id, answer_sdp, control_url, dict(self._control_headers)

    async def _codex_headers(self) -> dict[str, str]:
        credential = await self._credential_value()
        account_id = extract_chatgpt_account_id(credential)
        if account_id is None:
            raise ProviderAuthError(
                "OpenAI Subscription OAuth token is missing a ChatGPT account id; please reconnect"
            )
        headers = self._headers_from_credential(credential)
        headers["chatgpt-account-id"] = account_id
        headers.update(CODEX_LIVE_HEADERS)
        headers.update(self._call_headers)
        self._control_headers = dict(headers)
        return headers

    async def _public_headers(self) -> dict[str, str]:
        headers = await self._headers()
        self._control_headers = dict(headers)
        return headers


def _parse_codex_call(response: httpx.Response) -> tuple[str, str]:
    answer_sdp = response.text
    if not answer_sdp.lstrip().startswith("v=0"):
        raise ValueError("GPT-Live call creation returned no SDP answer")
    location = response.headers.get("location") or ""
    segments = [segment for segment in location.split("?", 1)[0].split("/") if segment]
    call_id = segments[-1] if segments else ""
    if not _CALL_ID.fullmatch(call_id):
        raise ValueError("GPT-Live call creation returned no call id")
    return call_id, answer_sdp


def _parse_public_session(response: httpx.Response) -> tuple[str, str]:
    data = response.json()
    session = data.get("session") if isinstance(data, dict) else None
    transport = data.get("transport") if isinstance(data, dict) else None
    call_id = session.get("id") if isinstance(session, dict) else None
    answer_sdp = transport.get("sdp") if isinstance(transport, dict) else None
    if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
        raise ValueError("GPT-Live session creation returned no session id")
    if not isinstance(answer_sdp, str) or not answer_sdp.lstrip().startswith("v=0"):
        raise ValueError("GPT-Live session creation returned no SDP answer")
    return call_id, answer_sdp


async def _join_control(connect: WebSocketConnector, url: str, headers: dict[str, str]) -> Any:
    options: JsonObject = {
        "additional_headers": headers,
        "open_timeout": _CONTROL_OPEN_TIMEOUT_SECONDS,
        "max_size": _CONTROL_MAX_FRAME_BYTES,
    }
    if url.startswith("wss://"):
        options["ssl"] = shared_ssl_context()
    return await connect(url, **options)


class OpenAILiveWire:
    """A joined GPT-Live call in one of the two OpenAI dialects."""

    def __init__(self, *, call_id: str, answer_sdp: str, socket: Any, dialect: str) -> None:
        self._call_id = call_id
        self._answer_sdp = answer_sdp
        self._socket = socket
        self._dialect = dialect
        self._normalizer: _CodexEvents | _PublicEvents = (
            _CodexEvents() if dialect == DIALECT_CODEX else _PublicEvents()
        )

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def media(self) -> JsonObject:
        return {"type": MEDIA_WEBRTC, "sdp": self._answer_sdp}

    async def events(self) -> AsyncIterator[WireEvent]:
        try:
            async for frame in self._socket:
                if not isinstance(frame, str) or _is_audio_frame(frame):
                    continue
                try:
                    event = json.loads(frame)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                for normalized in self._normalizer.normalize(event):
                    yield normalized
                    if isinstance(normalized, WireClosed):
                        return
        except ConnectionClosed:
            pass
        yield WireClosed(reason=None, usage=None, confirmed=False)

    async def deliver_result(self, delegation_id: str, text: str) -> None:
        if self._dialect == DIALECT_CODEX:
            for chunk in chunk_text(text, _CODEX_APPEND_MAX_BYTES):
                await self._send(
                    {
                        "type": "delegation.context.append",
                        "delegation_item_id": delegation_id,
                        "channel": "speakable",
                        "content": [{"type": "input_text", "text": chunk}],
                    }
                )
            return
        for chunk in chunk_text(text, _PUBLIC_APPEND_MAX_BYTES):
            await self._send(
                {
                    "type": "session.commentary.append",
                    "delegation_id": delegation_id,
                    "content": chunk,
                }
            )

    async def send_audio(self, pcm: bytes) -> None:
        """WebRTC audio flows between the accessor and OpenAI; nothing to forward."""

    async def announce(self, text: str) -> None:
        if self._dialect == DIALECT_CODEX:
            for chunk in chunk_text(text, _CODEX_APPEND_MAX_BYTES):
                await self._send(
                    {
                        "type": "session.context.append",
                        "channel": "speakable",
                        "content": [{"type": "input_text", "text": chunk}],
                    }
                )
            return
        for chunk in chunk_text(text, _PUBLIC_APPEND_MAX_BYTES):
            await self._send(
                {"type": "session.commentary.append", "delegation_id": None, "content": chunk}
            )

    async def request_close(self) -> None:
        await self._send({"type": "session.close"})

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.close()

    async def _send(self, event: JsonObject) -> None:
        try:
            await self._socket.send(json.dumps(event, ensure_ascii=False))
        except ConnectionClosed as exc:
            raise WireSendError("Live control channel is closed") from exc


def _is_audio_frame(frame: str) -> bool:
    head = frame[:96]
    return any(marker in head for marker in _AUDIO_FRAME_MARKERS)


def chunk_text(text: str, max_bytes: int) -> list[str]:
    """Split *text* into pieces of at most *max_bytes* UTF-8 bytes.

    Pieces break at whitespace when possible and never split a character.
    """

    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break
        cut = _byte_prefix_length(remaining, max_bytes)
        space = remaining.rfind(" ", 0, cut)
        if space > cut // 2:
            cut = space
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [chunk for chunk in chunks if chunk]


def _byte_prefix_length(text: str, max_bytes: int) -> int:
    size = 0
    for index, character in enumerate(text):
        size += len(character.encode("utf-8"))
        if size > max_bytes:
            return max(index, 1)
    return len(text)


def _usage(event: JsonObject) -> JsonObject | None:
    usage = event.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def _expires_at(event: JsonObject) -> float | None:
    session = event.get("session")
    value = session.get("expires_at") if isinstance(session, dict) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _problem(event: JsonObject) -> WireProblem:
    error = event.get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("code") or error.get("type") or "unknown"
    message = error.get("message")
    return WireProblem(code=str(code), message=message if isinstance(message, str) else "")


class _CodexEvents:
    """Normalize Codex-route events; turns carry ids, roles, and full transcripts."""

    def __init__(self) -> None:
        self._turns: dict[str, tuple[str, str]] = {}

    def normalize(self, event: JsonObject) -> list[WireEvent]:
        kind = event.get("type")
        if kind == "session.started":
            return [WireStarted(expires_at=_expires_at(event))]
        if kind in {"turn.created", "turn.done"}:
            return self._turn(event.get("turn"), final=kind == "turn.done")
        if kind == "turn.delta":
            return self._turn_delta(event)
        if kind == "delegation.created":
            return self._delegation(event.get("item"))
        if kind == "session.usage.updated":
            usage = _usage(event)
            return [WireUsage(usage)] if usage is not None else []
        if kind == "session.closed":
            reason = event.get("reason")
            return [
                WireClosed(
                    reason=reason if isinstance(reason, str) else None,
                    usage=_usage(event),
                    confirmed=True,
                )
            ]
        if kind == "error":
            return [_problem(event)]
        return []

    def _turn(self, turn: Any, *, final: bool) -> list[WireEvent]:
        if not isinstance(turn, dict):
            return []
        turn_id, role, text = turn.get("id"), turn.get("role"), turn.get("transcript")
        if not isinstance(turn_id, str) or role not in _CAPTION_ROLES:
            return []
        text = text if isinstance(text, str) else ""
        if final:
            self._turns.pop(turn_id, None)
        else:
            self._turns[turn_id] = (role, text)
        return [WireCaption(role=role, text=text.strip(), final=final)]

    def _turn_delta(self, event: JsonObject) -> list[WireEvent]:
        turn_id, delta = event.get("turn_id"), event.get("delta")
        if not isinstance(turn_id, str) or not isinstance(delta, str) or turn_id not in self._turns:
            return []
        role, text = self._turns[turn_id]
        text += delta
        self._turns[turn_id] = (role, text)
        return [WireCaption(role=role, text=text.strip(), final=False)]

    @staticmethod
    def _delegation(item: Any) -> list[WireEvent]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return []
        parts = item.get("content")
        texts = [
            part["text"]
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        request = " ".join(text.strip() for text in texts if text.strip())
        return [WireDelegation(delegation_id=item["id"], request=request or None)]


class _PublicEvents:
    """Normalize public-API events; transcripts are role-tagged deltas without turns."""

    def __init__(self) -> None:
        self._role: str | None = None
        self._text = ""

    def normalize(self, event: JsonObject) -> list[WireEvent]:
        kind = event.get("type")
        if kind == "session.started":
            return [WireStarted(expires_at=_expires_at(event))]
        if kind == "session.input_transcript.delta":
            return self._delta("user", event.get("delta"))
        if kind == "session.output_transcript.delta":
            return self._delta("assistant", event.get("delta"))
        if kind == "session.delegation.created":
            delegation = event.get("delegation")
            if (
                isinstance(delegation, dict)
                and isinstance(delegation.get("id"), str)
                and delegation.get("target", "client") == "client"
            ):
                return [WireDelegation(delegation_id=delegation["id"], request=None)]
            return []
        if kind == "session.usage.updated":
            usage = _usage(event)
            return [WireUsage(usage)] if usage is not None else []
        if kind == "session.closed":
            reason = event.get("reason")
            return [
                *self._finish(),
                WireClosed(
                    reason=reason if isinstance(reason, str) else None,
                    usage=_usage(event),
                    confirmed=True,
                ),
            ]
        if kind == "error":
            return [_problem(event)]
        return []

    def _delta(self, role: str, delta: Any) -> list[WireEvent]:
        if not isinstance(delta, str) or not delta:
            return []
        events = self._finish() if role != self._role else []
        self._role = role
        self._text += delta
        events.append(WireCaption(role=role, text=self._text.strip(), final=False))
        return events

    def _finish(self) -> list[WireEvent]:
        role, text = self._role, self._text.strip()
        self._role, self._text = None, ""
        if role is None or not text:
            return []
        return [WireCaption(role=role, text=text, final=True)]
