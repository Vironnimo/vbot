"""Computer calls resolve temporary participants through their bound Session."""

from dataclasses import replace

import pytest

from core.agents.temporary import TemporaryAgentConfig
from core.runs import RunExecutionOwner
from core.runtime.runtime import Runtime
from core.tools.availability import ToolAccess
from core.utils.config import Config
from tests.resources.extensions.computer_use_helpers import computer as computer


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [True, False])
async def test_temporary_computer_dispatch_checks_binding_and_permission(
    tmp_path, computer, granted
):
    runtime = Runtime(Config(data_dir=tmp_path / "runtime"))
    runtime.start()
    try:
        binding = runtime._temporary_agents.create(
            owner_name="swarm",
            group_id="group",
            participant_id="peer",
            config=TemporaryAgentConfig(
                model="fixture/model",
                cwd=tmp_path,
                tool_access=ToolAccess(
                    mode="selected", allowed=("computer",), granted=("computer",) if granted else ()
                ),
                allowed_skills=[],
                tools={},
                name="Peer",
            ),
        )
        service, ctx, client, _ = computer
        service.host = runtime._extension_host()
        owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "epoch")
        ctx = replace(
            ctx,
            agent_id=binding.address.agent_id,
            session_id=binding.address.session_id,
            project_id=binding.address.project_id,
            execution_owner=owner,
        )
        assert not runtime.agents.exists(ctx.agent_id)
        registry = service.api.operations.tool_registry
        arguments = {"action": "capture", "pid": 1, "window_id": 2, "mode": "ax"}
        result = await registry.dispatch(ctx, arguments, ["computer"])
        assert result["ok"] is granted
        if granted:
            assert any(name == "get_window_state" for name, _ in client.calls)
            assert result["data"]["elements"]
        else:
            assert client.calls == [] and client.connects == 0
        for invalid in (
            None,
            replace(owner, generation_id="replaced"),
            replace(owner, participant_id="other"),
            replace(owner, group_id="other"),
            replace(owner, extension="other"),
        ):
            before = (list(client.calls), client.connects)
            rejected = await registry.dispatch(
                replace(ctx, execution_owner=invalid), arguments, ["computer"]
            )
            assert not rejected["ok"]
            assert (client.calls, client.connects) == before
    finally:
        runtime.stop()
