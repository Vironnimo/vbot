"""Playback reference contracts and the monitor that follows device changes."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol, override

import numpy as np

logger = logging.getLogger("vbot.desktop.speech.echo")
STATE_ACTIVE = "active"
"""A reference is open (flowing or legitimately silent) or is being opened."""

STATE_NO_REFERENCE = "no_reference"
"""The echo canceller is installed but no playback reference is available."""

_POLL_INTERVAL_S = 1.0
_RETRY_INTERVAL_S = 30.0
_MONITOR_JOIN_S = 2.0
ReferenceBlockCallback = Callable[[np.ndarray, int, float, bool], None]
"""``(block[frames, channels] float32, rate, arrival, discontinuity)``."""


class ReferenceTarget(Protocol):
    """Accept a generation-bound reference sink and state from its current monitor."""

    def _reference_sink(self, monitor: ReferenceMonitor) -> ReferenceBlockCallback: ...
    def _set_state(
        self, state: str, reason: str, monitor: ReferenceMonitor | None = None
    ) -> None: ...


class LoopbackStream(Protocol):
    """An open capture of the default playback endpoint."""

    @property
    def name(self) -> str:
        """Human-readable endpoint name for logs."""

    def is_active(self) -> bool:
        """False once the stream stopped delivering for good (device lost)."""

    def close(self) -> None:
        """Stop and release the stream."""


class LoopbackBackend(Protocol):
    """Opens the loopback of the default playback endpoint.

    Every method runs on the stage's reference monitor thread.
    """

    def start(self) -> None:
        """Prepare the thread (for example COM) before any other call."""

    def default_endpoint(self) -> str | None:
        """Identity of the current default playback endpoint, None without one.

        Must be cheap: it is polled. Raises ``OSError`` when change detection is
        not possible on this system.
        """

    def open_default(self, on_block: ReferenceBlockCallback) -> LoopbackStream:
        """Open the default endpoint's loopback.

        Raises ``LookupError`` when there is no loopback endpoint and ``OSError``
        (or another exception) when opening failed.
        """

    def stop(self) -> None:
        """Release what ``start`` prepared; no stream is open any more."""


class ReferenceMonitor(threading.Thread):
    """Open the playback loopback and follow default-device changes.

    Neither PortAudio library follows a change of the default playback device,
    so the monitor polls the default endpoint's identity (a cheap Core Audio
    query) and reopens the loopback when it changes or when the stream stopped.
    Polling on a timer bounds the uncancelled time after a device switch to
    about one interval even while audio keeps playing; reopening only on a
    silence gap could not tell "nothing plays" from "device switched" and would
    re-enumerate every device again and again while the speakers are quiet.
    """

    def __init__(
        self,
        stage: ReferenceTarget,
        backend: LoopbackBackend,
        poll_interval: float,
        retry_interval: float,
    ) -> None:
        super().__init__(name="vbot-echo-reference", daemon=True)
        self._stage = stage
        self._backend = backend
        self._poll_interval = poll_interval
        self._retry_interval = retry_interval
        self._stopping = threading.Event()
        self._stream: LoopbackStream | None = None
        self._endpoint: str | None = None
        self._failed_endpoint: str | None = None
        self._retry_at = 0.0
        self._detection_failed = False

    def stop(self) -> None:
        self._stopping.set()

    @override
    def run(self) -> None:
        try:
            self._backend.start()
        except Exception as error:
            logger.warning("Echo reference could not start: %s", type(error).__name__)
            self._update(STATE_NO_REFERENCE, "playback loopback unavailable")
            return
        try:
            while not self._stopping.is_set():
                self._poll()
                self._stopping.wait(self._poll_interval)
        finally:
            self._close_stream()
            try:
                self._backend.stop()
            except Exception as error:
                logger.warning("Echo reference cleanup failed: %s", type(error).__name__)

    def _poll(self) -> None:
        endpoint = self._current_endpoint()
        if endpoint is None:
            self._close_stream()
            self._endpoint = None
            self._failed_endpoint = None
            self._update(STATE_NO_REFERENCE, "no playback device")
            return
        stream = self._stream
        if stream is not None:
            if endpoint != self._endpoint:
                logger.info("Default playback device changed; reopening the echo reference")
            elif not _stream_active(stream):
                logger.warning("Echo reference stream stopped; reopening it")
            else:
                return
            self._close_stream()
            self._failed_endpoint = None
        elif endpoint == self._failed_endpoint and time.monotonic() < self._retry_at:
            return
        self._open(endpoint)

    def _current_endpoint(self) -> str | None:
        """Default endpoint identity; a constant when change detection is unavailable."""
        if self._detection_failed:
            return "default"
        try:
            return self._backend.default_endpoint()
        except OSError as error:
            self._detection_failed = True
            logger.warning(
                "Playback device changes cannot be detected (%s); keeping the first loopback",
                type(error).__name__,
            )
            return "default"

    def _open(self, endpoint: str) -> None:
        first_failure = endpoint != self._failed_endpoint
        try:
            stream = self._backend.open_default(self._stage._reference_sink(self))
        except LookupError:
            self._failed(endpoint)
            self._update(STATE_NO_REFERENCE, "the playback device has no loopback")
            return
        except Exception as error:
            self._failed(endpoint)
            log = logger.warning if first_failure else logger.debug
            log("Echo reference could not open the playback loopback: %s", type(error).__name__)
            self._update(STATE_NO_REFERENCE, "opening the playback loopback failed")
            return
        if self._stopping.is_set():
            _close_quietly(stream)
            return
        self._stream = stream
        self._endpoint = endpoint
        self._failed_endpoint = None
        logger.info("Echo reference opened: %s", stream.name)
        self._update(STATE_ACTIVE, f"playing device {stream.name}")

    def _failed(self, endpoint: str) -> None:
        self._failed_endpoint = endpoint
        self._retry_at = time.monotonic() + self._retry_interval

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            _close_quietly(stream)

    def _update(self, state: str, reason: str) -> None:
        self._stage._set_state(state, reason, self)


def _stream_active(stream: LoopbackStream) -> bool:
    try:
        return stream.is_active()
    except Exception:
        return False


def _close_quietly(stream: LoopbackStream) -> None:
    try:
        stream.close()
    except Exception as error:
        logger.warning("Echo reference stream did not close cleanly: %s", type(error).__name__)
