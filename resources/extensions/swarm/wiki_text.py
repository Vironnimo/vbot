"""Agent-facing Wiki capability, shared with management and probes."""

from typing import Any

WIKI_DESCRIPTION = (
    "Create, find, and collaboratively edit your group's shared Markdown Wiki pages. "
    "Use list with query to search titles and contents, or omit query for recently changed pages. "
    "Read current or historical versions and restore earlier content, including deleted pages. "
    "Pages are public within your group. Share page links in your group's"
    " discussions when you want "
    "others to notice them; Wiki edits do not send Board messages or wake participants."
)

WIKI_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "read", "create", "update", "delete", "history", "restore"],
            "description": (
                "List or search pages, read content, create or update a page, "
                "delete it, list its history, or restore a historical version."
            ),
        },
        "page_id": {
            "type": "string",
            "description": "Page ID returned by this Tool. Required except for list and create.",
        },
        "title": {
            "type": "string",
            "description": "Page title. Required for create; omit for update to keep its title.",
        },
        "content": {
            "type": "string",
            "description": (
                "Complete Markdown content. Required for create. For update, omit "
                "to keep content or use old_text and new_text for a targeted "
                "change."
            ),
        },
        "old_text": {
            "type": "string",
            "description": (
                "Nonempty passage to replace once with update. Supply new_text and omit content. "
                "Use enough context to identify one match; minor formatting differences are "
                "tolerated. Omit for a whole-page update."
            ),
        },
        "new_text": {
            "type": "string",
            "description": (
                "Replacement text for old_text, including an empty string to remove"
                " it. Omit unless using old_text."
            ),
        },
        "expected_revision": {
            "type": "integer",
            "description": (
                "Page revision observed before changing it. Required for update, delete, and "
                "restore. An older revision is accepted for old_text/new_text alone when the "
                "passage still matches uniquely, allowing formatting differences. Other "
                "changes require the current revision."
            ),
        },
        "revision": {
            "type": "integer",
            "description": (
                "Historical revision to read or restore. Required for restore; omit"
                " for read to get the current version."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Case-insensitive text to find in titles and content with list. "
                "Omit to list all pages."
            ),
        },
        "include_deleted": {
            "type": "boolean",
            "description": "Include deleted pages in list. Omit to show only live pages.",
        },
        "offset": {
            "type": "integer",
            "description": "Character offset for read. Omit to begin at the start.",
        },
        "limit": {
            "type": "integer",
            "description": (
                "Maximum characters for read (default 12000, up to 20000), or "
                "entries for list/history (default 20, up to 100). Omit for the "
                "default."
            ),
        },
        "cursor": {
            "type": "string",
            "description": "Continuation from list or history. Omit to start a new query.",
        },
        "request_id": {
            "type": "string",
            "description": (
                "Stable request identifier for create, update, delete, or restore. "
                "Required for those actions; reuse with identical arguments after "
                "an uncertain result."
            ),
        },
    },
    "required": ["action"],
}

WIKI_ERRORS = {
    "wiki_page_not_found": (
        "This page is not in your group's Wiki. Use list to find an available page."
    ),
    "wiki_revision_not_found": (
        "That revision does not exist for this page. Use history to find a saved version."
    ),
    "wiki_deleted": (
        "This page is deleted. Read its history and use restore with a saved "
        "revision to recover it."
    ),
    "wiki_revision_conflict": (
        "The supplied revision differs from the current page, and this change could not be "
        "safely applied to it. No change was applied. "
        "Read the current page, reconcile your change, and retry with its revision "
        "and a new request_id."
    ),
    "wiki_edit_conflict": (
        "old_text did not identify one matching passage, even with text-matching tolerance. "
        "No change was applied. Read the relevant content and supply old_text with enough "
        "surrounding context to identify a unique passage."
    ),
}
