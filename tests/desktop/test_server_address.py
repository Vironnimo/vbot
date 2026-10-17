"""Server addresses the Desktop accepts: host and port, or an ``https://`` URL."""

from __future__ import annotations

import pytest

from desktop.server_address import (
    ServerAddress,
    parse_server_address,
    validate_host,
    validate_port,
)


@pytest.mark.parametrize(
    ("host", "port", "expected", "url", "origin", "bridge_host", "display"),
    [
        pytest.param(
            "127.0.0.1",
            8420,
            ServerAddress("http", "127.0.0.1", 8420),
            "http://127.0.0.1:8420/",
            "http://127.0.0.1:8420",
            "127.0.0.1",
            "127.0.0.1:8420",
            id="loopback",
        ),
        # HTTP hosts keep their spelling: stored servers and Voice profiles are keyed by it.
        pytest.param(
            " Pi.LAN ",
            "9000",
            ServerAddress("http", "Pi.LAN", 9000),
            "http://Pi.LAN:9000/",
            "http://pi.lan:9000",
            "Pi.LAN",
            "Pi.LAN:9000",
            id="lan",
        ),
        pytest.param(
            "https://VBOT.Example.com",
            None,
            ServerAddress("https", "vbot.example.com", 443),
            "https://vbot.example.com/",
            "https://vbot.example.com",
            "https://vbot.example.com",
            "https://vbot.example.com",
            id="https-default-port",
        ),
        # The URL's own port wins over the port argument.
        pytest.param(
            "HTTPS://vbot.example.com:8443/",
            8420,
            ServerAddress("https", "vbot.example.com", 8443),
            "https://vbot.example.com:8443/",
            "https://vbot.example.com:8443",
            "https://vbot.example.com:8443",
            "https://vbot.example.com:8443",
            id="https-own-port",
        ),
        pytest.param(
            "http://pi.lan:8420",
            1,
            ServerAddress("http", "pi.lan", 8420),
            "http://pi.lan:8420/",
            "http://pi.lan:8420",
            "pi.lan",
            "pi.lan:8420",
            id="http-url",
        ),
        pytest.param(
            "http://pi.lan",
            None,
            ServerAddress("http", "pi.lan", 80),
            "http://pi.lan:80/",
            "http://pi.lan",
            "pi.lan",
            "pi.lan:80",
            id="http-url-default-port",
        ),
    ],
)
def test_a_server_address_is_a_host_and_port_or_a_url(
    host: object,
    port: object,
    expected: ServerAddress,
    url: str,
    origin: str,
    bridge_host: str,
    display: str,
) -> None:
    address = parse_server_address(host, port)

    assert address == expected
    assert (address.url, address.origin, address.bridge_host, address.display) == (
        url,
        origin,
        bridge_host,
        display,
    )
    # What the bridge and ``--host`` carry parses back to the same server.
    assert parse_server_address(address.bridge_host, address.port) == address


@pytest.mark.parametrize(
    "host",
    [
        "ftp://vbot.example.com",
        "https://user:secret@vbot.example.com",
        "https://vbot.example.com/app",
        "https://vbot.example.com/?next=x",
        "https://vbot.example.com/#top",
        "https://vbot.example.com:99999",
        "https://vbot.example.com:0",
        "https://",
        "https://[::1]",
        "https://vbot example.com",
        "https://<script>",
    ],
)
def test_a_url_with_more_than_scheme_host_and_port_is_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        parse_server_address(host, 8420)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.8.0.1", True),
        ("LOCALHOST", True),
        ("192.168.1.44", False),
        ("vbot.example.com", False),
    ],
)
def test_loopback_hosts_are_recognized(host: str, loopback: bool) -> None:
    assert parse_server_address(host, 8420).is_loopback is loopback


@pytest.mark.parametrize("host", ["", "   ", "bad host", "host/path", "a<b", "pi.lan:8420", None])
def test_validate_host_rejects_non_host_values_naming_their_source(host: object) -> None:
    with pytest.raises(ValueError, match="^settings.host "):
        validate_host(host, source="settings.host")


@pytest.mark.parametrize("port", [0, 65536, "not-a-port", None])
def test_validate_port_rejects_out_of_range_and_non_numeric(port: object) -> None:
    with pytest.raises(ValueError):
        validate_port(port)
