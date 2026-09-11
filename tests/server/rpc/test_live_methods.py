from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.providers.errors import ProviderAuthError, ProviderOutcomeUnknownError
from server.rpc.live_methods import _create, _status
from server.rpc.methods import build_method_handlers


def state(configured=True):
    return SimpleNamespace(
        runtime=SimpleNamespace(
            provider_credentials=SimpleNamespace(is_usable=Mock(return_value=configured))
        )
    )


@pytest.mark.asyncio
async def test_missing_key_prevents_provider_request(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("server.rpc.live_methods.LiveClient.from_runtime", factory)
    assert await _create(state(False), {"sdp": "v=0"}) == {"error": "api_key_required"}
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (ProviderAuthError("private"), "access_denied"),
        (ProviderOutcomeUnknownError("private", operation_key="test"), "outcome_unknown"),
    ],
)
async def test_provider_failures_do_not_leak_credentials(monkeypatch, error, code):
    factory = Mock(return_value=SimpleNamespace(create_session=AsyncMock(side_effect=error)))
    monkeypatch.setattr("server.rpc.live_methods.LiveClient.from_runtime", factory)
    assert await _create(state(), {"sdp": "v=0"}) == {"error": code}


def test_live_methods_use_canonical_registry_and_exact_connection():
    current = state()
    assert _status(current, {})["configured"] is True
    current.runtime.provider_credentials.is_usable.assert_called_once_with(
        "openai", "openai:api-key"
    )
    methods = build_method_handlers()
    assert methods["live.create"] is _create
    assert methods["live.status"] is _status
