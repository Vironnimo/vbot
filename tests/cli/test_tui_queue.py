"""Queue invalidation across terminal conversations and other accessors."""

from __future__ import annotations

import pytest

from cli.tui.app import ConversationApp
from tests.cli.test_tui import Server


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, "vbot"])
async def test_external_queue_changes_refresh_only_the_addressed_conversation(
    project: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server()
    address = f"main@{project}" if project else "main"
    app = ConversationApp(server, agent_id=address, session_id="session-one")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.queue_items
        scheduled = []
        monkeypatch.setattr(app.navigation, "schedule_refresh", lambda: scheduled.append(True))
        for changed_project, changed_session in (("other", "session-one"), (project, "other")):
            await server.bus.put(
                {
                    "type": "resource_changed",
                    "payload": {
                        "kind": "queue",
                        "scope": {
                            "agent_id": "main",
                            "project_id": changed_project,
                            "session_id": changed_session,
                        },
                    },
                }
            )
            await pilot.pause()
        assert scheduled == []

        server.queued = []
        await server.bus.put(
            {
                "type": "resource_changed",
                "payload": {
                    "kind": "queue",
                    "scope": {
                        "agent_id": "main",
                        "project_id": project,
                        "session_id": "session-one",
                    },
                },
            }
        )
        await pilot.pause()
        assert scheduled == [True]
        await app.navigation.refresh_scheduled()
        await pilot.pause()
        assert app.queue_items == []
        assert not any(method == "chat.stream" for method, _params in server.calls)
