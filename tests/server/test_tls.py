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
async def _serving(holder: TlsContextHolder) -> AsyncIterator[tuple[uvicorn.Server, int]]:
    config = uvicorn.Config(
        _scheme_app,
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


def test_a_connection_that_never_speaks_does_not_hold_up_shutdown(tmp_path: Path) -> None:
    holder = TlsContextHolder(*_write_certificate(tmp_path, serial=1))

    async def scenario() -> bytes:
        async with _serving(holder) as (server, port):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            server.should_exit = True
            try:
                return await asyncio.wait_for(reader.read(), 5)
            except ConnectionResetError:
                return b""
            finally:
                writer.close()

    # uvicorn's graceful shutdown would wait 30 s for a connection it could not close.
    assert asyncio.run(asyncio.wait_for(scenario(), 10)) == b""


def test_a_missing_or_broken_certificate_refuses_the_start(tmp_path: Path) -> None:
    cert, key = _write_certificate(tmp_path, serial=1)
    Path(cert).write_text("not a certificate", encoding="utf-8")

    with pytest.raises(AccessConfigError, match="could not be loaded"):
        TlsContextHolder(cert, key)
    with pytest.raises(AccessConfigError, match="does not exist"):
        TlsContextHolder(str(tmp_path / "missing.pem"), key)
