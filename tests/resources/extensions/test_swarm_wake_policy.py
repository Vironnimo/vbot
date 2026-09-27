"""Wake policy: ping-only settings, quiet participants, and wake announcements."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.sessions import DeliveryReceipt, SessionAddress, TemporarySessionBinding
from resources.extensions.swarm import _wake_pacing
from resources.extensions.swarm._wake_pacing import ADDRESSED_ROUTES, WakePacing
from resources.extensions.swarm.store import SwarmStore
from tests.resources.extensions.swarm_store_helpers import _swarm, open_swarm_database


@asynccontextmanager
async def _two_participants(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    """Open a Swarm whose second participant has a bound Session and is idle."""

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
        sender, recipient = swarm["participants"]
        await store.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_recipient", "ses_recipient"),
                "gen_recipient",
                "swarm",
                sid,
                recipient["id"],
                {},
            )
        )
        await store.set_participant_state(sid, recipient["id"], "idle")
        yield SimpleNamespace(
            store=store,
            sid=sid,
            swarm=swarm,
            sender=sender["id"],
            recipient=recipient["id"],
            recipient_name=recipient["display_name"],
            acknowledge=acknowledge,
        )
    finally:
        await store.close()
        database.close()


async def _run_wake(env: SimpleNamespace, run_id: str) -> list[str]:
    """Admit the announced wake, deliver every batch its Run receives, and finish it."""

    store, sid, recipient = env.store, env.sid, env.recipient
    claim = await store.claim_wake(sid, recipient, expected_epoch=0)
    assert claim["pending"]
    admitted = await store.mark_wake_admitted(
        sid, recipient, expected_epoch=0, run_id=run_id, boundary=claim["boundary"]
    )
    assert admitted["admitted"]
    batch = await store.prepare_automatic_delivery(sid, recipient, expected_epoch=0)
    await env.acknowledge(batch)
    await store.reconcile_run_finished(
        sid, recipient, run_id=run_id, expected_epoch=0, outcome="completed"
    )
    return [entry["text"] for entry in batch["entries"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["main", "discussion"])
@pytest.mark.parametrize("ping_only", [False, True], ids=["default-wakes", "ping-only-wakes"])
async def test_ping_only_wakes_retain_unaddressed_work_until_a_ping(
    tmp_path: Path, route: str, ping_only: bool
) -> None:
    async with _two_participants(tmp_path) as env:
        store, sid, swarm = env.store, env.sid, env.swarm
        sender, recipient, acknowledge = env.sender, env.recipient, env.acknowledge
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


@pytest.mark.asyncio
async def test_quiet_wake_waits_for_addressed_or_user_posts_without_freezing_a_batch(
    tmp_path: Path,
) -> None:
    async with _two_participants(tmp_path) as env:
        store, sid, recipient = env.store, env.sid, env.recipient

        async def quiet_wake() -> bool:
            wake = await store.prepare_wake(
                sid, recipient, expected_epoch=0, wake_routes=ADDRESSED_ROUTES
            )
            return bool(wake["wake"])

        await store.post(sid, env.sender, text="Status update", request_id="ordinary")
        assert not await quiet_wake()
        assert not (await store.list_wake_intents(sid)).entries
        await store.post(
            sid, env.sender, text=f"{env.recipient_name}, please check", request_id="addressed"
        )
        assert await quiet_wake()
        # The held scan froze nothing, so the Run receives both posts in one batch.
        assert await _run_wake(env, "addressed-run") == [
            "Status update",
            f"{env.recipient_name}, please check",
        ]

        await store.post(sid, env.sender, text="Another update", request_id="ordinary-2")
        assert not await quiet_wake()
        await store.post_human(sid, text="Please summarize", request_id="user")
        assert await quiet_wake()
        assert await _run_wake(env, "user-run") == ["Another update", "Please summarize"]


@pytest.mark.asyncio
async def test_wake_announces_only_its_first_batch_so_the_remainder_wakes_again(
    tmp_path: Path,
) -> None:
    async with _two_participants(tmp_path) as env:
        store, sid, swarm = env.store, env.sid, env.swarm
        await store.apply_delivery_settings(
            sid,
            {**swarm["delivery"], "batch_messages": 2},
            expected_revision=swarm["settings_revision"],
            request_id="small-batches",
            actor="test",
        )
        for index in range(3):
            await store.post(sid, env.sender, text=f"Post {index}", request_id=f"post-{index}")
        assert (await store.prepare_wake(env.sid, env.recipient, expected_epoch=0))["wake"]
        # A Run that ends after its first request received only the first batch.
        assert await _run_wake(env, "first-run") == ["Post 0", "Post 1"]
        assert (await store.prepare_wake(env.sid, env.recipient, expected_epoch=0))["wake"]
        assert await _run_wake(env, "second-run") == ["Post 2"]
        assert not (await store.prepare_wake(env.sid, env.recipient, expected_epoch=0))["wake"]


class _Timer:
    def __init__(self, delay: float, callback: Callable[..., None], args: tuple[Any, ...]):
        self.delay, self.callback, self.args = delay, callback, args
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback(*self.args)


class _Loop:
    def __init__(self) -> None:
        self.timers: list[_Timer] = []

    def call_later(self, delay: float, callback: Callable[..., None], *args: Any) -> _Timer:
        self.timers.append(_Timer(delay, callback, args))
        return self.timers[-1]


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    loop = _Loop()
    monkeypatch.setattr(_wake_pacing.asyncio, "get_running_loop", lambda: loop)
    ended: list[str] = []
    return SimpleNamespace(loop=loop, ended=ended, pacing=WakePacing(ended.append))


def test_runs_without_tools_lengthen_the_quiet_period_up_to_its_maximum(paced) -> None:
    pacing, loop = paced.pacing, paced.loop
    for run in range(5):
        pacing.run_finished("swarm", "reader", f"run-{run}", completed=True)
        assert pacing.wake_routes("swarm", "reader") == ADDRESSED_ROUTES
    assert [timer.delay for timer in loop.timers] == [30.0, 60.0, 120.0, 240.0, 240.0]
    assert [timer.cancelled for timer in loop.timers] == [True, True, True, True, False]
    assert pacing.wake_routes("swarm", "writer") is None

    # A replaced period ending late changes nothing.
    loop.timers[0].fire()
    assert paced.ended == [] and pacing.wake_routes("swarm", "reader") == ADDRESSED_ROUTES
    loop.timers[-1].fire()
    assert paced.ended == ["swarm"] and pacing.wake_routes("swarm", "reader") is None
    # The level survives the end of a period.
    pacing.run_finished("swarm", "reader", "run-5", completed=True)
    assert loop.timers[-1].delay == 240.0


def test_a_run_that_uses_a_tool_or_does_not_complete_ends_pacing(paced) -> None:
    pacing, loop = paced.pacing, paced.loop
    pacing.run_finished("swarm", "reader", "run-1", completed=True)
    pacing.run_finished("swarm", "reader", "run-2", completed=True)
    pacing.tool_used("swarm", "reader", "run-3")
    pacing.run_finished("swarm", "reader", "run-3", completed=True)
    assert loop.timers[-1].cancelled and pacing.wake_routes("swarm", "reader") is None
    pacing.run_finished("swarm", "reader", "run-4", completed=True)
    assert loop.timers[-1].delay == 30.0

    # A Tool used by an earlier Run does not count for the next one.
    pacing.tool_used("swarm", "reader", "run-5")
    pacing.run_finished("swarm", "reader", "run-6", completed=True)
    assert loop.timers[-1].delay == 60.0
    pacing.run_finished("swarm", "reader", "run-7", completed=False)
    assert loop.timers[-1].cancelled and pacing.wake_routes("swarm", "reader") is None
    pacing.run_finished("swarm", "reader", "run-8", completed=True)
    assert loop.timers[-1].delay == 30.0


def test_forgetting_a_swarm_or_closing_cancels_its_quiet_periods(paced) -> None:
    pacing, loop = paced.pacing, paced.loop
    pacing.run_finished("stopped", "reader", "run-1", completed=True)
    pacing.run_finished("running", "reader", "run-2", completed=True)
    pacing.forget("stopped")
    assert [timer.cancelled for timer in loop.timers] == [True, False]
    assert pacing.wake_routes("stopped", "reader") is None
    assert pacing.wake_routes("running", "reader") == ADDRESSED_ROUTES
    pacing.close()
    assert loop.timers[1].cancelled and pacing.wake_routes("running", "reader") is None
