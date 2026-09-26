"""Compare automatic collaboration with explicitly selected ping-only wakes."""

from pathlib import Path
from typing import Any

import pytest

from core.sessions import DeliveryReceipt, SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SwarmStore
from tests.resources.extensions.swarm_store_helpers import _swarm, open_swarm_database


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["main", "discussion"])
@pytest.mark.parametrize("ping_only", [False, True], ids=["default-wakes", "ping-only-wakes"])
async def test_ping_only_wakes_retain_unaddressed_work_until_a_ping(
    tmp_path: Path, route: str, ping_only: bool
) -> None:
    receipts: dict[str, DeliveryReceipt] = {}

    async def lookup(
        _address: SessionAddress, _generation: str, _owner: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return receipts.get(receipt_id)

    database = open_swarm_database(tmp_path)
    store = SwarmStore(database, lookup_delivery_receipt=lookup)
    await store.open()

    async def acknowledge(prepared: dict[str, Any]) -> None:
        receipts[prepared["receipt_id"]] = DeliveryReceipt(
            prepared["receipt_id"],
            prepared["content_hash"],
            prepared["effect_kind"],
            {"kind": "note", "sequence": len(receipts)},
        )
        assert await store.reconcile_delivery(prepared["receipt_id"])

    try:
        started = await _swarm(store)
        sid = str(started["swarm_id"])
        swarm = await store.get_swarm(sid)
        sender, recipient = [item["id"] for item in swarm["participants"]]
        await store.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_recipient", "ses_recipient"),
                "gen_recipient",
                "swarm",
                sid,
                recipient,
                {},
            )
        )
        if ping_only:
            await store.apply_delivery_settings(
                sid,
                {
                    **swarm["delivery"],
                    "main": {"mode": "all", "wake_idle": False},
                    "discussion": {"mode": "all", "wake_idle": False},
                    "ping": {"mode": "all", "wake_idle": True},
                },
                expected_revision=swarm["settings_revision"],
                request_id="ping-only-settings",
                actor="test",
            )
        discussion_id = swarm["main_discussion_id"]
        if route == "discussion":
            discussion = await store.create_discussion(
                sid, sender, title="Review", text="Review context", request_id="discussion"
            )
            discussion_id = discussion["discussion_id"]
            await store.join_discussion(sid, recipient, discussion_id)
            await acknowledge(await store.prepare_inbox_delivery(sid, recipient))

        await store.set_participant_state(sid, recipient, "idle")
        ordinary = await store.post(
            sid,
            sender,
            discussion_id=discussion_id,
            text="Which source supports the proposed change?",
            request_id="ordinary-question",
        )
        expected = [(ordinary["post_id"], "Which source supports the proposed change?")]
        wake = await store.prepare_wake(sid, recipient, expected_epoch=0)
        assert wake["wake"] is (not ping_only)
        assert (await store.participant_status(sid, recipient))["pending_count"] == 1
        intents = (await store.list_wake_intents(sid)).entries
        assert [entry["participant_id"] for entry in intents] == ([] if ping_only else [recipient])

        if ping_only:
            assert not (await store.claim_wake(sid, recipient, expected_epoch=0))["pending"]
            ping = await store.post(
                sid,
                sender,
                discussion_id=discussion_id,
                text="Please review the source question above.",
                recipients=[recipient],
                request_id="review-ping",
            )
            expected.append((ping["post_id"], "Please review the source question above."))
            assert (await store.prepare_wake(sid, recipient, expected_epoch=0))["wake"]
            assert (await store.participant_status(sid, recipient))["pending_count"] == 2

        claim = await store.claim_wake(sid, recipient, expected_epoch=0)
        assert claim["pending"]
        assert (
            await store.mark_wake_admitted(
                sid,
                recipient,
                expected_epoch=0,
                run_id="review-run",
                boundary=claim["boundary"],
            )
        )["admitted"]

        delivered: list[tuple[str, str]] = []
        # A batch prepared before the ping remains authoritative until acknowledged.
        for _ in range(3):
            batch = await store.prepare_automatic_delivery(sid, recipient, expected_epoch=0)
            if not batch["entries"]:
                break
            assert await store.reconcile_delivery(batch["receipt_id"]) is False
            delivered.extend((entry["id"], entry["text"]) for entry in batch["entries"])
            await acknowledge(batch)
        assert delivered == expected
        assert (await store.participant_status(sid, recipient))["pending_count"] == 0
        assert (await store.prepare_inbox_delivery(sid, recipient))["entries"] == []
        await store.reconcile_run_finished(
            sid, recipient, run_id="review-run", expected_epoch=0, outcome="completed"
        )
        assert not (await store.prepare_wake(sid, recipient, expected_epoch=0))["wake"]
        assert not (await store.list_wake_intents(sid)).entries
    finally:
        await store.close()
        database.close()
