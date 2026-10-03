"""The ``librarian.*`` RPC edge: parameters, results and refusal codes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.agents import AgentNotFoundError
from core.automation import LibrarianBusyError, LibrarianUnavailableError
from core.automation.librarian import LibrarianStateError
from server.rpc.methods import dispatch_rpc


class _FakeLibrarian:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def status(self, agent_id: str) -> dict[str, Any]:
        return await self._answer("status", agent_id)

    async def run(self, agent_id: str) -> dict[str, Any]:
        return await self._answer("run", agent_id)

    async def overview(self) -> dict[str, Any]:
        return await self._answer("overview", "librarian")

    async def _answer(self, method: str, agent_id: str) -> dict[str, Any]:
        self.calls.append((method, agent_id))
        if self.error is not None:
            raise self.error
        return {"agent_id": agent_id, "running": method == "run"}


def _state(librarian: _FakeLibrarian) -> Any:
    return SimpleNamespace(runtime=SimpleNamespace(librarian=librarian))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "agent_id"),
    [
        ("status", {"agent_id": "main"}, "main"),
        ("run", {"agent_id": "main"}, "main"),
        # Every Agent's passes at once, with the Librarian's availability.
        ("overview", {}, "librarian"),
    ],
)
async def test_librarian_methods_answer(method: str, params: dict[str, Any], agent_id: str) -> None:
    librarian = _FakeLibrarian()

    response = await dispatch_rpc(
        _state(librarian), {"method": f"librarian.{method}", "params": params}
    )

    assert response == {"ok": True, "result": {"agent_id": agent_id, "running": method == "run"}}
    assert librarian.calls == [(method, agent_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "error", "code"),
    [
        ("status", {}, None, "invalid_request"),
        ("run", {"agent_id": "main", "force": True}, None, "invalid_request"),
        ("overview", {"agent_id": "main"}, None, "invalid_request"),
        (
            "status",
            {"agent_id": "ghost"},
            AgentNotFoundError("Agent not found: ghost"),
            "agent_not_found",
        ),
        ("run", {"agent_id": "main"}, LibrarianBusyError("A pass is running."), "agent_busy"),
        (
            "run",
            {"agent_id": "main"},
            LibrarianUnavailableError("Agent main has no Skills of its own."),
            "invalid_request",
        ),
        (
            "status",
            {"agent_id": "librarian"},
            LibrarianUnavailableError("The Librarian gets no pass itself."),
            "invalid_request",
        ),
        ("status", {"agent_id": "main"}, LibrarianStateError("unreadable"), "domain_error"),
    ],
)
async def test_librarian_methods_refuse_with_a_stable_code(
    method: str, params: dict[str, Any], error: Exception | None, code: str
) -> None:
    response = await dispatch_rpc(
        _state(_FakeLibrarian(error)), {"method": f"librarian.{method}", "params": params}
    )

    assert response["ok"] is False
    assert response["error"]["code"] == code
