"""Owner-bound Extension attention, page counts and approval visibility."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.extensions.notifications import MAX_NOTIFICATION_BODY, MAX_NOTIFICATION_TITLE, page_link
from core.runtime import Runtime
from core.sessions import SessionAddress
from core.utils.timestamps import is_canonical_timestamp
from tests.core.extensions.extension_test_support import tool_context


@pytest.mark.asyncio
async def test_owner_notifications_and_page_badges_validate_before_publishing_and_retire(
    runtime: Runtime,
) -> None:
    registry = runtime.extensions
    assert registry is not None
    identity = registry.registration_identity("swarm")
    root = runtime._extension_host()
    assert root.for_owner is not None
    host = root.for_owner(identity)
    assert host.notify is not None and host.set_page_badge is not None
    notifications: list[dict[str, Any]] = []
    changes: list[tuple[Any, ...]] = []
    runtime.set_extension_notification_publisher(notifications.append)
    runtime.set_extension_change_publisher(lambda *args: changes.append(args))
    try:
        for title in (" first ", "replacement"):
            await host.notify(
                title, " plain\nbody ", page_id="swarms", route="/card/one", key=" one "
            )
        assert [
            {key: value for key, value in row.items() if key != "at"} for row in notifications
        ] == [
            {
                "extension": "swarm",
                "page_id": "swarms",
                "route": "/card/one",
                "title": title,
                "body": " plain\nbody ",
                "key": " one ",
            }
            for title in (" first ", "replacement")
        ]
        assert all(is_canonical_timestamp(row["at"]) for row in notifications)
        await host.notify(
            "t" * MAX_NOTIFICATION_TITLE, "b" * MAX_NOTIFICATION_BODY, page_id="swarms"
        )
        invalid: list[dict[str, Any]] = [
            {"title": "t" * (MAX_NOTIFICATION_TITLE + 1)},
            {"body": "b" * (MAX_NOTIFICATION_BODY + 1)},
            {"title": ""},
            {"title": "one\ntwo"},
            {"body": "nul\x00"},
            {"key": ""},
            {"key": "k" * 129},
            {"page_id": "another-owner-page"},
            *(
                {"route": route}
                for route in (
                    "https://example.com",
                    "//example.com",
                    "/card/../one",
                    "/card//one",
                    "/card/",
                    "/",
                    "/card?one",
                    "/card#one",
                    "/card\\one",
                    "/card/%2f",
                    "/card/\x00",
                    "/" + "r" * 1024,
                )
            ),
        ]
        for fields in invalid:
            with pytest.raises(ValueError):
                await host.notify(
                    **{"title": "valid", "body": "body", "page_id": "swarms", **fields}
                )
        assert len(notifications) == 3
        host.set_page_badge("swarms", 3)
        host.set_page_badge("swarms", 3)
        assert registry.page_badge(identity, "swarms") == 3
        assert changes == [("swarm", "page_badges", ("swarms",), 1)]
        host.set_page_badge("swarms", 0)
        assert registry.page_badge(identity, "swarms") == 0
        for count in (-1, True, 1.5, 2**31):
            with pytest.raises(ValueError):
                host.set_page_badge("swarms", count)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            host.set_page_badge("other", 1)
        late = host.notify("late", "", page_id="swarms")
        registry.retire_registration()
        with pytest.raises(ValueError):
            await late
        with pytest.raises(ValueError):
            host.set_page_badge("swarms", 9)
        assert registry.page_badge(identity, "swarms") == 0
        assert len(notifications) == 3 and len(changes) == 2
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_pending_approval_read_exposes_only_requested_owned_runs_and_no_approval_content(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = runtime.extensions
    assert registry is not None
    root = runtime._extension_host()
    assert root.for_owner is not None
    host = root.for_owner(registry.registration_identity("swarm"))
    assert host.pending_approvals is not None and host.input_authority is not None
    address = SessionAddress("project", "tmp_worker", "ses_one")
    records = AsyncMock(return_value={"owned": SimpleNamespace(run_id="owned", address=address)})
    monkeypatch.setattr(runtime.chat_sessions, "owned_runs_by_id_async", records)
    monkeypatch.setattr(
        runtime.chat_run_manager,
        "approvals_snapshot",
        Mock(
            return_value={
                "revision": 8,
                "requests": [{"run_id": "owned", "secret": "hidden"}, {"run_id": "foreign"}],
            }
        ),
    )
    try:
        result = await host.pending_approvals("group", ["owned", "foreign"])
        records.assert_awaited_once_with(
            owner_name="swarm", group_id="group", run_ids=["owned", "foreign"]
        )
        assert result == {
            "revision": 8,
            "requests": [
                {
                    "run_id": "owned",
                    "project_id": "project",
                    "agent_id": "tmp_worker",
                    "session_id": "ses_one",
                }
            ],
        }
        assert (
            await host.input_authority(
                replace(tool_context("bash", runtime.storage.data_dir), run_id="unknown")
            )
            == "none"
        )
        registry.retire_registration()
        with pytest.raises(ValueError):
            await host.pending_approvals("group", ["owned"])
        with pytest.raises(ValueError):
            await host.input_authority(tool_context("bash", runtime.storage.data_dir))
    finally:
        await runtime.aclose()


def test_extension_page_link_is_an_encoded_fragment_not_an_external_destination() -> None:
    assert (
        page_link("factory", "board", "/card/card one")
        == "/#extension:factory:board/card/card%20one"
    )
    assert page_link("factory", "board") == "/#extension:factory:board"
    assert page_link("b\u00fcro owner", "board") == "/#extension:b%C3%BCro%20owner:board"
    for owner, page, route in (
        ("../other", "board", ""),
        ("factory", "board/other", ""),
        ("factory", "board", "//other"),
    ):
        with pytest.raises(ValueError):
            page_link(owner, page, route)
