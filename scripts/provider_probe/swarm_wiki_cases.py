"""Independent Wiki conformance inputs and durable-effect checks.

Every case runs as its own Tool Call, so a repeated input is a new request: it
reports that nothing changed instead of replaying the earlier Tool Result.
"""

from __future__ import annotations

from typing import Any

_TOLERANCE = [
    ("formatting", "Before\r\n“Ready” — wait…\r\nAfter", '"Ready" -- wait...', "Done", True),
    (
        "indentation",
        "Before\n    First\n    Second\nAfter",
        "First\nSecond",
        "Changed\nSecond",
        True,
    ),
    ("whitespace", "Before\nFirst   finding\nAfter", "First finding", "Verified finding", True),
    (
        "similar_context",
        "Intro\nThe parser handles nested lists correctly.\nStatus: pending\nEnd",
        "The parser handles nested list correctly.\nStatus: pending",
        "The parser handles nested list correctly.\nStatus: done",
        True,
    ),
    ("ambiguous", "“Ready” and “Ready”", '"Ready"', "Done", False),
    (
        "changed",
        "Start\nThe result is verified.\nEnd",
        "Start\nThe result is pending.\nEnd",
        "Changed",
        False,
    ),
]

# Content and revision after each tolerance edit; every page first gets a peer's rename.
_TOLERANCE_EFFECTS = {
    "formatting": ("Before\r\nDone\r\nAfter", 3),
    "indentation": ("Before\n    Changed\n    Second\nAfter", 3),
    "whitespace": ("Before\nVerified finding\nAfter", 3),
    # The page keeps its own wording in the lines the edit does not change.
    "similar_context": ("Intro\nThe parser handles nested lists correctly.\nStatus: done\nEnd", 3),
    "ambiguous": ("“Ready” and “Ready”", 2),
    "changed": ("Start\nThe result is verified.\nEnd", 2),
}


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
    update = {"action": "update", "page_id": target, "expected_revision": 1, "title": "Renamed"}
    create_args = {"action": "create", "title": "New", "content": ""}
    tolerance_cases = []
    for name, content, old, new, succeeds in _TOLERANCE:
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
        ("recovered_page_title", {"action": "read", "page_id": "Fixture"}, True),
        ("recovered_read_without_page", {"action": "read"}, True),
        ("recovered_search", {"action": "search", "query": "beta"}, True),
        ("recovered_limit_clamp", {**read, "limit": 50000}, True),
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
        ("create_repeat", create_args, True),
        ("create_same_title", {**create_args, "content": "different"}, True),
        (
            "recovered_create_heading",
            {"action": "create", "content": "# Heading page\nbody"},
            True,
        ),
        ("update_title", update, True),
        ("update_repeat", update, True),
        ("update_stale", {**update, "title": "Stale title"}, False),
        (
            "update_patch",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 1,
                "old_text": "beta",
                "new_text": "gamma",
            },
            True,
        ),
        (
            "update_content_without_revision",
            {"action": "update", "page_id": target, "content": "whole page"},
            False,
        ),
        (
            "update_content",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 3,
                "content": "whole page",
            },
            True,
        ),
        (
            "update_missing",
            {"action": "update", "page_id": target, "expected_revision": 4},
            False,
        ),
        (
            "update_absent",
            {
                "action": "update",
                "page_id": target,
                "expected_revision": 4,
                "old_text": "absent",
                "new_text": "",
            },
            False,
        ),
        ("delete", {"action": "delete", "page_id": target, "expected_revision": 4}, True),
        ("read_deleted", read, True),
        ("history", {"action": "history", "page_id": target}, True),
        ("history_one", {"action": "history", "page_id": target, "limit": 1}, True),
        (
            "restore",
            {"action": "restore", "page_id": target, "expected_revision": 5, "revision": 1},
            True,
        ),
        (
            "restore_invalid",
            {"action": "restore", "page_id": target, "expected_revision": 6, "revision": 900},
            False,
        ),
    ] + tolerance_cases


async def verify_wiki_effect(
    store: Any, sid: str, pid: str, name: str, result: dict[str, Any]
) -> bool:
    if name.startswith("tolerance_"):
        suffix = name.removeprefix("tolerance_")
        expected_content, expected_revision = _TOLERANCE_EFFECTS[suffix]
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
        "create_repeat": (1, "New", "", False),
        "create_same_title": (1, "New", "different", False),
        "recovered_create_heading": (1, "Heading page", "# Heading page\nbody", False),
        "update_title": (2, "Renamed", "alpha\nbeta", False),
        "update_repeat": (2, "Renamed", "alpha\nbeta", False),
        "update_stale": (2, "Renamed", "alpha\nbeta", False),
        "update_patch": (3, "Renamed", "alpha\ngamma", False),
        "update_content_without_revision": (3, "Renamed", "alpha\ngamma", False),
        "update_content": (4, "Renamed", "whole page", False),
        "delete": (5, "Renamed", "whole page", True),
        "restore": (6, "Fixture", "alpha\nbeta", False),
        "restore_invalid": (6, "Fixture", "alpha\nbeta", False),
    }.get(name)
    if expected is None:
        return True
    page_id = (result.get("data") or {}).get("page_id")
    if not isinstance(page_id, str):
        # A failed call names no page; its effect is checked on the fixture page.
        listed = await store.wiki(sid, pid, {"action": "list", "include_deleted": True})
        page_id = next(
            (entry["page_id"] for entry in listed["entries"] if entry["title"] == expected[1]),
            None,
        )
        if page_id is None:
            return False
    current = await store.wiki(sid, pid, {"action": "read", "page_id": page_id})
    return tuple(current[key] for key in ("revision", "title", "content", "deleted")) == expected
