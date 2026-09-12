"""Swarm store: delivery behavior."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
import pytest

from core.sessions import DeliveryReceipt, SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SwarmStore, SwarmStoreError
from tests.resources.extensions.swarm_store_helpers import (
    _swarm,
)
from tests.resources.extensions.swarm_store_helpers import (
    store as store,
)


@pytest.mark.asyncio
async def test_prepared_delivery_reconciles_only_matching_canonical_receipts(tmp_path) -> None:
    receipts: dict[str, DeliveryReceipt] = {}

    async def lookup(
        _address: SessionAddress, _generation: str, _owner: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return receipts.get(receipt_id)

    value = SwarmStore(tmp_path / "delivery.db", lookup_delivery_receipt=lookup)
    await value.open()
    try:
        started = await _swarm(value)
        swarm = await value.get_swarm(started["swarm_id"])
        sender, recipient = [item["id"] for item in swarm["participants"]]
        binding = TemporarySessionBinding(
            SessionAddress(None, "tmp_agent", "ses_delivery"),
            "gen_delivery",
            "swarm",
            started["swarm_id"],
            recipient,
            {},
        )
        await value.bind_participant_session(binding)
        await value.post(started["swarm_id"], sender, text="one", request_id="delivery-one")
        prepared = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        assert [entry["text"] for entry in prepared["entries"]] == ["one"]
        assert await value.reconcile_delivery(prepared["receipt_id"]) is False
        receipts[prepared["receipt_id"]] = DeliveryReceipt(
            prepared["receipt_id"],
            prepared["content_hash"],
            prepared["effect_kind"],
            {"kind": "tool", "sequence": 4},
        )
        assert await value.reconcile_delivery(prepared["receipt_id"]) is True
        assert await value.reconcile_delivery(prepared["receipt_id"]) is True
        assert (await value.prepare_inbox_delivery(started["swarm_id"], recipient))["entries"] == []
        receipts[prepared["receipt_id"]] = DeliveryReceipt(
            prepared["receipt_id"],
            "different",
            prepared["effect_kind"],
            {"kind": "tool", "sequence": 4},
        )
        with pytest.raises(SwarmStoreError, match="receipt_conflict"):
            await value.reconcile_delivery(prepared["receipt_id"])
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_inbox_batches_complete_posts_without_exceeding_character_budget(tmp_path) -> None:
    async def lookup(*_arguments: object) -> DeliveryReceipt | None:
        return None

    value = SwarmStore(tmp_path / "batch.db", lookup_delivery_receipt=lookup)
    await value.open()
    try:
        started = await _swarm(value)
        swarm = await value.get_swarm(started["swarm_id"])
        sender, recipient = [item["id"] for item in swarm["participants"]]
        await value.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_agent", "ses_batch"),
                "gen_batch",
                "swarm",
                started["swarm_id"],
                recipient,
                {},
            )
        )
        for index, size in enumerate((16_000, 8_000, 1), start=1):
            await value.post(
                started["swarm_id"], sender, text="x" * size, request_id=f"batch-{index}"
            )
        prepared = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        assert [len(entry["text"]) for entry in prepared["entries"]] == [16_000, 8_000]
        assert prepared["pending_remaining"] == 1
        discussions = await value.list_discussions(started["swarm_id"], recipient)
        assert discussions.entries[0]["pending_count"] == 3
        await value.apply_delivery_settings(
            started["swarm_id"],
            {**swarm["delivery"], "batch_chars": 16_000},
            expected_revision=1,
            request_id="smaller-batches",
            actor="user",
        )
        current = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        assert [len(entry["text"]) for entry in current["entries"]] == [16_000]
        assert current["pending_remaining"] == 2
        assert prepared["entries"][1]["text"] == "x" * 8_000
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_settings_are_revisioned_audited_and_closed_swarms_reject_board_mutations(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    original = await store.get_swarm(started["swarm_id"])
    updated_delivery = {**original["delivery"], "main": {"mode": "pull", "wake_idle": False}}
    saved = await store.apply_delivery_settings(
        started["swarm_id"],
        updated_delivery,
        expected_revision=1,
        request_id="settings-1",
        actor="user",
    )
    assert saved["revision"] == 2
    assert (await store.get_swarm(started["swarm_id"]))["profile_snapshot"]["delivery"] == original[
        "delivery"
    ]
    assert (await store.get_swarm(started["swarm_id"]))["delivery"] == updated_delivery
    replay = await store.apply_delivery_settings(
        started["swarm_id"],
        updated_delivery,
        expected_revision=1,
        request_id="settings-1",
        actor="user",
    )
    assert replay["replayed"] is True
    await store.begin_stop(started["swarm_id"], request_id="stop", actor="user")
    await store.finish_stop(
        started["swarm_id"], request_id="stop-finish", actor="user", drain_report={"drained": True}
    )
    with pytest.raises(SwarmStoreError, match="swarm_closed"):
        await store.post(
            started["swarm_id"],
            original["participants"][0]["id"],
            text="closed",
            request_id="closed-post",
        )


@pytest.mark.asyncio
async def test_overlapping_prepared_batches_acknowledge_each_recipient_once(tmp_path) -> None:
    receipts: dict[str, DeliveryReceipt] = {}

    async def lookup(
        _address: SessionAddress, _generation: str, _owner: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return receipts.get(receipt_id)

    value = SwarmStore(tmp_path / "overlap.db", lookup_delivery_receipt=lookup)
    await value.open()
    try:
        started = await _swarm(value)
        swarm = await value.get_swarm(started["swarm_id"])
        sender, recipient = [item["id"] for item in swarm["participants"]]
        await value.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_agent", "ses_overlap"),
                "gen_overlap",
                "swarm",
                started["swarm_id"],
                recipient,
                {},
            )
        )
        await value.post(started["swarm_id"], sender, text="shared", request_id="overlap-post")
        inbox = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        page = await value.read_posts(started["swarm_id"], recipient)
        board = await value.prepare_board_read_delivery(
            started["swarm_id"], recipient, [entry["id"] for entry in page.entries]
        )
        for prepared in (board, inbox):
            receipts[prepared["receipt_id"]] = DeliveryReceipt(
                prepared["receipt_id"],
                prepared["content_hash"],
                prepared["effect_kind"],
                {"kind": "tool", "sequence": 9},
            )
        assert await value.reconcile_delivery(board["receipt_id"])
        assert await value.reconcile_delivery(inbox["receipt_id"])
        assert (await value.prepare_inbox_delivery(started["swarm_id"], recipient))["entries"] == []
    finally:
        await value.close()
