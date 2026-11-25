"""Bounded shared-post dispatch into native canonical participant Sessions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.utils.ids import new_id

from ._native_agent_text import native_input

type Json = dict[str, Any]

_POST_PAGE_SIZE = 100


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

    async def schedule(self, limit: int) -> None:
        native = self.native
        for topic in await native._all("list_topics"):
            if topic["state"] != "open" or not topic.get("direct_member_id"):
                continue
            factory = await native.store.get_factory(topic["factory_id"])
            if factory["state"] != "active":
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
                read_sequence = participant["read_sequence"]
                if current and current["state"] == "failed":
                    failed_sequence = current["runtime"].get("failed_sequence")
                    if failed_sequence is None:
                        continue
                    read_sequence = max(read_sequence, failed_sequence)
                elif current and (
                    current["runtime"].get("pending") or current["state"] == "draining"
                ):
                    continue
                posts = (
                    await native.store.recent_posts(topic["id"], limit=_POST_PAGE_SIZE)
                    if current is None
                    else await native.store.list_posts(
                        topic["id"], after=read_sequence, limit=_POST_PAGE_SIZE
                    )
                )
                addressed = [
                    post
                    for post in posts
                    if post["sequence"] > read_sequence
                    and post["author"] != f"member:{member['id']}"
                    and (
                        (post["author"] == "user" and not post["recipients"])
                        or member["id"] in post["recipients"]
                    )
                ]
                if not addressed:
                    if posts and posts[-1]["sequence"] > read_sequence:
                        await native.store.mark_topic_read(
                            topic["id"], member["id"], posts[-1]["sequence"]
                        )
                        if current is not None and len(posts) == _POST_PAGE_SIZE:
                            native.service.request_schedule()
                    continue
                active = current is not None and current["state"] == "running"
                if not active and not await native._capacity(factory, member, limit):
                    continue
                content = native_input(
                    "discussion",
                    topic,
                    posts=_context_posts(posts if current is None else addressed),
                    member=member,
                )
                if current:
                    runtime = deepcopy(current["runtime"])
                    runtime.pop("failure", None)
                    runtime.pop("failed_sequence", None)
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
                    native.sessions._launch_later(execution)

    async def publish_answer(self, execution: Json) -> None:
        """A natural final answer is visible in the same direct conversation."""
        from core.runs import is_silent_answer

        native = self.native
        if execution["kind"] != "topic":
            return
        topic = await native.store.get_topic(execution["scope_id"])
        if not topic.get("direct_member_id"):
            return
        if topic["state"] != "open" or execution["member_id"] not in topic["participants"]:
            return
        runtime = execution["runtime"]
        run_id = runtime.get("run_id")
        if not run_id:
            return
        snapshot = await native.groups.inspect(
            execution["group_id"], execution["participant_id"], {"limit": 12}
        )
        if snapshot.generation_id != runtime.get("generation_id"):
            return
        messages = [msg for msg in snapshot.page.messages if msg.run_id == run_id]
        answer = next(
            (
                msg.content
                for msg in reversed(messages)
                if msg.role == "assistant"
                and not msg.tool_calls
                and isinstance(msg.content, str)
                and msg.content.strip()
            ),
            None,
        )
        if answer and not is_silent_answer(answer):
            await native.store.record_answer(topic["id"], execution["member_id"], run_id, answer)
            await native._publish("topics", await native.store.get_topic(topic["id"]))

    async def acknowledged(self, execution: Json, pending: Json) -> None:
        if execution["kind"] == "topic" and "topic_sequence" in pending:
            await self.native.store.mark_topic_read(
                execution["scope_id"], execution["member_id"], pending["topic_sequence"]
            )
