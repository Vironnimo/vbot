"""Live voice: provider-neutral realtime voice calls owned by the server.

The ``live_voice`` Task Model binding selects the voice Model and Connection.
A Live call keeps the provider control channel, delegated reasoning, Tool
execution, and announcements on the server. Accessors attach only media and
answer UI requests through the :class:`LiveCallHost` the server supplies.

Accessor updates published through :meth:`LiveCallHost.publish`:

* ``{"type": "state", "phase": "connecting" | "live" | "closing" | "closed" | "failed"}``
* ``{"type": "caption", "role": "user" | "assistant", "text": str, "final": bool}``
* ``{"type": "activity", "busy": bool, "label": str | None}``
* ``{"type": "closed", "reason": str | None, "usage": dict | None}``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.model_tasks._live_brain import BrainTarget, LiveBrain
from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import ControlJoinError, open_openai_live_wire
from core.model_tasks._live_tools import LIVE_TOOL_APP, LIVE_TOOL_TERMINAL, VOICE_INSTRUCTIONS
from core.model_tasks.constants import TASK_LIVE_VOICE
from core.model_tasks.model_tasks import (
    TaskModelError,
    TaskModelTargetRef,
    public_provider_target_id,
)
from core.model_tasks.options import live_backend_candidates
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.errors import (
    ProviderAuthError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.utils.errors import ConfigError, TaskError, VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

__all__ = [
    "LIVE_START_REJECTION_CODES",
    "LIVE_TOOL_APP",
    "LIVE_TOOL_TERMINAL",
    "LiveCall",
    "LiveCallHost",
    "LiveRunNotice",
    "LiveRuntime",
    "LiveStartRejected",
    "LiveVoiceError",
    "LiveVoiceService",
]

_LOGGER = get_logger(__name__)

_OPENAI_PROVIDER_ID = "openai"
_MAX_OFFER_BYTES = 65536

LIVE_START_REJECTION_CODES = frozenset(
    {
        "not_configured",
        "invalid_offer",
        "access_denied",
        "rate_limited",
        "outcome_unknown",
        "provider_error",
        "control_failed",
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
    """Server-side attachment of one Live call to its owning accessor.

    ``execute_tool`` runs one app operation (``vbot_app`` or ``vbot_terminal``)
    and returns a JSON-serializable result, reporting operation failures inside
    the result. Concurrent delegations may call it concurrently; the host
    serializes executions. ``publish`` delivers an accessor update without
    blocking.
    """

    async def execute_tool(self, name: str, arguments: JsonObject) -> JsonObject: ...

    def publish(self, update: JsonObject) -> None: ...


@dataclass(frozen=True)
class LiveRunNotice:
    """A finished vBot Run the call announces at most once.

    ``kind`` names the outcome (for example ``completed``, ``failed``, or
    ``interrupted``). ``agent_id`` is the exact Agent address (``agent`` or
    ``agent@project``).
    """

    kind: str
    run_id: str
    agent_id: str
    session_id: str
    excerpt: str
    truncated: bool


class LiveCall(Protocol):
    """One running Live call.

    ``media`` tells the accessor how to connect audio, currently
    ``{"type": "webrtc", "sdp": <answer>}``.
    """

    @property
    def id(self) -> str: ...

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


class LiveVoiceService:
    """Start Live calls for the configured ``live_voice`` Task Model binding."""

    def __init__(self, model_tasks: Any, runtime: LiveRuntime) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime

    def status(self) -> JsonObject:
        """Return ``{"configured", "usable", "target"}``; usable proves credentials only."""
        try:
            binding = self._model_tasks.binding_for(TASK_LIVE_VOICE)
        except TaskModelError:
            return {"configured": False, "usable": False, "target": None}
        return {
            "configured": True,
            "usable": bool(self._model_tasks.binding_is_usable(TASK_LIVE_VOICE)),
            "target": binding.target,
        }

    async def start_call(self, *, offer_sdp: str, host: LiveCallHost) -> LiveCall:
        """Create a provider call for *offer_sdp* and return it once control is joined.

        Raises :class:`LiveStartRejected` for expected failures.
        """

        if not _valid_offer(offer_sdp):
            raise LiveStartRejected("invalid_offer")
        target_ref, voice, brain_target = self._resolve_target()
        # Log label without a Provider Account id.
        label = public_provider_target_id(
            target_ref.provider_id, target_ref.model_id, target_ref.local_connection_id
        )
        try:
            wire = await open_openai_live_wire(
                self._runtime,
                target_ref,
                offer_sdp=offer_sdp,
                instructions=VOICE_INSTRUCTIONS,
                voice=voice,
            )
        except ControlJoinError as exc:
            _LOGGER.warning(
                "Live call control join failed: target=%s call_id=%s", label, exc.call_id
            )
            raise LiveStartRejected("control_failed", str(exc)) from exc
        except VBotError as exc:
            code = _start_failure_code(exc)
            _LOGGER.warning(
                "Live call creation failed: target=%s code=%s error_type=%s",
                label,
                code,
                type(exc).__name__,
            )
            raise LiveStartRejected(code, str(exc)) from exc
        except KeyError as exc:
            raise LiveStartRejected("not_configured", "Live voice Provider is unavailable") from exc
        brain = LiveBrain(
            self._runtime,
            brain_target,
            host.execute_tool,
            conversation_id=f"live:{wire.call_id}",
        )
        call = LiveCallSession(wire=wire, brain=brain, host=host, target=label)
        call.start()
        _LOGGER.info(
            "Live call started: call_id=%s target=%s backend_model=%s",
            call.id,
            label,
            brain_target.model_id,
        )
        return call

    def _resolve_target(self) -> tuple[TaskModelTargetRef, str | None, BrainTarget]:
        resolver = TaskBindingResolver(self._model_tasks, configuration_error=LiveVoiceError)
        try:
            _binding, options, target_ref = resolver.resolve(TASK_LIVE_VOICE)
        except LiveVoiceError as exc:
            raise LiveStartRejected("not_configured", str(exc)) from exc
        if target_ref.provider_id != _OPENAI_PROVIDER_ID:
            raise LiveStartRejected(
                "not_configured", f"Live voice does not support Provider {target_ref.provider_id}"
            )
        backend_model = options.get("backend_model")
        candidates = live_backend_candidates(
            self._runtime.models, target_ref.provider_id, target_ref.local_connection_id
        )
        if backend_model not in {model.model_id for model in candidates}:
            raise LiveStartRejected(
                "not_configured", "The Live voice backend model is not available"
            )
        voice = options.get("voice")
        return (
            target_ref,
            voice if isinstance(voice, str) and voice else None,
            BrainTarget(
                provider_id=target_ref.provider_id,
                connection_id=target_ref.connection_id,
                model_id=str(backend_model),
            ),
        )


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
        return "not_configured"
    return "provider_error"
