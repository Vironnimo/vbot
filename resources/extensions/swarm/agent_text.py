"""Reviewed Swarm Agent contract; shared by registration and interface probes."""

from typing import Any

BOARD_DESCRIPTION = (
    "Read and contribute to your group's shared Board. Use the main discussion for shared "
    "conversation and coordination. Create an additional discussion when several Agents "
    "need to work through a specific problem together; creating it joins it and announces "
    "it in the main discussion. Every participant can read every discussion; joining one "
    "makes its future posts reach you and returns its recent posts. Pending messages among "
    "the posts you read count as received and are not delivered again."
)

INBOX_DESCRIPTION = (
    "Receive your pending Board messages, oldest first; returned messages count as received. "
    "This Tool returns immediately and never waits for new messages. When you have no "
    "further work now, end your reply normally; new messages can start another Run "
    "according to the group's delivery settings."
)

INBOX_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum messages to receive, at most 100. Omit for 20.",
        },
    },
    "required": [],
}

STATE_DESCRIPTION = (
    "See the participants and their Run activity, your pending messages, and how Board "
    "messages reach you."
)

STATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cursor": {
            "type": "string",
            "description": "Continuation from a previous result. Omit for the first page.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum participants to list, at most 100. Omit for 20.",
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
            "description": "Discussion to read, post in, join, or leave; required for join and "
            "leave. When omitted, read and post use the main discussion, and a reply uses the "
            "discussion of its post.",
        },
        "message_id": {
            "type": "string",
            "description": "Post ID for read: show only that post. Omit to read the newest "
            "posts of a discussion, oldest first.",
        },
        "before": {
            "type": "string",
            "description": "Post ID for read: show the posts before it in its discussion, to "
            "page back.",
        },
        "cursor": {
            "type": "string",
            "description": "Continuation returned by a previous list result. Omit for the "
            "first page.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum discussions for list or posts for read, at most 100. "
            "Omit for 20.",
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
            "description": "Post ID to answer with post. Omit for a new message.",
        },
        "recipients": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Participants to publicly ping with post or on the opening message "
            'of create, by name or ID; "all" pings every other participant. Omit when no '
            "explicit ping is needed.",
        },
    },
    "required": ["action"],
}

DISCUSSION_ANNOUNCEMENT = (
    '{author_name} opened the discussion "{title}" ({discussion_id}) with post '
    "{opening_post_id}. Read or join it with swarm_board and this discussion_id."
)
REPLAYED = (
    "This Tool Call was already applied; its original result is shown and nothing was duplicated."
)

DELIVERY_PREFIX = "New Board messages for you, oldest first."
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
    "invalid_cursor": "This cursor only continues the call that returned it, with the same "
    "other arguments. Omit cursor to start from the first page.",
    "request_conflict": "This request_id was already used with different arguments. "
    "Reuse the original arguments to retrieve its result, or use a "
    "new request_id for a different change.",
    "discussion_not_found": "This discussion does not exist in your group. Call swarm_board "
    'with {"action": "list"} to see the current discussions.',
    "message_not_found": "This post is unavailable in your group. Read its discussion "
    "to find an available post.",
    "invalid_recipient": "A recipient is not a participant in your group. swarm_state lists "
    "the participants' names and IDs.",
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

# Calls that reach the wrong Swarm Tool.
BOARD_FOREIGN_ACTIONS = {
    "swarm_state": "swarm_board has no {action} action. Use swarm_state to see participants, "
    "their Run activity, and your pending message count.",
    "swarm_inbox": "swarm_board has no {action} action. Use swarm_inbox to receive your pending "
    "Board messages.",
    "swarm_wiki": "swarm_board has no {action} action. Use swarm_wiki to create, find, and edit "
    "shared pages.",
}
INBOX_ONLY_RECEIVE = (
    "swarm_inbox has no {action} action; it only receives your pending Board messages. Call it "
    "without arguments, or with limit."
)
STATE_ONLY_STATUS = (
    "swarm_state has no {action} action; it only shows the participants, their Run activity, "
    "your pending messages, and how Board messages reach you. Share progress, results, or "
    "requests for help on the Board with swarm_board, and end your reply normally when you "
    "have no further work now."
)

# Delivered messages: Inbox results and automatic delivery.
MESSAGES_IN = "In {discussion}:"
INBOX_MORE = "{count} more pending; call swarm_inbox again{arguments} to continue."
DELIVERY_MORE = {
    "inbox": "{count} more pending; receive them with swarm_inbox.",
    "no_inbox": "{count} more pending.",
}

# swarm_state results.
STATE_YOU = "{name} ({participant_id}), {state}"
STATE_PENDING = {
    "none": "No pending messages.",
    "one": "1 message for you; receive it with swarm_inbox.",
    "many": "{count} messages for you; receive them with swarm_inbox.",
    "one_no_inbox": "1 message for you.",
    "many_no_inbox": "{count} messages for you.",
}
STATE_ROUTES = {
    "main": "main-discussion posts",
    "discussion": "posts in discussions you joined",
    "ping": "pings",
}
STATE_DELIVERY = {
    "automatic": "{routes} reach you automatically, also while you are running.",
    "when_idle": "{routes} reach you automatically when you are idle.",
    "on_request": "{routes} reach you only through swarm_inbox.",
    "on_request_no_inbox": "{routes} reach you only when you read them with swarm_board.",
}
STATE_WAKE = "{routes} start a Run when you are idle."
STATE_NO_WAKE = "New messages do not start a Run when you are idle."
STATE_PARTICIPANTS = "{count} ({totals})"
STATE_ROSTER_HEADER = "Participants:"
STATE_ROSTER_LINE = "- {name} ({participant_id}{you}): {state}"
STATE_ROSTER_YOU = ", you"
STATE_MORE = "More participants exist. Continue with {call}"

# Notes on a call that ran after a repair the Agent should know about.
LIMIT_CLAMPED = "limit {requested} is above the maximum of {maximum}; used {maximum}."
CURSOR_AS_BEFORE = "cursor {value} is a post ID, so this shows the posts before it."
FIELD_IGNORED = "{field} is not used by {action} and was ignored."
CREATE_IGNORES_ID = (
    'discussion_id "{value}" names no discussion and was ignored; the new discussion has its '
    "own ID."
)
MESSAGE_DISCUSSION_IGNORED = (
    "discussion_id was ignored: post {post_id} belongs to the discussion shown with it."
)
USER_RECIPIENT = (
    "The user is not a participant and sees every Board post, so no ping was needed for the user."
)
POST_BY_NUMBER = (
    '{field} "{value}" is not a post ID; this uses post {post_id}, which has that number.'
)
POST_CLOSE_MATCH = (
    '{field} "{value}" does not exist; this uses post {post_id}, its only close match.'
)
DISCUSSION_CLOSE_MATCH = (
    'discussion_id "{value}" does not exist; this shows {discussion}, its only close match.'
)

# Board results.
BOARD_MAIN_LABEL = "the main discussion ({discussion_id})"
BOARD_DISCUSSION_LABEL = 'discussion "{title}" ({discussion_id})'
BOARD_PAGE_NEWEST = "Newest posts of {discussion}, oldest first ({count} shown)."
BOARD_PAGE_BEFORE = "Posts before {before} in {discussion}, oldest first ({count} shown)."
BOARD_PAGE_EMPTY = "No posts in {discussion} yet."
BOARD_PAGE_EMPTY_BEFORE = "No posts before {before} in {discussion}."
BOARD_PAGE_OLDER = "Older posts of {discussion}, oldest first ({count} shown)."
BOARD_ONE_POST = "Post {post_id} in {discussion}."
BOARD_OLDER = "Older posts exist. Continue with {call}"
BOARD_MORE_DISCUSSIONS = "More discussions exist. Continue with {call}"
BOARD_USER_REQUEST = "Post {post_id} holds the user's request. Read it with {call}"
BOARD_QUEUED = "Queued for {participants}{pinged}."
BOARD_PARTICIPANTS = {"one": "1 participant", "many": "{count} participants"}
BOARD_QUEUED_NONE = "No other participant receives it; it stays readable on the Board."
BOARD_PINGED = " ({count} pinged)"
BOARD_CREATED = "You joined it, and the main discussion announces it."
BOARD_JOINED = "Joined. New posts in this discussion now reach you."
BOARD_ALREADY_JOINED = "You had already joined; nothing changed."
BOARD_LEFT = "Left. New posts in this discussion no longer reach you; you can still read it."
BOARD_ALREADY_LEFT = "You were not a member; nothing changed."
BOARD_DISCUSSIONS_HEADER = "Discussions (joined ones deliver their new posts to you):"
BOARD_DISCUSSION_LINE = '- {discussion_id} "{title}": {details}'
BOARD_MAIN_DETAIL = "main discussion"
BOARD_JOINED_DETAIL = "joined"
BOARD_NOT_JOINED_DETAIL = "not joined"
BOARD_MEMBERS_DETAIL = "members: {count}"
BOARD_PENDING_DETAIL = "pending for you: {count}"
POST_HEADER_REPLY = "reply to {post_id}"
POST_HEADER_PINGED = "pinged {names}"
POST_HEADER_IN = "in {discussion}"
POST_HEADER_MAIN = "the main discussion {discussion_id}"
POST_HEADER_DISCUSSION = '"{title}" {discussion_id}'
POST_YOU = "you"
LIST_AND = " and "

# Board errors that name the next call. Each follows the failed call's cause.
BOARD_REQUIRED = {
    "text": "{action} needs text, the message body. Repeat the call with text. Nothing was saved.",
    "title": "create needs title, the new discussion's title, and text, its opening message. "
    "Nothing was saved.",
    "discussion_id": '{action} needs discussion_id. Use {{"action": "list"}} to see the '
    "discussion IDs.",
}
RECIPIENT_UNKNOWN = "recipients: {values} {verb} not a participant in your group."
RECIPIENT_SUGGESTIONS = "Did you mean {suggestions}?"
RECIPIENT_ROSTER = "Participants: {roster}."
RECIPIENT_RETRY = (
    'Repeat the call with recipients {corrected}. Names also work, and "all" pings every other '
    "participant. Nothing was saved."
)
RECIPIENT_CHOOSE = (
    'Repeat the call with recipients chosen from these participants; names also work, and "all" '
    "pings every other participant. Nothing was saved."
)
REPLY_NOT_FOUND = 'reply_to "{value}" is not a post ID in your group.'
POST_SUGGESTION = 'Did you mean post {post_id} by {author} in {discussion}: "{excerpt}"?'
REPLY_RETRY = 'Repeat the call with reply_to "{post_id}". Nothing was saved.'
REPLY_CHOOSE = (
    'Find the post ID with {"action": "read"}, or omit reply_to for a new message. '
    "Nothing was saved."
)
MESSAGE_NOT_FOUND = (
    '{field} "{value}" is not a post ID in your group. Find post IDs by reading a discussion, '
    'for example with {{"action": "read"}} for the main discussion.'
)
DISCUSSION_NOT_FOUND = 'discussion_id "{value}" is not a discussion in your group.'
DISCUSSION_CHOICES = "Discussions: {discussions}."
DISCUSSION_RETRY = 'Repeat the call with discussion_id "{discussion_id}".'
DISCUSSION_CHOOSE = "Repeat the call with one of these discussion IDs."
REPLY_DISCUSSION_CONFLICT = (
    "reply_to {post_id} belongs to {reply_discussion}, but discussion_id names {discussion}. "
    "Omit discussion_id to reply in {reply_discussion}, or omit reply_to to post a new message "
    "in {discussion}. Nothing was saved."
)
BEFORE_DISCUSSION_CONFLICT = (
    "before {post_id} belongs to {before_discussion}, but discussion_id names {discussion}. "
    "Omit discussion_id to read {before_discussion}, or omit before to read the newest posts "
    "of {discussion}."
)
CREATE_IN_DISCUSSION = (
    "create opens a new discussion, but discussion_id names the existing discussion "
    "{discussion_id}. To open a new discussion, repeat the call without discussion_id. To add a "
    'message to that discussion, use action post with discussion_id "{discussion_id}" and text. '
    "Nothing was saved."
)
POST_WITH_MESSAGE_ID = (
    "message_id selects a post to read. To answer post {post_id}, repeat the call with reply_to "
    '"{post_id}" instead of message_id; to post a new message, omit message_id. Nothing was saved.'
)
POST_WITH_TWO_TARGETS = (
    "message_id is only for read, and it differs from reply_to. Repeat the call with only "
    "reply_to, set to the post you answer. Nothing was saved."
)
NOTHING_CHANGED = "Nothing was changed."
FIELD_FOR_OTHER_ACTION = (
    "{action} does not use {field}, so the call may mean another action. Repeat it without "
    "{field}, or use an action that takes {field}: {actions}. The call was not run."
)
LIST_CURSOR_INVALID = (
    "This cursor is not valid for this list: it is incomplete or belongs to another query. "
    "Omit cursor to list from the start."
)
READ_CURSOR_INVALID = (
    "This cursor is not valid for this read: it is incomplete or belongs to another query. "
    "Omit cursor to read the newest posts, or use before with the oldest post ID you have "
    "to read the posts before it."
)
MESSAGE_WITH_PAGE = (
    "Use message_id to read one post, or before to read the posts before a post, not both."
)
