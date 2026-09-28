"""Tests for the async retry utility.

Verifies the shared backoff math, which errors are retried, retry exhaustion,
server Retry-After hints, retry logging, retry observers and caller-owned retries.
"""

import asyncio
import logging
from contextlib import nullcontext
from unittest.mock import AsyncMock, patch

import pytest

from core.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.utils import retry as retry_module
from core.utils.errors import ProviderError
from core.utils.retry import (
    BACKOFF_FACTOR,
    INITIAL_DELAY_SECONDS,
    JITTER_FACTOR,
    MAX_RETRIES,
    caller_owns_retries,
    compute_retry_delay,
    observe_retries,
    retry_async,
)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each retry wait instead of sleeping."""
    recorded: list[float] = []

    async def record(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(retry_module, "_sleep", record)
    return recorded


def _retry_log(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.utils.retry" and text in record.getMessage()
    ]


# ----- compute_retry_delay — shared backoff math -----


def test_compute_retry_delay_backoff_without_hint():
    """Without a hint the delay is exponential backoff plus bounded jitter."""
    for attempt in range(3):
        base = INITIAL_DELAY_SECONDS * (BACKOFF_FACTOR**attempt)
        delay, honored = compute_retry_delay(attempt)
        assert base <= delay <= base * (1 + JITTER_FACTOR)
        assert honored is False


@pytest.mark.parametrize(
    ("attempt", "retry_after", "expected"),
    [
        # A hint above the base backoff becomes the floor under the same jitter.
        (0, 30.0, (30.25, True)),
        # A hint below the computed backoff never shortens the wait.
        (2, 0.001, (4.25, False)),
    ],
)
def test_compute_retry_delay_uses_a_larger_retry_after_as_its_floor(
    attempt: int, retry_after: float, expected: tuple[float, bool]
):
    with patch("core.utils.retry.random.uniform", return_value=0.25):
        assert compute_retry_delay(attempt, retry_after=retry_after) == expected


def test_capped_retry_after_cannot_shorten_later_exponential_backoff():
    with patch("core.utils.retry.random.uniform", return_value=0):
        delays = [compute_retry_delay(attempt, retry_after=10_000)[0] for attempt in range(8)]
    assert delays == [60, 60, 60, 60, 60, 60, 64, 128]


# ----- retry_async -----


@pytest.mark.asyncio
async def test_retry_returns_a_first_attempt_success_without_logging(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")
    operation = AsyncMock(return_value="ok")

    assert await retry_async(operation) == "ok"

    assert operation.call_count == 1
    assert [record for record in caplog.records if record.name == "vbot.utils.retry"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [ProviderAuthError("Unauthorized"), ProviderError("Something went wrong")],
    ids=["auth-error", "default-provider-error"],
)
async def test_retry_reraises_a_non_retryable_error_at_once(error: ProviderError):
    operation = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as raised:
        await retry_async(operation)

    assert raised.value is error
    assert operation.call_count == 1
    assert error.attempts_made == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderRateLimitError("Rate limited"),
        ProviderTimeoutError("Connection timed out"),
        ProviderError("Transient", retryable=True),
    ],
    ids=["rate-limit", "timeout", "retryable-provider-error"],
)
async def test_retry_retries_a_retryable_error_and_logs_the_retry(
    error: ProviderError, sleeps: list[float], caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")
    operation = AsyncMock(side_effect=[error, "ok"])

    assert await retry_async(operation) == "ok"

    assert operation.call_count == 2
    assert len(sleeps) == 1
    [message] = _retry_log(caplog, "Retryable error")
    assert type(error).__name__ in message
    assert str(error) in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller_owned", "attempts"),
    [(False, MAX_RETRIES + 1), (True, 1)],
    ids=["standalone", "caller-owned"],
)
async def test_retry_gives_up_after_its_attempts_with_exponential_backoff(
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    caller_owned: bool,
    attempts: int,
):
    monkeypatch.setattr(retry_module.random, "uniform", lambda *_args: 0.0)
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")
    operation = AsyncMock(side_effect=ProviderRateLimitError("Rate limited"))

    with (
        caller_owns_retries() if caller_owned else nullcontext(),
        pytest.raises(ProviderRateLimitError, match="Rate limited") as raised,
    ):
        await retry_async(operation)

    assert operation.call_count == attempts
    assert raised.value.attempts_made == attempts
    assert sleeps == [
        INITIAL_DELAY_SECONDS * BACKOFF_FACTOR**attempt for attempt in range(attempts - 1)
    ]
    # Every retry line counts against the same total the exhaustion line reports.
    retries = _retry_log(caplog, "Retryable error")
    assert [f"attempt {number}/{attempts} " in line for number, line in enumerate(retries, 1)] == [
        True
    ] * (attempts - 1)
    exhausted = _retry_log(caplog, "Retries exhausted")
    if caller_owned:
        # One attempt retried nothing; the caller logs the error it catches.
        assert exhausted == []
    else:
        [message] = exhausted
        assert f"after {attempts} attempts" in message
        assert "ProviderRateLimitError" in message


@pytest.mark.asyncio
async def test_retry_honors_a_server_retry_after_hint_and_logs_it(
    sleeps: list[float], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(retry_module.random, "uniform", lambda *_args: 0.0)
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")
    error = ProviderRateLimitError("Rate limited")
    error.retry_after = 10.0

    with pytest.raises(ProviderRateLimitError):
        await retry_async(AsyncMock(side_effect=error))

    # Every exponential backoff (1 s, 2 s, 4 s) is below the hint.
    assert sleeps == [10.0, 10.0, 10.0]
    messages = _retry_log(caplog, "Retryable error")
    assert len(messages) == MAX_RETRIES
    assert all("honoring server Retry-After" in message for message in messages)


@pytest.mark.asyncio
async def test_retry_observers_are_isolated_and_reset_after_failure():
    notices = [[], []]

    async def request(index):
        failure = ProviderTimeoutError(f"request-{index}")
        operation = AsyncMock(side_effect=[failure, "done"])
        with observe_retries(notices[index].append):
            assert await retry_async(operation, initial_delay=0) == "done"
        return failure

    failures = await asyncio.gather(request(0), request(1))
    for index, entries in enumerate(notices):
        assert len(entries) == 2
        assert all(entry.error is failures[index] for entry in entries)
        assert [(entry.attempt, entry.max_attempts, entry.waiting) for entry in entries] == [
            (2, 4, True),
            (2, 4, False),
        ]
    with pytest.raises(ProviderAuthError), observe_retries(notices[0].append):
        await retry_async(AsyncMock(side_effect=ProviderAuthError("rejected")))
    await retry_async(AsyncMock(side_effect=[ProviderTimeoutError(), "done"]), initial_delay=0)
    assert len(notices[0]) == 2


@pytest.mark.asyncio
async def test_retry_observer_failure_does_not_change_request_outcome(caplog):
    def broken(_notice):
        raise RuntimeError("observer sentinel")

    with observe_retries(broken):
        assert (
            await retry_async(
                AsyncMock(side_effect=[ProviderTimeoutError(), "done"]), initial_delay=0
            )
            == "done"
        )
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_caller_retry_ownership_is_nested_task_local_and_restored():
    async def externally_owned():
        operation = AsyncMock(side_effect=ProviderTimeoutError())
        with caller_owns_retries():
            with caller_owns_retries(), pytest.raises(ProviderTimeoutError):
                await retry_async(operation, initial_delay=0)
            with pytest.raises(ProviderTimeoutError):
                await retry_async(operation, initial_delay=0)
        assert operation.await_count == 2
        restored = AsyncMock(side_effect=[ProviderTimeoutError(), "restored"])
        assert await retry_async(restored, initial_delay=0) == "restored"

    async def standalone():
        operation = AsyncMock(side_effect=ProviderTimeoutError())
        with pytest.raises(ProviderTimeoutError):
            await retry_async(operation, initial_delay=0)
        assert operation.await_count == 4

    await asyncio.gather(externally_owned(), standalone())
