"""Tests for Session Context accounting and token Usage aggregation."""

from __future__ import annotations

import json
import random
import weakref
from copy import deepcopy
from typing import Any

import pytest

from core.chat.messages import ChatMessage
from core.chat.usage import (
    ContextRoute,
    RequestContextUsage,
    aggregate_session_usage,
    latest_session_context_usage,
    projected_request_input,
)
from core.providers.adapter import (
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    estimate_wire_request_input_tokens,
)
from core.providers.providers import RequestInput
from core.utils.tokens import estimate_request_input_tokens
from tests.core.providers.github_copilot_test_support import copilot_metadata
from tests.core.providers.github_copilot_test_support import make_adapter as make_copilot_adapter
from tests.core.providers.opencode_go_test_support import GEMINI_MODEL as GO_GEMINI_MODEL
from tests.core.providers.opencode_go_test_support import go_adapter
from tests.core.providers.opencode_zen_test_support import (
    CHAT_MODEL,
    GEMINI_MODEL,
    MESSAGES_MODEL,
    RESPONSES_MODEL,
    zen_adapter,
)

JsonObject = dict[str, Any]


def _assistant(usage: JsonObject | None) -> ChatMessage:
    return ChatMessage.assistant(model="openai/gpt-5.2", content="ok", usage=usage)


NO_TURNS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
}


@pytest.mark.parametrize(
    ("messages", "totals"),
    [
        ([], NO_TURNS),
        (
            # Every turn counts: estimated counters join the totals, and a
            # counter a turn does not report counts as zero.
            [
                ChatMessage.user(content="hello"),
                _assistant(
                    {
                        "input_tokens": 1000,
                        "output_tokens": 50,
                        "cache_read_tokens": 800,
                        "cache_write_tokens": 100,
                        "reasoning_tokens": 30,
                    }
                ),
                _assistant({"input_tokens": 500, "output_tokens": 5}),
                _assistant(
                    {
                        "input_tokens": 9000,
                        "output_tokens": 90,
                        "cache_read_tokens": 500,
                        "reasoning_tokens": 50,
                        "input_tokens_estimated": True,
                        "output_tokens_estimated": True,
                        "estimated": True,
                    }
                ),
            ],
            {
                "input_tokens": 10_500,
                "output_tokens": 145,
                "cache_read_tokens": 1300,
                "cache_write_tokens": 100,
                "reasoning_tokens": 80,
            },
        ),
        (
            [
                ChatMessage.user(content="hi"),
                ChatMessage.note(content="internal"),
                _assistant(None),
                _assistant(
                    {
                        "input_tokens": -5,
                        "output_tokens": "junk",
                        "cache_read_tokens": True,
                        "reasoning_tokens": True,
                    }
                ),
            ],
            NO_TURNS,
        ),
    ],
    ids=["empty", "every-turn-counts", "junk-ignored"],
)
def test_session_usage_totals(messages: list[ChatMessage], totals: JsonObject) -> None:
    assert aggregate_session_usage(messages) == totals


def test_latest_session_context_usage_restores_saved_snapshot_plus_new_messages() -> None:
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.6-luna",
        content=None,
        usage={
            "input_tokens": 10_000,
            "output_tokens": 100,
            "context_usage": {
                "tokens": 10_100,
                "estimated": True,
                "provider_input_tokens": 10_000,
                "provider_output_tokens": 100,
                "context_window": 400_000,
            },
        },
        tool_calls=[],
    )
    tool_result = ChatMessage.tool(
        tool_call_id="call-1",
        name="read",
        content="new result",
    )
    delta_tokens, _ = estimate_request_input_tokens(
        [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "read",
                "content": "new result",
            }
        ]
    )

    context_usage = latest_session_context_usage([assistant, tool_result])

    assert context_usage == {
        "tokens": 10_100 + delta_tokens,
        "estimated": True,
        "provider_input_tokens": 10_000,
        "provider_output_tokens": 100,
        "context_window": 400_000,
        "estimated_delta_tokens": delta_tokens,
    }


def test_latest_session_context_usage_has_no_projection_without_a_snapshot() -> None:
    # Every Assistant step the Agentic Loop persists carries a snapshot; the
    # counters alone are not reinterpreted as a Context size.
    assistant = _assistant({"input_tokens": 10_000, "output_tokens": 100})

    assert latest_session_context_usage([assistant, ChatMessage.user("next")]) is None


def test_latest_session_context_usage_prefers_newer_compaction_checkpoint() -> None:
    # The checkpoint fills the window of the Model that last answered.
    assistant = _assistant(
        {
            "input_tokens": 20_000,
            "output_tokens": 500,
            "context_usage": {"tokens": 20_500, "estimated": False, "context_window": 128_000},
        }
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="summary",
        projection=[ChatMessage.user("tail")],
        compacted_token_count=10_000,
        context_tokens_before=20_500,
        context_tokens_after=4_000,
    )

    assert latest_session_context_usage([assistant, checkpoint]) == {
        "tokens": 4_000,
        "estimated": True,
        "context_window": 128_000,
    }


class _BiasedInputAdapter:
    def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
        return 120_000 + estimate_request_input_tokens(messages, tools)[0]


def _route(adapter: Any = None, model: str = "model") -> ContextRoute:
    return ContextRoute(adapter or _BiasedInputAdapter(), model, f"provider/{model}")


class _Calibration:
    """A fixed correction that records what it learns."""

    def __init__(self, factor: float) -> None:
        self.value = factor
        self.samples: list[tuple[str, int, int]] = []

    def input_estimate_factor(self, model: str) -> float:
        return self.value

    def record_input_estimate(self, model: str, *, measured: int, estimated: int) -> None:
        self.samples.append((model, measured, estimated))


def test_request_measurement_cancels_existing_estimation_bias_and_counts_changes():
    accounting = RequestContextUsage()
    adapter = _BiasedInputAdapter()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    tools = [{"type": "function", "function": {"name": "read", "description": "x" * 500}}]
    args = {"target": _route(adapter), "tools": tools, "scope": "epoch"}
    accounting.observe({"input_tokens": 150_000, "output_tokens": 20_000}, base, **args)
    assert accounting.project(base, **args)["tokens"] == 150_000
    assert accounting.project(base, **args)["estimated"] is False
    # A projection names the window of the Model the request goes to.
    assert accounting.project(base, **args, context_window=200_000)["context_window"] == 200_000
    assistant = {"role": "assistant", "content": "done"}
    after = [*base, assistant]
    delta = estimate_request_input_tokens([assistant])[0]
    projection = accounting.project(after, **args)
    assert projection == {
        "tokens": 150_000 + delta,
        "estimated": True,
        "provider_input_tokens": 150_000,
        "provider_output_tokens": 20_000,
        "estimated_delta_tokens": delta,
    }
    # Output Usage measures generation, not how much of it is replayed.
    assert projection["tokens"] < 151_000
    unanchored = accounting.project(base, **{**args, "scope": "new-epoch"})
    assert unanchored["tokens"] > 120_000
    assert "provider_input_tokens" not in unanchored
    # The send budget keeps the measured part apart: only estimates carry a margin.
    assert projected_request_input(accounting.project(base, **args)) == RequestInput.measured(
        150_000
    )
    assert projected_request_input(projection) == RequestInput.measured(
        150_000 + delta, estimated_change=delta
    )
    assert projected_request_input(unanchored) == RequestInput.estimate(unanchored["tokens"])


def test_estimate_factor_scales_only_the_estimated_part_of_a_projection():
    calibration = _Calibration(1.5)
    accounting = RequestContextUsage(calibration)
    adapter = _BiasedInputAdapter()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"target": _route(adapter), "tools": [], "scope": "epoch"}
    raw = adapter.estimate_request_input_tokens(base, model_id="model")
    assert accounting.project(base, **args)["tokens"] == round(raw * 1.5)
    assert accounting.estimate(base, target=args["target"], tools=[]) == round(raw * 1.5)
    # A measurement teaches the calibration with the uncorrected estimate.
    accounting.observe({"input_tokens": 150_000}, base, **args)
    assert calibration.samples == [("provider/model", 150_000, raw)]
    assert accounting.project(base, **args)["tokens"] == 150_000
    assistant = {"role": "assistant", "content": "done " * 200}
    delta = estimate_request_input_tokens([assistant])[0]
    prepared = accounting.prepare([*base, assistant], **args)
    projection = accounting.project_prepared(prepared)
    assert projection["estimated_delta_tokens"] == round(delta * 1.5)
    assert projection["tokens"] == 150_000 + round(delta * 1.5)
    accounting.observe({"input_tokens": 9, "input_tokens_estimated": True}, base, **args)
    assert len(calibration.samples) == 1
    # Reusing a prepared request must still see calibration taught by another Run.
    calibration.value = 2.0
    assert accounting.project_prepared(prepared)["tokens"] == 150_000 + round(delta * 2.0)
    accounting.reset()
    assert accounting.project_prepared(prepared) == {
        "tokens": round((raw + delta) * 2.0),
        "estimated": True,
    }


def test_measurement_continues_across_runs_until_compaction():
    """A new Run resumes the Session's anchor; history corrects with its factor."""
    first = RequestContextUsage(_Calibration(1.5))
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    first.observe({"input_tokens": 150_000, "output_tokens": 50}, base, **args)
    answer = {"role": "assistant", "content": "done"}
    assistant = _assistant(
        {
            "input_tokens": 150_000,
            "output_tokens": 50,
            "context_usage": first.project([*base, answer], **args),
            "context_estimation": first.estimation_record(args["target"]),
        }
    )
    newer = ChatMessage.user("next task " * 100)
    # A later Run has a new Adapter instance for the same route.
    resumed = RequestContextUsage.resume([assistant, newer], _Calibration(1.5))
    next_args = {**args, "target": _route()}
    next_request = [*base, answer, {"role": "user", "content": "next task " * 100}]
    projection = resumed.project(next_request, **next_args)
    assert projection["provider_input_tokens"] == 150_000
    delta = estimate_request_input_tokens(next_request[2:])[0]
    assert projection["tokens"] == 150_000 + round(delta * 1.5)
    history = latest_session_context_usage([assistant, newer])
    assert history is not None
    assert history["tokens"] - assistant.usage["context_usage"]["tokens"] == round(
        estimate_request_input_tokens(next_request[3:])[0] * 1.5
    )

    checkpoint = ChatMessage.compaction_checkpoint(
        summary="summary",
        projection=[ChatMessage.user("tail")],
        compacted_token_count=10_000,
        context_tokens_before=150_000,
        context_tokens_after=4_000,
    ).with_compaction_context_tokens(
        context_tokens_before=150_000, context_tokens_after=4_000, estimate_factor=2.0
    )
    after_compaction = RequestContextUsage.resume([assistant, checkpoint], _Calibration(1.5))
    assert "provider_input_tokens" not in after_compaction.project(base, **next_args)
    assert latest_session_context_usage([assistant, checkpoint, newer])["tokens"] == 4_000 + round(
        estimate_request_input_tokens([{"role": "user", "content": "next task " * 100}])[0] * 2.0
    )


@pytest.mark.parametrize("change", ["route", "scope", "tools", "system", "reset"])
def test_request_measurement_does_not_cross_rebuilt_context(change):
    accounting = RequestContextUsage()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    accounting.observe({"input_tokens": 10_000}, base, **args)
    if change == "system":
        base = [{"role": "system", "content": "new rules"}, *base[1:]]
    elif change == "reset":
        accounting.reset()
    else:
        args[{"route": "target"}.get(change, change)] = {
            "route": _route(model="different"),
            "scope": "new-epoch",
            "tools": [{"function": {"name": "new"}}],
        }[change]
    prepared = accounting.prepare(base, **args)
    projection = accounting.project_prepared(prepared)
    assert projection["estimated"] is True
    assert projection["tokens"] > 120_000
    assert "provider_input_tokens" not in projection


def test_request_projection_accounts_for_retired_images_and_persists_signed_delta():
    accounting = RequestContextUsage()
    image = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 2000}}
        ],
    }
    base = [{"role": "user", "content": "task"}, image]
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    accounting.observe({"input_tokens": 30_000}, base, **args)
    projection = accounting.project(base[:1], **args)
    assert projection["estimated_delta_tokens"] < 0
    assert projection["tokens"] == 30_000 - estimate_request_input_tokens([image])[0]
    assistant = _assistant(
        {"input_tokens": 30_000, "output_tokens": 500, "context_usage": projection}
    )
    assert latest_session_context_usage([assistant]) == projection
    newer = ChatMessage.user("next task")
    restored = latest_session_context_usage([assistant, newer])
    assert restored["tokens"] > projection["tokens"]
    assert restored["estimated"] is True


def test_estimated_input_never_anchors_or_replaces_a_measurement():
    accounting = RequestContextUsage()
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    base = [{"role": "user", "content": "task"}]
    accounting.observe({"input_tokens": 10, "input_tokens_estimated": True}, base, **args)
    assert accounting.project(base, **args) == {
        "tokens": 120_000 + estimate_request_input_tokens(base)[0],
        "estimated": True,
    }

    accounting.observe({"input_tokens": 10_000}, base, **args)
    next_request = [*base, {"role": "assistant", "content": "hello"}]
    before = accounting.project(next_request, **args)
    accounting.observe(
        {"input_tokens": before["tokens"], "input_tokens_estimated": True}, next_request, **args
    )
    assert accounting.project(next_request, **args) == before
    assert before["provider_input_tokens"] == 10_000


def test_removal_cannot_project_nonempty_request_to_zero_tokens():
    accounting = RequestContextUsage()
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    base = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "word " * 500}]
    accounting.observe({"input_tokens": 100}, base, **args)
    projected = accounting.project(base[:1], **args)
    assert projected["tokens"] > 0
    assert projected["estimated"] is True
    assert "provider_input_tokens" not in projected


class _RecordingAdapter:
    """Counts with ``adapter``'s wire and records how many messages each count reads."""

    def __init__(self, adapter: Any, reads: list[int]) -> None:
        self.adapter = adapter
        self.reads = reads

    def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
        self.reads.append(len(messages))
        return estimate_wire_request_input_tokens(
            self.adapter, messages, model_id=model_id, tools=tools
        )


_TOOLS = [
    {
        "name": "read",
        "description": "Read a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


def test_a_request_is_counted_once_and_its_continuation_by_what_it_adds() -> None:
    """A Step's request is counted once; the next Step reads its newest turn, not history."""
    reads: list[int] = []
    adapter = _BiasedInputAdapter()
    accounting = RequestContextUsage()
    args: JsonObject = {
        "target": _route(_RecordingAdapter(adapter, reads)),
        "tools": _TOOLS,
        "scope": "epoch",
    }
    request: list[JsonObject] = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
    ]
    prepared = accounting.prepare(request, **args)
    accounting.observe_prepared({"input_tokens": 5_000}, prepared)

    for step in range(100):
        call = {"id": f"call_{step}", "name": "read", "arguments": {"path": f"{step}.txt"}}
        request = [
            *request,
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": f"line {step}\n" * 20},
        ]
        reads.clear()
        continued = accounting.prepare(request, **args)
        assert max(reads) <= 5
    assert continued.tokens == adapter.estimate_request_input_tokens(
        request, model_id="model", tools=_TOOLS
    )
    # The same request again, such as the one its response is measured against,
    # costs no count, and a rebuilt copy of it no whole count.
    reads.clear()
    assert accounting.prepare(request, **args) == continued
    assert reads == []
    assert accounting.prepare(deepcopy(request), **args) == continued
    assert sum(reads) < len(request)

    # A change in place anywhere is noticed and counted whole.
    request[1]["content"] = "a changed task " * 100
    changed = accounting.prepare(request, **args)
    assert changed.tokens == adapter.estimate_request_input_tokens(
        request, model_id="model", tools=_TOOLS
    )
    # A prepared request holds digests and a count, which no later edit rewrites.
    assert accounting.project_prepared(prepared)["tokens"] == 5_000


class _Weakly(dict):
    """A request message that can tell whether anything still holds it."""


class _WeakBlocks(list):
    """Message content that can tell whether anything still holds it."""


def test_counting_holds_no_older_request_and_a_bounded_memo() -> None:
    """Counting keeps digests: once a newer request is counted, older content is released."""
    accounting = RequestContextUsage()
    args: JsonObject = {"target": _route(), "tools": [], "scope": "epoch"}
    held = []
    for index in range(10):
        content = _WeakBlocks([{"type": "text", "text": f"private {index}"}])
        message = _Weakly(role="user", content=content)
        held.append((weakref.ref(message), weakref.ref(content)))
        accounting.project([message], **args)
        del content, message

    assert [message() is None and content() is None for message, content in held[:-1]] == [True] * 9
    memo = accounting._estimates
    assert len(memo) == 4
    assert all(
        len(key) == len(request_hash) == 64 and isinstance(count, int)
        for (key, request_hash), count in memo.items()
    )


_IMAGE = {"type": "media", "media_type": "image/png", "base64": "aW1hZ2U="}

_WIRES = {
    "go-gemini": lambda: (go_adapter(), GO_GEMINI_MODEL),
    "chat-completions": lambda: (zen_adapter(), CHAT_MODEL),
    "messages": lambda: (zen_adapter(), MESSAGES_MODEL),
    "responses": lambda: (zen_adapter(), RESPONSES_MODEL),
    "gemini": lambda: (zen_adapter(), GEMINI_MODEL),
    "copilot-messages": lambda: (
        make_copilot_adapter(
            metadata=copilot_metadata("Anthropic", "claude-sonnet-4.6", ["/v1/messages"])
        ),
        "claude-sonnet-4.6",
    ),
    "generic": lambda: (object(), "model"),
}


class _Conversation:
    """Random request changes of the kinds Runs, hooks and Compaction make."""

    CHANGES = {
        "user": 3,
        "step": 5,
        "results": 2,
        "answer": 2,
        "edit": 1,
        "mutate": 1,
        "truncate": 1,
        "copy_tail": 1,
        "deep_copy": 0.5,
        "back": 1,
        "repeat": 1,
        "system": 0.5,
        "tools": 0.5,
        "route": 0.5,
        "adapter": 0.3,
    }

    def __init__(self, seed: str) -> None:
        self.rng = random.Random(seed)
        self.ids = 0
        self.calls: list[str] = []
        self.earlier: list[list[JsonObject]] = []

    def next_change(self) -> str:
        return self.rng.choices(list(self.CHANGES), weights=list(self.CHANGES.values()))[0]

    def apply(self, change: str, request: list[JsonObject]) -> list[JsonObject]:
        self.earlier = [*self.earlier[-7:], request]
        changed: list[JsonObject] = getattr(self, change)(request)
        return changed

    def user(self, request: list[JsonObject]) -> list[JsonObject]:
        content: Any = f"task {self.rng.random()}"
        if self.rng.random() < 0.3:
            content = [{"type": "text", "text": content}, _IMAGE]
        return [*request, {"role": "user", "content": content}]

    def step(self, request: list[JsonObject]) -> list[JsonObject]:
        ids = [self._call_id() for _ in range(self.rng.randint(1, 3))]
        if self.calls and self.rng.random() < 0.1:
            ids[0] = self.rng.choice(self.calls)  # a Provider repeating an id
        self.calls += ids
        assistant = {
            "role": "assistant",
            "content": self.rng.choice(["", "Let me look."]),
            "tool_calls": [
                {"id": id_, "name": "read_file", "arguments": {"path": id_}} for id_ in ids
            ],
        }
        answered = ids[: self.rng.randint(0, len(ids))]
        return [*request, assistant, *map(self._result, answered)]

    def results(self, request: list[JsonObject]) -> list[JsonObject]:
        newest, older = self.calls[-3:], self.calls[:-3]
        call_id = self.rng.choice(
            [*newest, *newest, self.rng.choice(older or ["ghost"]), f"ghost_{self.ids}"]
        )
        return [*request, self._result(call_id)]

    def answer(self, request: list[JsonObject]) -> list[JsonObject]:
        calls: JsonObject = self.rng.choice([{}, {"tool_calls": []}, {"tool_calls": None}])
        content = self.rng.choice(["Done.", ""])
        return [*request, {"role": "assistant", "content": content, **calls}]

    def edit(self, request: list[JsonObject]) -> list[JsonObject]:
        index = self.rng.randrange(len(request))
        edited = {**request[index], "content": f"edited {self.rng.random()}"}
        return [*request[:index], edited, *request[index + 1 :]]

    def mutate(self, request: list[JsonObject]) -> list[JsonObject]:
        self.rng.choice(request)["content"] = f"mutated {self.rng.random()}"
        return request

    def truncate(self, request: list[JsonObject]) -> list[JsonObject]:
        return request[: self.rng.randint(1, max(1, len(request) - 1))]

    def copy_tail(self, request: list[JsonObject]) -> list[JsonObject]:
        return [*request[:-1], dict(request[-1])]

    def deep_copy(self, request: list[JsonObject]) -> list[JsonObject]:
        return deepcopy(request)

    def back(self, request: list[JsonObject]) -> list[JsonObject]:
        return self.rng.choice(self.earlier)

    def repeat(self, request: list[JsonObject]) -> list[JsonObject]:
        return request

    def system(self, request: list[JsonObject]) -> list[JsonObject]:
        system = {"role": "system", "content": f"rules {self.rng.random()}"}
        if self.rng.random() < 0.5:
            return [*request, system]
        return [system, *request[1:]]

    def _call_id(self) -> str:
        self.ids += 1
        # Messages and Responses wires allow no dots or colons: these ids are rewritten.
        return f"functions.read:{self.ids}" if self.rng.random() < 0.3 else f"call_{self.ids}"

    def _result(self, call_id: str) -> JsonObject:
        result: JsonObject = {"role": "tool", "tool_call_id": call_id, "content": '{"ok":true}'}
        # Gemini names an unnamed result after its call, wherever that is.
        if self.rng.random() < 0.5:
            result["name"] = "read_file"
        if self.rng.random() < 0.3:
            result[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = [_IMAGE]
        return result


@pytest.mark.parametrize("wire", list(_WIRES))
def test_every_request_counts_exactly_as_its_whole_wire_estimate(wire: str) -> None:
    """However a request continues or changes, its count equals counting it whole."""
    adapter, model = _WIRES[wire]()
    conversation = _Conversation(wire)
    reads: list[int] = []

    def routes() -> list[ContextRoute]:
        recording = _RecordingAdapter(adapter, reads)
        return [ContextRoute(recording, model, f"{wire}/{name}") for name in ("model", "alias")]

    targets = routes()
    target, tools = targets[0], _TOOLS
    request: list[JsonObject] = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
    ]
    accounting = RequestContextUsage()
    digests: dict[str, str] = {}
    counted_apart = 0
    for _ in range(150):
        change = conversation.next_change()
        if change == "tools":
            tools = conversation.rng.choice([_TOOLS, [{**_TOOLS[0], "description": "Read."}], []])
        elif change == "route":
            target = conversation.rng.choice(targets)
        elif change == "adapter":  # a later Run's Adapter instance for the same route
            targets = routes()
            target = targets[0]
        else:
            request = conversation.apply(change, request)
        reads.clear()

        prepared = accounting.prepare(request, target=target, tools=tools, scope="epoch")

        whole = estimate_wire_request_input_tokens(adapter, request, model_id=model, tools=tools)
        assert prepared.tokens == whole, change
        # Equal content has one digest, whichever way it was counted.
        content = json.dumps(request, sort_keys=True)
        assert digests.setdefault(content, prepared.request) == prepared.request, change
        counted_apart += bool(reads) and max(reads) < len(request)
    assert len(set(digests.values())) == len(digests)
    assert counted_apart >= 40
