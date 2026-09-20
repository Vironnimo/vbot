"""Public extraction settings expose setup metadata, never credentials."""

import json

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.test_rpc import StubAdapter, make_state


@pytest.mark.asyncio
async def test_fetch_settings_roundtrip_and_credential_projection(tmp_path, monkeypatch):
    state = make_state(tmp_path, StubAdapter())
    monkeypatch.setattr(
        state.runtime,
        "resolve_environment_credential",
        lambda name: "private-test-key" if name == "PARALLEL_API_KEY" else "",
    )
    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "web_fetch": {"provider": "parallel", "mode": "prefer"},
            },
        },
    )
    assert response["ok"] is True
    data = response["result"]["web_fetch"]
    assert data["provider"] == "parallel" and data["mode"] == "prefer"
    service = next(item for item in data["services"] if item["id"] == "parallel")
    assert service["configured"] is True
    assert service["api_key_env"] == "PARALLEL_API_KEY" and service["pricing_url"].startswith(
        "https://"
    )
    assert "private-test-key" not in json.dumps(response)
    assert state.runtime.storage.load_web_fetch_settings() == {
        "provider": "parallel",
        "mode": "prefer",
    }
