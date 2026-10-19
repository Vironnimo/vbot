"""Recovery and delegation contracts of the access service."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.access.config import access_config
from core.access.devices import DeviceStore, new_device
from server.access import AccessError, AccessService, Principal
from server.access._rules import RequestFacts

_HOST = Principal("host")


def _service(**kwargs: Any) -> AccessService:
    return AccessService(
        access_config(
            "authenticated", public_url="https://vbot.example", trusted_proxies=["10.0.0.2"]
        ),
        local_token=None,
        **kwargs,
    )


def _facts(service: AccessService) -> RequestFacts:
    return service.inspect(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/pair/start",
            "scheme": "https",
            "client": ("203.0.113.7", 40000),
            "headers": [(b"host", b"vbot.example")],
        }
    )


async def _pair(service: AccessService) -> tuple[str, str]:
    facts = _facts(service)
    started = service.pair_start({"kind": "desktop", "name": "Desktop"}, facts)
    service.decide_pairing(started["code"], _HOST, approved=True)
    payload, _cookie = await service.pair_poll(started, facts)
    return payload["device"]["id"], payload["token"]


def _principal(service: AccessService, token: str) -> Principal:
    principal = service.authenticate(
        [(b"authorization", f"Bearer {token}".encode())], _facts(service)
    ).principal
    assert principal is not None
    return principal


@pytest.mark.parametrize("recovery", ["retry", "close"])
def test_failed_revocation_is_durable_after_retry_or_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    async def scenario() -> None:
        service = _service()
        await service.start(tmp_path)
        device_id, token = await _pair(service)

        def fail_write(*args: object, **kwargs: object) -> None:
            raise OSError("storage unavailable")

        with monkeypatch.context() as broken:
            broken.setattr("core.access.devices.write_json_document", fail_write)
            with pytest.raises(AccessError) as failed:
                await service.revoke_devices([device_id], _HOST)
            assert (failed.value.code, failed.value.retry_after) == ("devices_unavailable", 30)
        if recovery == "retry":
            assert [device.id for device in await service.revoke_devices([device_id], _HOST)] == [
                device_id
            ]
        await service.close()
        reloaded = DeviceStore(tmp_path)
        reloaded.load()
        assert reloaded.lookup(token, now=datetime.now(UTC))[0] == "invalid"

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["write_failure", "cancelled"])
def test_expiry_closes_connections_even_when_persistence_fails_or_caller_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    async def scenario() -> None:
        now = [datetime(2026, 1, 1, tzinfo=UTC)]
        service = _service(utc_now=lambda: now[0])
        await service.start(tmp_path)
        device_id, token = await _pair(service)
        connected = asyncio.Event()

        async def connection() -> None:
            service.connections.register(device_id)
            connected.set()
            await asyncio.Event().wait()

        live = asyncio.create_task(connection())
        await connected.wait()
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        write = DeviceStore._write

        def persist(store: DeviceStore, devices: Any) -> None:
            loop.call_soon_threadsafe(entered.set)
            if outcome == "write_failure":
                raise OSError("storage unavailable")
            assert release.wait(2), "expiry write was not released"
            write(store, devices)

        now[0] += timedelta(days=91)
        with monkeypatch.context() as broken:
            broken.setattr(DeviceStore, "_write", persist)
            assert (
                service.authenticate(
                    [(b"authorization", f"Bearer {token}".encode())], _facts(service)
                ).principal
                is None
            )
            await entered.wait()
            if outcome == "cancelled":
                expiry = next(
                    task
                    for task in asyncio.all_tasks()
                    if task.get_name() == "access-device-expiry"
                )
                expiry.cancel()
                release.set()
            await service.close()
        await asyncio.gather(live, return_exceptions=True)
        assert live.cancelled()
        assert service.connections.count(device_id) == 0
        await service.close()

    asyncio.run(scenario())


def test_redeeming_a_fresh_ticket_repairs_an_old_browser_delegation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = _service()
        await service.start(tmp_path)
        first_id, first_token = await _pair(service)
        ticket = service.issue_ticket(_principal(service, first_token), "Window")["ticket"]
        browser = await service.redeem_ticket(ticket, _facts(service))
        assert browser is not None and browser.token is not None
        browser_principal = _principal(service, browser.token)
        assert browser_principal.device is not None
        # Existing orphaned browsers, including explicitly spared ones, remain
        # usable. A fresh grant must establish the new logout relationship.
        await service.revoke_devices(
            [first_id], _HOST, spare=frozenset({browser_principal.device.id})
        )
        second_id, second_token = await _pair(service)
        ticket = service.issue_ticket(_principal(service, second_token), "Window")["ticket"]
        reused = await service.redeem_ticket(ticket, _facts(service), browser_principal)
        assert reused is not None and reused.token is None
        listed = service.list_devices(_HOST)["devices"]
        assert (
            next(item for item in listed if item["id"] == browser_principal.device.id)[
                "approved_by"
            ]
            == second_id
        )
        await service.revoke_devices([second_id], _principal(service, second_token))
        assert (
            service.authenticate(
                [(b"cookie", f"__Host-vbot_device={browser.token}".encode())], _facts(service)
            ).principal
            is None
        )
        await service.close()

    asyncio.run(scenario())


def test_a_failed_claim_keeps_its_lifetime_and_a_slow_claim_gets_a_full_retry_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        clock = [1_000.0]
        service = _service(clock=lambda: clock[0])
        await service.start(tmp_path)
        facts = _facts(service)
        started = service.pair_start({"kind": "cli", "name": "Laptop"}, facts)
        service.decide_pairing(started["code"], _HOST, approved=True)
        write = DeviceStore._write

        def fail_write(*args: object, **kwargs: object) -> None:
            raise OSError("storage unavailable")

        with monkeypatch.context() as broken:
            broken.setattr(DeviceStore, "_write", fail_write)
            with pytest.raises(AccessError) as failed:
                await service.pair_poll(started, facts)
            assert failed.value.code == "devices_unavailable"
        clock[0] += 61
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def slow_write(store: DeviceStore, devices: Any) -> None:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2), "claim write was not released"
            write(store, devices)

        with monkeypatch.context() as slow:
            slow.setattr(DeviceStore, "_write", slow_write)
            poll = asyncio.create_task(service.pair_poll(started, facts))
            await entered.wait()
            clock[0] = 1_601.0
            # The admitted claim stays recoverable even beyond the original TTL.
            service.pairings.sweep()
            release.set()
            approved = await poll
        assert approved[0]["status"] == "approved"
        clock[0] += 59
        assert await service.pair_poll(started, facts) == approved
        clock[0] += 2
        assert (await service.pair_poll(started, facts))[0] == {"status": "expired"}
        await service.close()

    asyncio.run(scenario())


def test_expiry_cascades_to_ticket_browsers_but_keeps_independent_pairings(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = DeviceStore(tmp_path)
    store.load()
    issuer, _token = new_device(
        name="Desktop", kind="desktop", approved_by="host", client=None, now=now
    )
    store.add(issuer)
    for via in ("ticket", "pairing"):
        child, _token = new_device(
            name=via,
            kind="browser",
            approved_by=issuer.id,
            client=None,
            now=now + timedelta(days=89),
            via=via,
        )
        store.add(child)

    # An older store can contain a newer child without having renewed its issuer.
    async def scenario() -> None:
        service = _service(utc_now=lambda: now + timedelta(days=91))
        await service.start(tmp_path)
        assert [device["via"] for device in service.list_devices(_HOST)["devices"]] == ["pairing"]
        await service.close()

    asyncio.run(scenario())
