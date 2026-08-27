"""Compaction run coordination tests."""

from types import SimpleNamespace
from unittest.mock import Mock

from core.chat import ChatMessage
from core.compaction.run_coordination import CompactionRunCoordinator
from core.runs import COMPACTION_COMPLETED_EVENT


def _checkpoint(*, duration_ms: int | None) -> ChatMessage:
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Earlier decisions.",
        projection=[],
        compacted_token_count=123,
    )
    checkpoint = checkpoint.with_compaction_context_tokens(
        context_tokens_before=155_499,
        context_tokens_after=34_691,
    )
    if duration_ms is not None:
        checkpoint = checkpoint.with_compaction_duration_ms(duration_ms=duration_ms)
    return checkpoint


def _emit_completed(*, duration_ms: int | None) -> dict:
    run = SimpleNamespace(terminal_payload_extras={}, emit=Mock())
    checkpoint = _checkpoint(duration_ms=duration_ms)

    CompactionRunCoordinator._emit_compaction_completed(
        run,  # type: ignore[arg-type]
        [checkpoint],
        checkpoint,
    )

    assert run.emit.call_count == 1
    call = run.emit.call_args
    event_type: str = call.args[0]
    payload: dict = call.args[1]
    assert event_type == COMPACTION_COMPLETED_EVENT
    return payload


def test_completed_payload_carries_the_stamped_duration() -> None:
    payload = _emit_completed(duration_ms=54_000)

    assert payload["duration_ms"] == 54_000
    assert payload["message"]["usage"]["compaction_duration_ms"] == 54_000


def test_completed_payload_omits_duration_for_checkpoints_without_one() -> None:
    payload = _emit_completed(duration_ms=None)

    assert "duration_ms" not in payload
