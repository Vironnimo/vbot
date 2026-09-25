"""Recording double for request-boundary accounting regression tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RecordingUsageRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
    ) -> dict[str, Any]:
        row = next(call for call in self.calls if call["id"] == call_id)
        row["status"] = status
        return await self.update(call_id, usage or {})

    async def update(self, call_id: str, usage: Mapping[str, Any]) -> dict[str, Any]:
        row = next(call for call in self.calls if call["id"] == call_id)
        row["usage"] = {**usage, "usage_call_id": call_id}
        return dict(row["usage"])
