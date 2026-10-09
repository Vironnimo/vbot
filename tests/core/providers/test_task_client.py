"""Shared Provider task HTTP client: target binding, auth, retry policies and
response classification."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.debug import DebugContext, DebugTraceStore, ProviderDebugRecorder
from core.debug.store import drain_debug_traces
from core.providers.accounts import ConnectionRef
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderContentRefusedError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.task_client import (
    DEFAULT_TASK_REQUEST_RETRY_POLICY,
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    TaskRequestRetryPolicy,
    classify_task_response,
    merge_extra_options,
)

_PROVIDER_BASE_URL = "https://provider.example/api/v1"
_CONNECTION_BASE_URL = "https://connection.example/api/v1"
_THINGS_URL = f"{_PROVIDER_BASE_URL}/things"
_OK = {"ok": True}


def _make_provider(
    connection_base_url: str | None = None,
    *,
    adapter: str = "openai_compatible",
    base_url: str = _PROVIDER_BASE_URL,
) -> ProviderConfig:
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="EXAMPLE_API_KEY"),
        base_url=connection_base_url,
    )
    return ProviderConfig(
        id="example",
        name="Example",
        adapter=adapter,
        base_url=base_url,
        connections=[connection],
        extra_headers={"X-Title": "vBot"},
    )


def _counting_client() -> ProviderTaskClient:
    """A client whose token getter issues ``t1``, ``t2``, ... one per request attempt."""

    issued: list[str] = []

    async def next_token() -> str:
        issued.append(f"t{len(issued) + 1}")
        return issued[-1]

    provider = _make_provider()
    return ProviderTaskClient(
        provider=provider,
        connection=provider.get_connection("api-key"),
        token_getter=next_token,
        model_id="example/some-model",
    )


def _json(response: httpx.Response) -> Any:
    return response.json()


async def _consume_json(response: httpx.Response) -> Any:
    await response.aread()
    return _json(response)


# ---------------------------------------------------------------------------
# Target binding and request fields
# ---------------------------------------------------------------------------


class _StubRuntime:
    """Minimal ``TaskClientRuntime`` stand-in for target resolution."""

    def __init__(self, provider: ProviderConfig, traces: DebugTraceStore | None = None) -> None:
        self.providers = SimpleNamespace(get=lambda provider_id: provider)
        self._traces = traces

    def provider_debug_recorder(
        self, *, body_limit: int | None = None
    ) -> ProviderDebugRecorder | None:
        if self._traces is None:
            return None
        return ProviderDebugRecorder(self._traces, body_limit=body_limit)

    def get_connection_token_getter(self, connection: ConnectionRef):  # type: ignore[no-untyped-def]
        async def _get_token() -> str:
            return "sk-test"

        return _get_token


@pytest.mark.parametrize(
    ("adapter", "base_url", "connection_base_url", "expected_base_url"),
    [
        ("openai_compatible", _PROVIDER_BASE_URL, None, _PROVIDER_BASE_URL),
        ("openai_compatible", _PROVIDER_BASE_URL, _CONNECTION_BASE_URL, _CONNECTION_BASE_URL),
        # Native chat bases map to the Adapter's OpenAI-compatible API.
        ("ollama", "http://localhost:11434", None, "http://localhost:11434/v1"),
        ("ollama_cloud", "https://ollama.com", None, "https://ollama.com/v1"),
        ("lmstudio", "http://localhost:1234", "http://lab:1234/", "http://lab:1234/v1"),
        ("lmstudio", "http://localhost:1234/v1", None, "http://localhost:1234/v1"),
    ],
    ids=[
        "provider-url",
        "connection-url",
        "ollama-native",
        "ollama-cloud-native",
        "lmstudio-connection-native",
        "lmstudio-already-v1",
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_from_runtime_binds_the_resolved_connection_credential_and_base_url(
    adapter: str,
    base_url: str,
    connection_base_url: str | None,
    expected_base_url: str,
) -> None:
    route = respx.post(f"{expected_base_url}/things").mock(
        return_value=httpx.Response(200, json=_OK)
    )
    target = SimpleNamespace(
        provider_id="example",
        model_id="example/some-model",
        connection_id="example:api-key",
        local_connection_id="api-key",
    )
    client = ProviderTaskClient.from_runtime(
        _StubRuntime(_make_provider(connection_base_url, adapter=adapter, base_url=base_url)),
        target,
    )

    result = await client.post_and_parse("/things", timeout=5.0, parse=_json, json={"model": "m"})

    assert result == _OK
    request = route.calls.last.request
    assert (request.headers["authorization"], request.headers["x-title"]) == (
        "Bearer sk-test",
        "vBot",
    )


@pytest.mark.parametrize("for_run", [True, False], ids=["run-request", "request-outside-a-run"])
@respx.mock
@pytest.mark.asyncio
async def test_debug_mode_traces_only_requests_made_for_a_run(
    tmp_path: Path, for_run: bool
) -> None:
    respx.post(_THINGS_URL).mock(return_value=httpx.Response(200, json=_OK))
    traces = DebugTraceStore(tmp_path, trace_limit=10)
    context = DebugContext(
        run_id="run-1",
        agent_id="agent-1",
        session_id="session-1",
        provider_id="example",
        connection_id="example:api-key",
        model_id="example/some-model",
        streaming=False,
        iteration_number=0,
    )
    client = ProviderTaskClient.from_runtime(
        _StubRuntime(_make_provider(None), traces),
        SimpleNamespace(
            provider_id="example",
            model_id="example/some-model",
            connection_id="example:api-key",
            local_connection_id="api-key",
        ),
        debug_context=context if for_run else None,
    )

    await client.post_and_parse("/things", timeout=5.0, parse=_json, json={"model": "m"})
    await drain_debug_traces()

    recorded = traces.get_traces()
    if not for_run:
        assert recorded == []
        return
    trace = traces.get_trace(recorded[0]["trace_id"])
    assert (trace["context"]["run_id"], trace["response"]["body"]) == ("run-1", '{"ok":true}')


@respx.mock
@pytest.mark.asyncio
async def test_keyless_connection_sends_no_auth_header_but_keeps_extra_headers() -> None:
    connection = ConnectionConfig(
        id="default", type="none", label="Default", auth=AuthConfig(header="", prefix="")
    )
    provider = ProviderConfig(
        id="local",
        name="Local",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:8080/v1",
        connections=[connection],
        extra_headers={"X-Title": "vBot"},
    )
    client = ProviderTaskClient(
        provider=provider, connection=connection, credential="", model_id="local/some-model"
    )
    route = respx.post("http://127.0.0.1:8080/v1/things").mock(return_value=httpx.Response(200))

    await client.post_and_parse("/things", timeout=5.0, parse=lambda response: None)

    headers = route.calls.last.request.headers
    assert "authorization" not in headers
    assert headers["x-title"] == "vBot"


def test_extra_options_add_fields_but_never_override_authored_payload() -> None:
    payload = {"model": "safe-model", "prompt": "keep me"}

    merge_extra_options(payload, {"extra_options": {"future_option": 3, "empty": ""}})

    assert payload == {"model": "safe-model", "prompt": "keep me", "future_option": 3}
    with pytest.raises(ProviderError, match="model") as raised:
        merge_extra_options(payload, {"extra_options": {"other": 1, "model": "redirected"}})
    assert raised.value.retryable is False
    assert payload == {"model": "safe-model", "prompt": "keep me", "future_option": 3}


# ---------------------------------------------------------------------------
# Retry policies
# ---------------------------------------------------------------------------

_VERIFIED_503_POLICY = TaskRequestRetryPolicy(
    replay_safe=False, verified_safe_retry_status_codes=frozenset({503})
)


@pytest.mark.parametrize(
    ("replies", "retry_policy", "expected", "calls", "detail"),
    [
        pytest.param(
            [httpx.Response(503, text="overloaded"), httpx.Response(200, json=_OK)],
            DEFAULT_TASK_REQUEST_RETRY_POLICY,
            _OK,
            2,
            None,
            id="replay-safe-retries-transient-status",
        ),
        pytest.param(
            httpx.ConnectError("connection refused"),
            DEFAULT_TASK_REQUEST_RETRY_POLICY,
            NetworkError,
            None,
            "connection refused",
            id="replay-safe-connection-failure-is-network-error",
        ),
        pytest.param(
            [httpx.Response(400, text="bad request")],
            DEFAULT_TASK_REQUEST_RETRY_POLICY,
            ProviderError,
            1,
            "400 bad request",
            id="fatal-status-is-not-retried",
        ),
        pytest.param(
            [httpx.Response(400, json={"error": {"message": "Invalid value for 'moderation'."}})],
            NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            ProviderError,
            1,
            "Invalid value for 'moderation'",
            id="other-client-error-is-no-refusal",
        ),
        *(
            pytest.param(
                [httpx.Response(status, json={"error": error})],
                NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
                ProviderContentRefusedError,
                1,
                error["message"],
                id=case,
            )
            for case, status, error in (
                (
                    "refusal-by-code",
                    400,
                    {"code": "moderation_blocked", "message": "Blocked by moderation."},
                ),
                (
                    "refusal-forwarded-message",
                    400,
                    {"code": 400, "message": "Your request was rejected by the safety system."},
                ),
                (
                    "refusal-flagged-not-auth",
                    403,
                    {"code": 403, "message": "Your input was flagged for violence."},
                ),
            )
        ),
        pytest.param(
            [httpx.Response(401, text="bad key")],
            DEFAULT_TASK_REQUEST_RETRY_POLICY,
            ProviderAuthError,
            1,
            "401 bad key",
            id="auth-status",
        ),
        pytest.param(
            [httpx.ConnectTimeout("connect timed out"), httpx.Response(200, json=_OK)],
            NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            _OK,
            2,
            None,
            id="non-idempotent-retries-failure-before-send",
        ),
        pytest.param(
            [httpx.Response(429, text="rate limited"), httpx.Response(200, json=_OK)],
            NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            _OK,
            2,
            None,
            id="non-idempotent-retries-rate-limit",
        ),
        pytest.param(
            [httpx.Response(503, text="not processed"), httpx.Response(200, json=_OK)],
            _VERIFIED_503_POLICY,
            _OK,
            2,
            None,
            id="non-idempotent-retries-verified-status",
        ),
        pytest.param(
            httpx.ReadTimeout("ambiguous transport failure"),
            NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            ProviderOutcomeUnknownError,
            1,
            "ambiguous transport failure",
            id="non-idempotent-ambiguous-transport-failure",
        ),
        pytest.param(
            [httpx.Response(503, text="gateway failure")],
            NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            ProviderOutcomeUnknownError,
            1,
            "503 gateway failure",
            id="non-idempotent-unverified-server-status",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streamed"])
async def test_post_retries_only_what_its_policy_proves_safe(
    replies: Any,
    retry_policy: TaskRequestRetryPolicy,
    expected: Any,
    calls: int | None,
    detail: str | None,
    streaming: bool,
) -> None:
    if streaming and isinstance(replies, list):
        replies = [
            httpx.Response(
                reply.status_code, headers=reply.headers, stream=httpx.ByteStream(reply.content)
            )
            if isinstance(reply, httpx.Response)
            else reply
            for reply in replies
        ]
    route = respx.post(_THINGS_URL).mock(side_effect=replies)
    client = _counting_client()

    async def post() -> Any:
        return await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=_json,
            consume=_consume_json if streaming else None,
            json={"prompt": "p"},
            retry_policy=retry_policy,
        )

    if isinstance(expected, type):
        with pytest.raises(expected) as raised:
            await post()
        error = raised.value
        assert isinstance(error, NetworkError | ProviderError)
        assert error.retryable is (expected is NetworkError)
        assert detail is not None and detail in str(error)
        assert isinstance(error, ProviderContentRefusedError) is (
            expected is ProviderContentRefusedError
        )
        if isinstance(error, ProviderOutcomeUnknownError):
            assert error.operation_key
    else:
        assert await post() == expected

    if calls is None:
        assert route.call_count > 1
    else:
        assert route.call_count == calls
    # Every attempt asks the token getter again, and none carries an idempotency key.
    sent = [call.request.headers for call in route.calls]
    assert [headers["authorization"] for headers in sent] == [
        f"Bearer t{attempt}" for attempt in range(1, len(sent) + 1)
    ]
    assert all("idempotency-key" not in headers for headers in sent)


@pytest.mark.parametrize(
    ("retry_policy", "expected", "calls"),
    [
        (DEFAULT_TASK_REQUEST_RETRY_POLICY, _OK, 2),
        (NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY, ProviderOutcomeUnknownError, 1),
    ],
    ids=["replay-safe-retries", "non-idempotent-outcome-unknown"],
)
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streamed"])
async def test_retryable_parse_failure_follows_the_retry_policy(
    retry_policy: TaskRequestRetryPolicy, expected: Any, calls: int, streaming: bool
) -> None:
    route = respx.post(_THINGS_URL).mock(return_value=httpx.Response(200, json=_OK))
    parsed: list[int] = []

    def parse(response: httpx.Response) -> Any:
        parsed.append(1)
        if len(parsed) == 1:
            raise ProviderError("incomplete batch", retryable=True)
        return response.json()

    async def consume(response: httpx.Response) -> Any:
        await response.aread()
        return parse(response)

    async def post() -> Any:
        return await _counting_client().post_and_parse(
            "/things",
            timeout=5.0,
            parse=parse,
            consume=consume if streaming else None,
            retry_policy=retry_policy,
        )

    if isinstance(expected, type):
        with pytest.raises(expected):
            await post()
    else:
        assert await post() == expected
    assert route.call_count == calls


@respx.mock
@pytest.mark.asyncio
async def test_idempotency_header_reuses_one_operation_key_across_retries() -> None:
    route = respx.post(_THINGS_URL).mock(
        side_effect=[httpx.Response(503, text="overloaded"), httpx.Response(200, json=_OK)]
    )
    policy = TaskRequestRetryPolicy(replay_safe=False, idempotency_header_name="Idempotency-Key")

    result = await _counting_client().post_and_parse(
        "/things", timeout=5.0, parse=_json, retry_policy=policy
    )

    operation_keys = [call.request.headers["idempotency-key"] for call in route.calls]
    assert result == _OK
    assert len(operation_keys) == 2
    assert len(set(operation_keys)) == 1
    assert operation_keys[0]


@respx.mock
@pytest.mark.asyncio
async def test_get_is_replay_safe_and_retries_a_transient_server_error() -> None:
    route = respx.get(f"{_PROVIDER_BASE_URL}/jobs/abc").mock(
        side_effect=[httpx.Response(500, text="transient"), httpx.Response(200, json=_OK)]
    )

    result = await _counting_client().get_and_parse("/jobs/abc", timeout=5.0, parse=_json)

    assert result == _OK
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "POST_STREAM"])
@pytest.mark.parametrize("outcome", ["http_rejection", "parser_auth_error"])
async def test_only_an_http_rejection_refreshes_the_token_and_replays_the_same_request(
    method: str, outcome: str
) -> None:
    class Getter:
        token = "old-test-token"
        refreshes = 0

        async def __call__(self) -> str:
            return self.token

        async def refresh_after_rejection(
            self, rejected: str, *, status_code: int, response_body: str
        ) -> str | None:
            assert (rejected, status_code) == ("old-test-token", 401)
            self.refreshes += 1
            self.token = "new-test-token"
            return self.token

    getter = Getter()
    provider = _make_provider()
    client = ProviderTaskClient(
        provider=provider,
        connection=provider.connections[0],
        model_id="test-model",
        token_getter=getter,
    )
    first_status = 401 if outcome == "http_rejection" else 200
    route = respx.route(method=method.split("_")[0], url=_PROVIDER_BASE_URL + "/task").mock(
        side_effect=[
            httpx.Response(first_status, json={"result": "initial"}),
            httpx.Response(200, json={"result": "done"}),
        ]
    )

    def parse(response: httpx.Response) -> str:
        if outcome == "parser_auth_error":
            raise ProviderAuthError("test parser auth failure")
        return str(response.json()["result"])

    async def consume(response: httpx.Response) -> str:
        await response.aread()
        return parse(response)

    async def invoke() -> str:
        if method == "GET":
            return await client.get_and_parse("/task", timeout=1, parse=parse)
        return await client.post_and_parse(
            "/task",
            timeout=1,
            parse=parse,
            consume=consume if method == "POST_STREAM" else None,
            json={"prompt": "same input"},
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    if outcome == "http_rejection":
        assert await invoke() == "done"
        assert getter.refreshes == 1
        assert route.call_count == 2
        assert route.calls.last.request.headers["Authorization"] == "Bearer new-test-token"
        assert route.calls[0].request.content == route.calls[1].request.content
    else:
        with pytest.raises(ProviderError):
            await invoke()
        assert getter.refreshes == 0
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Incremental response consumption
# ---------------------------------------------------------------------------


class _GatedResponseStream(httpx.AsyncByteStream):
    def __init__(self, *, read_failure: bool = False) -> None:
        self.release = asyncio.Event()
        self.read_failure = read_failure
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"first"
        await self.release.wait()
        if self.read_failure:
            raise httpx.ReadError("test interrupted response")
        yield b"second"

    @override
    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "outcome", ["completed", "early_return", "cancelled", "read_failure", "consumer_failure"]
)
@respx.mock
@pytest.mark.asyncio
async def test_stream_delivers_before_completion_and_closes_every_outcome_without_replay(
    tmp_path: Path, outcome: str
) -> None:
    stream = _GatedResponseStream(read_failure=outcome == "read_failure")
    route = respx.post(_THINGS_URL).mock(return_value=httpx.Response(200, stream=stream))
    observer = SimpleNamespace(start=AsyncMock(return_value="call"), finish=AsyncMock())
    traces = DebugTraceStore(tmp_path, trace_limit=10)
    client = ProviderTaskClient.from_runtime(
        _StubRuntime(_make_provider(), traces),
        SimpleNamespace(
            provider_id="example",
            model_id="example/some-model",
            connection_id="example:api-key",
            local_connection_id="api-key",
        ),
        usage_observer=observer,
        debug_context=DebugContext(
            run_id="run-1",
            agent_id="agent-1",
            session_id="session-1",
            provider_id="example",
            connection_id="example:api-key",
            model_id="example/some-model",
            streaming=True,
            iteration_number=0,
        ),
    )
    first_received = asyncio.Event()
    chunks: list[bytes] = []

    async def consume(response: httpx.Response) -> bytes:
        assert not response.is_closed
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            first_received.set()
            if outcome == "early_return":
                return chunk
            if outcome == "consumer_failure":
                raise ProviderError("test unusable stream", retryable=True)
        return b"".join(chunks)

    def parse(_response: httpx.Response) -> bytes:
        raise AssertionError("a streaming call must only use its consumer")

    task = asyncio.create_task(
        client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=parse,
            consume=consume,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )
    )
    try:
        await first_received.wait()
        assert chunks == [b"first"]
        if outcome not in {"early_return", "consumer_failure"}:
            assert not task.done() and not stream.closed
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stream.release.set()
            if outcome in {"read_failure", "consumer_failure"}:
                with pytest.raises(ProviderOutcomeUnknownError) as raised:
                    await task
                assert raised.value.retryable is False
            else:
                assert await task == (b"first" if outcome == "early_return" else b"firstsecond")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert stream.closed and route.call_count == 1
    expected_status = (
        "cancelled"
        if outcome == "cancelled"
        else "failed"
        if outcome in {"read_failure", "consumer_failure"}
        else "completed"
    )
    observer.start.assert_awaited_once()
    observer.finish.assert_awaited_once_with(
        "call",
        usage=None,
        result=b"".join(chunks) if expected_status == "completed" else None,
        status=expected_status,
    )
    await drain_debug_traces()
    recorded = traces.get_traces()
    assert len(recorded) == 1
    trace = traces.get_trace(recorded[0]["trace_id"])
    assert trace["response"]["body"] == b"".join(chunks).decode()


@pytest.mark.parametrize(
    ("status_code", "accepted"),
    [(200, True), (200, False), (400, False)],
    ids=["accepted", "unusable", "rejected"],
)
@respx.mock
@pytest.mark.asyncio
async def test_stream_records_usage_read_by_its_consumer_even_when_the_result_is_unusable(
    status_code: int,
    accepted: bool,
) -> None:
    class UsageStream(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"usage":{"input_tokens":4}}'

    route = respx.post(_THINGS_URL).mock(
        return_value=httpx.Response(status_code, stream=UsageStream())
    )
    observer = SimpleNamespace(start=AsyncMock(return_value="call"), finish=AsyncMock())
    provider = _make_provider()
    client = ProviderTaskClient(
        provider=provider,
        connection=provider.connections[0],
        credential="test-token",
        model_id="test-model",
        usage_observer=observer,
    )

    async def consume(response: httpx.Response) -> str:
        assert status_code == 200
        await response.aread()
        if not accepted:
            raise ValueError("test unusable result")
        return "parsed"

    async def invoke() -> Any:
        return await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=_json,
            consume=consume,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    if accepted:
        assert await invoke() == "parsed"
    else:
        with pytest.raises(ProviderOutcomeUnknownError if status_code == 200 else ProviderError):
            await invoke()
    assert route.call_count == 1
    observer.finish.assert_awaited_once_with(
        "call",
        usage={"input_tokens": 4},
        result="parsed" if accepted else None,
        status="completed" if accepted else "failed",
    )


# ---------------------------------------------------------------------------
# classify_task_response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "idempotent", "retryable", "detail"),
    [
        (httpx.Response(500, text="boom"), False, False, "500 boom"),
        (httpx.Response(500, text="boom"), True, True, "500 boom"),
        (httpx.Response(503), False, True, "503"),
    ],
    ids=["generation-server-error", "replay-safe-server-error", "bare-status"],
)
def test_task_error_response_carries_status_body_and_idempotency_aware_retry(
    response: httpx.Response, idempotent: bool, retryable: bool, detail: str
) -> None:
    with pytest.raises(ProviderError) as raised:
        classify_task_response(response, idempotent=idempotent)

    assert raised.value.retryable is retryable
    assert detail in str(raised.value)
