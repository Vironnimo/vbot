"""Runtime double for server app lifespan and HTTP edge tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.server.rpc_test_support import StubAdapter, StubRuntime


class ServerStubRuntime(StubRuntime):
    """``StubRuntime`` that records the app lifespan's stop and bootstrap calls.

    Tests attach the service an endpoint reads (``speech``, ``attachment_store``,
    ...) as keyword arguments, replacing the stub's own where it has one.
    """

    def __init__(self, data_dir: Path, **services: Any) -> None:
        super().__init__(data_dir, StubAdapter())
        self.bootstrap_activated = False
        self.stopped = False
        for name, service in services.items():
            setattr(self, name, service)

    def stop(self) -> None:
        self.stopped = True

    def activate_bootstrap(self) -> None:
        self.bootstrap_activated = True
