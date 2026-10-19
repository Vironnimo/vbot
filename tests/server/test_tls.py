"""Direct TLS: HTTPS and loopback HTTP on one listener, and certificate reload.

These tests serve a real uvicorn listener on a loopback port: classifying a
connection by its first byte is a transport contract no in-process client reaches.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import os
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from core.access.config import AccessConfigError
from server.access import tls
from server.access.tls import TlsContextHolder, sniffing_protocol_class


def _write_certificate(directory: Path, serial: int) -> tuple[str, str]:
    """Write a self-signed certificate for localhost; a renewal moves the file times on."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    _move_file_times(cert_path, key_path, serial)
    return str(cert_path), str(key_path)


def _move_file_times(cert: Path, key: Path, step: int) -> None:
    stamp = 1_700_000_000_000_000_000 + step * 1_000_000_000
    for path in (cert, key):
        os.utime(path, ns=(stamp, stamp))


async def _scheme_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    body = scope["scheme"].encode()
    headers = [(b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def _serving(
    holder: TlsContextHolder, app: Any = _scheme_app
) -> AsyncIterator[tuple[uvicorn.Server, int]]:
    config = uvicorn.Config(
        app,
        http=sniffing_protocol_class(holder),
        lifespan="off",
        log_config=None,
        timeout_graceful_shutdown=30,
    )
    server = uvicorn.Server(config)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        await asyncio.sleep(0.005)
    try:
        yield server, port
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def _get(port: int, client_tls: ssl.SSLContext | None) -> tuple[str, int | None]:
    """One request; returns the scheme the app saw and the served certificate's serial."""
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        port,
        ssl=client_tls,
        server_hostname="localhost" if client_tls else None,
    )
    serial = None
    if client_tls is not None:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        serial = x509.load_der_x509_certificate(der).serial_number
    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    response = await reader.read()
    writer.close()
    return response.split(b"\r\n\r\n", 1)[1].decode(), serial


def _trusting(cert: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cert)
    return context


def test_one_listener_serves_https_and_plain_http_and_reloads_its_certificate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cert, key = _write_certificate(tmp_path, serial=1)
    renewed = tmp_path / "renewed.pem"
    holder = TlsContextHolder(cert, key)

    async def scenario() -> list[tuple[str, int | None]]:
        async with _serving(holder) as (_server, port):
            served = [await _get(port, None), await _get(port, _trusting(cert))]
            _write_certificate(tmp_path, serial=2)
            renewed.write_bytes(Path(cert).read_bytes())
            assert holder.reload_if_changed() is True
            assert holder.reload_if_changed() is False
            served.append(await _get(port, _trusting(cert)))
            # A broken renewal keeps serving the previous certificate.
            Path(cert).write_text("half written", encoding="utf-8")
            _move_file_times(Path(cert), Path(key), 3)
            assert holder.reload_if_changed() is False
            _move_file_times(Path(cert), Path(key), 4)
            assert holder.reload_if_changed() is False
            served.append(await _get(port, _trusting(str(renewed))))
            return served

    served = asyncio.run(scenario())

    # The guard sees the transport through the scheme: http only for plain connections.
    assert served == [("http", None), ("https", 1), ("https", 2), ("https", 2)]
    failures = [r for r in caplog.records if "TLS certificate reload failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].levelname == "WARNING"


def _blocking_tls_request(port: int, cert: str, path: str) -> ssl.SSLSocket:
    """Send one keep-alive request from a client that reads only when asked.

    Unlike an asyncio client it does not answer the server's close_notify by itself,
    just like an idle pooled connection of a blocking HTTP client.
    """
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    client = _trusting(cert).wrap_socket(raw, server_hostname="localhost")
    client.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    return client


def _read_until(client: ssl.SSLSocket, end: bytes = b"") -> bytes:
    """Read until the received bytes end with ``end``, or else until the close."""
    received = bytearray()
    while not (end and received.endswith(end)) and (chunk := client.recv(65536)):
        received += chunk
    return bytes(received)


def test_shutdown_finishes_responses_without_waiting_for_idle_connections(
    tmp_path: Path,
) -> None:
    cert, key = _write_certificate(tmp_path, serial=1)
    holder = TlsContextHolder(cert, key)
    body = b"x" * 1_000_000

    async def scenario() -> tuple[bytes, bytes, float]:
        entered, release = asyncio.Event(), asyncio.Event()

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["path"] != "/slow":
                await _scheme_app(scope, receive, send)
                return
            entered.set()
            await release.wait()
            headers = [(b"content-length", str(len(body)).encode())]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        loop = asyncio.get_running_loop()
        async with _serving(holder, app) as (server, port):
            _, silent = await asyncio.open_connection("127.0.0.1", port)
            idle = await asyncio.to_thread(_blocking_tls_request, port, cert, "/")
            assert await asyncio.to_thread(_read_until, idle, b"\r\n\r\nhttps")
            busy = await asyncio.to_thread(_blocking_tls_request, port, cert, "/slow")
            await entered.wait()
            stopping = loop.time()
            server.should_exit = True
            # The shutdown closes the idle connection while the request is in flight.
            idle_rest = await asyncio.to_thread(_read_until, idle)
            release.set()
            response = await asyncio.to_thread(_read_until, busy)
        stopped = loop.time() - stopping
        silent.close()
        idle.close()
        busy.close()
        return idle_rest, response, stopped

    idle_rest, response, stopped = asyncio.run(asyncio.wait_for(scenario(), 10))

    assert idle_rest == b""
    # Closing the socket at shutdown still writes the response the client has not read.
    assert response.split(b"\r\n\r\n", 1)[1] == body
    # A connection that never spoke and a client that never answers close_notify
    # would each hold uvicorn's graceful shutdown up for 30 s.
    assert stopped < 2


def test_a_missing_or_broken_certificate_refuses_the_start(tmp_path: Path) -> None:
    cert, key = _write_certificate(tmp_path, serial=1)
    Path(cert).write_text("not a certificate", encoding="utf-8")

    with pytest.raises(AccessConfigError, match="could not be loaded"):
        TlsContextHolder(cert, key)
    with pytest.raises(AccessConfigError, match="does not exist"):
        TlsContextHolder(str(tmp_path / "missing.pem"), key)


@pytest.mark.parametrize(("failure", "renew"), [("load", True), ("stat", True), ("stat", False)])
def test_certificate_reload_retries_after_io_recovery_without_another_file_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    renew: bool,
) -> None:
    cert, key = _write_certificate(tmp_path, serial=1)
    holder = TlsContextHolder(cert, key)
    original = holder.context
    if renew:
        _write_certificate(tmp_path, serial=2)
    with monkeypatch.context() as transient:
        if failure == "load":

            def unavailable(*_args: str) -> ssl.SSLContext:
                raise AccessConfigError("temporary file lock")

            transient.setattr(tls, "load_tls_context", unavailable)
        else:
            transient.setattr(holder, "_file_stamp", lambda: None)
        assert not holder.reload_if_changed()
        assert not holder.reload_if_changed()
        assert holder.context is original
    with caplog.at_level("INFO", logger="vbot.server.access"):
        assert holder.reload_if_changed() is renew
        assert not holder.reload_if_changed()
    assert (holder.context is not original) is renew
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    recoveries = [record for record in caplog.records if record.levelname == "INFO"]
    assert len(warnings) == len(recoveries) == 1
