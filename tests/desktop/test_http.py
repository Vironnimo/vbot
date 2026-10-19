"""Desktop HTTP verification and shared initialization contracts."""

from __future__ import annotations

import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from desktop import _http


def test_shared_context_keeps_verification_ignores_environment_and_builds_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "not-a-desktop-certificate-file")
    monkeypatch.setenv("SSL_CERT_DIR", "not-a-desktop-certificate-directory")
    monkeypatch.setattr(_http, "_context", None)
    create = Mock(wraps=httpx.create_ssl_context)
    monkeypatch.setattr(_http.httpx, "create_ssl_context", create)

    with ThreadPoolExecutor(max_workers=4) as workers:
        contexts = list(workers.map(lambda _: _http.shared_ssl_context(), range(8)))

    assert all(context is contexts[0] for context in contexts)
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname
    assert contexts[0].get_ca_certs()
    create.assert_called_once_with(trust_env=False)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30", 30.0),
        ("Mon, 19 Oct 2026 12:00:30 GMT", 30.0),
        ("Sun, 18 Oct 2026 12:00:00 GMT", 0.0),
        ("not-a-delay", None),
        ("NaN", None),
        ("Infinity", None),
        (None, None),
    ],
)
def test_retry_hint_keeps_the_servers_wait(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: float | None
) -> None:
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 19, 12, tzinfo=UTC)
    monkeypatch.setattr(_http, "datetime", clock)
    headers = httpx.Headers({} if value is None else {"Retry-After": value})

    assert _http.retry_after_seconds(headers) == expected
