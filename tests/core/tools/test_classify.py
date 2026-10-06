"""The classify Tool through production dispatch."""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.model_tasks import TaskUsageContext
from core.model_tasks.decision_types import DecisionError, validate_input
from core.model_tasks.decisions import DecisionService
from core.providers.adapter import tool_result_text
from core.tools.classify import register_classify_tool
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.tools.tools_test_support import dispatch_as_executor


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name="classify",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )


def _answer(question: dict[str, Any]) -> dict[str, Any]:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.82}
    if question["type"] == "choice":
        labels = list(question["criteria"])
        return {
            "type": "choice",
            "choice": labels[0],
            "confidence": 0.9,
            "probabilities": {label: (0.9 if i == 0 else 0.1) for i, label in enumerate(labels)},
        }
    return {"type": "score", "score": 2.6}


class Classify:
    """The registered Tool against a Decision service that records each validated request."""

    def __init__(
        self, tmp_path, errors: dict[int, DecisionError] | None = None, available: bool = True
    ) -> None:
        self.received: list[tuple[Any, list[dict[str, Any]]]] = []
        self.calls: list[dict[str, Any]] = []

        async def classify(items, questions, *, context=None, usage_context=None):
            self.calls.append({"context": context, "usage_context": usage_context})
            outcomes: list[dict[str, Any] | DecisionError] = []
            for index, item in enumerate(items):
                state = item if context is None else {"context": context, "item": item}
                state, checked = validate_input(state, questions)
                self.received.append((state, checked))
                if errors and index in errors:
                    outcomes.append(errors[index])
                    continue
                outcomes.append(
                    {"answers": {question["id"]: _answer(question) for question in checked}}
                )
            return outcomes

        self.registry = ToolRegistry()
        register_classify_tool(
            self.registry,
            cast(DecisionService, SimpleNamespace(available=lambda: available, classify=classify)),
        )
        self.context = _context(tmp_path)

    async def call(self, arguments: Any) -> tuple[dict[str, Any], str]:
        envelope = await dispatch_as_executor(self.registry, self.context, arguments)
        return envelope, str(tool_result_text(json.dumps(envelope)))


@pytest.mark.asyncio
async def test_dispatch_preserves_item_data_attributes_usage_and_enforces_readiness(tmp_path):
    tool = Classify(tmp_path)
    item = '{"type":"TRUE","nested":[{"score":"3.0"}]}'

    result, text = await tool.call(
        {"items": [item], "questions": [{"id": "q", "type": "yes_no", "instructions": "Valid?"}]}
    )

    assert result["ok"] and text == "probability of yes 0.82"
    assert tool.received == [(item, [{"id": "q", "type": "noul", "instructions": "Valid?"}])]
    assert tool.calls[0]["usage_context"] == TaskUsageContext(
        agent_id="agent", session_id="session", run_id="run"
    )
    unready = Classify(tmp_path, available=False)
    refused, _ = await unready.call({"items": ["x"], "questions": [{"type": "yes_no"}]})
    assert refused["error"]["code"] == "tool_not_ready"
    assert unready.received == []


@pytest.mark.asyncio
async def test_each_item_is_judged_with_the_shared_context_and_read_back_in_order(tmp_path):
    tool = Classify(tmp_path)
    files = [
        "core/sessions/export.py\n\ndef export(session): ...",
        "webui/src/components/a/very/long/path/that/goes/on/and/on/Component.svelte",
    ]

    result, text = await tool.call(
        {
            "items": files,
            "context": "Fix the session export timeout",
            "questions": [{"type": "yes_no", "instructions": "Is this file needed?"}],
        }
    )

    assert result["ok"] is True
    assert [state for state, _ in tool.received] == [
        {"context": "Fix the session export timeout", "item": file} for file in files
    ]
    assert text == (
        "item 1 (core/sessions/export.py): probability of yes 0.82\n"
        "item 2 (webui/src/components/a/very/long/path/that/goes...): probability of yes 0.82"
    )


@pytest.mark.asyncio
async def test_answers_of_several_questions_read_as_one_line(tmp_path):
    tool = Classify(tmp_path)

    _result, text = await tool.call(
        {
            "items": ["The build failed twice today with the same timeout."],
            "questions": [
                {"id": "flaky", "type": "yes_no", "instructions": "Is this a flaky test?"},
                {
                    "id": "kind",
                    "type": "choice",
                    "instructions": "What kind of failure is it?",
                    "options": {"infra": "Infrastructure", "code": "A code regression"},
                },
                {
                    "id": "urgency",
                    "type": "score",
                    "instructions": "How urgent is a fix?",
                    "levels": ["None", "Low", "Medium", "High", "Critical"],
                },
            ],
        }
    )

    assert text == (
        "flaky: probability of yes 0.82; kind: infra (confidence 0.9; infra 0.9, code 0.1); "
        "urgency: level 2.6 on levels 0-4"
    )


@pytest.mark.asyncio
async def test_failed_items_are_named_while_the_others_are_kept(tmp_path):
    unknown = DecisionError(
        "The Provider may have processed this request, but no usable result arrived. Asking "
        "again may incur another charge.",
        code="outcome_unknown",
    )
    tool = Classify(tmp_path, errors={1: unknown})
    question = [{"type": "yes_no", "instructions": "Spam?"}]

    _result, text = await tool.call({"items": ["first", "second", "third"], "questions": question})
    _all, failed = await Classify(tmp_path, errors={0: unknown}).call(
        {"items": ["only"], "questions": question}
    )

    assert text == (
        "item 1 (first): probability of yes 0.82\n"
        f"item 2 (second): not classified: {unknown}\n"
        "item 3 (third): probability of yes 0.82\n"
        "\nItem 2 was not classified; the others were. Repeat the call with only those items."
    )
    assert failed == f"Error (outcome_unknown): {unknown}"


class TestOtherShapes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arguments",
        [
            {"state": "x", "question": "Is it spam?", "type": "noul"},
            {"input": "x", "type": "boolean", "prompt": "Is it spam?"},
            {"items": "x", "questions": [{"question": "Is it spam?"}], "type": "Yes/No"},
            {"text": "x", "questions": {"type": "probability", "text": "Is it spam?"}},
        ],
    )
    async def test_one_text_and_question_written_another_way(self, tmp_path, arguments):
        tool = Classify(tmp_path)

        result, _text = await tool.call(arguments)

        assert result["ok"] is True
        assert tool.received == [
            ("x", [{"id": "q1", "type": "noul", "instructions": "Is it spam?"}])
        ]

    @pytest.mark.asyncio
    async def test_a_text_beside_items_is_their_context(self, tmp_path):
        tool = Classify(tmp_path)

        await tool.call(
            {"state": "task", "items": ["a"], "questions": [{"type": "yes_no", "text": "A?"}]}
        )

        assert tool.calls[0]["context"] == "task"

    @pytest.mark.asyncio
    async def test_question_spellings_and_label_lists(self, tmp_path):
        tool = Classify(tmp_path)
        fix = "Fix the crash first: it is small, confirmed three times and blocks " + "x" * 80

        result, text = await tool.call(
            {
                "items": ["Crash on start"],
                "questions": {
                    "category": {
                        "kind": "classification",
                        "question": "Which kind of report is this?",
                        "labels": ["bug", {"label": "feature", "description": "A request"}],
                    },
                    "7": {
                        "type": "rating",
                        "prompt": "How severe?",
                        "criteria": {"0": "Cosmetic", "1": "Annoying", "2": "Blocking"},
                    },
                    # Session shape: option texts too long to be labels.
                    "next": {
                        "type": "choice",
                        "instructions": "What comes next?",
                        "criteria": [fix, "Release it as it is"],
                    },
                    "done": {
                        "type": "yes_no",
                        "instructions": "Done?",
                        "yes": "All steps ran",
                        "No": "A step is missing",
                    },
                },
            }
        )

        assert result["ok"] is True
        assert f"next: 1 '{fix[:77]}...' (confidence 0.9; 1 0.9, 2 0.1)" in text
        assert tool.received[0][1] == [
            {
                "id": "category",
                "type": "choice",
                "instructions": "Which kind of report is this?",
                "criteria": {"bug": "bug", "feature": "A request"},
            },
            {
                "id": "7",
                "type": "score",
                "instructions": "How severe?",
                "criteria": ["Cosmetic", "Annoying", "Blocking"],
            },
            {
                "id": "next",
                "type": "choice",
                "instructions": "What comes next?",
                "criteria": {"1": fix, "2": "Release it as it is"},
            },
            {
                "id": "done",
                "type": "noul",
                "instructions": "Done?",
                "criteria": {"true": "All steps ran", "false": "A step is missing"},
            },
        ]

    @pytest.mark.asyncio
    async def test_missing_ids_follow_position_and_skip_taken_ones(self, tmp_path):
        tool = Classify(tmp_path)

        await tool.call(
            {
                "items": ["x"],
                "questions": [
                    {"type": "yes_no", "instructions": "A?"},
                    {"id": "q1", "type": "yes_no", "instructions": "B?"},
                    {"id": "", "type": "yes_no", "instructions": "C?"},
                ],
            }
        )

        assert [question["id"] for question in tool.received[0][1]] == ["q2", "q1", "q3"]

    @pytest.mark.asyncio
    async def test_numbers_and_objects_as_items_are_judged_as_their_json_text(self, tmp_path):
        tool = Classify(tmp_path)

        await tool.call(
            {"items": [42, {"title": "Crash"}], "questions": [{"type": "yes_no", "text": "A?"}]}
        )

        assert [state for state, _ in tool.received] == ["42", '{"title": "Crash"}']


class TestRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("question", "message"),
        [
            (
                "Is it spam?",
                'question "q1" needs "type": "yes_no" for the probability of yes, "choice" to '
                'pick one of your options, or "score" to rate on your levels. Send one of: '
                '{"id":"q1","type":"yes_no","instructions":"Is it spam?"} or {"id":"q1",'
                '"type":"choice","instructions":"Is it spam?","options":{"<label>":'
                '"<what it means>","<other label>":"<what it means>"}} or {"id":"q1",'
                '"type":"score","instructions":"Is it spam?","levels":["<lowest level>",'
                '"<next level>","<highest level>"]}',
            ),
            (
                {"id": "kind", "type": "choice", "instructions": "Which?", "options": ["bug"]},
                'question "kind" is a choice, so "options" names at least two labels and what '
                'each means. Send: {"id":"kind","type":"choice","instructions":"Which?",'
                '"options":{"<label>":"<what it means>","<other label>":"<what it means>"}}',
            ),
            (
                {
                    "id": "s",
                    "type": "score",
                    "instructions": "How bad?",
                    "levels": {"1": "Low", "2": "High"},
                },
                'question "s" is a score, and levels are numbered from 0, so level 0 would be '
                '"Low". If that is meant, send: {"id":"s","type":"score","instructions":'
                '"How bad?","levels":["Low","High"]}',
            ),
            (
                {"id": "s", "type": "score", "instructions": "How bad?"},
                'question "s" is a score, so "levels" lists what each level means, lowest '
                'first; the answer is a level number from 0. Send: {"id":"s","type":"score",'
                '"instructions":"How bad?","levels":["<lowest level>","<next level>",'
                '"<highest level>"]}',
            ),
            (
                {"id": "n", "type": "yes_no", "instructions": "Ok?", "criteria": {"likely": "Y"}},
                'question "n" is a yes_no question; describe what yes and no mean in its '
                'instructions instead. Send: {"id":"n","type":"yes_no","instructions":"Ok?"}',
            ),
            (
                {"id": "n", "type": "nou1", "instructions": "Ok?"},
                'question "n" has type "nou1"; use "yes_no" for the probability of yes, "choice" '
                'to pick one of your options, or "score" to rate on your levels.',
            ),
            (
                {"id": "n", "type": "yes_no", "instructions": "Ok?", "explain": True},
                'question "n" has "explain", which classify cannot use: a yes_no question takes '
                "only id, type, instructions, and answers carry no explanations. Send it "
                'without it: {"id":"n","type":"yes_no","instructions":"Ok?"}',
            ),
            (
                {
                    "id": "c",
                    "type": "choice",
                    "instructions": "Ok?",
                    "options": {"a": "A", "b": "B"},
                    "levels": ["x", "y"],
                },
                'a question gives "options" and "levels" with different values; a choice '
                'question takes only "options". Send the question with one "options".',
            ),
            (
                {"id": "n", "type": "yes_no", "question": "Is it late?", "prompt": "Is it done?"},
                'a question gives "instructions" twice with different values. Send the question '
                'you mean: {"id":"n","type":"yes_no","instructions":"Is it late?"} or {"id":"n",'
                '"type":"yes_no","instructions":"Is it done?"}',
            ),
            (
                {"id": "n", "type": "yes_no"},
                'question "n" needs "instructions": the question itself. Send: {"id":"n",'
                '"type":"yes_no","instructions":"<the question>"}',
            ),
        ],
    )
    async def test_an_unanswerable_question_names_the_corrected_one(
        self, tmp_path, question, message
    ):
        tool = Classify(tmp_path)

        result, text = await tool.call({"items": ["x"], "questions": [question]})

        assert result["ok"] is False
        assert text == f"Error (invalid_arguments): classify was not run: {message}"
        assert tool.received == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            (
                {"questions": [{"type": "yes_no", "instructions": "A?"}]},
                'it needs "items": the texts to classify, such as ["<first text>", '
                '"<second text>"].',
            ),
            (
                {"items": ["x"]},
                'it needs "questions", at least one, such as [{"type":"yes_no",'
                '"instructions":"<the question>"}].',
            ),
            (
                {
                    "items": ["x"],
                    "questions": [
                        {"id": "a", "type": "yes_no", "instructions": "A?"},
                        {"id": "a", "type": "yes_no", "instructions": "B?"},
                    ],
                },
                'two questions have the id "a"; give each its own id.',
            ),
            (
                {
                    "items": ["x"],
                    "context": "task",
                    "state": "other",
                    "questions": [{"type": "yes_no", "instructions": "A?"}],
                },
                'the call has "items", "context" and a third text, so it is not clear what the '
                'third text is. Put shared text in "context" and the texts to classify in '
                '"items".',
            ),
        ],
    )
    async def test_an_incomplete_call_says_what_is_missing(self, tmp_path, arguments, message):
        tool = Classify(tmp_path)

        _result, text = await tool.call(arguments)

        assert text == f"Error (invalid_arguments): classify was not run: {message}"
        assert tool.received == []

    @pytest.mark.asyncio
    async def test_fields_outside_several_questions_stay_refused(self, tmp_path):
        tool = Classify(tmp_path)

        result, text = await tool.call(
            {
                "items": ["x"],
                "type": "yes_no",
                "questions": [{"instructions": "A?"}, {"instructions": "B?"}],
            }
        )

        assert result["ok"] is False
        assert text == (
            'Error (invalid_arguments): classify was not run: "type" is outside "questions", '
            "and the call has 2 questions, so it is not clear which one it belongs to. Put it "
            "inside each question it applies to."
        )
        assert tool.received == []

    @pytest.mark.asyncio
    async def test_a_field_given_twice_with_different_values_offers_both(self, tmp_path):
        tool = Classify(tmp_path)

        _result, text = await tool.call(
            {
                "items": ["x"],
                "prompt": "Is it done?",
                "questions": [{"id": "q", "type": "yes_no", "instructions": "Is it late?"}],
            }
        )
        same, _ = await tool.call(
            {
                "items": ["x"],
                "type": "yes_no",
                "questions": [{"id": "q", "type": "yes_no", "instructions": "Is it late?"}],
            }
        )

        assert text == (
            'Error (invalid_arguments): classify was not run: the call gives "instructions" '
            "both inside and outside its question, with different values. Send the question "
            'you mean: {"id":"q","type":"yes_no","instructions":"Is it late?"} or '
            '{"id":"q","type":"yes_no","instructions":"Is it done?"}'
        )
        assert same["ok"] is True
        assert tool.received == [
            ("x", [{"id": "q", "type": "noul", "instructions": "Is it late?"}])
        ]
