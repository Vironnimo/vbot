"""Recording double for request-boundary accounting regression tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.usage._failures import classify_failure


class RecordingUsageRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.input_estimates: list[tuple[str, int, int]] = []

    def input_estimate_factor(self, model: str) -> float:
        return 1.0

    def record_input_estimate(self, model: str, *, measured: int, estimated: int) -> None:
        self.input_estimates.append((model, measured, estimated))

    async def start(self, **context: Any) -> str:
        call_id = f"usage-{len(self.calls) + 1}"
        self.calls.append({**context, "id": call_id, "status": "started", "usage": None})
        return call_id

    async def finish(
        self,
        call_id: str,
        usage: Mapping[str, Any] | None = None,
        *,
        status: str = "completed",
        failure: Any = None,
    ) -> dict[str, Any]:
        row = next(call for call in self.calls if call["id"] == call_id)
        row["status"] = status
        row["failure_class"] = classify_failure(failure)[0] if status == "failed" else None
        return await self.update(call_id, usage or {})

    async def update(
        self,
        call_id: str,
        usage: Mapping[str, Any],
        *,
        input_estimate: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        row = next(call for call in self.calls if call["id"] == call_id)
        row["usage"] = {**usage, "usage_call_id": call_id}
        measured = usage.get("input_tokens")
        if (
            input_estimate is not None
            and isinstance(measured, int)
            and usage.get("input_tokens_estimated") is not True
        ):
            self.input_estimates.append((input_estimate[0], measured, input_estimate[1]))
        return dict(row["usage"])
