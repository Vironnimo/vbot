"""Independent Wiki conformance inputs and durable-effect checks."""

from __future__ import annotations

from typing import Any


async def wiki_cases(store: Any, sid: str, pid: str) -> list[tuple[str, dict[str, Any], bool]]:
    async def create(title: str, content: str):
        return await store.wiki(
            sid, pid, {"action": "create", "title": title, "content": content, "request_id": title}
        )

    page = await create("Fixture", "alpha\nbeta")
    await create("Long", "x" * 12500)
    long = (await store.wiki(sid, pid, {"action": "list", "query": "Long"}))["entries"][0]
    listing = await store.wiki(sid, pid, {"action": "list", "limit": 1})
    target = page["page_id"]
    read = {"action": "read", "page_id": target}
    update = {
        "action": "update",
        "page_id": target,
        "expected_revision": 1,
        "title": "Renamed",
        "request_id": "rename",
    }
    create_args = {"action": "create", "title": "New", "content": "", "request_id": "new"}
    tolerance_cases = []
    for name, content, old, new, succeeds in [
        ("formatting", "Before\r\n“Ready” — wait…\r\nAfter", '"Ready" -- wait...', "Done", True),
        (
            "indentation",
            "Before\n    First\n    Second\nAfter",
            "First\nSecond",
            "Changed\nSecond",
            True,
        ),
        ("whitespace", "Before\nFirst   finding\nAfter", "First finding", "Verified finding", True),
        ("ambiguous", "“Ready” and “Ready”", '"Ready"', "Done", False),
        (
            "changed",
            "Start\nThe result is verified.\nEnd",
            "Start\nThe result is pending.\nEnd",
            "Changed",
            False,
        ),
    ]:
        fixture = await create("tolerance_" + name, content)
        await store.wiki(
            sid,
            pid,
            {
                "action": "update",
                "page_id": fixture["page_id"],
                "expected_revision": 1,
                "title": "Peer " + name,
                "request_id": "peer-" + name,
            },
        )
        tolerance_cases.append(
            (
                "tolerance_" + name,
                {
                    "action": "update",
                    "page_id": fixture["page_id"],
                    "expected_revision": 1,
                    "old_text": old,
                    "new_text": new,
                    "request_id": "tolerance-" + name,
                },
                succeeds,
            )
        )
    return [
        ("list_default", {"action": "list"}, True),
        ("list_max", {"action": "list", "limit": 100}, True),
        ("list_cursor", listing["next_call"]["arguments"], True),
        ("list_changed_cursor", {**listing["next_call"]["arguments"], "query": "wrong"}, False),
        ("list_query", {"action": "list", "query": "BETA"}, True),
        ("list_deleted", {"action": "list", "include_deleted": True}, True),
        ("read_default", read, True),
        ("read_revision", {**read, "revision": 1}, True),
        ("read_offset", {**read, "offset": 6, "limit": 4}, True),
        ("read_large", {"action": "read", "page_id": long["page_id"]}, True),
        ("read_max", {**read, "limit": 20000}, True),
        ("read_unknown", {**read, "page_id": "foreign"}, False),
        (
            "recovered_wrapper",
            {"request": {"operation": "READ", "page_id": target, "limti": "5"}},
            True,
        ),
        ("recovered_boolean", {"action": "list", "include_deleted": "false"}, True),
        ("recovered_identity", {**read, "swarm_id": sid, "participant_id": pid}, True),
        ("invalid_identity", {**read, "swarm_id": "foreign"}, False),
        ("invalid_fraction", {**read, "limit": 1.5}, False),
        ("recovered_null", {**read, "revision": None}, True),
        ("invalid_field", {**read, "publish": True}, False),
        ("create", create_args, True),
        ("create_replay", create_args, True),
        ("create_conflict", {**create_args, "content": "different"}, False),
        ("update_title", update, True),
        ("update_replay", update, True),
        ("update_stale", {**update, "request_id": "stale"}, False),
        (
            "update_patch",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 1,
                "old_text": "beta",
                "new_text": "gamma",
                "request_id": "patch",
            },
            True,
        ),
        (
            "update_content",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 3,
                "content": "whole page",
                "request_id": "replace",
            },
            True,
        ),
        (
            "update_missing",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 4,
                "request_id": "missing",
            },
            False,
        ),
        (
            "update_ambiguous",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 4,
                "old_text": "absent",
                "new_text": "",
                "request_id": "ambiguous",
            },
            False,
        ),
        (
            "delete",
            {"action": "delete", "page_id": target, "expected_revision": 4, "request_id": "delete"},
            True,
        ),
        ("read_deleted", read, True),
        ("history", {"action": "history", "page_id": target}, True),
        ("history_one", {"action": "history", "page_id": target, "limit": 1}, True),
        (
            "restore",
            {
                "action": "restore",
                "page_id": target,
                "expected_revision": 5,
                "revision": 1,
                "request_id": "restore",
            },
            True,
        ),
        (
            "restore_invalid",
            {
                "action": "restore",
                "page_id": target,
                "expected_revision": 6,
                "revision": 900,
                "request_id": "missing-revision",
            },
            False,
        ),
    ] + tolerance_cases


async def verify_wiki_effect(
    store: Any, sid: str, pid: str, name: str, result: dict[str, Any]
) -> bool:
    if name.startswith("tolerance_"):
        suffix = name.removeprefix("tolerance_")
        expected_content, expected_revision = {
            "formatting": ("Before\r\nDone\r\nAfter", 3),
            "indentation": ("Before\n    Changed\n    Second\nAfter", 3),
            "whitespace": ("Before\nVerified finding\nAfter", 3),
            "ambiguous": ("“Ready” and “Ready”", 2),
            "changed": ("Start\nThe result is verified.\nEnd", 2),
        }[suffix]
        pages = await store.wiki(sid, pid, {"action": "list", "query": "Peer " + suffix})
        if len(pages["entries"]) != 1:
            return False
        current = await store.wiki(
            sid,
            pid,
            {
                "action": "read",
                "page_id": pages["entries"][0]["page_id"],
            },
        )
        return (current["title"], current["content"], current["revision"], current["deleted"]) == (
            "Peer " + suffix,
            expected_content,
            expected_revision,
            False,
        )
    expected = {
        "create": (1, "New", "", False),
        "create_replay": (1, "New", "", False),
        "update_title": (2, "Renamed", "alpha\nbeta", False),
        "update_replay": (2, "Renamed", "alpha\nbeta", False),
        "update_patch": (3, "Renamed", "alpha\ngamma", False),
        "update_content": (4, "Renamed", "whole page", False),
        "delete": (5, "Renamed", "whole page", True),
        "restore": (6, "Fixture", "alpha\nbeta", False),
    }.get(name)
    if expected is None or not result.get("ok"):
        return True
    current = await store.wiki(sid, pid, {"action": "read", "page_id": result["data"]["page_id"]})
    return tuple(current[key] for key in ("revision", "title", "content", "deleted")) == expected
