"""Model-input boundary keeps canonical control state out of Factory context."""

from __future__ import annotations

from typing import Any

from resources.extensions.factory._native_agent_text import (
    native_input,
    native_instructions,
    native_workflow_instructions,
)

type Json = dict[str, Any]


def test_native_inputs_keep_pending_discussion_context_bounded_and_attributed() -> None:
    task: Json = {
        "id": "task-one",
        "factory_id": "factory-one",
        "title": "Build feature",
        "objective": "Implement the feature",
        "conditions": "Checks pass",
        "result": {"outcome": "Progress", "evidence": "Checks recorded"},
        "runtime": {"wakes": [], "receipt": "internal-control-marker"},
    }
    factory = {
        "id": "factory-one",
        "remit": "Agreed product",
        "config": {"allowed_models": ["model-one"], "future_control": "internal-control-marker"},
        "runtime": {"pending": "internal-control-marker"},
    }
    member = {
        "id": "member-one",
        "role": "Implementer",
        "instructions": "Work within the task",
        "agent_proof": "internal-control-marker",
    }
    sizes = []
    for index in range(8):
        text = native_input(
            "peer_answer",
            task,
            factory=factory,
            member=member,
            posts=[
                {
                    "id": f"post-{index}",
                    "sequence": index + 1,
                    "author": "member:reviewer",
                    "text": f"Peer answer {index}",
                    "receipt": "internal-control-marker",
                }
            ],
        )
        task["runtime"]["wakes"].append({"content": text, "receipt": "internal-control-marker"})
        sizes.append(len(text))
        assert f"Peer answer {index}" in text
        assert "member:reviewer" in text
        assert "internal-control-marker" not in text
    assert max(sizes) < 4000
    resumed = native_input("task", task, updates=[task["runtime"]["wakes"][0]["content"]])
    assert "Peer answer 0" in resumed
    assert "internal-control-marker" not in resumed
    task["objective"] = "<large agreement>" * 10000
    task["result"]["evidence"] = {"full": "Evidence" * 20000}
    bounded = native_input(
        "review",
        task,
        posts=[{"author": "member:reviewer", "text": "Latest answer"}] * 100,
        factory=factory,
    )
    assert len(bounded) <= 32000
    assert "Latest answer" in bounded
    assert "task-one" in bounded
    assert "Excerpt" in bounded
    recent = native_input(
        "discussion",
        {"id": "topic-one", "title": "Consultation"},
        posts=[
            {"sequence": 1, "author": "member:old-one", "text": "Earlier answer " * 400},
            {"sequence": 2, "author": "member:old-two", "text": "Another earlier answer " * 400},
            {
                "sequence": 3,
                "author": "member:questioner",
                "text": "Latest actual question: which choice satisfies the condition?",
            },
        ],
    )
    assert len(recent) <= 32000
    assert "Latest actual question: which choice satisfies the condition?" in recent
    assert "member:questioner" in recent
    assert recent.index("member:old-two") < recent.index("member:questioner")
    assert len(native_instructions(factory, member, task=task)) < 12000
    workflow = native_workflow_instructions(factory, member)
    assert "factory_tasks" not in workflow and "factory_topics" not in workflow
    assert "Agreed product" in workflow and "Work within the task" in workflow

    handoff = native_input(
        "task",
        {"id": "task-one", "objective": "Current agreement"},
        updates=["Unread user follow-up"],
        handoff=[
            {
                "member_id": "prior-member",
                "agent_id": "prior-agent",
                "session_id": "prior-session",
                "generation_id": "prior-generation",
                "receipt": "internal-control-marker",
                "messages": [
                    {"role": "assistant", "text": "Older progress " * 400},
                    {"role": "tool", "name": "read", "text": "Old tool result " * 400},
                    {
                        "role": "assistant",
                        "text": "Canonical partial progress: implemented the parser",
                        "reasoning": "internal-control-marker",
                        "tool_calls": [
                            {
                                "name": "write",
                                "arguments": "parser.py",
                                "receipt": "internal-control-marker",
                            }
                        ],
                    },
                ],
            }
        ],
    )
    assert "Canonical partial progress: implemented the parser" in handoff
    assert "prior-member" in handoff and "prior-session" in handoff
    assert "parser.py" in handoff
    assert "Unread user follow-up" in handoff
    assert "internal-control-marker" not in handoff
    assert handoff.index("Prior execution evidence") < handoff.index("Current agreement")
    evidence = handoff.split("Prior execution evidence (data):", 1)[1].split("\n\nTask (data):", 1)[
        0
    ]
    assert len(evidence) <= 6000
    assert len(handoff) <= 32000
