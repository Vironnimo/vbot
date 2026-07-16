"""Shared dependencies and assertions for Run tests."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatLoop, ChatSessionManager
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    ActiveRunError,
    ChatRunManager,
    QueuedRunItem,
    Run,
    RunAdmissionBlockedError,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
    WaitingWorkLimitError,
)
from core.utils.errors import VBotError


def assert_timing_payload(payload: dict[str, Any]) -> None:
    timing = payload.get("timing")
    assert isinstance(timing, dict)
    assert isinstance(timing.get("started_at"), str)
    assert isinstance(timing.get("completed_at"), str)
    assert isinstance(timing.get("duration_ms"), int)
    assert timing["duration_ms"] >= 0


__all__ = [
    "asyncio",
    "logging",
    "aclosing",
    "Path",
    "SimpleNamespace",
    "Any",
    "pytest",
    "ChatLoop",
    "ChatSessionManager",
    "ASSISTANT_OUTPUT_DELTA_EVENT",
    "REASONING_DELTA_EVENT",
    "RUN_STARTED_EVENT",
    "TOOL_CALL_DELTA_EVENT",
    "ActiveRunError",
    "ChatRunManager",
    "QueuedRunItem",
    "Run",
    "RunAdmissionBlockedError",
    "RunCancelledError",
    "RunNotFoundError",
    "RunStatus",
    "WaitingWorkLimitError",
    "VBotError",
    "assert_timing_payload",
]
