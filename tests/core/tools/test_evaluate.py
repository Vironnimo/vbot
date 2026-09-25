import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.model_tasks import TaskUsageContext
from core.model_tasks.decision_types import validate_input
from core.tools.contracts import ToolContractError
from core.tools.evaluate import register_evaluate_tool
from core.tools.tools import ToolContext, ToolNotAllowedError, ToolRegistry


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
    ctx = ToolContext(
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
    args = {
        "state": {"type": "TRUE", "nested": [{"score": "3.0"}]},
        "questions": [{"id": "q", "type": "noul", "instructions": "Is this valid?"}],
    }
    result = await registry.dispatch(ctx, args, allowed_tools=["evaluate"])
    assert result["ok"] and result["data"]["answers"]["q"]["noul"] == 0.5
    assert handler.await_args.args == (args["state"], args["questions"])
    assert handler.await_args.kwargs["usage_context"] == TaskUsageContext(
        agent_id="agent", session_id="session", run_id="run"
    )
    with pytest.raises(ToolContractError, match='"instructions" is not a parameter'):
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
    received = []

    async def execute(state, questions, *, usage_context):
        state, questions = validate_input(state, questions)
        received.append((state, questions))
        return {
            "answers": {"q": {"type": "noul", "noul": 0.8}},
            "model": "fixture",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    registry = ToolRegistry()
    register_evaluate_tool(registry, SimpleNamespace(available=lambda: True, evaluate=execute))
    ctx = ToolContext(
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
    state = '{"value":"TRUE","score":"3.0"}'
    questions = [{"id": "q", "type": "noul", "instructions": "Has values?"}]
    result = await registry.dispatch(ctx, {"state": state, "questions": json.dumps(questions)})
    assert result["ok"] and received == [(state, questions)]
    with pytest.raises(ToolContractError):
        await registry.dispatch(
            ctx, {"state": state, "questions": [{**questions[0], "type": "nou1"}]}
        )
    with pytest.raises(ToolContractError, match='"execute" is not a parameter'):
        await registry.dispatch(ctx, {"state": state, "questions": questions, "execute": "program"})
    for invalid in [
        {"state": state, "questions": [{**questions[0], "criteria": {"yes": "Yes", "no": "No"}}]},
        {"state": state, "questions": [{**questions[0], "explain": True}]},
    ]:
        assert not (await registry.dispatch(ctx, invalid))["ok"]
    assert received == [(state, questions)]
