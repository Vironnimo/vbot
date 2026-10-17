"""The local access token file: published per server start, removed only by its writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.access.local_token import (
    LocalAccess,
    create_local_access_token,
    local_token_path,
    loopback_addresses,
    publish_local_access_token,
    read_local_access,
    read_local_access_token,
    remove_local_access_token,
)


def test_each_start_publishes_a_fresh_token_that_only_its_writer_removes(tmp_path: Path) -> None:
    first = create_local_access_token(tmp_path, 8420)
    publish_local_access_token(first, ["127.0.0.1"])
    second = create_local_access_token(tmp_path, 8420)

    # A new start drops the earlier server's file until it listens itself.
    assert read_local_access_token(tmp_path, 8420) is None
    publish_local_access_token(second, ["127.0.0.1"])
    assert first.path == local_token_path(tmp_path, 8420)
    assert first.path == tmp_path.resolve() / "runtime" / "local-access-8420.token"
    assert first.value != second.value
    assert read_local_access(tmp_path, 8420) == LocalAccess(second.value, 8420, ("127.0.0.1",))
    assert read_local_access_token(tmp_path, 8421) is None

    # An earlier server's exit leaves the newer server's token in place.
    remove_local_access_token(first)
    assert read_local_access_token(tmp_path, 8420) == second.value
    remove_local_access_token(second)
    assert not second.path.exists()


@pytest.mark.parametrize(
    ("bound", "loopback"),
    [
        pytest.param(["127.0.0.1"], ("127.0.0.1",), id="ipv4-loopback"),
        pytest.param(["0.0.0.0"], ("127.0.0.1",), id="ipv4-wildcard"),
        pytest.param(["::"], ("::1",), id="ipv6-wildcard"),
        pytest.param(["::1", "127.0.0.1", "::1"], ("::1", "127.0.0.1"), id="both"),
        pytest.param(["192.168.1.5"], (), id="lan-address-only"),
    ],
)
def test_the_file_names_the_loopback_addresses_the_server_holds(
    bound: list[str], loopback: tuple[str, ...]
) -> None:
    assert loopback_addresses(bound) == loopback


@pytest.mark.parametrize(
    ("url", "admitted"),
    [
        pytest.param("http://127.0.0.1:8420", True, id="bound-literal"),
        pytest.param("http://127.0.0.1:8420/api/rpc", True, id="bound-literal-path"),
        # A name may resolve to an address another program holds (::1 first).
        pytest.param("http://localhost:8420", False, id="name"),
        pytest.param("http://[::1]:8420", False, id="unbound-ipv6-loopback"),
        pytest.param("http://127.0.0.2:8420", False, id="unbound-ipv4-loopback"),
        pytest.param("http://[::ffff:127.0.0.1]:8420", False, id="mapped-spelling"),
        pytest.param("http://127.0.0.1:8421", False, id="other-port"),
        pytest.param("https://127.0.0.1:8420", False, id="https"),
        pytest.param("http://192.168.1.5:8420", False, id="lan"),
    ],
)
def test_the_token_goes_only_to_a_bound_loopback_literal(url: str, admitted: bool) -> None:
    assert LocalAccess("vbl_" + "A" * 43, 8420, ("127.0.0.1",)).admits(url) is admitted


def test_a_malformed_or_legacy_token_file_holds_no_token(tmp_path: Path) -> None:
    path = local_token_path(tmp_path, 8420)
    path.parent.mkdir(parents=True)
    token = "vbl_" + "A" * 43
    for content in (
        "",
        "vbl_short",
        "x" * 2000,
        # The plain format of earlier versions names no address to send it to.
        token,
        json.dumps({"token": "vbt_" + "A" * 43, "loopback": ["127.0.0.1"]}),
        json.dumps({"token": token}),
        json.dumps({"token": token, "loopback": "127.0.0.1"}),
    ):
        path.write_text(content, encoding="ascii")
        assert read_local_access(tmp_path, 8420) is None
    path.write_text(json.dumps({"token": token, "loopback": ["evil.example", "::1"]}))
    assert read_local_access(tmp_path, 8420) == LocalAccess(token, 8420, ("::1",))
