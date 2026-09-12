"""Status decoration never compromises captured output or scope cleanup."""

import io
import threading

import pytest

from cli import _progress
from cli._progress import ProgressPrinter, status_line


class Terminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("status", ["busy", "success", "warning", "error", "info"])
def test_status_keeps_payload_and_plain_capture(status, monkeypatch):
    monkeypatch.setattr(_progress, "_windows_color", lambda stream: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    payload = "resource: provider:connection:account"
    plain = status_line(status, payload, stream=io.StringIO())
    colored = status_line(status, payload, stream=Terminal())
    assert plain.endswith(payload)
    assert colored.endswith(payload)
    assert "\033[" not in plain
    assert "\033[" in colored
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\033[" not in status_line(status, payload, stream=Terminal())
    monkeypatch.setenv("TERM", "dumb")
    assert status_line(status, payload, stream=Terminal()) == plain


def test_progress_flushes_before_work_and_stops_on_exception():
    heartbeat = threading.Event()

    class Output(io.StringIO):
        flushes = 0

        def flush(self):
            self.flushes += 1
            if self.flushes >= 2:
                heartbeat.set()

    output = Output()
    progress = ProgressPrinter(interval=0.01, stream=output)
    with pytest.raises(ValueError), progress:
        progress.emit("busy", "test-owned phase")
        assert output.flushes == 1
        assert heartbeat.wait(2)
        raise ValueError("test-owned failure")
    assert not progress._thread.is_alive()
    assert progress.messages == {"test-owned phase"}
    assert "\r" not in output.getvalue()


def test_info_preserves_active_phase_and_completion_clears_it():
    with ProgressPrinter(stream=io.StringIO()) as progress:
        progress.emit("busy", "phase")
        progress.emit("info", "detail\ncontinuation")
        assert progress._active is not None
        progress.emit("success", "done")
        assert progress._active is None
    assert progress.messages == {"phase", "detail", "continuation", "done"}
