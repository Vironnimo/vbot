"""Reasoning/content boundaries shared by complete and interrupted Model responses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import streaming
from core.chat.streaming import StreamingAccumulator, stream_model_response
from core.providers.errors import ProviderStreamingUnsupportedError
from tests.core.chat.chat_loop_streaming_test_support import stream_runtime
from tests.core.chat.chat_loop_support import StubAdapter, build_chat_loop, history

_HISTORY = "<reasoning_history>\nOld thought.\n</reasoning_history>\n"


class _ResponseAdapter:
    def __init__(self, content: str, reasoning: str | None, *, fallback: bool) -> None:
        self.response = {"content": content, "reasoning": reasoning}
        self.fallback = fallback

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        if self.fallback:
            raise ProviderStreamingUnsupportedError("test fallback")
        for field in ("reasoning", "content"):
            # Delimiters, escapes and Markdown markers can all straddle chunks.
            for character in self.response[field] or "":
                yield {"type": f"{field}_delta", "text": character}
        yield {"type": "finish", "reason": "stop"}

    async def send(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.response

    def normalize_response(self, response: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return response


@pytest.mark.parametrize("path", ["stream", "partial", "fallback"])
@pytest.mark.parametrize(
    ("source", "native", "content", "reasoning"),
    [
        pytest.param("<think>Plan</think>Answer", None, "Answer", "Plan", id="leading"),
        pytest.param(
            "<thinking>Plan</thinking><reasoning>Check</reasoning>Answer",
            None,
            "Answer",
            "Plan\nCheck",
            id="consecutive-blocks",
        ),
        pytest.param("<think>Plan", None, None, "Plan", id="truncated"),
        pytest.param("<think>Plan</think>", None, None, "Plan", id="reasoning-only"),
        pytest.param("Plan</think>Answer", None, "Answer", "Plan", id="template-opener"),
        pytest.param(
            "<think>Plan</think>Text and </think> later.",
            None,
            "Text and </think> later.",
            "Plan",
            id="answer-not-rescanned",
        ),
        pytest.param(
            "Wrap <think>tags</think> like this.",
            None,
            "Wrap <think>tags</think> like this.",
            None,
            id="paired-tags-in-prose",
        ),
        pytest.param("<think></think>Answer", None, "<think></think>Answer", None, id="empty"),
        pytest.param(_HISTORY + "Answer", "Native", "Answer", "Native", id="history-native"),
        pytest.param(
            _HISTORY + "<think>Plan</think>Answer", None, "Answer", "Plan", id="history-first"
        ),
        pytest.param(
            "<think>Plan</think>" + _HISTORY + "Answer", None, "Answer", "Plan", id="history-last"
        ),
        pytest.param(
            "Use <reasoning_history>tags</reasoning_history> here.",
            None,
            "Use <reasoning_history>tags</reasoning_history> here.",
            None,
            id="history-example",
        ),
        pytest.param(
            "<think>Literal answer</think>",
            "Native `</think>` stays.",
            "<think>Literal answer</think>",
            "Native `</think>` stays.",
            id="native-leading-tags",
        ),
        pytest.param(
            "Answer </think> literal",
            "Native",
            "Answer </think> literal",
            "Native",
            id="native-orphan",
        ),
        pytest.param("<think>Plan</think>Answer", "", "Answer", "Plan", id="empty-native"),
    ],
)
@pytest.mark.asyncio
async def test_finalization_owns_reasoning_boundaries(
    path: str, source: str, native: str | None, content: str | None, reasoning: str | None
) -> None:
    adapter = _ResponseAdapter(source, native, fallback=path == "fallback")
    if path == "partial":
        accumulator = StreamingAccumulator()
        async for delta in adapter.stream():
            if delta["type"] != "finish":
                accumulator.add_delta(delta)
        response = accumulator.finalize_partial_fields().to_response_dict()
    else:
        response = await stream_model_response(adapter, [], model_id="test")
    assert (response["content"], response["reasoning"]) == (content, reasoning)
    assert adapter.response == {"content": source, "reasoning": native}


@pytest.mark.parametrize(
    "example",
    [
        pytest.param("Use `</think>` here.", id="inline"),
        pytest.param("Use ``a ` </think> b`` here.", id="longer-inline"),
        pytest.param("Use `a\n</thinking>\nb` here.", id="multiline-inline"),
        pytest.param("```xml\n</think>\n```", id="backtick-fence"),
        pytest.param("~~~xml\r\n</thinking>\r\n~~~", id="tilde-fence-crlf"),
        pytest.param("````xml\n```\n</reasoning>\n````", id="shorter-fence-inside"),
        pytest.param("  ```xml\n</think>\n  ```", id="indented-fence"),
        pytest.param("> ~~~xml\n> </think>\n> ~~~", id="quoted-fence"),
        pytest.param("- ~~~xml\n  </think>\n  ~~~", id="list-fence"),
        pytest.param("```xml\n> ```\n</think>\n```", id="quoted-marker-inside-fence"),
        pytest.param("Use ```a\n</think>\n``` here.", id="multiline-long-code-span"),
        pytest.param("    </think>", id="indented-code"),
        pytest.param("\t</reasoning>", id="tab-code"),
        pytest.param('The delimiter is "</think>".', id="double-quoted"),
        pytest.param("The delimiter is '</think>'.", id="single-quoted"),
        pytest.param("The delimiter is “</think>”.", id="typographic-quoted"),
        pytest.param(r"The delimiter is \</think>.", id="escaped-tag"),
        pytest.param("`<think>example</think>`", id="inline-whole-block"),
        pytest.param("```xml\n<think>example</think>\n```", id="fenced-whole-block"),
        pytest.param("    <think>example</think>", id="indented-whole-block"),
        pytest.param("`<reasoning_history>example</reasoning_history>`", id="literal-history"),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_code_examples_never_act_as_reasoning_delimiters(example: str, wrapped: bool) -> None:
    source = f"<think>Before.\n{example}\nAfter.</think>Answer" if wrapped else example
    accumulator = StreamingAccumulator()
    for character in source:
        accumulator.add_delta({"type": "content_delta", "text": character})
    result = accumulator.finalize_assistant_fields()
    expected = ("Answer", f"Before.\n{example}\nAfter.") if wrapped else (example, None)
    assert (result.content, result.reasoning) == expected


@pytest.mark.parametrize(
    "example",
    ["`unfinished </think>", "```xml\n</think>", "~~~xml\n</thinking>"],
)
def test_truncated_code_examples_remain_literal(example: str) -> None:
    for source, expected in [
        (example, (example, None)),
        ("<think>Before.\n" + example, (None, "Before.\n" + example)),
    ]:
        accumulator = StreamingAccumulator()
        accumulator.add_delta({"type": "content_delta", "text": source})
        result = accumulator.finalize_partial_fields()
        assert (result.content, result.reasoning) == expected


@pytest.mark.parametrize("fallback", [False, True], ids=["stream", "fallback"])
@pytest.mark.parametrize("native", [False, True], ids=["inline", "native"])
@pytest.mark.asyncio
async def test_chat_finalizes_text_once_before_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool, native: bool
) -> None:
    thought = "Inspect `</think>`, then continue."
    answer = "Answer with a literal </think> after the actual thinking block."
    source = answer if native else f"<think>{thought}</think>{answer}"
    response = {"content": source, "reasoning": thought if native else None}
    deltas = [
        *([{"type": "reasoning_delta", "text": thought}] if native else []),
        {"type": "content_delta", "text": source},
        {"type": "finish", "reason": "stop"},
    ]
    adapter = StubAdapter(
        [response] if fallback else [],
        stream_responses=[
            ProviderStreamingUnsupportedError("test fallback") if fallback else deltas
        ],
    )
    runtime = stream_runtime(tmp_path, adapter)
    split = streaming.split_inline_reasoning
    calls = 0

    def record_split(content: str | None, reasoning: str | None) -> tuple[str | None, str | None]:
        nonlocal calls
        calls += 1
        return split(content, reasoning)

    monkeypatch.setattr(streaming, "split_inline_reasoning", record_split)
    result = await build_chat_loop(runtime).send("coder", "Inspect", session_id="session-one")
    saved = next(message for message in history(runtime) if message.role == "assistant")
    assert (result.content, result.reasoning) == (answer, thought)
    assert (saved.content, saved.reasoning) == (answer, thought)
    assert calls == 1
