"""Live voice: provider-neutral realtime voice calls owned by the server.

The ``live_voice`` Task Model binding selects the voice Model and Connection.
Every call is recorded as one Run of the built-in Live voice Agent in a fresh
Session: what the user and the voice model said, the updates vBot gave it, and
every Tool call the voice model made, with the Agent's Tools. The binding's
``backend`` option says who answers what the voice model hands on:

* ``vbot``: the built-in Live backend Agent, one Chat Run per request in a
  second Session of the call (``_live_backend.py``). The voice model reaches it
  through ``vbot_request`` or its Provider's native delegation.
* ``openai``: OpenAI's hosted backend model, which calls the voice Agent's
  Tools (GPT-Live only).
* ``none``: the voice model uses only the voice Agent's Tools (xAI only).

The :class:`LiveCallHost` the server supplies runs the Live Tools, says what
vBot shows, and attaches the accessor's media and display. The bound Provider
decides the media kind: ``webrtc`` (OpenAI; the accessor connects audio to the
provider) or ``relay`` (xAI; audio passes through the server as PCM16 mono
24 kHz).

Accessor updates published through :meth:`LiveCallHost.publish`:

* ``{"type": "state", "phase": "connecting" | "live" | "closing" | "closed" | "failed"}``
* ``{"type": "sessions", "voice": {"agent_id", "session_id"}, "backend": {...} | None}``
  (the call's Sessions; again when the backend Session is created)
* ``{"type": "expiry", "seconds": float}`` (after ``live`` when the provider
  limits the session: seconds until it ends the call)
* ``{"type": "caption", "role": "user" | "assistant", "text": str, "final": bool}``
* ``{"type": "activity", "busy": bool, "label": str | None}``
* ``{"type": "playback_clear", "generation": <int>}`` (relay media: discard
  earlier audio and start the new generation at sample zero)
* ``{"type": "closed", "reason": str | None, "usage": dict | None}``; reasons
  include ``closed`` (an ordinary close), ``expired`` (the provider's session
  limit), ``connection_lost``, ``start_timeout``, and ``aborted``

Relay audio for the accessor goes through :meth:`LiveCallHost.publish_audio`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from core.agents import LIVE_BACKEND_AGENT_ID, LIVE_VOICE_AGENT_ID
from core.model_tasks._live_backend import LiveBackend
from core.model_tasks._live_brief import (
    backend_instructions,
    live_tool_guidance,
    voice_instructions,
)
from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import (
    ControlJoinError,
    OpenAIBackend,
    open_openai_live_wire,
)
from core.model_tasks._live_options import (
    LIVE_BACKEND_NONE,
    LIVE_BACKEND_OPENAI,
    LIVE_BACKEND_VBOT,
    live_backend_choices,
)
from core.model_tasks._live_results import (
    LIVE_UPDATE_PREFIX,
    live_failure,
    live_result_text,
    live_success,
)
from core.model_tasks._live_wire import MEDIA_RELAY, MEDIA_WEBRTC, LiveWire
from core.model_tasks._live_xai import open_xai_live_wire
from core.model_tasks.constants import TASK_LIVE_VOICE
from core.model_tasks.model_tasks import (
    TaskModelError,
    TaskModelTargetRef,
    parse_task_model_target_id,
    public_provider_target_id,
)
from core.model_tasks.task_execution import TaskBindingResolver, TaskUsage
from core.providers.errors import (
    ProviderAuthError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.tools.live import TOOL_VBOT_REQUEST, LiveToolHosts
from core.usage import UsageRecorder
from core.utils.errors import ConfigError, TaskError, VBotError
from core.utils.ids import new_id
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

__all__ = [
    "LIVE_MEDIA_KINDS",
    "LIVE_START_REJECTION_CODES",
    "LIVE_UPDATE_PREFIX",
    "LiveCall",
    "LiveCallHost",
    "LiveRunNotice",
    "LiveRuntime",
    "LiveStartRejected",
    "LiveVoiceError",
    "LiveVoiceService",
    "backend_instructions",
    "live_failure",
    "live_result_text",
    "live_success",
]

_LOGGER = get_logger(__name__)

_MAX_OFFER_BYTES = 65536

LIVE_MEDIA_KINDS = frozenset({MEDIA_WEBRTC, MEDIA_RELAY})

LIVE_START_REJECTION_CODES = frozenset(
    {
        "not_configured",
        "provider_unavailable",
        "backend_unavailable",
        "invalid_offer",
        "access_denied",
        "rate_limited",
        "outcome_unknown",
        "provider_error",
        "control_failed",
        "media_mismatch",
    }
)


class LiveVoiceError(TaskError):
    """Expected Live voice configuration or execution error."""


class LiveStartRejected(LiveVoiceError):  # noqa: N818 - names the outcome
    """A Live call could not start; ``code`` is a stable accessor-facing reason."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in LIVE_START_REJECTION_CODES:
            raise ValueError(f"unknown Live start rejection code: {code}")
        super().__init__(message or code)
        self.code = code


class LiveCallHost(Protocol):
    """Server-side owner of one Live call's Live Tools and accessor.

    ``wake_phrases`` address other vBot Agents during the call (validated,
    printable). ``run_live_tool`` runs one Live Tool by name with the
    arguments as the Model wrote them and never raises for operation failures:
    they come back as a failure envelope naming the next valid call; calls can
    arrive concurrently. ``known_refs`` lists the refs earlier Tool results
    named (one ``- s1: Session at Coder`` line each, empty when none).
    ``current_state`` returns what the app and vBot show right now as text
    (the full overview); empty when unknown. ``publish`` delivers an accessor
    update and ``publish_audio`` relayed assistant audio (PCM16 mono 24 kHz),
    both without blocking.
    """

    @property
    def wake_phrases(self) -> tuple[str, ...]: ...

    async def run_live_tool(self, name: str, arguments: Any) -> JsonObject: ...

    def known_refs(self) -> str: ...

    async def current_state(self) -> str: ...

    def publish(self, update: JsonObject) -> None: ...

    def publish_audio(self, pcm: bytes, *, generation: int = 1, start_samples: int = 0) -> None: ...


@dataclass(frozen=True)
class LiveRunNotice:
    """A finished vBot Run the call announces at most once.

    ``kind`` names the outcome (for example ``completed``, ``failed``, or
    ``interrupted``). ``agent_id`` is the exact Agent address (``agent`` or
    ``agent@project``). ``session_ref`` is the call's short ref for the
    Session (such as ``s3``), which the spoken notice names instead of the id.
    """

    kind: str
    run_id: str
    agent_id: str
    session_id: str
    excerpt: str
    truncated: bool
    session_ref: str = ""


class LiveCall(Protocol):
    """One running Live call.

    ``media`` tells the accessor how to connect audio:
    ``{"type": "webrtc", "sdp": <answer>}`` or ``{"type": "relay", "audio":
    {"encoding": "pcm16", "sample_rate": 24000, "channels": 1}}``.
    """

    @property
    def id(self) -> str:
        """The call id of the transport contract; the Provider may own it."""
        ...

    @property
    def log_id(self) -> str:
        """The vBot-owned id naming this call in logs, never a Provider id."""
        ...

    @property
    def media(self) -> JsonObject: ...

    async def close(self) -> None:
        """Ask the provider to finish the call and wait briefly for final usage."""
        ...

    async def abort(self) -> None:
        """Tear the call down immediately without waiting for the provider."""
        ...

    def announce_run(self, notice: LiveRunNotice) -> None:
        """Queue a spoken notice about a finished Run; repeated run ids are ignored."""
        ...

    def push_audio(self, pcm: bytes) -> None:
        """Queue microphone PCM of a relay call without blocking.

        Ignored for WebRTC calls, before the call is live, and while it closes.
        """
        ...

    def report_playback(
        self, generation: int, played_samples: int, *, enabled: bool, cleared: bool = False
    ) -> None:
        """Keep the accessor's actual rendered PCM prefix (relay only)."""
        ...

    def reset_playback(self) -> None:
        """Drop pending relay output after owner loss or overflow."""
        ...

    def sync_playback(self) -> None:
        """Publish the relay generation to a newly attached owner."""
        ...

    async def wait_closed(self) -> None:
        """Return once the call is closed or failed and its tasks have stopped."""
        ...


class LiveRuntime(Protocol):
    """Runtime seams a Live call needs: credentials, adapters, Models, and Chat."""

    @property
    def providers(self) -> Any: ...

    @property
    def models(self) -> Any: ...

    @property
    def chat_loop(self) -> Any: ...

    @property
    def chat_sessions(self) -> Any: ...

    @property
    def agents(self) -> Any: ...

    def get_connection_token_getter(self, connection: Any) -> Any: ...

    def get_adapter(self, connection: Any) -> Any: ...


@dataclass(frozen=True)
class _WireSetup:
    """What opening one Provider's wire needs beyond the target."""

    offer_sdp: str | None
    voice: str | None
    instructions: str
    tools: tuple[JsonObject, ...]
    openai_backend: OpenAIBackend | None


async def _open_openai(runtime: Any, target_ref: TaskModelTargetRef, setup: _WireSetup) -> LiveWire:
    return await open_openai_live_wire(
        runtime,
        target_ref,
        offer_sdp=setup.offer_sdp or "",
        instructions=setup.instructions,
        voice=setup.voice,
        backend=setup.openai_backend,
    )


async def _open_xai(runtime: Any, target_ref: TaskModelTargetRef, setup: _WireSetup) -> LiveWire:
    return await open_xai_live_wire(
        runtime,
        target_ref,
        instructions=setup.instructions,
        voice=setup.voice,
        tools=list(setup.tools),
    )


@dataclass(frozen=True)
class _ProviderWire:
    """How a Provider's Live calls connect.

    ``tools`` means the voice model calls function Tools itself; otherwise
    it hands every request on through native delegation. ``backends`` are
    the ``backend`` values the wire can serve, the fallback first.
    """

    media: str
    tools: bool
    backends: tuple[str, ...]
    open: Callable[[Any, TaskModelTargetRef, _WireSetup], Awaitable[LiveWire]]


_PROVIDER_WIRES: dict[str, _ProviderWire] = {
    "openai": _ProviderWire(
        media=MEDIA_WEBRTC,
        tools=False,
        backends=(LIVE_BACKEND_VBOT, LIVE_BACKEND_OPENAI),
        open=_open_openai,
    ),
    "xai": _ProviderWire(
        media=MEDIA_RELAY,
        tools=True,
        backends=(LIVE_BACKEND_NONE, LIVE_BACKEND_VBOT),
        open=_open_xai,
    ),
}


@dataclass(frozen=True)
class _CallPlan:
    target_ref: TaskModelTargetRef
    wire: _ProviderWire
    voice: str | None
    backend: str
    openai_backend_model: str | None


@dataclass(frozen=True)
class _VoiceSetup:
    """What the voice model is told and offered, and who answers its requests."""

    instructions: str
    tools: tuple[JsonObject, ...]
    openai_backend: OpenAIBackend | None


class LiveVoiceService:
    """Start Live calls for the configured ``live_voice`` Task Model binding.

    *hosts* routes the Live Tool calls of a call's Sessions to the call.
    """

    def __init__(
        self,
        model_tasks: Any,
        runtime: LiveRuntime,
        *,
        hosts: LiveToolHosts,
        usage_recorder: UsageRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._hosts = hosts
        self._usage_recorder = usage_recorder
        self._clock = clock

    def status(self) -> JsonObject:
        """Return ``{"configured", "usable", "target", "media"}``.

        ``usable`` proves credentials only. ``media`` is the media kind a start
        must request (``"webrtc"`` or ``"relay"``), or ``None`` when the bound
        Provider has no Live wire.
        """
        try:
            binding = self._model_tasks.binding_for(TASK_LIVE_VOICE)
        except TaskModelError:
            return {"configured": False, "usable": False, "target": None, "media": None}
        return {
            "configured": True,
            "usable": bool(self._model_tasks.binding_is_usable(TASK_LIVE_VOICE)),
            "target": binding.target,
            "media": _target_media(binding.target),
        }

    async def start_call(
        self,
        *,
        media: str,
        offer_sdp: str | None = None,
        host: LiveCallHost,
    ) -> LiveCall:
        """Create a provider call and return it once control is joined.

        *media* is the kind the accessor prepared; WebRTC needs *offer_sdp*.
        Raises :class:`LiveStartRejected` for expected failures, including
        ``media_mismatch`` when the bound target needs the other media kind.
        """

        if media == MEDIA_WEBRTC and not _valid_offer(offer_sdp):
            raise LiveStartRejected("invalid_offer")
        plan = self._resolve_target()
        target_ref = plan.target_ref
        if media != plan.wire.media:
            raise LiveStartRejected(
                "media_mismatch", f"Live voice for {target_ref.provider_id} needs {plan.wire.media}"
            )
        # Log label without a Provider Account id.
        label = public_provider_target_id(
            target_ref.provider_id, target_ref.model_id, target_ref.local_connection_id
        )
        chat = self._runtime.chat_loop
        title = f"Live call · {self._clock():%Y-%m-%d %H:%M}"
        call: LiveCallSession | None = None

        def end_call() -> None:
            # The user stopped the voice Run in the app.
            if call is not None:
                call.abort_soon()

        try:
            voice = await chat.start_external_run(
                LIVE_VOICE_AGENT_ID,
                model=label,
                title=title,
                extra_tools=[TOOL_VBOT_REQUEST] if plan.backend == LIVE_BACKEND_VBOT else [],
                on_cancel=end_call,
            )
        except VBotError as exc:
            _LOGGER.warning(
                "Live call Session could not start (target=%s error_type=%s)",
                label,
                type(exc).__name__,
            )
            raise LiveStartRejected("not_configured", str(exc)) from exc
        try:
            setup = self._voice_setup(plan, voice.tool_definitions, host.wake_phrases)
            wire = await self._open_wire(plan, label, offer_sdp, setup)
        except BaseException:
            await asyncio.shield(voice.discard())
            raise
        log_id = new_id("live")
        call = LiveCallSession(
            wire=wire.wire,
            voice=voice,
            backend_factory=(
                (
                    lambda session_host, on_session: LiveBackend(
                        chat=chat,
                        sessions=self._runtime.chat_sessions,
                        hosts=self._hosts,
                        host=session_host,
                        title=title,
                        voice_session_id=voice.session_id,
                        on_session=on_session,
                    )
                )
                if plan.backend == LIVE_BACKEND_VBOT
                else None
            ),
            host=host,
            hosts=self._hosts,
            target=label,
            log_id=log_id,
            usage_accounting=wire.accounting,
            usage_call_id=wire.usage_call_id,
        )
        call.start()
        _LOGGER.info(
            "Live call started (call=%s target=%s media=%s backend=%s)",
            call.log_id,
            label,
            plan.wire.media,
            plan.backend,
        )
        return call

    def _voice_setup(
        self, plan: _CallPlan, voice_tools: Sequence[JsonObject], wake_phrases: Sequence[str]
    ) -> _VoiceSetup:
        tools = tuple(voice_tools)
        openai_backend: OpenAIBackend | None = None
        if plan.backend == LIVE_BACKEND_OPENAI:
            offered = tuple(tool for tool in tools if tool.get("name") != TOOL_VBOT_REQUEST)
            offered_names = {str(tool.get("name")) for tool in offered}
            openai_backend = OpenAIBackend(
                model=plan.openai_backend_model or "",
                instructions=live_tool_guidance(offered_names.__contains__),
                tools=offered,
            )
        wire_tools = tools if plan.wire.tools else ()
        instructions = voice_instructions(
            tools=[str(tool.get("name")) for tool in wire_tools],
            delegates=not plan.wire.tools,
            wake_phrases=wake_phrases,
        )
        return _VoiceSetup(
            instructions=instructions, tools=wire_tools, openai_backend=openai_backend
        )

    async def _open_wire(
        self, plan: _CallPlan, label: str, offer_sdp: str | None, setup: _VoiceSetup
    ) -> _OpenedWire:
        target_ref = plan.target_ref
        accounting = TaskUsage(self._usage_recorder, TASK_LIVE_VOICE, target_ref)
        usage_call_id = await accounting.start()
        wire: LiveWire | None = None
        status: Literal["failed", "cancelled"] = "failed"
        try:
            wire = await plan.wire.open(
                self._runtime,
                target_ref,
                _WireSetup(
                    offer_sdp=offer_sdp,
                    voice=plan.voice,
                    instructions=setup.instructions,
                    tools=setup.tools,
                    openai_backend=setup.openai_backend,
                ),
            )
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except ControlJoinError as exc:
            # The created call has a Provider call id; it never enters the log.
            _LOGGER.warning(
                "Live call control join failed (target=%s error_type=%s)",
                label,
                exc.error_type,
            )
            raise LiveStartRejected("control_failed", str(exc)) from exc
        except VBotError as exc:
            code = _start_failure_code(exc)
            _LOGGER.warning(
                "Live call creation failed (target=%s code=%s error_type=%s)",
                label,
                code,
                type(exc).__name__,
            )
            raise LiveStartRejected(code, str(exc)) from exc
        except KeyError as exc:
            raise LiveStartRejected(
                "provider_unavailable", "Live voice Provider is unavailable"
            ) from exc
        finally:
            if wire is None:
                await accounting.finish(usage_call_id, status=status)
        return _OpenedWire(wire=wire, accounting=accounting, usage_call_id=usage_call_id)

    def _resolve_target(self) -> _CallPlan:
        resolver = TaskBindingResolver(self._model_tasks, configuration_error=LiveVoiceError)
        try:
            _binding, options, target_ref = resolver.resolve(TASK_LIVE_VOICE)
        except LiveVoiceError as exc:
            raise LiveStartRejected("not_configured", str(exc)) from exc
        provider_wire = _PROVIDER_WIRES.get(target_ref.provider_id)
        if provider_wire is None:
            raise LiveStartRejected(
                "not_configured", f"Live voice does not support Provider {target_ref.provider_id}"
            )
        voice = options.get("voice")
        backend = self._backend(target_ref, options, provider_wire)
        backend_model = options.get("openai_backend_model")
        if backend == LIVE_BACKEND_OPENAI and not (
            isinstance(backend_model, str) and backend_model
        ):
            raise LiveStartRejected(
                "backend_unavailable", "The OpenAI backend model of Live voice is not set"
            )
        if (
            backend == LIVE_BACKEND_VBOT
            and not self._runtime.agents.get(LIVE_BACKEND_AGENT_ID).model
        ):
            # Every request would fail; the user picks the Model in Settings.
            raise LiveStartRejected(
                "backend_unavailable", "The Live backend Agent has no Model set"
            )
        return _CallPlan(
            target_ref=target_ref,
            wire=provider_wire,
            voice=voice if isinstance(voice, str) and voice else None,
            backend=backend,
            openai_backend_model=backend_model if isinstance(backend_model, str) else None,
        )

    def _backend(
        self, target_ref: TaskModelTargetRef, options: JsonObject, provider_wire: _ProviderWire
    ) -> str:
        """Return who answers the voice model's requests; the wire must serve it."""
        offered = live_backend_choices(self._model_tasks.model_for_target(target_ref))
        backend = options.get("backend")
        if backend in (None, ""):
            backend = offered[0] if offered else provider_wire.backends[0]
        if backend not in provider_wire.backends or (offered and backend not in offered):
            raise LiveStartRejected(
                "backend_unavailable", f"Live voice cannot use the backend {backend}"
            )
        return str(backend)


@dataclass(frozen=True)
class _OpenedWire:
    wire: LiveWire
    accounting: TaskUsage
    usage_call_id: str


def _target_media(target: str) -> str | None:
    try:
        provider_id = parse_task_model_target_id(target).provider_id
    except TaskModelError:
        return None
    provider_wire = _PROVIDER_WIRES.get(provider_id)
    return provider_wire.media if provider_wire is not None else None


def _valid_offer(offer_sdp: object) -> bool:
    return (
        isinstance(offer_sdp, str)
        and offer_sdp.startswith("v=0")
        and "m=audio" in offer_sdp
        and len(offer_sdp.encode("utf-8")) <= _MAX_OFFER_BYTES
    )


def _start_failure_code(error: VBotError) -> str:
    if isinstance(error, ProviderAuthError):
        return "access_denied"
    if isinstance(error, ProviderRateLimitError):
        return "rate_limited"
    if isinstance(error, ProviderOutcomeUnknownError):
        return "outcome_unknown"
    if isinstance(error, ConfigError):
        return "provider_unavailable"
    return "provider_error"
