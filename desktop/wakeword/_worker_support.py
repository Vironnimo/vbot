"""Worker support."""

from __future__ import annotations

import random
import re
import threading
import time
from collections.abc import Callable

import httpx

from desktop.wakeword._worker_constants import (
    _ERROR_NO_SERVER,
    _ERROR_SERVER_UNREACHABLE,
    _ERROR_SPEECH_TO_TEXT_READINESS_FAILED,
    _ERROR_SPEECH_TO_TEXT_UNAVAILABLE,
    _ERROR_SPEECH_TO_TEXT_UNCONFIGURED,
    _INTERRUPTIBLE_SLEEP_SLICE_SECONDS,
    _RPC_TIMEOUT,
    _TASK_MODEL_STATUS_METHOD,
    _TASK_SPEECH_TO_TEXT,
    _VOICE_CANCEL_PHRASES,
    logger,
)


def check_speech_to_text_readiness(
    server_url: str,
    *,
    post: Callable[..., httpx.Response] = httpx.post,
) -> str | None:
    """Return a stable activation error when server-side STT is not executable."""

    normalized_server_url = (server_url or "").rstrip("/")
    if not normalized_server_url:
        return _ERROR_NO_SERVER

    try:
        response = post(
            f"{normalized_server_url}/api/rpc",
            json={
                "method": _TASK_MODEL_STATUS_METHOD,
                "params": {"task_type": _TASK_SPEECH_TO_TEXT},
            },
            timeout=_RPC_TIMEOUT,
            trust_env=False,
        )
    except httpx.RequestError:
        logger.warning("Speech-to-text readiness check could not reach the server", exc_info=True)
        return _ERROR_SERVER_UNREACHABLE

    if response.status_code != 200:
        logger.warning(
            "Speech-to-text readiness check failed: HTTP %s",
            response.status_code,
        )
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Speech-to-text readiness check returned invalid JSON")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        logger.warning("Speech-to-text readiness RPC was rejected")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    result = payload.get("result")
    if not isinstance(result, dict):
        logger.warning("Speech-to-text readiness RPC returned an invalid result")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    if result.get("configured") is False:
        return _ERROR_SPEECH_TO_TEXT_UNCONFIGURED
    if result.get("usable") is False:
        return _ERROR_SPEECH_TO_TEXT_UNAVAILABLE
    if result.get("configured") is not True or result.get("usable") is not True:
        logger.warning("Speech-to-text readiness RPC omitted readiness fields")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    return None


def _backoff_sleep(attempt: int, running: threading.Event | None = None) -> None:
    """Sleep with exponential backoff and jitter, interruptible by ``running``.

    With a running flag, the sleep ends early when the worker is stopped
    mid-backoff, so a disable does not wait out the full delay before the retry
    loop notices it should bail.
    """
    delay = min((2**attempt) + random.random(), 10.0)
    if running is None:
        time.sleep(delay)
        return
    _sleep_while_running(running, delay)


def _sleep_while_running(running: threading.Event, duration_seconds: float) -> None:
    """Sleep in small slices so stop() can interrupt the post-detection hold."""
    deadline = time.monotonic() + max(0.0, duration_seconds)
    while running.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_INTERRUPTIBLE_SLEEP_SLICE_SECONDS, remaining))


def _response_text_preview(response: httpx.Response) -> str:
    """Return a bounded response-body preview for diagnostics."""
    try:
        text = response.text.strip()
    except Exception:
        return ""
    if not text:
        return ""
    return text[:500]


def _is_voice_cancel_phrase(transcript: str) -> bool:
    """Return whether a normalized transcript ends with a reserved cancel phrase."""
    normalized = re.sub(r"[^\wäöüß]+", " ", transcript.casefold(), flags=re.UNICODE).strip()
    return any(
        normalized == phrase or normalized.endswith(f" {phrase}")
        for phrase in _VOICE_CANCEL_PHRASES
    )
