"""Bounded shared-post dispatch into native canonical participant Sessions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.utils.ids import new_id

from ._native_agent_text import native_input

type Json = dict[str, Any]


def _context_posts(posts: list[Json]) -> list[Json]:
    """Recent shared context stays bounded independently of the post page size."""
    result: list[Json] = []
    remaining = 24000
    for post in reversed(posts):
        if remaining <= 0 or len(result) >= 20:
            break
        value = dict(post)
        value["text"] = post["text"][:remaining]
        if len(value["text"]) != len(post["text"]):
            value["truncated"] = True
        remaining -= len(value["text"])
        result.append(value)
    return list(reversed(result))


class NativeDiscussions:
    def __init__(self, native: Any) -> None:
        self.native = native

    async def wake_origins(self) -> None:
        for topic in await self.native._all("list_topics"):
            factory = await self.native.store.get_factory(topic["factory_id"])
            if topic["state"] == "open" and factory["state"] == "active":
                await self._wake_origin(topic)

    async def schedule(self, limit: int) -> None:
        native = self.native
        for topic in await native._all("list_topics"):
            if topic["state"] != "open":
                continue
            factory = await native.store.get_factory(topic["factory_id"])
            if factory["state"] != "active":
                continue
            task = await native.store.get_task(topic["task_id"]) if topic.get("task_id") else None
            if task and task["state"] in {"paused", "cancelled"}:
                continue
            executions = await native.all_executions(topic["factory_id"])
            for participant in await native.store.topic_participants(topic["id"]):
                member = await native.store.get_member(participant["member_id"])
                if member["state"] != "active":
                    continue
                current = next(
                    (
                        item
                        for item in executions
                        if item["kind"] == "topic"
                        and item["scope_id"] == topic["id"]
                        and item["member_id"] == member["id"]
                        and item["epoch"] == native.topic_epoch(factory, member)
                    ),
                    None,
                )
                if current and (current["runtime"].get("pending") or current["state"] == "failed"):
                    continue
                posts = (
                    await native.store.recent_posts(topic["id"], limit=100)
                    if current is None
                    else await native.store.list_posts(
                        topic["id"], after=participant["read_sequence"], limit=100
                    )
                )
                addressed = [
                    post
                    for post in posts
                    if post["author"] != f"member:{member['id']}"
                    and (
                        (post["author"] == "user" and not post["recipients"])
                        or member["id"] in post["recipients"]
                    )
                ]
                if (
                    task
                    and task["assignee_id"] == member["id"]
                    and current is None
                    and participant["read_sequence"] > 0
                ):
                    continue
                if task and task["assignee_id"] == member["id"] and task["runtime"].get("sessions"):
                    # Linked consultations answer the responsible task Session.
                    continue
                if current and not addressed:
                    if posts:
                        await native.store.mark_topic_read(
                            topic["id"], member["id"], posts[-1]["sequence"]
                        )
                    continue
                active = current is not None and current["state"] == "running"
                if not active and not await native._capacity(factory, member, limit):
                    continue
                content = native_input(
                    "discussion",
                    topic,
                    posts=_context_posts(posts if current is None else addressed),
                    task=task,
                    member=member,
                )
                if current:
                    runtime = deepcopy(current["runtime"])
                    runtime["pending"] = {
                        "id": new_id("fin"),
                        "kind": "continuation",
                        "content": content,
                    }
                    execution = await native.store.change_execution(
                        current["id"],
                        {"runtime": runtime, "state": "running" if active else "admitting"},
                        actor="factory",
                    )
                else:
                    execution = await native._prepare("topic", topic, member, content)
                runtime = deepcopy(execution["runtime"])
                runtime["pending"]["topic_sequence"] = (
                    posts[-1]["sequence"] if posts else participant["read_sequence"]
                )
                execution = await native.store.change_execution(
                    execution["id"], {"runtime": runtime}, actor="factory"
                )
                if not active:
                    native._launch_later(execution)
            await self._wake_origin(topic)

    async def acknowledged(self, execution: Json, pending: Json) -> None:
        if execution["kind"] == "topic" and "topic_sequence" in pending:
            await self.native.store.mark_topic_read(
                execution["scope_id"], execution["member_id"], pending["topic_sequence"]
            )

    async def _wake_origin(self, topic: Json) -> None:
        if not topic.get("task_id"):
            return
        native = self.native
        task = await native.store.get_task(topic["task_id"])
        if task["state"] not in {"ready", "working"} and not (
            task["state"] == "blocked" and task["reason"] in {"input", "peer", "children"}
        ):
            return
        if task["assignee_id"] not in topic["participants"]:
            return
        marks = task["runtime"].get("topic_sequences", {})
        posts = await native.store.list_posts(
            topic["id"], after=marks.get(topic["id"], 0), limit=100
        )
        answers = [
            post
            for post in posts
            if (post["author"] == "user" or post["author"].startswith("member:"))
            and post["author"] != f"member:{task['assignee_id']}"
            and (not post["recipients"] or task["assignee_id"] in post["recipients"])
        ]
        if not posts:
            return
        runtime = deepcopy(task["runtime"])
        runtime.setdefault("topic_sequences", {})[topic["id"]] = posts[-1]["sequence"]
        if answers:
            await native._wake_task(
                task,
                native_input("peer_answer", task, topic=topic, posts=_context_posts(answers)),
                runtime_patch={"topic_sequences": runtime["topic_sequences"]},
            )
        else:
            await native.store.change_task(
                task["id"], {"runtime": runtime}, actor="factory", event="discussion_observed"
            )
