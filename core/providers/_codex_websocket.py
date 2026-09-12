"""Codex websocket."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.providers._openai_constants import (
    _CODEX_WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
    _CODEX_WEBSOCKET_STATUS_CODE,
    CODEX_RESPONSES_ENDPOINT,
    CodexWebSocketConnector,
    CodexWebSocketRoute,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    normalize_responses_stream_event,
)

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder


@dataclass
class _CodexWebSocketContinuation:
    route: CodexWebSocketRoute
    last_request_payload: dict[str, Any]
    last_response_id: str
    last_response_items: list[dict[str, Any]]


class _CodexPreviousResponseMissingError(Exception):
    """Connection-scoped continuation vanished and must be replayed in full."""


class _CodexWebSocketTransportError(NetworkError):
    """Codex WebSocket failed before or after receiving a provider event."""

    def __init__(self, message: str, *, events_received: bool) -> None:
        super().__init__(message)
        self.events_received = events_received


def _codex_websocket_response_head(websocket: Any) -> tuple[int, dict[str, str]]:
    response = getattr(websocket, "response", None)
    raw_status = getattr(response, "status_code", _CODEX_WEBSOCKET_STATUS_CODE)
    status_code = (
        raw_status
        if isinstance(raw_status, int) and not isinstance(raw_status, bool)
        else _CODEX_WEBSOCKET_STATUS_CODE
    )
    raw_headers = getattr(response, "headers", None)
    try:
        headers = dict(raw_headers) if raw_headers is not None else {}
    except (TypeError, ValueError):
        headers = {}
    return status_code, {str(name): str(value) for name, value in headers.items()}


def _codex_responses_error_code(event: Mapping[str, Any]) -> str | None:
    response = event.get("response")
    payload = response if isinstance(response, Mapping) else event
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    code = payload.get("code")
    return code if isinstance(code, str) and code else None


def _codex_payloads_match_except_input(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    ignored = {"input", "previous_response_id"}
    current_rest = {key: value for key, value in current.items() if key not in ignored}
    previous_rest = {key: value for key, value in previous.items() if key not in ignored}
    return current_rest == previous_rest


class CodexWebSocket:
    """Own one route-isolated socket and connection-local response continuation."""

    def __init__(
        self,
        *,
        base_url: str,
        connect: CodexWebSocketConnector,
        debug_recorder: ProviderDebugRecorder | None,
        response_input: Callable[[dict[str, Any], str], Any],
    ) -> None:
        self._base_url = base_url
        self._codex_websocket_connect = connect
        self._debug_recorder = debug_recorder
        self._response_input = response_input
        self._codex_websocket: Any | None = None
        self._codex_websocket_route: CodexWebSocketRoute | None = None
        self._codex_websocket_continuation: _CodexWebSocketContinuation | None = None
        self._codex_websocket_lock = asyncio.Lock()

    async def stream(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        route: CodexWebSocketRoute,
        state: ResponsesStreamState,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._codex_websocket_lock:
            if self._codex_websocket_route not in {None, route}:
                await self.aclose()
            request_payload = self._build_codex_cached_request(payload, route)
            retried_missing_continuation = False
            while True:
                try:
                    async for delta in self._stream_codex_websocket_attempt(
                        request_payload,
                        headers=headers,
                        route=route,
                        state=state,
                    ):
                        yield delta
                except _CodexPreviousResponseMissingError:
                    if (
                        "previous_response_id" not in request_payload
                        or retried_missing_continuation
                    ):
                        self._codex_websocket_continuation = None
                        await self.aclose()
                        raise ProviderError(
                            "Codex WebSocket continuation was not found",
                            retryable=False,
                        ) from None
                    retried_missing_continuation = True
                    self._codex_websocket_continuation = None
                    await self.aclose()
                    request_payload = copy.deepcopy(payload)
                    continue
                except BaseException:
                    self._codex_websocket_continuation = None
                    await self.aclose()
                    raise

                self._remember_codex_websocket_continuation(payload, route, state)
                return

    async def _stream_codex_websocket_attempt(
        self,
        request_payload: dict[str, Any],
        *,
        headers: dict[str, str],
        route: CodexWebSocketRoute,
        state: ResponsesStreamState,
    ) -> AsyncIterator[dict[str, Any]]:
        wire_payload = {"type": "response.create", **request_payload}
        wire_text = json.dumps(wire_payload, ensure_ascii=False, separators=(",", ":"))
        capture = (
            self._debug_recorder.begin_capture(
                method="WEBSOCKET",
                url=self._codex_websocket_url(),
                headers=headers,
                body=wire_text.encode("utf-8"),
            )
            if self._debug_recorder is not None
            else None
        )
        events_received = False
        model_delta_received = False
        finish_received = False
        try:
            websocket = await self._ensure_codex_websocket(route, headers)
            if capture is not None:
                status_code, response_headers = _codex_websocket_response_head(websocket)
                capture.record_response_head(status_code, response_headers)
            send_result = websocket.send(wire_text)
            if inspect.isawaitable(send_result):
                await send_result
            while True:
                raw_frame = await websocket.recv()
                if isinstance(raw_frame, bytes):
                    frame_bytes = raw_frame
                    frame_text = raw_frame.decode("utf-8", errors="replace")
                elif isinstance(raw_frame, str):
                    frame_text = raw_frame
                    frame_bytes = raw_frame.encode("utf-8")
                else:
                    raise TypeError("Codex WebSocket returned a non-text frame")
                events_received = True
                if capture is not None:
                    capture.feed_body(frame_bytes + b"\n")
                event = json.loads(frame_text)
                if not isinstance(event, Mapping):
                    raise ValueError("Codex WebSocket event must be an object")
                event_data = dict(event)
                if (
                    "previous_response_id" in request_payload
                    and not model_delta_received
                    and _codex_responses_error_code(event_data) == "previous_response_not_found"
                ):
                    raise _CodexPreviousResponseMissingError
                event_type = event_data.get("type")
                event_name = event_type if isinstance(event_type, str) else ""
                deltas = normalize_responses_stream_event(event_name, event_data, state)
                for delta in deltas:
                    if delta.get("type") in {
                        "content_delta",
                        "reasoning_delta",
                        "tool_call_delta",
                    }:
                        model_delta_received = True
                    if delta.get("type") == "finish":
                        finish_received = True
                    yield delta
                if finish_received:
                    return
        except _CodexPreviousResponseMissingError:
            raise
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            transport_error = (
                exc
                if isinstance(exc, _CodexWebSocketTransportError)
                else _CodexWebSocketTransportError(
                    f"Codex WebSocket failed: {exc}",
                    events_received=events_received,
                )
            )
            if capture is not None:
                capture.record_error(transport_error)
            raise transport_error from exc
        finally:
            if capture is not None:
                capture.finalize()

    async def _ensure_codex_websocket(
        self,
        route: CodexWebSocketRoute,
        headers: dict[str, str],
    ) -> Any:
        if self._codex_websocket is not None and self._codex_websocket_route == route:
            return self._codex_websocket
        await self.aclose()
        connection = self._codex_websocket_connect(
            self._codex_websocket_url(),
            additional_headers=headers,
            open_timeout=_CODEX_WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
            max_size=None,
        )
        websocket = await connection if inspect.isawaitable(connection) else connection
        self._codex_websocket = websocket
        self._codex_websocket_route = route
        return websocket

    async def aclose(self) -> None:
        websocket = self._codex_websocket
        self._codex_websocket = None
        self._codex_websocket_route = None
        self._codex_websocket_continuation = None
        if websocket is None:
            return
        try:
            close_result = websocket.close()
            if inspect.isawaitable(close_result):
                await close_result
        except Exception:
            pass

    def _codex_websocket_url(self) -> str:
        base_url = self._base_url.rstrip("/")
        if base_url.startswith("https://"):
            websocket_base = f"wss://{base_url.removeprefix('https://')}"
        elif base_url.startswith("http://"):
            websocket_base = f"ws://{base_url.removeprefix('http://')}"
        else:
            raise ProviderError(
                f"Codex WebSocket requires an HTTP(S) base URL, got {base_url!r}",
                retryable=False,
            )
        return f"{websocket_base}{CODEX_RESPONSES_ENDPOINT}"

    def _build_codex_cached_request(
        self,
        payload: dict[str, Any],
        route: CodexWebSocketRoute,
    ) -> dict[str, Any]:
        continuation = self._codex_websocket_continuation
        if continuation is None or continuation.route != route:
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        if not _codex_payloads_match_except_input(
            payload,
            continuation.last_request_payload,
        ):
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        current_input = payload.get("input")
        previous_input = continuation.last_request_payload.get("input")
        if not isinstance(current_input, list) or not isinstance(previous_input, list):
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        baseline = [*previous_input, *continuation.last_response_items]
        if len(current_input) < len(baseline) or current_input[: len(baseline)] != baseline:
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        request_payload = copy.deepcopy(payload)
        request_payload["previous_response_id"] = continuation.last_response_id
        request_payload["input"] = copy.deepcopy(current_input[len(baseline) :])
        return request_payload

    def _remember_codex_websocket_continuation(
        self,
        payload: dict[str, Any],
        route: CodexWebSocketRoute,
        state: ResponsesStreamState,
    ) -> None:
        completed_response = state.completed_response
        if not isinstance(completed_response, Mapping):
            self._codex_websocket_continuation = None
            return
        response_id = completed_response.get("id")
        normalized_response = state.normalized_response()
        response_items = self._response_input(normalized_response, route[1])
        if (
            not isinstance(response_id, str)
            or not response_id
            or not isinstance(response_items, list)
        ):
            self._codex_websocket_continuation = None
            return
        self._codex_websocket_continuation = _CodexWebSocketContinuation(
            route=route,
            last_request_payload=copy.deepcopy(payload),
            last_response_id=response_id,
            last_response_items=[
                copy.deepcopy(item) for item in response_items if isinstance(item, dict)
            ],
        )
