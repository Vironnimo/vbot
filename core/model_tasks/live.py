"""Live voice: provider-neutral realtime voice calls owned by the server.

The ``live_voice`` Task Model binding selects the voice Model and Connection.
A Live call keeps the provider control channel, delegated reasoning, and
announcements here. The :class:`LiveCallHost` the server supplies owns what the
Models are told and the Tools they may call (:class:`LiveBrief`), runs those
Tools, and attaches the accessor's media and display.

The bound Provider decides the media kind: ``webrtc`` (OpenAI; the accessor
connects audio to the provider) or ``relay`` (xAI; audio passes through the
server as PCM16 mono 24 kHz). A backend model answers delegated requests with
the host's Tools; a voice model that calls function Tools itself may run
without one and call the host's Tools directly (direct Tools mode, backend
``""``).

Accessor updates published through :meth:`LiveCallHost.publish`:

* ``{"type": "state", "phase": "connecting" | "live" | "closing" | "closed" | "failed"}``
* ``{"type": "expiry", "seconds": float}`` (after ``live`` when the provider
  limits the session: seconds until it ends the call)
* ``{"type": "caption", "role": "user" | "assistant", "text": str, "final": bool}``
* ``{"type": "activity", "busy": bool, "label": str | None}``
* ``{"type": "playback_clear"}`` (relay media: drop audio not yet played)
* ``{"type": "closed", "reason": str | None, "usage": dict | None}``; reasons
  include ``closed`` (an ordinary close), ``expired`` (the provider's session
  limit), ``connection_lost``, ``start_timeout``, and ``aborted``

Relay audio for the accessor goes through :meth:`LiveCallHost.publish_audio`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from core.model_tasks._live_brain import BrainTarget, LiveBrain
from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import ControlJoinError, open_openai_live_wire
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
from core.model_tasks.options import (
    BACKEND_THINKING_EFFORT_DEFAULT,
    backend_thinking_efforts,
    live_backend_candidates,
)
from core.model_tasks.task_execution import TaskBindingResolver, TaskUsage
from core.providers.errors import (
    ProviderAuthError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.usage import UsageRecorder
from core.utils.errors import ConfigError, TaskError, VBotError
from core.utils.ids import new_id
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

__all__ = [
    "LIVE_MEDIA_KINDS",
    "LIVE_START_REJECTION_CODES",
    "LIVE_UPDATE_PREFIX",
    "LiveBrief",
    "LiveCall",
    "LiveCallHost",
    "LiveRunNotice",
    "LiveRuntime",
    "LiveStartRejected",
    "LiveToolRun",
    "LiveVoiceError",
    "LiveVoiceService",
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


@dataclass(frozen=True)
class LiveBrief:
    """What the Models of one call are told, and the Tools they may call.

    ``voice_instructions`` configure the voice model. ``request_tool`` is the
    function Tool through which a voice model without native delegation hands
    the user's request on (one required text parameter, ``request``); it is
    offered only when a backend model answers. ``delegation_instructions`` are
    the backend model's system prompt. ``tools`` are the host's Tool
    definitions (``name``, ``description``, JSON Schema ``parameters``),
    offered to the backend model, or in direct Tools mode to the voice model.
    """

    voice_instructions: str
    request_tool: JsonObject
    delegation_instructions: str
    tools: tuple[JsonObject, ...]


@dataclass(frozen=True)
class LiveToolRun:
    """The outcome of one Tool call a Model made.

    ``result`` is a Tool result envelope (:func:`live_success`,
    :func:`live_failure`). ``changed`` names the Tool when it ran and may have
    changed something in the app; it stays empty for lookups and for calls
    rejected before they ran.
    """

    result: JsonObject
    changed: str = ""


class LiveCallHost(Protocol):
    """Server-side owner of one Live call's Tools, accessor, and records.

    ``brief`` supplies the call's instructions and Tools once the call knows
    whether the voice model calls the Tools itself (*direct_tools*).
    ``run_tool`` runs one Tool call as the Model made it (any name, raw
    arguments) and never raises for operation failures: they come back as a
    failure result naming the next valid call. *rejection* is the failure a
    Provider Adapter already reported for unusable arguments; the host returns
    it as the result without running anything. Delegations run one at a time;
    direct Tool calls may call ``run_tool`` concurrently. ``known_refs`` lists
    the refs earlier Tool results named (one ``- s1: Session at Coder`` line
    each, empty when none), so a later delegation can target them without
    reading them again. ``current_state`` returns what the app and vBot show
    right now as text (the full overview), given to each delegation so it need
    not look first; empty when unknown.
    ``publish`` delivers an accessor update, ``publish_audio`` relayed
    assistant audio (PCM16 mono 24 kHz), and ``record`` one delegation record
    for local measurement, all without blocking.
    """

    def brief(self, *, direct_tools: bool) -> LiveBrief: ...

    async def run_tool(
        self, name: Any, arguments: Any, *, rejection: JsonObject | None = None
    ) -> LiveToolRun: ...

    def known_refs(self) -> str: ...

    async def current_state(self) -> str: ...

    def publish(self, update: JsonObject) -> None: ...

    def publish_audio(self, pcm: bytes) -> None: ...

    def record(self, event: JsonObject) -> None: ...


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

    async def wait_closed(self) -> None:
        """Return once the call is closed or failed and its tasks have stopped."""
        ...


class LiveRuntime(Protocol):
    """Runtime seams a Live call needs: task credentials, adapters, and Models."""

    @property
    def providers(self) -> Any: ...

    @property
    def models(self) -> Any: ...

    def get_connection_token_getter(self, connection: Any) -> Any: ...

    def get_adapter(self, connection: Any) -> Any: ...


@dataclass(frozen=True)
class _WireSetup:
    """What opening one Provider's wire needs beyond the target."""

    offer_sdp: str | None
    voice: str | None
    direct_tools: bool
    brief: LiveBrief


async def _open_openai(runtime: Any, target_ref: TaskModelTargetRef, setup: _WireSetup) -> LiveWire:
    return await open_openai_live_wire(
        runtime,
        target_ref,
        offer_sdp=setup.offer_sdp or "",
        instructions=setup.brief.voice_instructions,
        voice=setup.voice,
    )


async def _open_xai(runtime: Any, target_ref: TaskModelTargetRef, setup: _WireSetup) -> LiveWire:
    brief = setup.brief
    return await open_xai_live_wire(
        runtime,
        target_ref,
        instructions=brief.voice_instructions,
        voice=setup.voice,
        tools=list(brief.tools) if setup.direct_tools else [brief.request_tool],
        direct_tools=setup.direct_tools,
    )


@dataclass(frozen=True)
class _ProviderWire:
    """How a Provider's Live calls connect.

    ``direct_tools`` means the voice model can call the host's Tools itself,
    so the backend model is optional.
    """

    media: str
    direct_tools: bool
    open: Callable[[Any, TaskModelTargetRef, _WireSetup], Awaitable[LiveWire]]


_PROVIDER_WIRES: dict[str, _ProviderWire] = {
    "openai": _ProviderWire(media=MEDIA_WEBRTC, direct_tools=False, open=_open_openai),
    "xai": _ProviderWire(media=MEDIA_RELAY, direct_tools=True, open=_open_xai),
}


@dataclass(frozen=True)
class _CallPlan:
    target_ref: TaskModelTargetRef
    wire: _ProviderWire
    voice: str | None
    # ``None`` runs the call in direct Tools mode.
    brain_target: BrainTarget | None


class LiveVoiceService:
    """Start Live calls for the configured ``live_voice`` Task Model binding."""

    def __init__(
        self, model_tasks: Any, runtime: LiveRuntime, *, usage_recorder: UsageRecorder | None = None
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._usage_recorder = usage_recorder

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
        direct_tools = plan.brain_target is None
        brief = host.brief(direct_tools=direct_tools)
        setup = _WireSetup(
            offer_sdp=offer_sdp, voice=plan.voice, direct_tools=direct_tools, brief=brief
        )
        accounting = TaskUsage(self._usage_recorder, TASK_LIVE_VOICE, target_ref)
        usage_call_id = await accounting.start()
        wire: LiveWire | None = None
        status: Literal["failed", "cancelled"] = "failed"
        try:
            wire = await plan.wire.open(self._runtime, target_ref, setup)
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
        # OpenAI assigns the call id; logs and derived ids use this one instead.
        log_id = new_id("live")
        brain_target = plan.brain_target
        brain = (
            LiveBrain(
                self._runtime,
                brain_target,
                instructions=brief.delegation_instructions,
                tools=brief.tools,
                run_tool=host.run_tool,
                conversation_id=f"live:{log_id}",
                record=host.record,
                usage_recorder=self._usage_recorder,
            )
            if brain_target is not None
            else None
        )
        call = LiveCallSession(
            wire=wire,
            brain=brain,
            host=host,
            target=label,
            log_id=log_id,
            usage_accounting=accounting,
            usage_call_id=usage_call_id,
        )
        call.start()
        _LOGGER.info(
            "Live call started (call=%s target=%s media=%s backend_model=%s backend_effort=%s)",
            call.log_id,
            label,
            plan.wire.media,
            brain_target.model_id if brain_target is not None else "none",
            (brain_target.thinking_effort if brain_target is not None else None) or "default",
        )

        return call

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
        return _CallPlan(
            target_ref=target_ref,
            wire=provider_wire,
            voice=voice if isinstance(voice, str) and voice else None,
            brain_target=self._brain_target(target_ref, options, provider_wire),
        )

    def _brain_target(
        self, target_ref: TaskModelTargetRef, options: JsonObject, provider_wire: _ProviderWire
    ) -> BrainTarget | None:
        """Return the backend model, or ``None`` for direct Tools mode."""

        backend_model = options.get("backend_model")
        if backend_model in (None, "") and provider_wire.direct_tools:
            # Direct Tools mode ignores the backend reasoning effort entirely.
            return None
        candidates = live_backend_candidates(
            self._runtime.models, target_ref.provider_id, target_ref.local_connection_id
        )
        if backend_model not in {model.model_id for model in candidates}:
            raise LiveStartRejected(
                "backend_unavailable", "The Live voice backend model is not available"
            )
        return BrainTarget(
            provider_id=target_ref.provider_id,
            connection_id=target_ref.connection_id,
            model_id=str(backend_model),
            thinking_effort=_backend_thinking_effort(options),
        )


def _target_media(target: str) -> str | None:
    try:
        provider_id = parse_task_model_target_id(target).provider_id
    except TaskModelError:
        return None
    provider_wire = _PROVIDER_WIRES.get(provider_id)
    return provider_wire.media if provider_wire is not None else None


def _backend_thinking_effort(options: JsonObject) -> str | None:
    """Return the configured backend effort; ``None`` leaves it to the Model.

    A missing or null option is unset, like save validation treats it, and
    uses the default; ``""`` explicitly selects the Model default.
    """

    effort = options.get("backend_thinking_effort")
    if effort is None:
        effort = BACKEND_THINKING_EFFORT_DEFAULT
    if not isinstance(effort, str) or effort not in backend_thinking_efforts():
        raise LiveStartRejected(
            "backend_unavailable", "The Live voice backend reasoning effort is not valid"
        )
    return effort or None


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
