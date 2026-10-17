"""Shared fixtures of the server tests."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _test_client_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``TestClient`` requests come from this computer by default.

    Its default peer ``testclient`` is no address, and the access guard refuses
    a peer it cannot place. Tests of other peers pass ``client=`` explicitly.
    """
    defaults = TestClient.__init__.__defaults__
    assert defaults is not None and defaults[-1] == ("testclient", 50000)
    monkeypatch.setattr(TestClient.__init__, "__defaults__", (*defaults[:-1], ("127.0.0.1", 50000)))
