"""Swarm references that Agents send to file Tools, and the Swarm Tool calls they mean.

Swarm participants see Wiki pages as ``w3`` and Board posts as ``#42``, and some
send them to ``read``, ``edit`` or ``search_files``: a page number as a path, a
post as ``message_id`` (Swarm Sessions, 2026-10-12). The file Tools cannot run
such a call, but their errors name the Swarm call that reads or changes the
page or post, when the Agent is offered that Tool. The bundled Swarm
Extension owns these Tools; this module is the one place outside it that names
them and their call shape (``extensions/swarm.md``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SWARM_WIKI_TOOL = "swarm_wiki"
SWARM_BOARD_TOOL = "swarm_board"

_PAGE_NUMBER = re.compile(r"w(\d+)(?:\.md)?", re.IGNORECASE)


def wiki_page_call(
    missing: Path,
    cwd: Path,
    offers: Callable[[str], bool],
    *,
    change: bool = False,
    fields: Mapping[str, Any] | None = None,
) -> str | None:
    """Name the swarm_wiki call for a missing path that is a bare Wiki page number.

    ``missing`` is the resolved path; only a page number in the working directory
    itself (``w3``, ``w3.md``) counts, and only when the Agent is offered swarm_wiki.
    A read call carries ``fields`` (such as a character ``offset``) along.
    """
    match = _PAGE_NUMBER.fullmatch(missing.name)
    if match is None or os.path.normcase(missing.parent) != os.path.normcase(cwd):
        return None
    if not offers(SWARM_WIKI_TOOL):
        return None
    page = f"w{int(match[1])}"
    if change:
        return (
            f'Swarm Wiki page {page} changes with {SWARM_WIKI_TOOL}: action "update", '
            f'page_id "{page}", and old_text and new_text for the passage.'
        )
    call = json.dumps({"action": "read", "page_id": page, **(fields or {})}, ensure_ascii=False)
    return f"Swarm Wiki page {page} reads with {SWARM_WIKI_TOOL} {call}."


def board_post_call(message_id: Any, offers: Callable[[str], bool]) -> str | None:
    """Name the swarm_board call that reads post ``message_id``, when it is offered."""
    if not isinstance(message_id, str) or not message_id.strip():
        return None
    if not offers(SWARM_BOARD_TOOL):
        return None
    post = message_id.strip()
    call = json.dumps({"action": "read", "message_id": post}, ensure_ascii=False)
    return f"{post} is a Swarm Board post; read it with {SWARM_BOARD_TOOL} {call}."
