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
from tests.resources.extensions.computer_use.computer_use_test_support import Harness
from tests.resources.extensions.computer_use.computer_use_test_support import (
    computer as computer,
)


@pytest.mark.asyncio
async def test_temporary_computer_dispatch_checks_binding_and_permission(
    config: Config, tmp_path: Path, computer: Harness
) -> None:
    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.extensions is not None
        root = runtime._extension_host()  # noqa: SLF001 - the Runtime hands hosts only to Extensions.
        assert root.for_owner is not None
        groups = root.for_owner(runtime.extensions.registration_identity("swarm")).temporary_agents
        assert groups is not None
        computer.service.host = root
        arguments = {"action": "screenshot"}

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
                computer.context_for("computer"),
                agent_id=binding.address.agent_id,
                session_id=binding.address.session_id,
                project_id=binding.address.project_id,
                execution_owner=owner,
            )
            assert not runtime.agents.exists(call.agent_id)
            return call, owner

        # Without the permission grant the desktop is never touched.
        denied_call, _ = await participant_call("denied", granted=False)
        result = await computer.call("computer", arguments, denied_call)
        assert result["error"]["code"] == "tool_not_allowed"
        assert denied_call.result_media == []

        granted_call, owner = await participant_call("granted", granted=True)
        result = await computer.call("computer", arguments, granted_call)
        assert result["ok"] is True
        assert len(granted_call.result_media) == 1

        # A call whose execution owner does not match its binding is rejected unused.
        for invalid in (
            None,
            replace(owner, generation_id="replaced"),
            replace(owner, participant_id="other"),
            replace(owner, group_id="other"),
            replace(owner, extension="other"),
        ):
            rejected_call = replace(granted_call, execution_owner=invalid, result_media=[])
            rejected = await computer.call("computer", arguments, rejected_call)
            assert rejected["error"]["code"] == "computer_use_unavailable"
            assert rejected_call.result_media == []
        assert computer.target.inputs == []
    finally:
        await runtime.aclose()
