"""Computer calls resolve temporary participants through their bound Session."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.agents.temporary import TemporaryAgentConfig
from core.runs import RunExecutionOwner
from core.runtime.runtime import Runtime
from core.tools.availability import ToolAccess
from core.utils.config import Config
from tests.resources.extensions.computer_use.computer_use_test_support import computer as computer


@pytest.mark.asyncio
async def test_temporary_computer_dispatch_checks_binding_and_permission(
    config: Config, tmp_path: Path, computer: Any
) -> None:
    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.extensions is not None
        root = runtime._extension_host()  # noqa: SLF001 - the Runtime hands hosts only to Extensions.
        assert root.for_owner is not None
        groups = root.for_owner(runtime.extensions.registration_identity("swarm")).temporary_agents
        assert groups is not None
        service, ctx, client, _ = computer
        service.host = root
        registry = service.api.operations.tool_registry
        arguments = {"action": "capture", "pid": 1, "window_id": 2, "mode": "ax"}

        async def participant_call(participant_id: str, *, granted: bool) -> tuple[Any, Any]:
            binding = await groups.create(
                "group",
                participant_id,
                TemporaryAgentConfig(
                    model="fixture/model",
                    cwd=tmp_path,
                    tool_access=ToolAccess(
                        mode="selected",
                        allowed=("computer",),
                        granted=("computer",) if granted else (),
                    ),
                    allowed_skills=[],
                    tools={},
                    name="Peer",
                ),
            )
            owner = RunExecutionOwner(
                "swarm", "group", participant_id, binding.generation_id, "epoch"
            )
            call = replace(
                ctx,
                agent_id=binding.address.agent_id,
                session_id=binding.address.session_id,
                project_id=binding.address.project_id,
                execution_owner=owner,
            )
            assert not runtime.agents.exists(call.agent_id)
            return call, owner

        # Without the permission grant the desktop is never touched.
        denied_call, _ = await participant_call("denied", granted=False)
        result = await registry.dispatch(denied_call, arguments, ["computer"])
        assert result["ok"] is False
        assert client.calls == [] and client.connects == 0

        granted_call, owner = await participant_call("granted", granted=True)
        result = await registry.dispatch(granted_call, arguments, ["computer"])
        assert result["ok"] is True
        assert any(name == "get_window_state" for name, _ in client.calls)
        assert result["data"]["elements"]

        # A call whose execution owner does not match its binding is rejected unused.
        for invalid in (
            None,
            replace(owner, generation_id="replaced"),
            replace(owner, participant_id="other"),
            replace(owner, group_id="other"),
            replace(owner, extension="other"),
        ):
            before = (list(client.calls), client.connects)
            rejected = await registry.dispatch(
                replace(granted_call, execution_owner=invalid), arguments, ["computer"]
            )
            assert not rejected["ok"]
            assert (client.calls, client.connects) == before
    finally:
        await runtime.aclose()
