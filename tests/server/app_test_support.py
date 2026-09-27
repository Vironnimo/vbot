"""Smallest runtime surface the server app lifespan reads, for HTTP edge tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.runs import ChatRunManager


class ServerStubRuntime:
    """Runtime double without Agents, Sessions or Providers.

    Tests attach the one service an endpoint reads (``speech``,
    ``attachment_store``, ...) as keyword arguments.
    """

    def __init__(self, data_dir: Path, **services: Any) -> None:
        self.storage = SimpleNamespace(data_dir=data_dir)
        self.chat_run_manager = ChatRunManager()
        self.chat_runs = self.chat_run_manager
        self.chat_loop = object()
        self.streaming_chat_loop = object()
        self.command_dispatcher = object()
        self.bootstrap_activated = False
        self.stopped = False
        for name, service in services.items():
            setattr(self, name, service)

    def start(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        self.stopped = True

    def activate_bootstrap(self) -> None:
        self.bootstrap_activated = True
