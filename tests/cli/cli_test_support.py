"""Shared fakes for CLI command tests: a server target and a scripted RPC endpoint."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from cli.server_management import ServerInstance, build_server_base_url
from core.utils.logging import resolve_daily_log_path

# Runs one ``vbot`` command line against the test target: (exit code, stdout, stderr).
RunCli = Callable[..., tuple[int, str, str]]


def make_instance(tmp_path: Path, *, host: str = "127.0.0.1", port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host=host,
        port=port,
        data_dir=data_dir,
        url=build_server_base_url(host, port),
        log_path=resolve_daily_log_path(data_dir),
    )


class FakeRpc:
    """Answers the CLI's RPC posts from scripted replies and records every call.

    Each method answers with its queued replies in order; the last reply repeats. A call to a
    method without a reply fails the test, so every RPC a command sends is accounted for.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[Any] = []
        self._replies: dict[str, list[httpx.Response]] = {}

    def reply(self, method: str, result: dict[str, Any] | None = None) -> FakeRpc:
        response = httpx.Response(200, json={"ok": True, "result": result or {}})
        self._replies.setdefault(method, []).append(response)
        return self

    def fail(self, method: str, code: str, message: str, *, status: int = 400) -> FakeRpc:
        error = {"code": code, "message": message}
        response = httpx.Response(status, json={"ok": False, "error": error})
        self._replies.setdefault(method, []).append(response)
        return self

    def respond(self, method: str, body: Any, *, status: int = 200) -> FakeRpc:
        """Queue a raw response body, for malformed or transport-level replies."""
        self._replies.setdefault(method, []).append(httpx.Response(status, json=body))
        return self

    def post(
        self, url: str, *, json: dict[str, Any], timeout: Any, trust_env: bool
    ) -> httpx.Response:
        assert trust_env is False, "CLI RPC must ignore ambient proxy settings"
        method = json["method"]
        self.calls.append((method, json["params"]))
        self.timeouts.append(timeout)
        replies = self._replies.get(method)
        if not replies:
            raise AssertionError(f"unexpected RPC {method} {json['params']}")
        return replies.pop(0) if len(replies) > 1 else replies[0]

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]

    def params(self, method: str) -> dict[str, Any]:
        """Return the params of the only call to ``method``."""
        matching = [params for called, params in self.calls if called == method]
        assert len(matching) == 1, (method, self.calls)
        return matching[0]
