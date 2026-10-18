"""Desktop target classifications, connection budgets and health identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from desktop import probe as desktop_probe
from desktop.probe import DesktopTarget


@dataclass
class FakeResponse:
    status_code: int
    payload: Any = None

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


PROBE_TARGET = DesktopTarget("vbot.lan", 9000, "http://vbot.lan:9000/")


HEALTH_URL = "http://vbot.lan:9000/health"


ROOT_URL = "http://vbot.lan:9000/"


STATUS_URL = "http://vbot.lan:9000/api/auth/status"


HEALTH_OK = FakeResponse(200, {"status": "ok"})


AUTHENTICATED = FakeResponse(200, {"mode": "authenticated", "authenticated": False})


ACCESS_DENIED = FakeResponse(
    403, {"ok": False, "error": {"code": "access_denied", "message": "Access denied"}}
)


AVAILABLE = desktop_probe.PROBE_WEBUI_AVAILABLE


UNAVAILABLE = desktop_probe.PROBE_WEBUI_UNAVAILABLE


NOT_VBOT = desktop_probe.PROBE_NOT_VBOT_SERVER


@pytest.mark.parametrize(
    ("health", "root", "auth_status", "status", "access_mode"),
    [
        pytest.param(HEALTH_OK, FakeResponse(200), AUTHENTICATED, AVAILABLE, "authenticated"),
        # A server from before access modes has no status route.
        pytest.param(HEALTH_OK, FakeResponse(399), FakeResponse(404), AVAILABLE, None),
        pytest.param(
            HEALTH_OK, FakeResponse(200), FakeResponse(200, {"mode": "x"}), AVAILABLE, None
        ),
        pytest.param(HEALTH_OK, FakeResponse(400), None, UNAVAILABLE, None),
        pytest.param(HEALTH_OK, FakeResponse(500), None, UNAVAILABLE, None),
        pytest.param(HEALTH_OK, httpx.ConnectError("closed"), None, UNAVAILABLE, None),
        pytest.param(
            httpx.ConnectError("refused"), None, None, desktop_probe.PROBE_SERVER_UNREACHABLE, None
        ),
        pytest.param(ACCESS_DENIED, None, None, desktop_probe.PROBE_ACCESS_DENIED, None),
        pytest.param(FakeResponse(403, {"status": "ok"}), None, None, NOT_VBOT, None),
        pytest.param(FakeResponse(503, {"status": "ok"}), None, None, NOT_VBOT, None),
        pytest.param(FakeResponse(200, {"status": "starting"}), None, None, NOT_VBOT, None),
        pytest.param(
            FakeResponse(200, {"status": "ok", "version": "dev"}), None, None, NOT_VBOT, None
        ),
        pytest.param(FakeResponse(200, ValueError("no json")), None, None, NOT_VBOT, None),
        pytest.param(FakeResponse(200, ["ok"]), None, None, NOT_VBOT, None),
    ],
    ids=[
        "webui-200-authenticated",
        "webui-399-without-access-modes",
        "webui-unknown-access-mode",
        "webui-400",
        "webui-500",
        "webui-request-error",
        "health-request-error",
        "health-access-denied",
        "health-403",
        "health-503",
        "health-starting",
        "health-extra-keys",
        "health-not-json",
        "health-not-object",
    ],
)
def test_probe_target_classifies_the_server_with_one_request_per_step(
    health: FakeResponse | httpx.RequestError,
    root: FakeResponse | httpx.RequestError | None,
    auth_status: FakeResponse | None,
    status: str,
    access_mode: str | None,
) -> None:
    replies = {HEALTH_URL: health, ROOT_URL: root, STATUS_URL: auth_status}
    requested: list[str] = []

    def get(url: str, *, timeout: httpx.Timeout | float, trust_env: bool) -> FakeResponse:
        assert (timeout, trust_env) == (desktop_probe.PROBE_TIMEOUT_SECONDS, False)
        requested.append(url)
        reply = replies[url]
        assert reply is not None, f"unexpected request: {url}"
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = desktop_probe.probe_target(PROBE_TARGET, get=get)

    assert (result.status, result.access_mode) == (status, access_mode)
    # No retry loop: /health once, the WebUI root once when /health is vBot's,
    # and the access status once when the WebUI is there.
    expected = [HEALTH_URL] if root is None else [HEALTH_URL, ROOT_URL]
    assert requested == (expected if auth_status is None else [*expected, STATUS_URL])


def test_probe_target_reports_a_configuration_error_without_a_request() -> None:
    def get(url: str, *, timeout: httpx.Timeout | float, trust_env: bool) -> FakeResponse:
        raise AssertionError(f"unexpected request: {url}")

    target = DesktopTarget("bad host", 8420, "", configuration_error="bad host")

    assert desktop_probe.probe_target(target, get=get).status == desktop_probe.PROBE_INVALID_TARGET


@pytest.mark.parametrize(
    ("root_fails", "host", "connect"),
    [
        pytest.param(False, "vbot.test", 0.75, id="remote"),
        pytest.param(True, "vbot.test", 0.75, id="remote-root-fails"),
        # A refused loopback connect waits out its whole budget on Windows (about 2 s).
        pytest.param(False, "127.0.0.1", 0.2, id="loopback-address"),
        pytest.param(False, "localhost", 0.2, id="localhost"),
    ],
)
def test_probe_reuses_one_unproxied_client_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, root_fails: bool, host: str, connect: float
) -> None:
    clients: list[httpx.Client] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if root_fails:
            raise httpx.ConnectError("test transport unavailable", request=request)
        return httpx.Response(200, text="test WebUI")

    class ProbeClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["verify"] is desktop_probe._http.shared_ssl_context()
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(self)

    monkeypatch.setattr(desktop_probe.httpx, "Client", ProbeClient)
    target = DesktopTarget(host, 8420, f"http://{host}:8420/")
    result = desktop_probe.probe_target(target, timeout=0.75)

    assert result.status == (
        desktop_probe.PROBE_WEBUI_UNAVAILABLE if root_fails else desktop_probe.PROBE_WEBUI_AVAILABLE
    )
    assert len(clients) == 1
    assert clients[0].is_closed
    assert [request.url.path for request in requests] == (
        ["/health", "/"] if root_fails else ["/health", "/", "/api/auth/status"]
    )
    assert all(
        request.extensions["timeout"]
        == {"connect": connect, "read": 0.75, "write": 0.75, "pool": 0.75}
        for request in requests
    )
