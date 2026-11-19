"""Interpreter selection and bounded output for Python child processes."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


def python_executable() -> str:
    """Use CPython itself when a packaged Windows role hosts this process."""
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.name.casefold() in {
        "vbot.server.exe",
        "vbot.desktop.exe",
        "vbot.update.exe",
        "vbot.python.exe",
    }:
        return str(executable.with_name("python.exe"))
    return sys.executable


@dataclass
class OutputCapture:
    """Retain both ends of CLI output and report how many bytes were omitted."""

    limit: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total: int = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        missing = max(0, self.limit // 2 - len(self.head))
        self.head.extend(chunk[:missing])
        self.tail.extend(chunk[missing:])
        if len(self.tail) > self.limit - len(self.head):
            del self.tail[: len(self.tail) - (self.limit - len(self.head))]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def text(self) -> str:
        marker = (
            f"\n[... {self.total - self.limit} output bytes omitted ...]\n".encode()
            if self.truncated
            else b""
        )
        return (bytes(self.head) + marker + bytes(self.tail)).decode("utf-8", errors="replace")
