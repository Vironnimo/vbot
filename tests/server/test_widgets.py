"""Agent widgets end to end: shown by show_widget, served by capability, values sent back."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.tools.show_widget import register_show_widget_tool
from server.app import create_app
from tests.server.rpc_test_support import StubAdapter, StubRuntime

_CODE = '<input type="range" name="line_height" min="1" max="2" step="0.1" value="1.4">'


def _rpc(client: TestClient, method: str, **params: Any) -> dict[str, Any]:
    return cast(
        dict[str, Any], client.post("/api/rpc", json={"method": method, "params": params}).json()
    )


def _widgets(value: Any) -> Iterator[dict[str, Any]]:
    """Every projected ``widget`` reference inside a History payload."""
    if isinstance(value, dict):
        widget = value.get("widget")
        if isinstance(widget, dict) and "url" in widget:
            yield widget
        for item in value.values():
            yield from _widgets(item)
    elif isinstance(value, list):
        for item in value:
            yield from _widgets(item)


def test_a_shown_widget_is_served_and_its_values_reach_the_agent_with_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_widget",
                        "name": "show_widget",
                        "arguments": {"title": "Text spacing", "widget_code": _CODE},
                    }
                ],
            },
            {"content": "Adjust the slider.", "tool_calls": None},
            {"content": "Applied 1.6.", "tool_calls": None},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    register_show_widget_tool(runtime.tools)
    app = create_app(runtime=cast(Any, runtime))
    session = {"agent_id": "coder", "session_id": "session-one"}

    with TestClient(app) as client:
        _rpc(client, "session.create", **session)
        _rpc(client, "chat.send", **session, content="Let me tune the spacing")
        [widget] = list(_widgets(_rpc(client, "chat.history", **session)["result"]))
        served = client.get(widget["url"])
        forged = client.get(widget["url"][:-4] + "AAAA")
        values = [{"widget_id": widget["id"], "state": {"line_height": 1.6}}]
        unknown = _rpc(
            client,
            "chat.send",
            **session,
            content="Take it",
            widget_states=[{"widget_id": "res_unknown", "state": {}}],
        )
        new_session = _rpc(
            client,
            "chat.send",
            agent_id="coder",
            new_session={},
            content="Take it",
            widget_states=values,
        )
        sent = _rpc(
            client, "chat.send", **session, content="Take it like this", widget_states=values
        )
        history = _rpc(client, "chat.history", **session)["result"]

    assert widget["title"] == "Text spacing"
    assert served.status_code == 200
    document = served.text
    assert _CODE in document and "vbot-widget-hello" in document
    policy = served.headers["content-security-policy"]
    assert "sandbox allow-scripts;" in policy and "allow-downloads" not in policy
    assert "connect-src 'none'" in policy and "frame-ancestors 'self'" in policy
    assert served.headers["cache-control"] == "no-store"
    assert forged.status_code == 404
    # An unknown widget or a new Session refuses the send before anything is stored.
    for refused in (unknown, new_session):
        assert refused["ok"] is False
        assert refused["error"]["code"] == "invalid_request"
    assert sent["ok"] is True
    # The Model reads the values as quoted data right before the user's words.
    request = json.dumps(adapter.requests[-1]["messages"], ensure_ascii=False)
    values_at = request.index('\\"Text spacing\\": {\\"line_height\\": 1.6}')
    assert values_at < request.index("Take it like this")
    # The user sees what was sent as its own row, ahead of the message it came with.
    rows = [message for message in history["messages"] if message["role"] != "run_summary"]
    assert [row["role"] for row in rows[-3:]] == ["widget_state", "user", "assistant"]
    assert rows[-3]["widgets"] == [
        {"widget_id": widget["id"], "title": "Text spacing", "state": {"line_height": 1.6}}
    ]
    assert [m["content"] for m in history["messages"] if m["role"] == "user"] == [
        "Let me tune the spacing",
        "Take it like this",
    ]
