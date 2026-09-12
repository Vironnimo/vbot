"""Web fetch: transport behavior."""

from __future__ import annotations

import pytest
from curl_cffi import CurlOpt

import core.tools.web_fetch as web_fetch_module
from tests.core.tools.web_fetch_helpers import (
    install_http_get,
    make_result,
)
from tests.core.tools.web_fetch_helpers import (
    stub_dns_resolution as stub_dns_resolution,
)
from tests.core.tools.web_fetch_helpers import (
    stub_http_session as stub_http_session,
)


@pytest.mark.asyncio
async def test_validate_public_target_returns_resolved_ip() -> None:
    host, pinned = await web_fetch_module._validate_public_target("https", "example.com", 443)

    assert host == "example.com"
    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_validate_public_target_returns_literal_ip() -> None:
    host, pinned = await web_fetch_module._validate_public_target("https", "93.184.216.34", 443)

    assert host == "93.184.216.34"
    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_fetch_with_retry_pins_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.com/page"

    install_http_get(monkeypatch, lambda _url: make_result(status_code=200, text="ok", url=url))

    resolve_map: dict[tuple[str, int], str] = {}
    async with web_fetch_module._make_session() as session:
        result = await web_fetch_module._fetch_with_retry(session, url, resolve_map)

        # The validated IP is both recorded and handed to curl's RESOLVE map so
        # the connection targets exactly the address that cleared validation.
        assert resolve_map[("example.com", 443)] == "93.184.216.34"
        assert session.curl_options[CurlOpt.RESOLVE] == ["example.com:443:93.184.216.34"]

    assert result.status_code == 200


@pytest.mark.asyncio
async def test_fetch_with_retry_brackets_ipv6_resolve_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/page"

    async def validate_ipv6_target(_scheme: str, host: str | None, _port: int) -> tuple[str, str]:
        assert host == "example.com"
        return "example.com", "2606:2800:220:1:248:1893:25c8:1946"

    monkeypatch.setattr(web_fetch_module, "_validate_public_target", validate_ipv6_target)
    install_http_get(monkeypatch, lambda _url: make_result(status_code=200, text="ok", url=url))

    resolve_map: dict[tuple[str, int], str] = {}
    async with web_fetch_module._make_session() as session:
        await web_fetch_module._fetch_with_retry(session, url, resolve_map)

        assert session.curl_options[CurlOpt.RESOLVE] == [
            "example.com:443:[2606:2800:220:1:248:1893:25c8:1946]"
        ]
