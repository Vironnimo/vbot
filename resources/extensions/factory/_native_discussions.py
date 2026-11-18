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
            direct = topic.get("direct_member_id")
            if task and task["state"] in ({"paused"} if direct else {"paused", "cancelled"}):
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
                    if not direct or failed_sequence is None:
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
                    if post["author"] != f"member:{member['id']}"
                    and (
                        (post["author"] == "user" and not post["recipients"])
                        or member["id"] in post["recipients"]
                    )
                ]
                if direct and current is None and member["id"] != direct and not addressed:
                    continue
                task_handles_replies = task and (
                    not direct
                    or task["state"] in {"ready", "working"}
                    or (
                        task["state"] == "blocked"
                        and task.get("reason") in {"input", "peer", "children"}
                    )
                )
                if (
                    task_handles_replies
                    and task
                    and task["assignee_id"] == member["id"]
                    and current is None
                    and participant["read_sequence"] > 0
                ):
                    continue
                if (
                    task_handles_replies
                    and task
                    and task["assignee_id"] == member["id"]
                    and (direct or task["runtime"].get("sessions"))
                ):
                    # Linked consultations answer the responsible task Session.
                    continue
                if current and not addressed:
                    if posts:
                        await native.store.mark_topic_read(
                            topic["id"], member["id"], posts[-1]["sequence"]
                        )
                        if len(posts) == _POST_PAGE_SIZE:
                            native.service.request_schedule()
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
                    card=await self._card_context(topic["card_id"])
                    if topic.get("card_id")
                    else None,
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
            await self._wake_origin(topic)

    async def _card_context(self, card_id: str) -> Json:
        from .store import FactoryError

        store = self.native.service.records()
        try:
            card = await store.get_card(card_id)
        except FactoryError as error:
            if error.code != "card_not_found":
                raise
            return {"id": card_id, "state": "deleted"}
        outputs = await store.recent_outputs(card_id)
        return {
            **card,
            "outputs": [
                {key: row.get(key, "") for key in ("step_name", "summary", "result", "findings")}
                for row in outputs
            ],
        }

    async def publish_answer(self, execution: Json) -> None:
        """A natural final answer is visible in the same direct conversation as a Tool reply."""
        from core.runs import is_silent_answer

        native = self.native
        if execution["kind"] == "topic":
            topic = await native.store.get_topic(execution["scope_id"])
            if not topic.get("direct_member_id"):
                return
        elif execution["kind"] == "task":
            task = await native.store.get_task(execution["scope_id"])
            if not task.get("conversation_id"):
                return
            topic = await native.store.get_topic(task["conversation_id"])
        else:
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
        if any(
            call.name == "factory_topics"
            and call.arguments.get("action") == "post"
            and call.arguments.get("id") == topic["id"]
            for msg in messages
            for call in msg.tool_calls or ()
        ):
            return
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
            topic["id"], after=marks.get(topic["id"], 0), limit=_POST_PAGE_SIZE
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
        if topic.get("direct_member_id") == task["assignee_id"]:
            await native.store.mark_topic_read(
                topic["id"], task["assignee_id"], posts[-1]["sequence"]
            )
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
            if len(posts) == _POST_PAGE_SIZE:
                native.service.request_schedule()
