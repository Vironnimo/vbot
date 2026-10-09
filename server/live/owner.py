"""The owner socket of a Live call: its protocol and per-socket frame queue.

The accessor that started a call owns its media and display. It attaches one
owner socket at ``/ws/live/{call_id}`` and answers UI requests through
``live.ui_result``.

Server to accessor:

* JSON text: call updates as the call publishes them - ``state``, ``caption``,
  ``activity``, ``playback_clear``, ``expiry`` and ``error`` from the call
  (see ``core/model_tasks/live.py``), ``action`` for each Live Tool call vBot
  ran (``server/live/_tools.py``), ``idle`` (``ends_in`` seconds before an
  unused call ends, ``null`` once it is in use again), and finally
  ``closed``. A call replaced by a newer start gets ``{"type": "closed",
  "reason": "replaced", "usage": null}``; a call that ends without its own
  ``closed`` update gets one with ``reason: null``; vBot names why it ended a
  call (``hung_up``, ``idle``) in place of the call's own reason;
* JSON text: ``{"type": "ui_request", "request_id", "action", "args"}`` - see
  ``server/live/_tools.py`` for ``open`` and ``terminal_view``;
* JSON text: ``{"type": "heartbeat", "timestamp"}`` while otherwise idle;
* binary, relay calls only: two little-endian uint32 fields, playback generation
  and source-sample offset, followed by PCM in the call's ``media.audio`` format.
  No audio waits for an absent owner. Exceeding ``owner_audio_limit_bytes``
  disconnects a slow owner; reattachment starts a fresh generation. A
  ``playback_clear`` removes every queued binary frame before being enqueued.

Updates wait for an owner: before it attaches and while it reconnects, they
are kept in a bounded buffer that drops the oldest caption, activity, action,
or heartbeat frames first, never a ``state``, ``ui_request``, ``error``, or
``closed`` frame. Updates a lost socket had not yet taken return to that
buffer, so a reconnecting owner still receives a pending UI request.

Accessor to server: binary frames of a relay call are microphone PCM in the
same format (even length, at most 64 KiB each). JSON text frames (at most
4096 characters): ``{"type": "context", "view", "selected_agent_id",
"selected_project_id", "chat_session"}`` reports what the app shows
(``chat_session`` is ``{"agent_id", "session_id"}`` or ``null``); the owner
sends one after it attaches and another whenever that changes.
``{"type": "stay"}`` keeps a call the ``idle`` update warned about.
``{"type": "playback", "generation", "played_samples", "enabled", "cleared"?}``
reports the AudioWorklet-rendered contiguous prefix in source samples. Generation
zero is initial readiness; a clear confirms the final old-generation prefix.
Mute, hold, gaps, and disconnect disable or clear playback rather than counting
discarded audio. Reports measure rendering, not acoustic output at the speaker.
Malformed frames and other text frames are ignored.

Close codes: 1000 after the ``closed`` update, 1008 for an unknown or already
forgotten call, 1013 when the owner fell behind, 4000 when a newer owner socket
replaced this one (do not reconnect). After any other close the owner may
reconnect within the reattach grace; the call ends if it does not.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any, Protocol

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.live")

LIVE_SOCKET_CLOSE_ENDED = 1000
LIVE_SOCKET_CLOSE_UNKNOWN_CALL = 1008
LIVE_SOCKET_CLOSE_LAGGED = 1013
LIVE_SOCKET_CLOSE_REPLACED = 4000

LIVE_AUDIO_FRAME_MAX_BYTES = 64 * 1024
LIVE_CONTEXT_FRAME_MAX_CHARS = 4096

OwnerFrame = JsonObject | bytes


class OwnerSide(Protocol):
    """The call an owner stream belongs to."""

    @property
    def log_id(self) -> str: ...

    def receive_audio(self, owner: LiveOwnerStream, pcm: bytes) -> None: ...

    def receive_text(self, owner: LiveOwnerStream, text: str) -> None: ...

    def detach(self, owner: LiveOwnerStream) -> None: ...


class LiveOwnerStream:
    """Frames for one attached owner socket, in delivery order.

    JSON objects go out as text frames and ``bytes`` as binary audio frames.
    Updates and audio have separate bounds: too many waiting updates end the
    stream as lagging, while audio beyond its bound is dropped.
    """

    def __init__(self, entry: OwnerSide, limit: int, audio_limit_bytes: int) -> None:
        self._entry = entry
        self._limit = limit
        self._audio_limit = audio_limit_bytes
        self._frames: deque[OwnerFrame] = deque()
        self._updates = 0
        self._audio_bytes = 0
        self._audio_dropped = False
        self._wakeup = asyncio.Event()
        self._close_code: int | None = None
        self._finished = False

    @property
    def close_code(self) -> int | None:
        """The code to close the socket with once the stream ended, else ``None``."""
        return self._close_code

    @property
    def finished(self) -> bool:
        """Whether :meth:`frames` yielded every frame and ended; close the socket then."""
        return self._finished

    def send(self, frame: OwnerFrame) -> bool:
        """Queue one frame; ``False`` when the stream ended or its updates fell behind."""
        if self._close_code is not None:
            return False
        if isinstance(frame, bytes):
            if self._audio_bytes + len(frame) > self._audio_limit:
                # A gap cannot be called heard. Reattach with a fresh playback
                # generation instead of silently splicing a spoken answer.
                if not self._audio_dropped:
                    self._audio_dropped = True
                    _LOGGER.warning(
                        "Live owner socket is slow; dropping assistant audio (call=%s)",
                        self._entry.log_id,
                    )
                return False
            self._audio_bytes += len(frame)
        else:
            if frame.get("type") == "playback_clear":
                # Barge-in must not wait behind the speech it is cancelling.
                self._frames = deque(
                    queued for queued in self._frames if not isinstance(queued, bytes)
                )
                self._audio_bytes = 0
            if self._updates >= self._limit:
                return False
            self._updates += 1
        self._frames.append(frame)
        self._wakeup.set()
        return True

    def take_undelivered(self) -> list[JsonObject]:
        """Remove and return the updates the socket has not taken yet; audio is dropped."""
        updates = [frame for frame in self._frames if not isinstance(frame, bytes)]
        self._frames.clear()
        self._updates = 0
        self._audio_bytes = 0
        return updates

    def end(self, code: int) -> None:
        """End the stream after the queued frames; the first code wins."""
        if self._close_code is None:
            self._close_code = code
            self._wakeup.set()

    async def frames(self) -> AsyncGenerator[OwnerFrame]:
        """Yield queued frames until the stream ends."""
        while True:
            while self._frames:
                frame = self._frames.popleft()
                if isinstance(frame, bytes):
                    self._audio_bytes -= len(frame)
                else:
                    self._updates -= 1
                yield frame
            if self._close_code is not None:
                self._finished = True
                return
            self._wakeup.clear()
            await self._wakeup.wait()

    def receive_audio(self, pcm: bytes) -> None:
        """Hand one inbound binary frame to the call as microphone audio."""
        self._entry.receive_audio(self, pcm)

    def receive_text(self, text: str) -> None:
        """Hand one inbound text frame (a context report or ``stay``) to the call."""
        self._entry.receive_text(self, text)

    def detach(self) -> None:
        """Release ownership after the socket closed."""
        self._entry.detach(self)
