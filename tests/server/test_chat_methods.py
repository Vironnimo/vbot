"""Tests for chat RPC handlers added in Phase 4 (cancellation controls).

Test coverage:
- ``chat.cancel_tool_call`` cancels a running tool call and returns ``{ ok: true }``.
- ``chat.cancel_tool_call`` returns the not-found error for an unknown run.
- ``chat.cancel_tool_call`` returns the not-found error for an unknown tool call id.
- ``chat.cancel`` with ``reason="user"`` propagates the reason to ``Run.request_cancel``.
- ``chat.cancel`` rejects unsupported params.
- ``chat.cancel_tool_call`` rejects unsupported params.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.runs import Run
from server.delegates import dispatch_rpc
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RPC_ERROR_RUN_NOT_FOUND
from tests.server.test_rpc import StubAdapter, make_state

JsonObject = dict[str, Any]


# ---------------------------------------------------------------------------
# chat.cancel_tool_call
# ---------------------------------------------------------------------------


class TestChatCancelToolCall:
    """Tests for the ``chat.cancel_tool_call`` RPC method."""

    @pytest.mark.asyncio
    async def test_cancels_running_tool_call_and_returns_ok(self, tmp_path: Path) -> None:
        """A registered tool call gets cancelled; the RPC returns ``{ok: True}``."""
        state = make_state(tmp_path, StubAdapter())
        run = await state.chat_runs.start(
            agent_id="coder",
            session_id="session-one",
            executor=_hold_forever_executor,
        )
        invocations: list[str] = []

        def abort() -> None:
            invocations.append("aborted")

        run.register_tool_cancel("tool-1", abort)

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel_tool_call",
                "params": {"run_id": run.id, "tool_call_id": "tool-1"},
            },
        )

        assert response["ok"] is True
        assert response["result"] == {"ok": True}
        assert invocations == ["aborted"]
        assert run.tool_call_cancelled("tool-1") is True
        assert run.cancel_requested is False

        # Clean up: cancel the held run so the executor task can finish.
        run.request_cancel()

    @pytest.mark.asyncio
    async def test_returns_not_found_for_unknown_run(self, tmp_path: Path) -> None:
        """An unknown run id surfaces as ``run_not_found``."""
        state = make_state(tmp_path, StubAdapter())

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel_tool_call",
                "params": {"run_id": "missing-run", "tool_call_id": "tool-1"},
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_RUN_NOT_FOUND
        assert "missing-run" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_returns_not_found_for_unknown_tool_call_id(self, tmp_path: Path) -> None:
        """An unknown tool call id surfaces as a not-found error."""
        state = make_state(tmp_path, StubAdapter())
        run = await state.chat_runs.start(
            agent_id="coder",
            session_id="session-one",
            executor=_hold_forever_executor,
        )

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel_tool_call",
                "params": {"run_id": run.id, "tool_call_id": "tool-missing"},
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_RUN_NOT_FOUND
        assert "tool-missing" in response["error"]["message"]
        assert run.tool_call_cancelled("tool-missing") is False
        assert run.cancel_requested is False

        # Clean up: cancel the held run so the executor task can finish.
        run.request_cancel()

    @pytest.mark.asyncio
    async def test_rejects_unsupported_params(self, tmp_path: Path) -> None:
        """Unknown params are rejected by the field allowlist."""
        state = make_state(tmp_path, StubAdapter())
        run = await state.chat_runs.start(
            agent_id="coder",
            session_id="session-one",
            executor=_hold_forever_executor,
        )

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel_tool_call",
                "params": {
                    "agent_id": "coder",
                    "run_id": run.id,
                    "tool_call_id": "tool-1",
                    "extra": True,
                },
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
        assert "unsupported chat.cancel_tool_call fields" in response["error"]["message"]
        assert "extra" in response["error"]["message"]

        # Clean up: cancel the held run so the executor task can finish.
        run.request_cancel()


# ---------------------------------------------------------------------------
# chat.cancel (extended with reason)
# ---------------------------------------------------------------------------


class TestChatCancelReason:
    """Tests for the ``reason`` param on ``chat.cancel``."""

    @pytest.mark.asyncio
    async def test_propagates_user_reason_to_run_request_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``chat.cancel`` forwards ``reason="user"`` to ``Run.request_cancel``."""
        state = make_state(tmp_path, StubAdapter())
        observed_reasons: list[str | None] = []
        original_request_cancel = Run.request_cancel

        def capturing_request_cancel(self: Run, reason: str | None = None) -> None:
            observed_reasons.append(reason)
            original_request_cancel(self, reason=reason)

        monkeypatch.setattr(Run, "request_cancel", capturing_request_cancel)

        run = await state.chat_runs.start(
            agent_id="coder",
            session_id="session-one",
            executor=_hold_forever_executor,
        )
        await asyncio.sleep(0)

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel",
                "params": {"run_id": run.id, "reason": "user"},
            },
        )

        assert response["ok"] is True
        assert observed_reasons == ["user"]
        assert run.cancel_reason == "user"

    @pytest.mark.asyncio
    async def test_omits_reason_when_not_provided(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``reason`` is omitted, ``Run.request_cancel`` receives ``None``."""
        state = make_state(tmp_path, StubAdapter())
        observed_reasons: list[str | None] = []
        original_request_cancel = Run.request_cancel

        def capturing_request_cancel(self: Run, reason: str | None = None) -> None:
            observed_reasons.append(reason)
            original_request_cancel(self, reason=reason)

        monkeypatch.setattr(Run, "request_cancel", capturing_request_cancel)

        run = await state.chat_runs.start(
            agent_id="coder",
            session_id="session-one",
            executor=_hold_forever_executor,
        )
        await asyncio.sleep(0)

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel",
                "params": {"run_id": run.id},
            },
        )

        assert response["ok"] is True
        assert observed_reasons == [None]
        assert run.cancel_reason is None

    @pytest.mark.asyncio
    async def test_rejects_unsupported_params(self, tmp_path: Path) -> None:
        """Unknown params are rejected by the field allowlist."""
        state = make_state(tmp_path, StubAdapter())

        response = await dispatch_rpc(
            state,
            {
                "method": "chat.cancel",
                "params": {"run_id": "any", "tool_call_id": "tool-1"},
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
        assert "unsupported chat.cancel fields" in response["error"]["message"]
        assert "tool_call_id" in response["error"]["message"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _hold_forever_executor(_run: Run) -> str:
    """Executor that waits forever until the test releases the run."""
    await asyncio.Event().wait()
    return "done"
