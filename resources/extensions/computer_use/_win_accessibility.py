"""Bounded UI Automation RPC to an owned, disposable native worker.

An application's accessibility provider can hang in COM. Keeping those calls
outside the desktop worker makes Stop and shutdown bounded even in that case.
The pipe is private, requests are serialized, and mutations are never replayed.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.utils.processes import subprocess_creation_flags

from .target import InputInterrupted, TargetError

_MAX_MESSAGE = 512_000
_TIMEOUT = 8.0


class AccessibilityClient:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self._process: subprocess.Popen[str] | None = None
        self._replies: queue.Queue[str] = queue.Queue(maxsize=2)
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None:
            return self._process
        self._replies = queue.Queue(maxsize=2)
        replies = self._replies
        executable = Path(sys.executable)
        if sys.platform == "win32" and executable.name.casefold() in {
            "vbot.server.exe",
            "vbot.desktop.exe",
        }:
            # Role hosts prepend their app entrypoint to script/flag arguments.
            # The generic host keeps this version's dependency paths without
            # starting a second server or Desktop.
            executable = executable.with_name("vBot.Python.exe")
        process = subprocess.Popen(
            [
                str(executable),
                "-E",
                "-P",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).with_name("_uia_worker.py")),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=subprocess_creation_flags(),
        )
        self._process = process

        def read() -> None:
            assert process.stdout is not None
            try:
                while line := process.stdout.readline(_MAX_MESSAGE + 1):
                    replies.put_nowait(line)
            except OSError, ValueError, queue.Full:
                pass
            finally:
                with suppress(queue.Full):
                    replies.put_nowait("")

        self._reader = threading.Thread(target=read, name="computer-use-uia", daemon=True)
        self._reader.start()
        return process

    def call(self, operation: str, **params: Any) -> dict[str, Any]:
        if self.stop.is_set():
            raise InputInterrupted()
        request = json.dumps({"operation": operation, **params}, ensure_ascii=True) + "\n"
        if len(request) > _MAX_MESSAGE:
            raise TargetError(
                "The accessibility request is too large. Nothing was done.", "invalid_arguments"
            )
        try:
            deadline = time.monotonic() + _TIMEOUT
            process = self._start()
            replies = self._replies

            def write() -> None:
                assert process.stdin is not None
                try:
                    process.stdin.write(request)
                    process.stdin.flush()
                except OSError, ValueError:
                    with suppress(queue.Full):
                        replies.put_nowait("")

            # A full pipe can block too, before COM returns any result. The same
            # deadline and Stop handling therefore cover sending the request.
            self._writer = threading.Thread(target=write, name="computer-use-uia-send", daemon=True)
            self._writer.start()
            while True:
                if self.stop.is_set():
                    self.close()
                    raise InputInterrupted()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    line = self._replies.get(timeout=min(0.05, remaining))
                    break
                except queue.Empty:
                    continue
            if not line or len(line) > _MAX_MESSAGE:
                raise ValueError("Invalid accessibility reply")
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError("Invalid accessibility reply")
        except OSError, ValueError, TimeoutError:
            self.close()
            effect = (
                "Its effect is unknown; check the screenshot before repeating it."
                if operation == "act"
                else "No input was sent."
            )
            raise TargetError(
                f"UI Automation did not return usable state. {effect} "
                "Use screenshots or inspect the window again.",
                "computer_use_failed" if operation == "act" else "accessibility_unavailable",
            ) from None
        if "error" in result:
            raise TargetError(str(result["error"]), str(result.get("code", "computer_use_failed")))
        if self._writer is not None:
            self._writer.join(timeout=1)
            self._writer = None
        return result

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        # The worker launches no children; a Windows venv redirector owns its
        # interpreter's lifetime. Kill bounds cleanup during a blocked COM call.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if self._reader is not None:
            self._reader.join(timeout=1)
            self._reader = None
        if self._writer is not None:
            self._writer.join(timeout=1)
            self._writer = None
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
