"""Desktop HTTP verification and shared initialization contracts."""

from __future__ import annotations

import ssl
from concurrent.futures import ThreadPoolExecutor
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
