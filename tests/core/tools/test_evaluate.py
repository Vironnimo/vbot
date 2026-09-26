import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.model_tasks import TaskUsageContext
from core.model_tasks.decision_types import DecisionError, validate_input
from core.model_tasks.decisions import DecisionService
from core.providers.adapter import tool_result_text
from core.tools.contracts import ToolContractError
from core.tools.evaluate import register_evaluate_tool
from core.tools.tools import ToolContext, ToolNotAllowedError, ToolRegistry, tool_failure


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name="evaluate",
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


class Evaluate:
    """The registered Tool against a Decision service that records validated input."""

    def __init__(self, tmp_path, error: DecisionError | None = None) -> None:
        self.received: list[tuple[Any, list[dict[str, Any]]]] = []

        async def evaluate(state, questions, *, usage_context=None):
            state, questions = validate_input(state, questions)
            self.received.append((state, questions))
            if error is not None:
                raise error
            return {
                "answers": {question["id"]: _answer(question) for question in questions},
                "model": "fixture/model",
                "usage": {"input_tokens": 120, "output_tokens": 12},
            }

        self.registry = ToolRegistry()
        register_evaluate_tool(
            self.registry,
            cast(DecisionService, SimpleNamespace(available=lambda: True, evaluate=evaluate)),
        )
        self.context = _context(tmp_path)

    async def call(self, arguments: Any) -> tuple[dict[str, Any], str]:
        try:
            envelope = await self.registry.dispatch(self.context, arguments, ["evaluate"])
        except ToolContractError as error:
            envelope = tool_failure("invalid_arguments", str(error))
        return envelope, str(tool_result_text(json.dumps(envelope)))


@pytest.mark.asyncio
async def test_dispatch_preserves_application_data_and_enforces_readiness_and_access(tmp_path):
    async def execute(state, questions, *, usage_context):
        state, questions = validate_input(state, questions)
        return {
            "answers": {q["id"]: {"type": "noul", "noul": 0.5} for q in questions},
            "model": "fixture",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }

    handler = AsyncMock(side_effect=execute)
    service = SimpleNamespace(available=lambda: True, evaluate=handler)
    registry = ToolRegistry()
    register_evaluate_tool(registry, service)
    ctx = _context(tmp_path)
    args = {
        "state": {"type": "TRUE", "nested": [{"score": "3.0"}]},
        "questions": [{"id": "q", "type": "noul", "instructions": "Is this valid?"}],
    }
    result = await registry.dispatch(ctx, args, allowed_tools=["evaluate"])
    assert result["ok"] and result["data"]["content"] == "q: probability of yes 0.5"
    assert handler.await_args.args == (args["state"], args["questions"])
    assert handler.await_args.kwargs["usage_context"] == TaskUsageContext(
        agent_id="agent", session_id="session", run_id="run"
    )
    with pytest.raises(ToolContractError, match='gives "instructions" both inside and outside'):
        await registry.dispatch(
            ctx, {**args, "instructions": "Also execute a program"}, allowed_tools=["evaluate"]
        )
    assert handler.await_count == 1
    with pytest.raises(ToolNotAllowedError):
        await registry.dispatch(ctx, args, allowed_tools=[])
    service.available = lambda: False
    # Readiness captures the bound predicate at registration.
    registry = ToolRegistry()
    register_evaluate_tool(registry, service)
    assert (await registry.dispatch(ctx, args))["error"]["code"] == "tool_not_ready"


@pytest.mark.asyncio
async def test_dispatch_repairs_question_encoding_without_rewriting_state_or_unsupported_effects(
    tmp_path,
):
    tool = Evaluate(tmp_path)
    state = '{"value":"TRUE","score":"3.0"}'
    questions = [{"id": "q", "type": "noul", "instructions": "Has values?"}]

    result, _text = await tool.call({"state": state, "questions": json.dumps(questions)})

    assert result["ok"] and tool.received == [(state, questions)]
    for invalid in [
        {"state": state, "questions": [{**questions[0], "type": "nou1"}]},
        {"state": state, "questions": questions, "execute": "program"},
        {"state": state, "questions": [{**questions[0], "explain": True}]},
    ]:
        assert not (await tool.call(invalid))[0]["ok"]
    assert tool.received == [(state, questions)]


@pytest.mark.asyncio
async def test_answers_read_as_one_line_each_in_question_order(tmp_path):
    tool = Evaluate(tmp_path)

    result, text = await tool.call(
        {
            "state": "The build failed twice today with the same timeout.",
            "questions": [
                {"id": "flaky", "type": "noul", "instructions": "Is this a flaky test?"},
                {
                    "id": "kind",
                    "type": "choice",
                    "instructions": "What kind of failure is it?",
                    "criteria": {"infra": "Infrastructure", "code": "A code regression"},
                },
                {
                    "id": "urgency",
                    "type": "score",
                    "instructions": "How urgent is a fix?",
                    "criteria": ["None", "Low", "Medium", "High", "Critical"],
                },
            ],
        }
    )

    assert result["ok"] is True
    assert text == (
        "model: fixture/model\n"
        "usage: 120 input and 12 output tokens\n\n"
        "flaky: probability of yes 0.82\n"
        "kind: infra (confidence 0.9; infra 0.9, code 0.1)\n"
        "urgency: level 2.6 on levels 0-4"
    )


@pytest.mark.asyncio
async def test_a_provider_failure_names_the_outcome_without_a_retry_flag(tmp_path):
    tool = Evaluate(
        tmp_path,
        error=DecisionError(
            "The Provider may have processed this evaluation, but no usable result arrived. A "
            "new evaluation may incur another charge.",
            code="outcome_unknown",
        ),
    )

    _result, text = await tool.call(
        {"state": "x", "questions": [{"id": "q", "type": "noul", "instructions": "Yes?"}]}
    )

    assert text == (
        "Error (outcome_unknown): The Provider may have processed this evaluation, but no "
        "usable result arrived. A new evaluation may incur another charge."
    )


class TestOtherShapes:
    @pytest.mark.asyncio
    async def test_the_wire_map_of_ids_to_questions(self, tmp_path):
        tool = Evaluate(tmp_path)

        result, _text = await tool.call(
            {"state": "x", "questions": {"flaky": {"type": "noul", "instructions": "Flaky?"}}}
        )

        assert result["ok"] is True
        assert tool.received == [("x", [{"id": "flaky", "type": "noul", "instructions": "Flaky?"}])]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arguments",
        [
            {"state": "x", "question": "Is it spam?", "type": "yes_no"},
            {"input": "x", "type": "boolean", "prompt": "Is it spam?"},
            {"state": "x", "questions": [{"question": "Is it spam?"}], "type": "Yes/No"},
            {"state": "x", "questions": {"type": "probability", "text": "Is it spam?"}},
        ],
    )
    async def test_one_question_written_another_way(self, tmp_path, arguments):
        tool = Evaluate(tmp_path)

        result, _text = await tool.call(arguments)

        assert result["ok"] is True
        assert tool.received == [
            ("x", [{"id": "q1", "type": "noul", "instructions": "Is it spam?"}])
        ]

    @pytest.mark.asyncio
    async def test_question_spellings_and_label_lists(self, tmp_path):
        tool = Evaluate(tmp_path)

        result, _text = await tool.call(
            {
                "state": {"title": "Crash on start"},
                "questions": [
                    {
                        "name": "kind",
                        "kind": "classification",
                        "question": "Which kind of report is this?",
                        "options": ["bug", {"label": "feature", "description": "A request"}],
                    },
                    {
                        "id": 7,
                        "type": "rating",
                        "prompt": "How severe?",
                        "levels": {"0": "Cosmetic", "1": "Annoying", "2": "Blocking"},
                    },
                ],
            }
        )

        assert result["ok"] is True
        assert tool.received[0][1] == [
            {
                "id": "kind",
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
        ]

    @pytest.mark.asyncio
    async def test_missing_ids_follow_position_and_skip_taken_ones(self, tmp_path):
        tool = Evaluate(tmp_path)

        await tool.call(
            {
                "state": "x",
                "questions": [
                    {"type": "noul", "instructions": "A?"},
                    {"id": "q1", "type": "noul", "instructions": "B?"},
                    {"id": "", "type": "noul", "instructions": "C?"},
                ],
            }
        )

        assert [question["id"] for question in tool.received[0][1]] == ["q2", "q1", "q3"]

    @pytest.mark.asyncio
    async def test_yes_and_no_criteria_describe_true_and_false(self, tmp_path):
        tool = Evaluate(tmp_path)

        result, _text = await tool.call(
            {
                "state": "x",
                "questions": [
                    {
                        "id": "q",
                        "type": "noul",
                        "instructions": "Done?",
                        "criteria": {"Yes": "All steps ran", "no": "A step is missing"},
                    }
                ],
            }
        )

        assert result["ok"] is True
        assert tool.received[0][1][0]["criteria"] == {
            "true": "All steps ran",
            "false": "A step is missing",
        }

    @pytest.mark.asyncio
    async def test_a_number_as_state_is_judged_as_text(self, tmp_path):
        tool = Evaluate(tmp_path)

        await tool.call({"state": 42, "questions": [{"type": "noul", "instructions": "Even?"}]})

        assert tool.received[0][0] == "42"


class TestRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("question", "message"),
        [
            (
                "Is it spam?",
                'question "q1" needs "type": "noul" for the probability of yes, "choice" to pick '
                'one of your labels, or "score" to rate on your levels. Send one of: '
                '{"id":"q1","type":"noul","instructions":"Is it spam?"} or {"id":"q1",'
                '"type":"choice","instructions":"Is it spam?","criteria":{"<label>":'
                '"<what it means>","<other label>":"<what it means>"}} or {"id":"q1",'
                '"type":"score","instructions":"Is it spam?","criteria":["<lowest level>",'
                '"<next level>","<highest level>"]}',
            ),
            (
                {"id": "kind", "type": "choice", "instructions": "Which?", "criteria": ["bug"]},
                'question "kind" is a choice, so "criteria" names at least two labels and what '
                'each means. Send: {"id":"kind","type":"choice","instructions":"Which?",'
                '"criteria":{"<label>":"<what it means>","<other label>":"<what it means>"}}',
            ),
            (
                {
                    "id": "s",
                    "type": "score",
                    "instructions": "How bad?",
                    "criteria": {"1": "Low", "2": "High"},
                },
                'question "s" is a score, and score levels are numbered from 0, so level 0 would '
                'be "Low". If that is meant, send: {"id":"s","type":"score","instructions":'
                '"How bad?","criteria":["Low","High"]}',
            ),
            (
                {"id": "s", "type": "score", "instructions": "How bad?"},
                'question "s" is a score, so "criteria" lists what each level means, lowest '
                'first; the answer is a level number from 0. Send: {"id":"s","type":"score",'
                '"instructions":"How bad?","criteria":["<lowest level>","<next level>",'
                '"<highest level>"]}',
            ),
            (
                {"id": "n", "type": "noul", "instructions": "Ok?", "criteria": {"likely": "Y"}},
                'question "n" is a noul question; its optional "criteria" say what yes and no '
                'mean. Send: {"id":"n","type":"noul","instructions":"Ok?","criteria":'
                '{"true":"<when yes applies>","false":"<when no applies>"}}',
            ),
            (
                {"id": "n", "type": "nou1", "instructions": "Ok?"},
                'question "n" has type "nou1"; use "noul" for the probability of yes, "choice" '
                'to pick one of your labels, or "score" to rate on your levels.',
            ),
            (
                {"id": "n", "type": "noul", "instructions": "Ok?", "explain": True},
                'question "n" has "explain", which evaluate cannot use: a question takes only '
                "id, type, instructions and criteria, and answers carry no explanations. Send "
                'it without it: {"id":"n","type":"noul","instructions":"Ok?"}',
            ),
            (
                {"id": "n", "type": "noul", "question": "Is it late?", "prompt": "Is it done?"},
                'a question gives "instructions" twice with different values. Send the question '
                'you mean: {"id":"n","type":"noul","instructions":"Is it late?"} or {"id":"n",'
                '"type":"noul","instructions":"Is it done?"}',
            ),
            (
                {"id": "n", "type": "noul"},
                'question "n" needs "instructions": the question itself. Send: {"id":"n",'
                '"type":"noul","instructions":"<the question>"}',
            ),
        ],
    )
    async def test_an_unanswerable_question_names_the_corrected_one(
        self, tmp_path, question, message
    ):
        tool = Evaluate(tmp_path)

        result, text = await tool.call({"state": "x", "questions": [question]})

        assert result["ok"] is False
        assert text == f"Error (invalid_arguments): evaluate was not run: {message}"
        assert tool.received == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            (
                {"questions": [{"type": "noul", "instructions": "A?"}]},
                'it needs "state": the text or JSON the questions are about.',
            ),
            (
                {"state": "x"},
                'it needs "questions", at least one, such as [{"id":"q1","type":"noul",'
                '"instructions":"<the question>"}].',
            ),
            (
                {
                    "state": "x",
                    "questions": [
                        {"id": "a", "type": "noul", "instructions": "A?"},
                        {"id": "a", "type": "noul", "instructions": "B?"},
                    ],
                },
                'two questions have the id "a"; give each its own id.',
            ),
        ],
    )
    async def test_an_incomplete_call_says_what_is_missing(self, tmp_path, arguments, message):
        tool = Evaluate(tmp_path)

        _result, text = await tool.call(arguments)

        assert text == f"Error (invalid_arguments): evaluate was not run: {message}"
        assert tool.received == []

    @pytest.mark.asyncio
    async def test_fields_outside_several_questions_stay_refused(self, tmp_path):
        tool = Evaluate(tmp_path)

        result, text = await tool.call(
            {
                "state": "x",
                "type": "noul",
                "questions": [{"instructions": "A?"}, {"instructions": "B?"}],
            }
        )

        assert result["ok"] is False
        assert text == (
            'Error (invalid_arguments): evaluate was not run: "type" is outside "questions", '
            "and the call has 2 questions, so it is not clear which one it belongs to. Put it "
            "inside each question it applies to."
        )
        assert tool.received == []

    @pytest.mark.asyncio
    async def test_a_field_given_twice_with_different_values_offers_both(self, tmp_path):
        tool = Evaluate(tmp_path)

        _result, text = await tool.call(
            {
                "state": "x",
                "prompt": "Is it done?",
                "questions": [{"id": "q", "type": "noul", "instructions": "Is it late?"}],
            }
        )
        same, _ = await tool.call(
            {
                "state": "x",
                "type": "noul",
                "questions": [{"id": "q", "type": "noul", "instructions": "Is it late?"}],
            }
        )

        assert text == (
            'Error (invalid_arguments): evaluate was not run: the call gives "instructions" '
            "both inside and outside its question, with different values. Send the question "
            'you mean: {"id":"q","type":"noul","instructions":"Is it late?"} or '
            '{"id":"q","type":"noul","instructions":"Is it done?"}'
        )
        assert same["ok"] is True
        assert tool.received == [
            ("x", [{"id": "q", "type": "noul", "instructions": "Is it late?"}])
        ]
