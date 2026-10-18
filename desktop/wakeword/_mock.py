"""The mock listener's detection clock and simulated command progression."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol, override

from desktop.wakeword.contracts import _Recording
from desktop.wakeword.engine import MockWakewordEngine

if TYPE_CHECKING:
    from desktop.wakeword.listener import ListenerSession, VoiceRuntime


class MockEvents(Protocol):
    def _begin_mock(self, session: ListenerSession, model_id: str) -> _Recording | None: ...
    def _advance_mock(
        self, session: ListenerSession, recording: _Recording, stage: str | None
    ) -> bool: ...


class MockListener(threading.Thread):
    """Run simulated detections without microphone or network activity."""

    def __init__(
        self, session: ListenerSession, model_id: str, runtime: VoiceRuntime, events: MockEvents
    ) -> None:
        super().__init__(name="vbot-voice-mock", daemon=True)
        self._session = session
        self._model_id = model_id
        self._runtime = runtime
        self._events = events

    @override
    def run(self) -> None:
        engine = MockWakewordEngine([0.0] * 25 + [1.0], model_id=self._model_id)
        engine.start()
        try:
            while not self._session.stop_event.wait(self._runtime.mock_frame_seconds):
                if engine.detect(b"") is not None:
                    self._command()
        finally:
            engine.stop()

    def _command(self) -> None:
        recording = self._events._begin_mock(self._session, self._model_id)
        if recording is None:
            return
        for stage in ("transcribing", "sending", None):
            if self._session.stop_event.wait(self._runtime.mock_stage_seconds):
                return
            if not self._events._advance_mock(self._session, recording, stage):
                return
