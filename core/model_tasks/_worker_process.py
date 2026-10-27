"""JSON-line transport for local Model worker children.

Task adapters own reply semantics and scheduling; this transport owns the child,
ASCII request lines, exchange deadlines and process-tree cleanup.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Generator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import Lock, Timer
from typing import Any


class WorkerStoppedError(RuntimeError):
    """An exchange reached EOF, possibly because its owner ended the child."""

    def __init__(self, reason: str) -> None:
        super().__init__("Local Model worker exited")
        self.reason = reason


class JsonLineWorker:
    """One isolated child with blocking, caller-serialized JSON-line exchanges."""

    def __init__(
        self,
        python: Path,
        script: Path,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        from core.utils.processes import subprocess_creation_flags

        self._ending = Lock()
        self._ended = ""
        self._process = subprocess.Popen(
            [str(python), "-I", "-B", str(script), *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=subprocess_creation_flags(),
            start_new_session=os.name != "nt",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "TOKENIZERS_PARALLELISM": "false",
                **(environment or {}),
            },
        )

    def exchange(
        self, request: Mapping[str, Any], deadline: float, *, read_limit: int = -1
    ) -> Generator[dict[str, Any]]:
        """Yield reply frames; close the iterator once the adapter has its answer.

        ASCII JSON also works when an isolated child reads its console code page.
        Ending the child from another thread releases a blocked pipe read; the
        first ending reason survives subsequent cancellation or cleanup.
        """
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        timer = Timer(deadline, self.end, ("deadline",))
        timer.daemon = True
        timer.start()
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            while line := process.stdout.readline(read_limit):
                yield json.loads(line)
        except OSError, ValueError:
            if not self._ended:
                raise
        finally:
            timer.cancel()
        raise WorkerStoppedError(self._ended)

    def end(self, reason: str) -> None:
        """End the process tree once, retaining why the first owner ended it."""
        from core.utils.processes import kill_process_tree

        with self._ending:
            self._ended = self._ended or reason
            process = self._process
            if process.poll() is None:
                try:
                    kill_process_tree(process)
                except OSError:
                    with suppress(OSError):
                        process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)

    def close(self) -> None:
        self.end("closed")
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                with suppress(OSError):
                    stream.close()
