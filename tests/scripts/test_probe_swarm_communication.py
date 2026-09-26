"""Continuation graders observe real Board effects and automatic-delivery receipts."""

import asyncio

import pytest

from scripts.provider_probe.swarm_communication import COMMUNICATION_GOAL, assess_communication
from scripts.provider_probe.workflow_swarm import _probe_swarm_tool
from tests.scripts.provider_probe_helpers import PROBE


class Responses:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def send(self, messages, **kwargs):
        self.requests.append((list(messages), kwargs))
        return next(self.responses)

    def normalize_response(self, raw, **kwargs):
        return raw

    def request_context_kwargs(self, **kwargs):
        return {"probe_session": kwargs["session_id"]}


def post(text, call_id="post"):
    return {
        "tool_calls": [
            {"id": call_id, "name": "swarm_board", "arguments": {"action": "post", "text": text}}
        ]
    }


def run(adapter, case):
    args = PROBE._parser().parse_args(["--scenario", "swarm_tool", "--swarm-case", case])
    return asyncio.run(_probe_swarm_tool(adapter, args))


def test_quiet_acknowledgment_is_a_valid_normal_end_without_a_forced_call():
    adapter = Responses({"content": ""})
    result = run(adapter, "communication_ack")
    assert result["first_attempt_success"], result
    assert result["posts"] == result["calls"] == []
    assert result["wakes"][0]["admitted"]
    assert result["deliveries"]
    messages, kwargs = adapter.requests[0]
    assert messages[-1]["role"] == "user"
    assert "<system-reminder>" in messages[-1]["content"]
    assert "tool_choice" not in kwargs
    assert kwargs["probe_session"]
    assert len(kwargs["tools"]) == 4
    assert messages[1] == {"role": "user", "content": COMMUNICATION_GOAL}
    for fact in ("ORCHID-73", "CHECK-42"):
        assert fact in messages[1]["content"]


@pytest.mark.parametrize("outcome", ["output_truncated", "error", "unknown", "", "tool_calls"])
def test_incomplete_or_failed_empty_response_is_not_a_quiet_success(outcome):
    result = run(Responses({"content": "", "terminal_outcome": outcome}), "communication_ack")
    assert not result["passed"], result
    assert not result["finished"]


def test_question_requires_a_real_shared_answer_then_quiet_acknowledgment():
    adapter = Responses(
        post("The verified release tag is ORCHID-73."),
        {"content": "Shared the release tag."},
        {"content": ""},
    )
    result = run(adapter, "communication_question")
    assert result["first_attempt_success"], result
    assert result["posts"][0]["text"] == "The verified release tag is ORCHID-73."
    assert len(result["wakes"]) == 2


def test_claiming_an_answer_without_sharing_it_fails():
    result = run(Responses({"content": "ORCHID-73"}, {"content": ""}), "communication_question")
    assert not result["passed"]
    assert not result["requested_effect_observed"]


def test_ack_loop_is_retained_and_fails_even_with_successful_tool_calls():
    result = run(
        Responses(
            post("Acknowledged."),
            {"content": "Done."},
            post("Acknowledged again.", "again"),
            {"content": "Done."},
        ),
        "communication_ack",
    )
    assert not result["passed"], result
    assert result["posts_after_trigger"] == 2
    assert all(call["result"]["ok"] for call in result["calls"])


def test_reused_provider_call_id_in_a_new_request_is_a_new_effect():
    result = run(
        Responses(
            post("Release ORCHID-73."),
            post("The tag was verified."),
            {"content": "Done."},
            {"content": ""},
        ),
        "communication_question",
    )
    assert result["passed"], result
    assert len({row["id"] for row in result["posts"]}) == 2


@pytest.mark.parametrize("text", ["All clear.", "ORCHID-73 is ready."])
def test_new_evidence_cannot_be_dropped_from_shared_revision(text):
    result = assess_communication("communication_refinement", [{"wake": 0, "text": text}], True)
    assert not result["passed"]
