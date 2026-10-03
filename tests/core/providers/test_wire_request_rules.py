"""A wire profile's request body and header rules on every Adapter request path.

``request.body_defaults`` fill top-level body keys the request did not build,
``request.extra_body`` replaces whatever the body holds and
``request.extra_headers`` ride on the request, whichever protocol, transport and
call (send or stream) carries it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest
import respx

from core.providers._wire_profile_files import parse_wire_profile_file

from . import anthropic_test_support as anthropic
from . import ollama_test_support as ollama
from . import openai_test_support as openai
from . import opencode_go_test_support as go
from . import wire_paths_test_support as paths

_MESSAGES = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "Hello"},
]

type _Sent = tuple[Mapping[str, Any], Mapping[str, str]]
type _Capture = Callable[[Mapping[str, Any]], Awaitable[_Sent]]


def _bind_request_rules(adapter: Any, model_id: str, request: Mapping[str, Any]) -> None:
    """Add a rule naming ``model_id`` with ``request`` to the Adapter's wire profile data."""

    wire = adapter.wire
    issues: list[str] = []
    parsed = parse_wire_profile_file(
        wire.provider_id,
        {
            "format_version": 1,
            "rules": [{"when": {"ids": [model_id]}, "set": {"request": request}}],
        },
        source="test.json",
        report=issues.append,
    )
    assert parsed is not None and not issues
    profiles = wire.profiles
    current = profiles.file_for(wire.provider_id) or parsed
    rules = current.rules if current is not parsed else ()
    rule = replace(parsed.rules[0], index=len(rules))
    profiles.replace_files(
        {**profiles.files, wire.provider_id: replace(current, rules=(*rules, rule))}
    )


def _over_http(path: paths.RequestPath) -> _Capture:
    async def capture(request: Mapping[str, Any]) -> _Sent:
        adapter = path.adapter()
        _bind_request_rules(adapter, path.model_id, request)
        with respx.mock:
            route = respx.post(path.url).mock(return_value=path.success())
            await paths.request(adapter, path, {}, _MESSAGES)
        sent = route.calls.last.request
        return json.loads(sent.content), sent.headers

    return capture


async def _over_codex_websocket(request: Mapping[str, Any]) -> _Sent:
    socket = openai.FakeCodexWebSocket(
        [[{"type": "response.completed", "response": openai.COMPLETED_RESPONSE}]]
    )
    connector = openai.FakeCodexWebSocketConnector([socket])
    adapter = openai.codex_adapter(
        codex_websocket_connect=connector, model_lookup=openai.bundled_model_lookup()
    )
    _bind_request_rules(adapter, paths.CODEX.model_id, request)
    stream = adapter.stream(_MESSAGES, model_id=paths.CODEX.model_id, conversation_id="a:s")
    [_ async for _ in stream]
    await adapter.aclose()
    ((_url, connect_kwargs),) = connector.calls
    return socket.sent_payloads[0], connect_kwargs["additional_headers"]


@pytest.mark.parametrize(
    ("capture", "built", "replaced"),
    [
        pytest.param(_over_http(paths.CHAT), "model", "messages", id="chat-send"),
        pytest.param(
            _over_http(paths.CHAT.streaming(paths.chat_sse)), "model", "messages", id="chat-stream"
        ),
        pytest.param(_over_http(paths.MESSAGES), "model", "messages", id="messages-send"),
        pytest.param(
            _over_http(paths.MESSAGES.streaming(lambda: anthropic.sse_response(anthropic.sse()))),
            "model",
            "messages",
            id="messages-stream",
        ),
        pytest.param(
            _over_http(paths.GO_MESSAGES), "model", "messages", id="go-inner-messages-send"
        ),
        pytest.param(
            _over_http(paths.PLATFORM), "model", "instructions", id="platform-responses-send"
        ),
        pytest.param(
            _over_http(paths.PLATFORM.streaming(paths.responses_sse)),
            "model",
            "instructions",
            id="platform-responses-stream",
        ),
        pytest.param(
            _over_http(paths.ZEN_RESPONSES), "model", "instructions", id="zen-responses-send"
        ),
        pytest.param(_over_http(paths.CODEX), "model", "instructions", id="codex-sse-send"),
        pytest.param(_over_codex_websocket, "model", "instructions", id="codex-websocket-stream"),
        pytest.param(_over_http(paths.OPENROUTER), "model", "instructions", id="openrouter-send"),
        pytest.param(
            _over_http(paths.OPENROUTER.streaming(paths.responses_sse)),
            "model",
            "instructions",
            id="openrouter-stream",
        ),
        pytest.param(
            _over_http(paths.COPILOT_RESPONSES),
            "model",
            "instructions",
            id="copilot-responses-send",
        ),
        pytest.param(
            _over_http(paths.COPILOT_RESPONSES.streaming(paths.responses_sse)),
            "model",
            "instructions",
            id="copilot-responses-stream",
        ),
        pytest.param(
            _over_http(paths.COPILOT_MESSAGES), "model", "system", id="copilot-messages-send"
        ),
        pytest.param(
            _over_http(paths.COPILOT_MESSAGES_STREAM),
            "model",
            "system",
            id="copilot-messages-stream",
        ),
        pytest.param(
            _over_http(paths.GO_RESPONSES), "model", "instructions", id="go-responses-send"
        ),
        pytest.param(
            _over_http(
                paths.GO_RESPONSES.streaming(
                    lambda: go.success_response("responses", streaming=True)
                )
            ),
            "model",
            "instructions",
            id="go-responses-stream",
        ),
        pytest.param(
            _over_http(paths.ZEN_GEMINI), "contents", "systemInstruction", id="zen-gemini-send"
        ),
        pytest.param(
            _over_http(paths.ZEN_GEMINI_STREAM),
            "contents",
            "systemInstruction",
            id="zen-gemini-stream",
        ),
        pytest.param(_over_http(paths.OLLAMA), "model", "messages", id="ollama-native-send"),
        pytest.param(
            _over_http(paths.OLLAMA.streaming(lambda: ollama.ndjson(ollama.TEXT_RESPONSE))),
            "model",
            "messages",
            id="ollama-native-stream",
        ),
    ],
)
@pytest.mark.asyncio
async def test_body_and_header_rules_reach_every_request_path(
    capture: _Capture, built: str, replaced: str
) -> None:
    """``built`` is a key every request builds; ``replaced`` a built key ``extra_body`` replaces."""

    body, headers = await capture(
        {
            "body_defaults": {"vbot_default": {"tiers": ["flex"]}, built: "body-default"},
            "extra_body": {"vbot_extra": {"on": True}, replaced: "extra-body"},
            "extra_headers": {"X-Wire-Extra": "on"},
        }
    )

    assert body["vbot_default"] == {"tiers": ["flex"]}
    assert body[built] not in (None, "body-default")
    assert body["vbot_extra"] == {"on": True}
    assert body[replaced] == "extra-body"
    assert headers["X-Wire-Extra"] == "on"
