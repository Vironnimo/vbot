"""RPC envelope unwrapping and SSE parsing used against vBot's public edge."""

import pytest

from scripts.perf_load_suite.rpc import RpcCallError, SseEvent, SseParser, unwrap_envelope


def test_successful_envelope_returns_its_result():
    assert unwrap_envelope("agent.create", 200, '{"ok": true, "result": {"id": "a"}}') == {
        "id": "a"
    }


def test_error_envelope_raises_with_its_code():
    with pytest.raises(RpcCallError) as caught:
        unwrap_envelope(
            "performance.snapshot",
            404,
            '{"ok": false, "error": {"code": "method_not_found", "message": "nope"}}',
        )

    assert (caught.value.method, caught.value.code, caught.value.message) == (
        "performance.snapshot",
        "method_not_found",
        "nope",
    )


@pytest.mark.parametrize("body", ["<html>502</html>", "[]", '{"ok": false}'])
def test_unusable_response_raises_with_the_http_status(body):
    with pytest.raises(RpcCallError) as caught:
        unwrap_envelope("chat.stream", 502, body)

    assert caught.value.code == "http_502"


def test_sse_parser_dispatches_on_blank_lines():
    parser = SseParser()
    lines = [
        ": comment",
        "id: 7",
        "event: assistant_output_delta",
        'data: {"a":',
        "data: 1}",
        "",
        "",
        "event: heartbeat",
        "data: {}",
        "",
    ]

    events = [event for event in map(parser.feed, lines) if event is not None]

    assert events == [
        SseEvent("assistant_output_delta", '{"a":\n1}', "7"),
        SseEvent("heartbeat", "{}", None),
    ]
