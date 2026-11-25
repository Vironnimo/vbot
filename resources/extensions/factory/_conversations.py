"""Project conversation entry, backed by a native Topic and its member Session."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from ._native_management import NativeFactoryManagement

type Json = dict[str, Any]


def _session_binding(execution: Json | None) -> Json | None:
    if execution is None or not execution["runtime"].get("address"):
        return None
    return {key: execution[key] for key in ("group_id", "participant_id")}


async def _bootstrap(management: NativeFactoryManagement, project_id: str) -> Json:
    host = management.service.host
    assert host is not None
    request_id = f"project-leader:{project_id}"
    prior = await management.store.identity_reservation(request_id)
    if prior:
        args = prior["payload"]
        if args.get("project_id") != project_id or not args.get("leader_name"):
            raise FactoryError(
                "receipt_conflict",
                "The reserved Project Leader request belongs to another creation.",
            )
    else:
        if host.services is None:
            raise FactoryError("model_required", "Choose a Leader or configure a Project Model.")
        projects = (await host.services.query("project.list", {}))["projects"]
        project = next((row for row in projects if row["project_id"] == project_id), None)
        if project is None:
            raise FactoryError("project_missing", "Choose a registered Project.")
        model = project.get("default_model")
        if not model:
            setting = await host.services.query(
                "settings.get_path", {"path": "defaults.agent.model", "allow_missing": True}
            )
            model = setting["setting"].get("value")
        if not model:
            raise FactoryError(
                "model_required", "Choose a Leader or set a default Model for this Project."
            )
        args = {
            "project_id": project_id,
            "leader_name": f"{project.get('display_name') or project_id} Leader"[:200],
            "leader_model": model,
            "request_id": request_id,
        }
    factory: Json = (await management._operation("factory.create", args, None))["factory"]
    return factory


async def _stop_discussion(management: NativeFactoryManagement, args: Json) -> Json:
    store = management.store
    topic = await store.get_topic(text(args, "topic_id"))
    factory = await store.get_factory(topic["factory_id"])
    if factory["project_id"] != text(args, "project_id") or not topic.get("direct_member_id"):
        raise FactoryError("conversation_mismatch", "Choose a discussion in this Project.")
    if topic.get("task_id"):
        raise FactoryError("task_linked", "Pause the linked task or stop the team.")
    if topic["state"] == "open":
        closed = await store.update_topic(
            topic["id"], {"state": "closed"}, expected_revision=topic["revision"], actor="user"
        )
        await management.service.native.changed("topics", closed, topic)
    return {"topic_id": topic["id"], "state": "closed"}


async def conversation(management: NativeFactoryManagement, name: str, args: Json) -> Json:
    store = management.store
    if name == "conversation.stop":
        return await _stop_discussion(management, args)
    sending = name == "conversation.send"
    project_id = text(args, "project_id")
    body = text(args, "text", maximum=20000).strip() if sending else ""
    receipt = text(args, "request_id", maximum=500) if sending else ""
    task_id = str(args.get("task_id") or "")
    card_id = str(args.get("card_id") or "")
    topic_id = str(args.get("topic_id") or "")
    if (task_id and card_id) or (args.get("new_topic") and (topic_id or card_id or task_id)):
        raise FactoryError(
            "ambiguous_conversation", "Choose one conversation or start a new topic."
        )
    factories = await store.list_factories(project_id=project_id)
    factory = factories[0] if factories else None
    if not factory:
        if args.get("member_id") or topic_id or task_id:
            raise FactoryError(
                "factory_not_found", "This Project has no team for that conversation."
            )
        if not sending:
            return {"topic": None, "posts": [], "factory_id": None}
        factory = await _bootstrap(management, project_id)
    factory_id = factory["id"]
    if sending and factory["state"] != "active":
        raise FactoryError("factory_inactive", "Resume this Factory before sending a message.")
    member_id = args.get("member_id") or factory["leader_member_id"]
    member = await store.get_member(member_id)
    if member["factory_id"] != factory_id or (sending and member["state"] != "active"):
        raise FactoryError("outside_factory", "Choose an active member of this Project.")
    topic = None
    if topic_id:
        topic = await store.get_topic(topic_id)
    elif not args.get("new_topic"):
        topic = await store.direct_topic(factory_id, member_id, task_id=task_id, card_id=card_id)
    if receipt and not topic_id:
        first = await store.direct_topic(factory_id, member_id, receipt=receipt)
        if first:
            if first["direct_member_id"] != member_id or (first.get("card_id") or "") != card_id:
                raise FactoryError(
                    "receipt_conflict", "This message receipt belongs to a different conversation."
                )
            topic = first
    legacy_topic = None
    if (
        not args.get("new_topic")
        and not task_id
        and not card_id
        and (not topic or not topic.get("direct_member_id"))
    ):
        previous = await store.previous_direct_discussion(factory_id, member_id, topic_id)
        if previous:
            topic = {**previous, "direct_member_id": member_id}
            legacy_topic = previous
    if topic and (
        topic["factory_id"] != factory_id
        or not topic.get("direct_member_id")
        or (args.get("member_id") and topic["direct_member_id"] != member_id)
        or (task_id and topic["task_id"] != task_id)
        or (card_id and topic.get("card_id") != card_id)
    ):
        raise FactoryError(
            "conversation_mismatch", "Open the conversation for this Project and member."
        )
    recipient = topic["direct_member_id"] if topic else member_id
    recipient_member = member if recipient == member_id else await store.get_member(recipient)
    if recipient_member["factory_id"] != factory_id:
        raise FactoryError("outside_factory", "Open the conversation in its own Factory.")
    (profile,) = await management.identities.member_profiles([recipient_member])
    participant = {
        "factory_id": factory_id,
        "agent_id": recipient_member["agent_id"],
        "agent_model": profile.get("agent_model"),
        "agent_proof": recipient_member.get("agent_proof"),
        "profile_available": profile["profile_available"],
        "member_name": profile["agent_name"]
        or recipient_member.get("name")
        or recipient_member["role"],
    }
    if sending:
        if not profile["profile_available"]:
            raise FactoryError(
                "member_profile_unavailable", "Choose a member with an available Agent profile."
            )
        if recipient_member["state"] != "active":
            raise FactoryError("member_retired", "This member is retired. Choose an active member.")
        if legacy_topic:
            topic = await store.update_topic(
                legacy_topic["id"],
                {"direct_member_id": member_id},
                expected_revision=legacy_topic["revision"],
            )
        if topic:
            await store.add_post(topic["id"], body, recipients=[recipient], receipt=receipt)
            topic = await store.get_topic(topic["id"])
        else:
            topic = await store.create_topic(
                factory_id,
                body.splitlines()[0][:120],
                task_id=task_id,
                participants=[member_id],
                initial_text=body,
                direct_member_id=member_id,
                start_receipt=receipt,
                card_id=card_id,
            )
        await management.service.native.changed("topics", topic)
    if not topic:
        return {"topic": None, "posts": [], **participant}
    history = await store.conversation_history(topic, args.get("before", ""))
    discussion = await store.latest_execution("topic", topic["id"], recipient_member["id"])
    epoch = management.service.native.topic_epoch(factory, recipient_member)
    current = discussion if discussion and discussion["epoch"] == epoch else None
    return {
        "session": discussion["runtime"].get("address") if discussion else None,
        "task_session": None,
        "session_binding": _session_binding(discussion),
        "task_session_binding": None,
        "replying": bool(current and current["state"] in {"admitting", "running"}),
        "topic": topic,
        **history,
        **participant,
        "task": None,
        "failure": current["runtime"].get("failure", "") if current else "",
    }
