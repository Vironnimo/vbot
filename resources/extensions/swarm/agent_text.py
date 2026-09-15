"""Reviewed Swarm Agent contract; shared by registration and interface probes."""

from typing import Any

BOARD_DESCRIPTION = (
    "Read and contribute to your group's shared Board. Use the main discussion for shared "
    "conversation and coordination. Create an additional discussion when several Agents "
    "need to work through a specific problem together. Discussions are public to all "
    "participants; joining controls which future discussion posts reach your Inbox. "
    "Use recipients to publicly ping participant IDs. Creating a discussion joins it and "
    "announces it in the main discussion. Joining returns recent posts. Reading posts "
    "also receives any matching pending Inbox messages when the Tool Result is saved."
)

INBOX_DESCRIPTION = (
    "Receive pending Board messages for you, oldest first. Returned messages count "
    "as delivered when this Tool Result is saved. Follow next_call when more "
    "remain. This Tool returns immediately. When you have no further work now, end "
    "your reply normally; new messages can start another Run according to the "
    "group's delivery settings."
)

INBOX_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Maximum pending messages to receive. Omit for 20.",
        },
    },
    "required": [],
}

STATE_DESCRIPTION = (
    "Inspect participants, their Run activity, pending message counts, and delivery settings."
)

STATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cursor": {
            "type": "string",
            "description": "Roster continuation from a status result. Omit for the first page.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Maximum roster entries for status. Omit for 20.",
        },
    },
    "required": [],
}


EMPTY_INBOX = (
    "No pending Board messages. Continue useful work if any remains; otherwise end "
    "your reply normally."
)

BOARD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "read", "post", "create", "join", "leave"],
            "description": "List discussions, read posts, post a message, create a discussion "
            "with an opening message, or join or leave a discussion.",
        },
        "discussion_id": {
            "type": "string",
            "description": "Discussion to read, post in, join, or leave. Required for join and "
            "leave. Omit for read to use the main discussion; omit for post to use the "
            "reply target's discussion, or the main discussion when not replying.",
        },
        "message_id": {
            "type": "string",
            "description": "Exact post to read. Omit to read a discussion page; when supplied, "
            "omit discussion_id, cursor, and limit. Discussion pages start with the newest "
            "posts, oldest first within each page.",
        },
        "cursor": {
            "type": "string",
            "description": "Continuation returned by a previous list or read result. "
            "For read, continuation retrieves older posts. Omit to start a fresh listing "
            "or read the newest posts.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Maximum entries for list or a discussion-page read. Omit for 20.",
        },
        "text": {
            "type": "string",
            "maxLength": 16000,
            "description": "Message body. Required for post and create.",
        },
        "title": {
            "type": "string",
            "maxLength": 120,
            "description": "Discussion title. Required for create.",
        },
        "reply_to": {
            "type": "string",
            "description": "Post to answer with post. Omit for a new message. Its discussion is "
            "used unless discussion_id explicitly selects the same discussion.",
        },
        "recipients": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Participant IDs to publicly ping with post or on the opening message "
            "of create. Omit when no explicit ping is needed.",
        },
    },
    "required": ["action"],
}

POST_SAVED = (
    "The post is saved on the Board. Recipient counts describe routing, not whether "
    "another participant has read it."
)
DISCUSSION_ANNOUNCEMENT = (
    '{author_name} opened the discussion "{title}".\n'
    "Discussion ID: {discussion_id}\n"
    "Opening post ID: {opening_post_id}\n"
    "Use swarm_board with action read or join and this discussion_id to view the discussion."
)
REPLAYED = (
    "This request was already applied. The original result is returned; no duplicate was created."
)

DELIVERY_PREFIX = (
    "New Board messages from the authors listed below. If pending_remaining is greater than "
    "zero, use swarm_inbox to read the remaining messages."
)
RESUME_REMINDER = (
    "The user resumed your work. Continue toward the group's goal from where you left off."
)
INITIAL_MESSAGE = (
    "Read the user's request in Board post {goal_post_id} using swarm_board, then discuss it "
    "with the other Agents on the Board before starting implementation.\n\n"
    "Take time to understand the request together and explore how to achieve the best possible "
    "result. Respond to one another, ask follow-up questions, compare alternatives, and work "
    "through disagreements. Explain your reasoning so others can examine and improve it. "
    "Agreement alone is not a substitute for that discussion.\n\n"
    "Let your shared understanding and direction develop through the conversation. Decide "
    "together when you are ready to move from discussion into doing the work."
)
DEFAULT_INSTRUCTIONS = (
    "You are one of several Agents working together to accomplish the user's request. "
    "Use the shared collaboration Tools available to you to exchange ideas, retain useful "
    "information, and explore alternatives together. How you organize your work is up to you.\n\n"
    "Every Agent can overlook requirements or make confident but unsupported claims. Examine "
    "important claims against evidence, develop one another's ideas, and work through "
    "disagreements. Agreement alone does not make a claim correct. Keep the user's request "
    "as your common reference as your work develops."
)
DEFAULT_PROMPT_BLOCKS = ["core:tools", "core:skills"]
DEFAULT_REMINDERS = {"delivery": True, "resume": True}
REMINDER_TEXTS = {
    "delivery": DELIVERY_PREFIX,
    "resume": RESUME_REMINDER,
}
ERRORS = {
    "invalid_arguments": "Use only the fields accepted by the selected action. Omit "
    "optional fields you do not need, and follow their "
    "descriptions for required values. No change was applied.",
    "invalid_value": "A required value is missing, has the wrong type, or exceeds its "
    "documented limit. Correct the named field and try again. No "
    "change was applied.",
    "invalid_cursor": "This cursor is unavailable for this query. Omit cursor to begin "
    "a new page, then use the returned next_call.",
    "request_conflict": "This request_id was already used with different arguments. "
    "Reuse the original arguments to retrieve its result, or use a "
    "new request_id for a different change.",
    "discussion_not_found": "This discussion is unavailable in your group. Use "
    "swarm_board with action list to choose a current "
    "discussion.",
    "message_not_found": "This post is unavailable in your group. Read its discussion "
    "to find an available post.",
    "invalid_recipient": "A recipient is not a participant in your group. Use "
    "swarm_state to obtain participant IDs.",
    "reply_discussion_mismatch": "The reply target belongs to another discussion. Omit "
    "discussion_id to reply in the target's discussion, "
    "or omit reply_to for a new post.",
    "exact_message_arguments": "To read one message_id, omit discussion_id, cursor, "
    "and limit. No change was applied.",
    "main_membership_required": "Everyone remains in the main discussion. When you "
    "have no further work now, end your reply normally.",
    "swarm_closed": "This Swarm is stopped or currently unavailable for changes. Its "
    "Board remains readable. The user can Resume the Swarm when it is "
    "ready.",
    "participant_inactive": "This Run cannot act for the participant. The saved Board "
    "remains readable; the user can Resume the Swarm to "
    "continue in its existing Sessions.",
}
