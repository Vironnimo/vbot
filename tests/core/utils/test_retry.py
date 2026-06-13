"""Tests for the async retry utility.

Verifies exponential backoff timing, jitter bounds, max-retries
enforcement, fatal-error propagation, first-attempt success, and
retry/exhaustion logging.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.utils.errors import ProviderError
from core.utils.retry import (
    BACKOFF_FACTOR,
    INITIAL_DELAY_SECONDS,
    JITTER_FACTOR,
    MAX_RETRIES,
    MAX_RETRY_AFTER_SECONDS,
    retry_async,
)

# ----- Success path -----


@pytest.mark.asyncio
async def test_retry_succeeds_on_first_attempt():
    """No retries needed when the first call succeeds."""
    # Arrange
    mock_fn = AsyncMock(return_value="ok")

    # Act
    result = await retry_async(mock_fn)

    # Assert
    assert result == "ok"
    assert mock_fn.call_count == 1


# ----- Fatal (non-retryable) errors -----


@pytest.mark.asyncio
async def test_retry_does_not_retry_auth_error():
    """ProviderAuthError (retryable=False) is re-raised immediately."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderAuthError("Unauthorized"))

    # Act / Assert
    with pytest.raises(ProviderAuthError, match="Unauthorized"):
        await retry_async(mock_fn)

    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_does_not_retry_base_provider_error():
    """Base ProviderError with retryable=False (the default) is not retried."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderError("Something went wrong"))

    # Act / Assert
    with pytest.raises(ProviderError, match="Something went wrong"):
        await retry_async(mock_fn)

    assert mock_fn.call_count == 1


# ----- Retryable errors: exhaustion -----


@pytest.mark.asyncio
async def test_retry_stops_after_max_retries_rate_limit():
    """Stops after MAX_RETRIES retries on ProviderRateLimitError."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderRateLimitError("Rate limited"))

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderRateLimitError, match="Rate limited"),
    ):
        await retry_async(mock_fn)

    assert mock_fn.call_count == MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_retry_stops_after_max_retries_timeout():
    """Stops after MAX_RETRIES retries on ProviderTimeoutError."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderTimeoutError("Connection timed out"))

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderTimeoutError, match="Connection timed out"),
    ):
        await retry_async(mock_fn)

    assert mock_fn.call_count == MAX_RETRIES + 1


# ----- Retryable errors: eventual success -----


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_rate_limit():
    """Succeeds when a retryable rate-limit error is followed by success."""
    # Arrange
    mock_fn = AsyncMock(side_effect=[ProviderRateLimitError("Rate limited"), "ok"])

    # Act
    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(mock_fn)

    # Assert
    assert result == "ok"
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_timeout():
    """Succeeds when a ProviderTimeoutError is followed by success."""
    # Arrange
    mock_fn = AsyncMock(side_effect=[ProviderTimeoutError("Timeout"), "ok"])

    # Act
    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(mock_fn)

    # Assert
    assert result == "ok"
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_custom_retryable_provider_error():
    """Base ProviderError with retryable=True is retried."""
    # Arrange
    mock_fn = AsyncMock(side_effect=[ProviderError("Transient", retryable=True), "ok"])

    # Act
    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(mock_fn)

    # Assert
    assert result == "ok"
    assert mock_fn.call_count == 2


# ----- Exponential backoff timing -----


@pytest.mark.asyncio
async def test_retry_exponential_backoff_increases_delay():
    """Delays grow exponentially across retries when jitter is zero."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderRateLimitError("Rate limited"))
    recorded_delays: list[float] = []

    async def mock_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", side_effect=mock_sleep),
        patch("core.utils.retry.random.uniform", return_value=0.0),
        pytest.raises(ProviderRateLimitError),
    ):
        await retry_async(mock_fn)

    assert len(recorded_delays) == MAX_RETRIES
    assert recorded_delays[0] == pytest.approx(INITIAL_DELAY_SECONDS)
    assert recorded_delays[1] == pytest.approx(INITIAL_DELAY_SECONDS * BACKOFF_FACTOR)
    assert recorded_delays[2] == pytest.approx(INITIAL_DELAY_SECONDS * BACKOFF_FACTOR**2)


# ----- Jitter bounds -----


@pytest.mark.asyncio
async def test_retry_jitter_is_bounded():
    """Jitter makes delays non-deterministic but bounded."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderRateLimitError("Rate limited"))
    recorded_delays: list[float] = []

    async def mock_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", side_effect=mock_sleep),
        pytest.raises(ProviderRateLimitError),
    ):
        await retry_async(mock_fn)

    assert len(recorded_delays) == MAX_RETRIES
    for attempt, delay in enumerate(recorded_delays):
        base_delay = INITIAL_DELAY_SECONDS * (BACKOFF_FACTOR**attempt)
        # delay = base_delay + uniform(0, base_delay * JITTER_FACTOR)
        # So delay is in [base_delay, base_delay * (1 + JITTER_FACTOR)]
        assert delay >= base_delay
        assert delay <= base_delay * (1 + JITTER_FACTOR)


# ----- Retry-After honoring -----


def _rate_limit_with_retry_after(seconds: float) -> ProviderRateLimitError:
    """Build a rate-limit error carrying a server ``Retry-After`` hint."""
    error = ProviderRateLimitError("Rate limited")
    error.retry_after = seconds
    return error


@pytest.mark.asyncio
async def test_retry_honors_retry_after_as_floor_over_backoff():
    """A ``retry_after`` larger than the backoff replaces every computed delay."""
    # Arrange
    mock_fn = AsyncMock(side_effect=_rate_limit_with_retry_after(10.0))
    recorded_delays: list[float] = []

    async def mock_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", side_effect=mock_sleep),
        patch("core.utils.retry.random.uniform", return_value=0.0),
        pytest.raises(ProviderRateLimitError),
    ):
        await retry_async(mock_fn)

    # Every attempt's exponential backoff (1s, 2s, 4s) is below 10s, so the
    # provider hint dominates throughout.
    assert recorded_delays == [10.0, 10.0, 10.0]


@pytest.mark.asyncio
async def test_retry_after_smaller_than_backoff_keeps_exponential():
    """A ``retry_after`` below the computed backoff does not shorten the wait."""
    # Arrange
    mock_fn = AsyncMock(side_effect=_rate_limit_with_retry_after(0.1))
    recorded_delays: list[float] = []

    async def mock_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", side_effect=mock_sleep),
        patch("core.utils.retry.random.uniform", return_value=0.0),
        pytest.raises(ProviderRateLimitError),
    ):
        await retry_async(mock_fn)

    assert recorded_delays[0] == pytest.approx(INITIAL_DELAY_SECONDS)
    assert recorded_delays[1] == pytest.approx(INITIAL_DELAY_SECONDS * BACKOFF_FACTOR)
    assert recorded_delays[2] == pytest.approx(INITIAL_DELAY_SECONDS * BACKOFF_FACTOR**2)


@pytest.mark.asyncio
async def test_retry_after_is_capped_at_maximum():
    """An excessive ``retry_after`` is clamped to ``MAX_RETRY_AFTER_SECONDS``."""
    # Arrange
    mock_fn = AsyncMock(side_effect=_rate_limit_with_retry_after(9999.0))
    recorded_delays: list[float] = []

    async def mock_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", side_effect=mock_sleep),
        patch("core.utils.retry.random.uniform", return_value=0.0),
        pytest.raises(ProviderRateLimitError),
    ):
        await retry_async(mock_fn)

    assert all(delay == MAX_RETRY_AFTER_SECONDS for delay in recorded_delays)


@pytest.mark.asyncio
async def test_retry_logs_when_honoring_retry_after(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Honoring a server hint is noted in the retry log line."""
    # Arrange
    mock_fn = AsyncMock(side_effect=[_rate_limit_with_retry_after(10.0), "ok"])
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")

    # Act
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        patch("core.utils.retry.random.uniform", return_value=0.0),
    ):
        result = await retry_async(mock_fn)

    # Assert
    assert result == "ok"
    retry_records = [
        record
        for record in caplog.records
        if record.name == "vbot.utils.retry" and "Retryable error" in record.getMessage()
    ]
    assert len(retry_records) == 1
    assert "honoring server Retry-After" in retry_records[0].getMessage()


# ----- Logging -----


@pytest.mark.asyncio
async def test_retry_logs_warning_on_each_retry_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each retry is logged at WARNING with the error class and message."""
    # Arrange
    mock_fn = AsyncMock(side_effect=[ProviderRateLimitError("Rate limited"), "ok"])
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")

    # Act
    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(mock_fn)

    # Assert
    assert result == "ok"
    retry_records = [
        record
        for record in caplog.records
        if record.name == "vbot.utils.retry" and "Retryable error" in record.getMessage()
    ]
    assert len(retry_records) == 1
    message = retry_records[0].getMessage()
    assert "ProviderRateLimitError" in message
    assert "Rate limited" in message


@pytest.mark.asyncio
async def test_retry_logs_warning_when_retries_exhausted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exhausting all retries logs a warning right before re-raising."""
    # Arrange
    mock_fn = AsyncMock(side_effect=ProviderRateLimitError("Rate limited"))
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderRateLimitError),
    ):
        await retry_async(mock_fn)

    exhausted_records = [
        record
        for record in caplog.records
        if record.name == "vbot.utils.retry" and "Retries exhausted" in record.getMessage()
    ]
    assert len(exhausted_records) == 1
    assert "ProviderRateLimitError" in exhausted_records[0].getMessage()


@pytest.mark.asyncio
async def test_retry_does_not_log_on_success_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A first-attempt success logs nothing (hot path stays quiet)."""
    # Arrange
    mock_fn = AsyncMock(return_value="ok")
    caplog.set_level(logging.WARNING, logger="vbot.utils.retry")

    # Act
    await retry_async(mock_fn)

    # Assert
    assert [record for record in caplog.records if record.name == "vbot.utils.retry"] == []
