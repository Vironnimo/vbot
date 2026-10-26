"""A Desktop PTY behind the existing server-owned Terminal Session."""

from __future__ import annotations

import asyncio
import base64
import codecs
import threading
from typing import TYPE_CHECKING, Any

from core.computers import ComputerError

from .process_tree import ProcessTreeFacts, RunningProcess

if TYPE_CHECKING:
    from core.computers import ComputerRegistry

_RECONNECT_ERRORS = {
    "computer_connection_lost",
    "computer_offline",
    "computer_timeout",
    "computer_busy",
    "computer_closing",
}
# The Desktop no longer shares terminals with this server: closing that
# capability ended its processes, so these answers are a definite end.
_ENDED_ERRORS = {"computer_capability_not_shared", "computer_capability_unavailable"}


class RemoteStopError(OSError):
    """A remote stop that did not reach or was not confirmed by the Desktop."""


class RemoteTerminalAdapter:
    """Blocking adapter; all process operations stay on its original computer."""

    def __init__(
        self,
        computers: ComputerRegistry,
        computer_id: str,
        instance_id: str,
        result: dict[str, Any],
    ) -> None:
        self._computers = computers
        self.computer_id = computer_id
        self.instance_id = instance_id
        self._process_id = str(result["process_id"])
        self._pid = int(result["pid"])
        self.metadata = dict(result)
        self.launch_pending = result.get("launch_pending") is True
        self._offset = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self._alive = True
        self._exit_code: int | None = None
        self._eof = False
        self._read_error: str | None = None
        self._closed = threading.Event()
        self._release_task: asyncio.Task[None] | None = None
        self._unsubscribe = self._computers.subscribe(self._connection_changed)

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def ended(self) -> bool:
        """Whether the Desktop confirmed that no process of this terminal runs."""
        return not self._alive

    @property
    def online(self) -> bool:
        try:
            info = self._computers.get(self.computer_id)
        except ComputerError:
            return False
        return info.online and info.instance_id == self.instance_id

    def _same_instance(self) -> bool:
        try:
            return self._computers.get(self.computer_id).instance_id == self.instance_id
        except Exception:
            return False

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        return self._computers.call_sync(
            self.computer_id,
            "terminal",
            operation,
            {"process_id": self._process_id, **params},
            timeout=30,
            instance_id=self.instance_id,
        )

    def read(self, size: int) -> str:
        if self._closed.is_set() or not self._same_instance():
            self._alive = False
            raise EOFError
        if self._eof:
            if self._read_error:
                raise RuntimeError(
                    f"The Desktop could not read terminal output: {self._read_error}"
                )
            raise EOFError
        if not self.online:
            self._closed.wait(0.25)
            raise TimeoutError
        try:
            result = self._call("read", offset=self._offset, size=min(size, 64 * 1024))
        except ComputerError as error:
            if error.code in _ENDED_ERRORS:
                self._alive = False
                raise EOFError from None
            # Reads have no effect and resume at their last received byte offset.
            # Never use this recovery path for a start, write, resize or kill.
            if error.code in _RECONNECT_ERRORS or not self.online:
                self._closed.wait(0.25)
                raise TimeoutError from None
            raise
        offset = result.get("offset")
        if isinstance(result.get("process"), dict):
            self.metadata.update(result["process"])
            self._pid = int(self.metadata.get("pid", self._pid))
            self.launch_pending = False
        next_offset = result.get("next_offset")
        if offset != self._offset or not isinstance(next_offset, int):
            raise RuntimeError("The Desktop returned an invalid terminal output offset")
        data = base64.b64decode(str(result.get("output", "")), validate=True)
        if next_offset != offset + len(data):
            raise RuntimeError("The Desktop returned an incomplete terminal output range")
        self._offset = next_offset
        self._eof = result.get("eof") is True
        self._read_error = str(result["error"]) if result.get("error") else None
        if result.get("missing") is True and self.launch_pending:
            self.launch_pending = False
            self._alive = False
            self._read_error = "The Desktop has no process for this unconfirmed launch"
        text = self._decoder.decode(data, final=self._eof)
        if text:
            return text
        if self._eof:
            if self._read_error:
                raise RuntimeError(
                    f"The Desktop could not read terminal output: {self._read_error}"
                )
            raise EOFError
        raise TimeoutError

    def write(self, text: str) -> None:
        self._call("write", data=text)

    def resize(self, rows: int, columns: int) -> None:
        self._call("resize", rows=rows, columns=columns)

    def status(self) -> dict[str, Any]:
        if not self._same_instance():
            self._alive = False
            return {"alive": False, "exit_code": self._exit_code}
        if self._closed.is_set() or not self.online:
            return {"alive": self._alive, "exit_code": self._exit_code}
        try:
            result = self._call("status")
        except ComputerError as error:
            if error.code in _ENDED_ERRORS:
                self._alive = False
            if error.code in _RECONNECT_ERRORS | _ENDED_ERRORS:
                return {"alive": self._alive, "exit_code": self._exit_code}
            raise
        if isinstance(result.get("process"), dict):
            self.metadata.update(result["process"])
            self._pid = int(self.metadata.get("pid", self._pid))
            self.launch_pending = False
        self._alive = result.get("alive") is True
        if result.get("missing") is True:
            self.launch_pending = False
        code = result.get("exit_code")
        self._exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
        return result

    def is_alive(self) -> bool:
        return self.status().get("alive") is True

    def exit_code(self) -> int | None:
        self.status()
        return self._exit_code

    def terminate(self) -> None:
        if not self._same_instance():
            self._alive = False
            return
        if not self.online:
            raise RemoteStopError(
                "its computer is offline, so the stop did not reach it. Its programs can "
                "still be running there; stop the terminal again once that computer is connected"
            )
        try:
            result = self._call("kill")
        except ComputerError as error:
            if error.code in _ENDED_ERRORS:
                self._alive = False
                return
            raise RemoteStopError(
                "its computer did not confirm the stop, so its programs can still be running "
                "there; check the terminal's status before stopping it again"
            ) from error
        except Exception as error:
            raise OSError("The Desktop did not confirm that its terminal stopped") from error
        if result.get("alive") is not False:
            raise OSError("The Desktop terminal is still running")
        self._alive = False
        code = result.get("exit_code")
        self._exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if not self._alive and self.online:
            try:
                self._call("release")
            except ComputerError:
                # The process is already dead. A lost cleanup reply must not
                # prevent the server from recording its completed outcome.
                pass
            else:
                self._unsubscribe()
        elif not self._same_instance():
            self._unsubscribe()

    def _connection_changed(self, computer_id: str, event: str, _data: dict[str, Any]) -> None:
        """Retry only cleanup of an ended process, pinned to its native instance."""
        if computer_id != self.computer_id or event != "connected":
            return
        if not self._same_instance():
            self._unsubscribe()
            return
        if self._closed.is_set() and not self._alive and self._release_task is None:
            self._release_task = asyncio.create_task(self._release_after_reconnect())

    async def _release_after_reconnect(self) -> None:
        try:
            await self._computers.call(
                self.computer_id,
                "terminal",
                "release",
                {"process_id": self._process_id},
                instance_id=self.instance_id,
            )
        except ComputerError:
            pass
        else:
            self._unsubscribe()
        finally:
            self._release_task = None

    def detach(self) -> None:
        """Stop local reads on server shutdown without a blocking network call."""
        self._closed.set()
        self._unsubscribe()

    def program_runs(self, _pid: int, program: str) -> bool:
        if not self.online:
            return False
        try:
            return self._call("program", program=program).get("running") is True
        except ComputerError:
            return False


class RemoteTerminalTree:
    """Process observations from the Desktop, with no local PID interpretation."""

    def __init__(self, adapter: RemoteTerminalAdapter) -> None:
        self._adapter = adapter

    def facts(self) -> ProcessTreeFacts:
        if not self._adapter.online:
            raise OSError("The terminal's computer is not connected")
        value = self._adapter.status()
        if "processes" not in value:
            raise OSError("The Desktop cannot observe the terminal's processes")
        processes = value.get("processes", [])
        return ProcessTreeFacts(
            running=tuple(
                RunningProcess(int(item["pid"]), str(item["name"]))
                for item in processes
                if isinstance(item, dict)
            ),
            cpu_seconds=float(value.get("cpu_seconds", 0)),
            started=int(value.get("started", 0)),
            nonzero_exits=(),
        )

    def exit_count(self) -> int:
        return 0

    def terminate(self) -> None:
        self._adapter.terminate()

    def close(self) -> None:
        """The adapter owns the native process and its observer."""
