"""Access configuration: the mode rules the server refuses to start without."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.access.config import (
    AccessConfigError,
    access_config,
    access_config_from_record,
    address_in,
    check_bind_host,
    default_host,
    inferred_access_mode,
    is_loopback_host,
    parse_ip,
)


@pytest.mark.parametrize(
    ("mode", "options", "refusal"),
    [
        ("open", {}, "Unknown access mode"),
        ("local", {"allow_networks": ["100.64.0.0/10"]}, "--allow-network applies only"),
        ("local", {"public_url": "https://vbot.example.org"}, "--public-url applies only"),
        ("lan", {"trusted_proxies": ["127.0.0.1"]}, "--trusted-proxy applies only"),
        ("lan", {"tls_cert": "cert.pem", "tls_key": "key.pem"}, "--tls-cert and --tls-key"),
        ("lan", {"allow_networks": ["10.0.0.5/8"]}, "needs an IP address or a CIDR"),
        ("lan", {"allow_networks": ["0.0.0.0/0"]}, "would cover every address"),
        ("authenticated", {"trusted_proxies": ["127.0.0.1"]}, "needs --public-url"),
        ("authenticated", {"public_url": "https://vbot.example.org"}, "needs HTTPS"),
        (
            "authenticated",
            {"public_url": "http://vbot.example.org", "trusted_proxies": ["127.0.0.1"]},
            "must start with https://",
        ),
        (
            "authenticated",
            {"public_url": "https://vbot.example.org/app", "trusted_proxies": ["127.0.0.1"]},
            "without path",
        ),
        (
            "authenticated",
            {"public_url": "https://user@vbot.example.org", "trusted_proxies": ["127.0.0.1"]},
            "without path, query or credentials",
        ),
        (
            "authenticated",
            {"public_url": "https://vbot.example.org", "tls_cert": "cert.pem"},
            "must be given together",
        ),
        (
            "authenticated",
            {
                "public_url": "https://vbot.example.org",
                "trusted_proxies": ["127.0.0.1"],
                "allow_networks": ["10.0.0.0/8"],
            },
            "--allow-network applies only",
        ),
        ("lan", {"allow_hosts": ["*.example.org"]}, "--allow-host needs a host name"),
        ("lan", {"allow_hosts": ["https://vbot.example.org"]}, "--allow-host needs"),
        ("local", {"allow_hosts": ["vbot.example.org:8420"]}, "without scheme, port"),
    ],
)
def test_access_config_refuses_combinations_the_mode_does_not_allow(
    mode: str, options: dict[str, Any], refusal: str
) -> None:
    with pytest.raises(AccessConfigError, match=refusal):
        access_config(mode, **options)


def test_access_config_normalizes_and_round_trips_through_the_record(tmp_path: Path) -> None:
    config = access_config(
        "authenticated",
        public_url="HTTPS://VBot.Example.org:443/",
        trusted_proxies=["127.0.0.1", "10.1.0.0/16", "127.0.0.1"],
        tls_cert=tmp_path / "cert.pem",
        tls_key=tmp_path / "key.pem",
        allow_hosts=["VBot.Home.Arpa.", "[::ffff:192.168.1.5]", "vbot.home.arpa"],
    )

    assert config.public_url == "https://vbot.example.org"
    assert config.public_origin() == ("https", "vbot.example.org", 443)
    assert config.trusted_proxies == ("127.0.0.1/32", "10.1.0.0/16")
    assert config.tls_cert == str((tmp_path / "cert.pem").resolve())
    assert config.allow_hosts == ("vbot.home.arpa", "192.168.1.5")
    assert access_config_from_record(config.to_record()) == config
    # Records written before --allow-host existed carry no list.
    record = access_config("local").to_record()
    del record["allow_hosts"]
    assert access_config_from_record(record) == access_config("local")
    assert access_config(
        "authenticated", public_url="https://[::1]:8443", trusted_proxies=["::1"]
    ).public_origin() == ("https", "::1", 8443)


@pytest.mark.parametrize(
    "record",
    [
        "local",
        {"mode": 1},
        {"mode": "lan", "allow_networks": "10.0.0.0/8"},
        {"mode": "authenticated", "public_url": "https://vbot.example.org"},
    ],
)
def test_access_config_from_record_refuses_malformed_or_invalid_records(record: Any) -> None:
    with pytest.raises(AccessConfigError):
        access_config_from_record(record)


@pytest.mark.parametrize(
    ("config", "host"),
    [
        (access_config("local"), "127.0.0.1"),
        (access_config("lan"), "0.0.0.0"),
        (
            access_config("authenticated", public_url="https://v.example", trusted_proxies=["::1"]),
            "127.0.0.1",
        ),
        (
            access_config(
                "authenticated", public_url="https://v.example", tls_cert="c.pem", tls_key="k.pem"
            ),
            "0.0.0.0",
        ),
    ],
)
def test_each_mode_has_its_default_bind_host(config: Any, host: str) -> None:
    assert default_host(config) == host


def test_local_mode_must_bind_loopback_and_launchers_without_a_mode_infer_it() -> None:
    for host in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"):
        assert is_loopback_host(host)
        assert inferred_access_mode(host) == "local"
        check_bind_host(access_config("local"), host)
    for host in ("0.0.0.0", "::", "192.168.1.5", "vbot.example.org"):
        assert inferred_access_mode(host) == "lan"
        with pytest.raises(AccessConfigError, match="must bind a loopback address"):
            check_bind_host(access_config("local"), host)
    check_bind_host(access_config("lan"), "0.0.0.0")


def test_lan_admits_private_networks_and_added_ones_only() -> None:
    networks = access_config("lan", allow_networks=["100.64.0.0/10"]).admitted_networks()
    assert networks is not None

    def admitted(text: str) -> bool:
        address = parse_ip(text)
        assert address is not None
        return address_in(address, networks)

    # A dual-stack socket reports IPv4 clients as IPv4-mapped IPv6 addresses.
    for client in (
        "127.0.0.1",
        "::1",
        "10.2.3.4",
        "172.31.0.1",
        "192.168.178.20",
        "169.254.1.1",
        "fe80::1%eth0",
        "fd12::1",
        "::ffff:192.168.1.9",
        "100.100.1.1",
    ):
        assert admitted(client), client
    for client in ("8.8.8.8", "172.32.0.1", "2001:db8::1", "::ffff:8.8.8.8"):
        assert not admitted(client), client
    assert access_config("local").admitted_networks() is not None
    assert parse_ip("testclient") is None
