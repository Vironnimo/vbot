"""Production decision Tool matrix with independent durable-state expectations."""

from typing import Any


async def decision_cases(store: Any, sid: str, pid: str) -> list[tuple[str, dict[str, Any], bool]]:
    async def seed(action, **args):
        return await store.decisions(
            sid,
            pid,
            {"action": action, "request_id": f"seed-{action}-{args.get('title', '')}", **args},
        )

    question = (await seed("create", title="Dimensions", text="Quality"))["question"]
    qid = question["question_id"]
    flat = (await seed("add_option", question_id=qid, expected_revision=1, title="2D"))["option"][
        "option_id"
    ]
    spatial = (
        await seed("add_option", question_id=qid, expected_revision=2, title="3D", text="Depth")
    )["option"]["option_id"]
    await seed("create", title="Second")
    listing = await store.decisions(sid, pid, {"action": "list", "limit": 1})
    reading = await store.decisions(sid, pid, {"action": "read", "question_id": qid, "limit": 1})
    read = {"action": "read", "question_id": qid}

    def change(action, revision=3, **args):
        return {
            "action": action,
            "question_id": qid,
            "expected_revision": revision,
            "request_id": action,
            **args,
        }

    position = change("position", position_revision=0, option_id=flat)
    return [
        ("list_default", {"action": "list"}, True),
        (
            "list_query",
            {"action": "list", "query": "QUALITY", "include_archived": True, "limit": 100},
            True,
        ),
        ("list_cursor", listing["next_call"]["arguments"], True),
        ("changed_cursor", {**listing["next_call"]["arguments"], "query": "other"}, False),
        ("read_default", read, True),
        ("read_options", {**read, "section": "options", "limit": 100}, True),
        ("read_positions", {**read, "section": "positions"}, True),
        ("read_cursor", reading["next_call"]["arguments"], True),
        ("read_missing", {**read, "question_id": "foreign"}, False),
        ("recover_identity", {**read, "swarm_id": sid, "participant_id": pid}, True),
        ("reject_identity", {**read, "swarm_id": "foreign"}, False),
        (
            "recover_wrapper",
            {"request": {"operation": "READ", "question_id": qid, "limti": "3"}},
            True,
        ),
        ("recover_boolean", {"action": "list", "include_archived": "false"}, True),
        ("reject_fraction", {**read, "limit": 1.5}, False),
        ("reject_constraint", {**read, "publish": True}, False),
        ("reject_null", {**read, "section": None}, False),
        ("create_default", {"action": "create", "title": "New", "request_id": "new"}, True),
        (
            "create_text",
            {
                "action": "create",
                "title": "New evidence",
                "text": "Exact **evidence**",
                "request_id": "evidence",
            },
            True,
        ),
        ("position", position, True),
        ("position_replay", position, True),
        ("position_stale", {**position, "request_id": "stale"}, False),
        (
            "position_move",
            change(
                "position",
                position_revision=1,
                option_id=spatial,
                note="Depth matters",
                request_id="move",
            ),
            True,
        ),
        (
            "position_undecided",
            change("position", position_revision=2, note="Need evidence", request_id="undecided"),
            True,
        ),
        (
            "withdraw",
            {
                "action": "withdraw",
                "question_id": qid,
                "position_revision": 3,
                "request_id": "withdraw",
            },
            True,
        ),
        ("update_title", change("update", title="Dimensions revised"), True),
        ("update_stale", change("update", text="Lost edit", request_id="stale-question"), False),
        ("add_option", change("add_option", 4, title="2.5D"), True),
        (
            "update_option",
            change("update_option", 5, option_id=flat, title="Flat", text="New evidence"),
            True,
        ),
        (
            "withdraw_option",
            change(
                "update_option", 6, option_id=flat, withdrawn=True, request_id="withdraw-option"
            ),
            True,
        ),
        (
            "position_withdrawn",
            change("position", 7, position_revision=4, option_id=flat, request_id="withdrawn"),
            False,
        ),
        (
            "restore_option",
            change(
                "update_option", 7, option_id=flat, withdrawn=False, request_id="restore-option"
            ),
            True,
        ),
        ("archive", change("update", 8, archived=True, request_id="archive"), True),
        (
            "position_archived",
            change("position", 9, position_revision=4, option_id=flat, request_id="archived"),
            False,
        ),
        (
            "reopen",
            change("update", 9, archived=False, text="New constraints", request_id="reopen"),
            True,
        ),
        ("reconsider", change("reconsider", 10), True),
        ("history", {"action": "history", "question_id": qid}, True),
        ("history_one", {"action": "history", "question_id": qid, "limit": 1}, True),
    ]


async def verify_decision_effect(
    store: Any, sid: str, pid: str, name: str, result: dict[str, Any]
) -> bool:
    if not result.get("ok") or "question" not in result["data"]:
        return True
    question = result["data"]["question"]
    read = await store.decisions(
        sid, pid, {"action": "read", "question_id": question["question_id"]}
    )
    expected = {
        "position": (1, "2D", "", False),
        "position_replay": (1, "2D", "", False),
        "position_move": (2, "3D", "Depth matters", False),
        "position_undecided": (3, None, "Need evidence", False),
        "withdraw": (4, None, "", True),
    }.get(name)
    if expected:
        position = read["my_position"]
        title = next(
            (
                option["title"]
                for option in read["entries"]
                if option["option_id"] == position["option_id"]
            ),
            None,
        )
        return (position["revision"], title, position["note"], position["withdrawn"]) == expected
    expected_revision = {
        "update_title": 4,
        "add_option": 5,
        "update_option": 6,
        "withdraw_option": 7,
        "restore_option": 8,
        "archive": 9,
        "reopen": 10,
        "reconsider": 11,
    }.get(name)
    return read["question"]["revision"] == expected_revision if expected_revision else True
