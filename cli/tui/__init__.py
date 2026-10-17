"""Full-screen, server-backed terminal conversation accessor.

Imported only by the interactive command, so ordinary CLI commands do not load
Textual or its terminal drivers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cli.server_management import ServerInstance


async def run_tui(
    *,
    target: ServerInstance,
    agent_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
) -> int:
    """Run until the operator detaches; server Runs keep their own lifetime."""
    from cli.tui.app import ConversationApp
    from cli.tui.drafts import DraftStore
    from cli.tui.transport import TerminalClient

    store = DraftStore(target.url)
    restored = await asyncio.to_thread(store.load)
    async with TerminalClient(target) as client:
        app = ConversationApp(
            client,
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            draft_store=store,
            restored_drafts=restored,
        )
        try:
            return await app.run_async() or 0
        finally:
            await app.persist_drafts()
