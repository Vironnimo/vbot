"""Public contract for optional shared decision questions."""

from typing import Any

DECISION_DESCRIPTION = (
    "Explore a shared question through alternatives and each participant's current position. "
    "Create questions, add or revise options, move your support, and read reasons and history. "
    "Positions show which version their author considered; counts do not declare a winner. "
    "Questions can be revisited during work. Share returned links in your group's discussions "
    "when attention is useful; changes do not send messages or wake participants."
)
DECISION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list",
                "read",
                "create",
                "update",
                "add_option",
                "update_option",
                "position",
                "withdraw",
                "reconsider",
                "history",
            ],
            "description": (
                "Discover questions, read options or positions, edit alternatives, "
                "state your own position, or start a fresh consideration of the same "
                "question."
            ),
        },
        "question_id": {
            "type": "string",
            "description": "Question ID. Required except for list and create.",
        },
        "title": {
            "type": "string",
            "description": (
                "Question or option title. Required for create and add_option; omit "
                "on update to keep it."
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "Markdown context for a question or option. Omit to keep existing "
                "text, or for empty text on creation."
            ),
        },
        "option_id": {
            "type": "string",
            "description": (
                "Option to edit or support. Required for update_option; omit with "
                "position to express an undecided position or concern without "
                "supporting an option."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Your reason, concern, or condition with position. Omit for an empty "
                "note. Longer evidence can be linked from shared documents."
            ),
        },
        "withdrawn": {
            "type": "boolean",
            "description": (
                "Whether an option is withdrawn, with update_option. Omit to keep its"
                " state; false restores it."
            ),
        },
        "archived": {
            "type": "boolean",
            "description": (
                "Whether a question is archived, with update. Omit to keep its state."
                " Archiving records no collective agreement."
            ),
        },
        "include_archived": {
            "type": "boolean",
            "description": "Include archived questions with list. Omit to list open questions.",
        },
        "expected_revision": {
            "type": "integer",
            "description": (
                "Question revision you read. Required for all changes except create "
                "and withdraw; other participants' position updates do not change it."
            ),
        },
        "position_revision": {
            "type": "integer",
            "description": (
                "Your previous position revision, required for position and withdraw."
                " Use 0 if you have never stated a position; read returns your "
                "current value."
            ),
        },
        "section": {
            "type": "string",
            "enum": ["options", "positions"],
            "description": (
                "Part of the question to read. Omit for options and a compact support"
                " overview; positions includes individual reasons."
            ),
        },
        "query": {
            "type": "string",
            "description": "Case-insensitive title or context search with list. Omit to browse.",
        },
        "cursor": {
            "type": "string",
            "description": (
                "Continuation returned by list, read, or history. Omit for current state."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Maximum entries returned. Omit for 20; up to 100.",
        },
        "request_id": {
            "type": "string",
            "description": (
                "Required for mutations. Reuse with identical arguments to retry an "
                "uncertain result."
            ),
        },
    },
    "required": ["action"],
}
DECISION_ERRORS = {
    "decision_not_found": (
        "This question is not in your group. Use list to find an available question."
    ),
    "decision_conflict": (
        "The question changed. No change was applied. Read the current "
        "question, consider the changes, and retry with its revision and a "
        "new request_id."
    ),
    "position_conflict": (
        "Your position changed. No change was applied. Read your current "
        "position and retry with its position_revision and a new request_id."
    ),
    "option_not_found": (
        "This option is not in the question. Read the question to find its options."
    ),
    "option_withdrawn": (
        "This option is withdrawn. Read the current alternatives before choosing a position."
    ),
    "decision_archived": (
        "This question is archived. Use update with archived false and the "
        "current revision to reopen it."
    ),
    "decision_option_limit": (
        "This question already has 50 options. Update an existing option or "
        "open a separate question."
    ),
}
