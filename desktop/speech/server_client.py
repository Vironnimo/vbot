"""HTTP client for the vBot server calls of Desktop speech input.

:class:`SpeechServerClient` wraps the server's JSON RPC endpoint
(``POST /api/rpc``) and speech upload endpoint (``POST
/api/speech/transcribe``) behind typed operations: speech-to-text readiness and
preparation, transcription, and the speech upload budget. Consumers that need
more RPC operations (Voice commands) extend it and reuse :meth:`_rpc`.

Failures raise typed errors whose ``error_code`` is a stable code:

- :class:`SpeechServerUnreachable` (``server_unreachable``): no HTTP response,
  or a retryable server status still failing after the last attempt (RPC).
- :class:`SpeechServerRejected`: the server answered and refused. The code names
  the failed operation (``transcription_failed`` here; subclasses add theirs);
  ``status_code`` and ``rpc_code`` keep the detail for diagnostics.
- :class:`SpeechServerInvalidResponse`: a response that does not match the
  contract, with the failed operation's code.

:class:`SpeechRequestCancelled` is not a server failure: it is raised when the
client's cancel event is set before an attempt or during a retry backoff. A
request already in flight runs to its own timeout.

Retries: idempotent reads (``settings.get_path``, ``task_model.status``) and
transcription make up to :data:`MAX_ATTEMPTS` attempts on transport failures and
retryable statuses. The best-effort ``speech.prepare_transcription`` hint makes
one attempt. Environment proxies are ignored (``trust_env=False``).
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Mapping
from typing import Any, Self

import httpx

logger = logging.getLogger("vbot.desktop.speech.server_client")

RPC_TIMEOUT_SECONDS = 10.0
# Local speech-to-text may download model weights on first use: keep the
# connection and upload phases bounded apart from the long inference wait.
TRANSCRIPTION_TIMEOUT = httpx.Timeout(600.0, connect=10.0, write=30.0, pool=10.0)
MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 10.0
# Mirrors the always-retryable set in core/utils/http_status.py; duplicated
# because the Desktop process does not import from core.
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

# Mirrors the server's default ``speech.upload_max_size_bytes``.
DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES = 104_857_600
# Headroom for container overhead beyond the canonical 44-byte WAV header.
UPLOAD_SAFETY_FRACTION = 0.9
WAV_HEADER_BYTES = 44
_MIN_UPLOAD_BUDGET_BYTES = WAV_HEADER_BYTES + 2

ERROR_SERVER_UNREACHABLE = "server_unreachable"
ERROR_TRANSCRIPTION_FAILED = "transcription_failed"
ERROR_SPEECH_TO_TEXT_UNCONFIGURED = "speech_to_text_unconfigured"
ERROR_SPEECH_TO_TEXT_UNAVAILABLE = "speech_to_text_unavailable"
ERROR_SPEECH_TO_TEXT_READINESS_FAILED = "speech_to_text_readiness_failed"

_RPC_PATH = "/api/rpc"
_TRANSCRIBE_PATH = "/api/speech/transcribe"
_UPLOAD_LIMIT_SETTING_PATH = "speech.upload_max_size_bytes"
_SPEECH_TO_TEXT_TASK = "speech_to_text"
_RESPONSE_PREVIEW_CHARS = 300


class SpeechServerError(RuntimeError):
    """A failed server call with a stable ``error_code``."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SpeechServerUnreachable(SpeechServerError):  # noqa: N818 - names the outcome
    """The server could not be reached (``server_unreachable``)."""

    def __init__(self, message: str) -> None:
        super().__init__(ERROR_SERVER_UNREACHABLE, message)


class SpeechServerRejected(SpeechServerError):  # noqa: N818 - names the outcome
    """The server answered and refused the operation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        rpc_code: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.status_code = status_code
        self.rpc_code = rpc_code


class SpeechServerInvalidResponse(SpeechServerError):  # noqa: N818 - names the outcome
    """The server's answer does not match the expected contract."""


class SpeechRequestCancelled(Exception):  # noqa: N818 - a signal, not a failure
    """The client's cancel event stopped a call before or between attempts."""


class SpeechServerClient:
    """Typed, retrying access to one vBot server for speech input.

    Safe to share between threads. ``cancel`` belongs to the owner's lifetime
    (for example one listener session): once set, every call of this client
    raises :class:`SpeechRequestCancelled` instead of starting or retrying a
    request. ``transport`` replaces the network transport (tests).
    """

    def __init__(
        self,
        server_url: str,
        *,
        cancel: threading.Event,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = (server_url or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("A server URL is required")
        self._server_url = base_url
        self._cancel = cancel
        # No keep-alive: a pooled connection the server closed in the meantime
        # would fail a single-attempt mutation for no reason.
        self._http = httpx.Client(
            transport=transport,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self._budget_lock = threading.Lock()
        self._upload_budget_bytes: int | None = None

    @property
    def server_url(self) -> str:
        """The normalized server base URL."""
        return self._server_url

    def close(self) -> None:
        """Release the connection pool."""
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- Operations ----------------------------------------------------------

    def speech_readiness(self) -> str | None:
        """Return ``None`` when server speech-to-text can run, else its problem code.

        Codes: ``speech_to_text_unconfigured``, ``speech_to_text_unavailable``,
        ``server_unreachable``, ``speech_to_text_readiness_failed`` (any other
        failure). Only cancellation raises.
        """
        try:
            result = self._rpc(
                "task_model.status",
                {"task_type": _SPEECH_TO_TEXT_TASK},
                error_code=ERROR_SPEECH_TO_TEXT_READINESS_FAILED,
                attempts=MAX_ATTEMPTS,
            )
        except SpeechServerError as exc:
            logger.warning("Speech-to-text readiness check failed: %s", exc)
            return exc.error_code
        configured = result.get("configured")
        usable = result.get("usable")
        if configured is False:
            return ERROR_SPEECH_TO_TEXT_UNCONFIGURED
        if usable is False:
            return ERROR_SPEECH_TO_TEXT_UNAVAILABLE
        if configured is not True or usable is not True:
            logger.warning("Speech-to-text readiness result omitted its readiness fields")
            return ERROR_SPEECH_TO_TEXT_READINESS_FAILED
        return None

    def prepare_transcription(self) -> str | None:
        """Ask the server to start loading its local speech-to-text model now.

        A best-effort hint sent when a recording starts, so a model that
        is not loaded yet loads while the user speaks. Returns the server's state
        (``loading``, ``loaded``, ``not_local``, ``unavailable``), or ``None``
        when the request failed or was cancelled. Never raises.
        """
        try:
            result = self._rpc(
                "speech.prepare_transcription",
                {},
                error_code=ERROR_TRANSCRIPTION_FAILED,
                attempts=1,
            )
        except SpeechRequestCancelled:
            return None
        except SpeechServerError as exc:
            logger.warning("Speech-to-text preparation failed: %s", exc)
            return None
        state = result.get("state")
        if state == "loading":
            logger.info("Server started loading its speech-to-text model")
        return state if isinstance(state, str) else None

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "recording.wav",
        media_type: str = "audio/wav",
    ) -> str:
        """Upload one recording and return its transcript (``""`` when silent).

        Failures raise with ``transcription_failed``, except an unreachable
        server (``server_unreachable``). A read timeout is not retried: the
        server may still be busy with the first upload.
        """
        response = self._post(
            _TRANSCRIBE_PATH,
            attempts=MAX_ATTEMPTS,
            timeout=TRANSCRIPTION_TIMEOUT,
            retry_transport_error=lambda exc: not isinstance(exc, httpx.ReadTimeout),
            files={"file": (filename, audio, media_type)},
        )
        if response.status_code != 200:
            raise SpeechServerRejected(
                ERROR_TRANSCRIPTION_FAILED,
                f"Speech transcription failed: HTTP {response.status_code} "
                f"{_response_preview(response)}".rstrip(),
                status_code=response.status_code,
            )
        payload = _json_object(response)
        text = payload.get("text") if payload is not None else None
        if not isinstance(text, str):
            raise SpeechServerInvalidResponse(
                ERROR_TRANSCRIPTION_FAILED, "Speech transcription returned no text"
            )
        return text

    def upload_budget_bytes(self) -> int:
        """Return how many audio payload bytes one recording may reach.

        Derived once per client from the server's active speech upload limit:
        the limit minus the WAV header, keeping :data:`UPLOAD_SAFETY_FRACTION`
        of it. When the limit cannot be read, the server default is used, so a
        recording is never unbounded. Only cancellation raises.
        """
        with self._budget_lock:
            if self._upload_budget_bytes is not None:
                return self._upload_budget_bytes
        budget = _upload_budget_for_limit(self._fetch_upload_limit_bytes())
        with self._budget_lock:
            if self._upload_budget_bytes is None:
                self._upload_budget_bytes = budget
            return self._upload_budget_bytes

    # -- Internals -----------------------------------------------------------

    def _fetch_upload_limit_bytes(self) -> int:
        try:
            result = self._rpc(
                "settings.get_path",
                {"path": _UPLOAD_LIMIT_SETTING_PATH},
                error_code=ERROR_TRANSCRIPTION_FAILED,
                attempts=MAX_ATTEMPTS,
            )
        except SpeechServerError as exc:
            logger.warning("Speech upload limit unavailable (%s); using the default limit", exc)
            return DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES
        setting = result.get("setting")
        value = setting.get("value") if isinstance(setting, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int) or value <= WAV_HEADER_BYTES:
            logger.warning("Speech upload limit is malformed; using the default limit")
            return DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES
        return value

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        error_code: str,
        attempts: int,
    ) -> dict[str, Any]:
        response = self._post(
            _RPC_PATH,
            attempts=attempts,
            timeout=RPC_TIMEOUT_SECONDS,
            retry_transport_error=lambda _exc: True,
            json={"method": method, "params": params},
        )
        payload = _json_object(response)
        if response.status_code in RETRYABLE_STATUS_CODES:
            raise SpeechServerUnreachable(f"{method} failed: HTTP {response.status_code}")
        if payload is not None and payload.get("ok") is False:
            error = payload.get("error")
            rpc_code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            rpc_code = rpc_code if isinstance(rpc_code, str) else None
            raise SpeechServerRejected(
                error_code,
                f"{method} was rejected: {message if isinstance(message, str) else rpc_code}",
                status_code=response.status_code,
                rpc_code=rpc_code,
            )
        if response.status_code != 200:
            raise SpeechServerRejected(
                error_code,
                f"{method} failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        result = payload.get("result") if payload is not None else None
        if payload is None or payload.get("ok") is not True or not isinstance(result, dict):
            raise SpeechServerInvalidResponse(error_code, f"{method} returned an invalid response")
        return result

    def _post(
        self,
        path: str,
        *,
        attempts: int,
        timeout: float | httpx.Timeout,
        retry_transport_error: Callable[[httpx.RequestError], bool],
        **request: Any,
    ) -> httpx.Response:
        """Return the first response worth interpreting.

        Retries transport failures (when ``retry_transport_error`` allows) and
        retryable statuses until ``attempts`` run out; the last attempt's
        retryable status is returned for the caller to interpret.
        """
        url = f"{self._server_url}{path}"
        for attempt in range(attempts):
            self._raise_if_cancelled()
            last_attempt = attempt == attempts - 1
            try:
                response = self._http.post(url, timeout=timeout, **request)
            except httpx.RequestError as exc:
                if last_attempt or not retry_transport_error(exc):
                    raise SpeechServerUnreachable(
                        f"{path} could not reach the server: {type(exc).__name__}"
                    ) from exc
                logger.info("Retrying %s after %s", path, type(exc).__name__)
                self._wait_before_retry(attempt)
                continue
            if response.status_code in RETRYABLE_STATUS_CODES and not last_attempt:
                logger.info("Retrying %s after HTTP %s", path, response.status_code)
                self._wait_before_retry(attempt)
                continue
            return response
        raise AssertionError("attempts must be at least 1")

    def _wait_before_retry(self, attempt: int) -> None:
        if self._cancel.wait(_backoff_delay(attempt)):
            raise SpeechRequestCancelled("Speech server request cancelled")

    def _raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise SpeechRequestCancelled("Speech server request cancelled")


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at :data:`MAX_BACKOFF_SECONDS`."""
    return float(min(2**attempt + random.random(), MAX_BACKOFF_SECONDS))


def _upload_budget_for_limit(limit_bytes: int) -> int:
    payload_bytes = max(0, limit_bytes - WAV_HEADER_BYTES)
    return max(_MIN_UPLOAD_BUDGET_BYTES, int(payload_bytes * UPLOAD_SAFETY_FRACTION))


DEFAULT_UPLOAD_BUDGET_BYTES = _upload_budget_for_limit(DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES)
"""The recording budget that applies while the server's own limit is unknown."""


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _response_preview(response: httpx.Response) -> str:
    try:
        text = response.text.strip()
    except Exception:
        return ""
    return text[:_RESPONSE_PREVIEW_CHARS]
