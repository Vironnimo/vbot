"""The fake Provider scripts turns from directives and streams valid OpenAI SSE."""

import json
import time

import pytest
from starlette.testclient import TestClient

from core.tools import model_names
from core.tools.model_names import model_tool_name
from scripts.perf_load_suite.directive import PerfDirective
from scripts.perf_load_suite.fake_provider import (
    DONE_FRAME,
    MARKER_EVERY_CHUNKS,
    PlanError,
    RequestLog,
    ScriptedToolCall,
    check_arguments,
    create_app,
    plan_response,
    tool_call_frames,
)
from scripts.perf_load_suite.fixture import BASH_COMMAND, SEARCH_NEEDLE, source_file_paths
from scripts.perf_load_suite.metrics import marker_latencies_ms

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "parameters": {
                "type": "object",
                "properties": {"args": {"type": "array", "items": {"type": "string"}}},
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": model_tool_name("bash"),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def _ids():
    counter = iter(range(1, 1000))
    return lambda: f"call_{next(counter)}"


def _tool_round(call_ids):
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "read", "arguments": "{}"}}
                for call_id in call_ids
            ],
        },
        *({"role": "tool", "tool_call_id": call_id, "content": "ok"} for call_id in call_ids),
    ]


def _body(messages, *, tools=TOOLS, stream=True):
    body = {"model": "perf-model", "messages": messages, "stream": stream}
    if tools is not None:
        body["tools"] = tools
    return body


def _directive(**fields):
    return PerfDirective(**{"tag": "L1-s000-t1", **fields}).render()


def test_tool_rounds_rotate_tools_until_the_final_text_round():
    user = {"role": "user", "content": _directive(steps=4, tools=("read", "search_files", "bash"))}
    messages = [{"role": "system", "content": "sys"}, user]
    kinds = []
    for round_index in range(4):
        planned = plan_response(_body(messages), next_call_id=_ids())
        kinds.append((planned.kind, planned.round_index, [c.name for c in planned.tool_calls]))
        messages = [*messages, *_tool_round([f"c{round_index}"])]

    assert kinds == [
        ("tool_calls", 0, ["read"]),
        ("tool_calls", 1, ["search_files"]),
        ("tool_calls", 2, [model_tool_name("bash")]),
        ("text", 3, []),
    ]


def test_scripted_arguments_target_the_fixture_project():
    user = {"role": "user", "content": _directive(steps=4, calls=3)}
    planned = plan_response(_body([user]), next_call_id=_ids())

    arguments = {call.name: call.arguments for call in planned.tool_calls}
    assert arguments["read"]["path"] in source_file_paths()
    assert arguments["search_files"] == {"args": ["-F", SEARCH_NEEDLE, "src"]}
    assert set(arguments[model_tool_name("bash")]) == {"command"}
    assert [call.call_id for call in planned.tool_calls] == ["call_1", "call_2", "call_3"]


@pytest.mark.parametrize("shell_name", ["bash", "powershell"])
def test_shell_uses_host_model_name_and_registry_scripted_arguments(monkeypatch, shell_name):
    monkeypatch.setattr(model_names, "_MODEL_NAMES", {"bash": shell_name})
    tools = [
        {
            "type": "function",
            "function": {"name": shell_name, "parameters": TOOLS[2]["function"]["parameters"]},
        }
    ]
    user = {"role": "user", "content": _directive(steps=2, tools=("bash",))}

    planned = plan_response(_body([user], tools=tools), next_call_id=_ids())

    assert planned.directive is not None and planned.directive.tools == ("bash",)
    assert planned.tool_calls == (
        ScriptedToolCall("call_1", shell_name, {"command": BASH_COMMAND}),
    )


def test_system_reminders_after_the_directive_do_not_hide_it():
    messages = [
        {"role": "user", "content": _directive(steps=2)},
        *_tool_round(["c1"]),
        {"role": "user", "content": "<system-reminder>\nsomething changed\n</system-reminder>"},
    ]

    planned = plan_response(_body(messages), next_call_id=_ids())

    assert (planned.kind, planned.round_index) == ("text", 1)


@pytest.mark.parametrize(
    "body",
    [
        _body([{"role": "user", "content": _directive(steps=2)}], tools=None),
        _body(
            [
                {"role": "user", "content": _directive(steps=2)},
                {"role": "user", "content": "Reflect on the conversation so far."},
            ]
        ),
    ],
    ids=["no-tools", "latest-user-message-without-directive"],
)
def test_utility_requests_are_auxiliary(body):
    assert plan_response(body, next_call_id=_ids()).kind == "aux"


def test_warmup_directive_is_one_text_response():
    user = {"role": "user", "content": PerfDirective(tag="w", warmup_tokens=500, rate=0).render()}

    planned = plan_response(_body([user]), next_call_id=_ids())

    assert planned.kind == "warmup"
    assert planned.directive is not None and planned.directive.text_tokens == 500


def test_tool_missing_from_the_request_fails_loudly():
    user = {"role": "user", "content": _directive(steps=2, tools=("write",))}

    with pytest.raises(PlanError, match="'write' is not offered"):
        plan_response(_body([user]), next_call_id=_ids())


def test_arguments_are_checked_against_the_offered_schema():
    schema = TOOLS[1]["function"]["parameters"]

    check_arguments("search_files", {"args": ["x"]}, schema)
    with pytest.raises(PlanError, match="expects an array"):
        check_arguments("search_files", {"args": "x"}, schema)
    with pytest.raises(PlanError, match="no properties"):
        check_arguments("search_files", {"args": [], "pattern": "x"}, schema)
    with pytest.raises(PlanError, match="requires"):
        check_arguments("search_files", {}, schema)


def _decode(frames):
    payloads = []
    for frame in frames:
        assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
        body = frame[len(b"data: ") : -2]
        payloads.append(body.decode() if frame == DONE_FRAME else json.loads(body))
    return payloads


def test_tool_call_stream_reassembles_to_the_scripted_calls():
    calls = (
        ScriptedToolCall("call_1", "read", {"path": "src/module_00.py"}),
        ScriptedToolCall("call_2", "bash", {"command": 'python -c "print(1)"'}),
    )

    payloads = _decode(tool_call_frames("perf-model", calls, prompt_tokens=100))

    assert payloads[-1] == "[DONE]"
    assert payloads[-2]["choices"] == [] and payloads[-2]["usage"]["prompt_tokens"] == 100
    assert payloads[-3]["choices"][0]["finish_reason"] == "tool_calls"
    assembled: dict[int, dict] = {}
    for payload in payloads[:-2]:
        for delta in payload["choices"][0]["delta"].get("tool_calls", []):
            entry = assembled.setdefault(delta["index"], {"arguments": ""})
            entry.update({key: delta[key] for key in ("id",) if key in delta})
            function = delta.get("function", {})
            if "name" in function:
                entry["name"] = function["name"]
            entry["arguments"] += function.get("arguments", "")
    assert [
        (entry["id"], entry["name"], json.loads(entry["arguments"]))
        for _, entry in sorted(assembled.items())
    ] == [(call.call_id, call.name, call.arguments) for call in calls]


def _stream_payloads(response):
    return [
        line.removeprefix("data: ")
        for line in response.text.split("\n")
        if line.startswith("data: ")
    ]


def test_text_stream_carries_markers_usage_and_is_recorded():
    log = RequestLog()
    client = TestClient(create_app(log))
    user = {"role": "user", "content": _directive(steps=1, tokens=25, rate=0, think_ms=0)}

    response = client.post("/v1/chat/completions", json=_body([user]))

    assert response.status_code == 200
    data = _stream_payloads(response)
    assert data[-1] == "[DONE]"
    chunks = [json.loads(item) for item in data[:-1]]
    assert chunks[-1]["usage"]["completion_tokens"] == 25
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks[:-2])
    assert len(text.split()) >= 25
    assert marker_latencies_ms(text, time.time())  # first chunk is always timed

    stats = client.get("/_perf/stats").json()["requests"]
    assert len(stats) == 1
    record = stats[0]
    assert (record["kind"], record["tag"], record["tokens"], record["disconnected"]) == (
        "text",
        "L1-s000-t1",
        25,
        False,
    )
    assert record["arrival"] <= record["first_byte"] <= record["completed"]

    client.post("/_perf/reset")
    assert client.get("/_perf/stats").json()["requests"] == []


def test_paced_text_marks_every_tenth_chunk():
    client = TestClient(create_app(RequestLog()))
    tokens = 3 * MARKER_EVERY_CHUNKS
    user = {"role": "user", "content": _directive(steps=1, tokens=tokens, rate=10_000)}

    response = client.post("/v1/chat/completions", json=_body([user]))

    chunks = [json.loads(item) for item in _stream_payloads(response)[:-1]]
    contents = [
        chunk["choices"][0]["delta"]["content"]
        for chunk in chunks
        if chunk["choices"] and "content" in chunk["choices"][0]["delta"]
    ]
    assert len(contents) == tokens
    timed = [index for index, content in enumerate(contents) if content.startswith("⟦t=")]
    assert timed == [0, MARKER_EVERY_CHUNKS, 2 * MARKER_EVERY_CHUNKS]


def test_malformed_directive_is_a_client_error_and_recorded():
    log = RequestLog()
    client = TestClient(create_app(log))

    response = client.post(
        "/v1/chat/completions",
        json=_body([{"role": "user", "content": "[[perf id=x bogus=1]]"}]),
    )

    assert response.status_code == 400
    assert "unknown directive key" in response.json()["error"]["message"]
    assert log.snapshot()[0]["kind"] == "error"


def test_models_endpoint_lists_the_perf_model():
    client = TestClient(create_app())

    assert [model["id"] for model in client.get("/v1/models").json()["data"]] == ["perf-model"]
