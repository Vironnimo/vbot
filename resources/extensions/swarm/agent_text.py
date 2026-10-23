"""Reviewed Swarm Agent contract; shared by registration and interface probes."""

from typing import Any

# Other participants receive a longer main-discussion post as its opening only.
FULL_POST_CHARS = 1000
OPENING_CHARS = 400

BOARD_DESCRIPTION = (
    "Read and contribute to your group's shared Board. Use the main discussion for shared "
    "conversation and coordination. To address a participant, write @ before their name, as in "
    "@Name; a name without @ addresses no one. A post reaches the participants it addresses in "
    "full and can start a Run for any who are idle, so write @ only before those you ask to act "
    "or answer. Other participants receive a main-discussion post longer than "
    f"{FULL_POST_CHARS} characters as its opening lines with the call to read the rest, so "
    "begin with what they most need to know. Create an "
    "additional discussion when several Agents need to work through a specific problem "
    "together; creating it joins it and announces it in the main discussion. Its posts reach "
    "only participants who joined it or are addressed, so post what concerns everyone in the "
    "main discussion. Every participant "
    "can read every discussion; joining one makes its future posts reach you in full and "
    "returns its recent posts. Messages for you reach you as the group's delivery settings "
    "allow, so you need not read the Board to receive them; posts you read here are not "
    "delivered again."
)

INBOX_DESCRIPTION = (
    "Receive your pending Board messages now, oldest first; returned messages count as "
    "received. New messages also reach you as the group's delivery settings allow, so you need "
    "not check right after posting. This Tool never waits for new messages: continue with "
    "other work your task still needs. When no such work is left, end your turn: reply with a "
    "short status and no tool call. New messages can start your next turn."
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

STATE_LIMIT = 100

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
            "description": (
                f"Maximum participants to list, at most 100. Omit to list up to {STATE_LIMIT}."
            ),
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
            "description": 'Discussion to read, post in, join, or leave, such as "d2"; required '
            "for join and leave. When omitted, read and post use the main discussion.",
        },
        "message_id": {
            "type": "string",
            "description": 'Post ID for read, such as "#42": show only that post. Omit to read '
            "the newest posts of a discussion, oldest first.",
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
POST_DUPLICATE = (
    "Another call in the same response already posted this text as {post_id}; nothing new was "
    "saved."
)

DELIVERY_PREFIX = "New Board messages for you, oldest first."
RESUME_REMINDER = (
    "The user resumed your work. Continue toward the group's goal from where you left off."
)
INITIAL_MESSAGE = (
    "Read the user's request on the Board with swarm_board "
    '{{"action": "read", "message_id": "{goal_post}"}}, then discuss it there with the '
    "other Agents before starting implementation.\n\n"
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
    "as your common reference as your work develops.\n\n"
    "Post when you add new information, answer a question, report a result, or request "
    "action. Do not acknowledge acknowledgments or post that you have nothing new to add. "
    "When your task needs no further work now, end your turn; no closing Board post is needed."
)
DEFAULT_PROMPT_BLOCKS = ["core:tools", "core:system_reminders", "core:skills"]
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
    "request_conflict": "This Tool Call was already applied with different arguments, so "
    "nothing changed. Make the change in a new Tool Call.",
    "discussion_not_found": "This discussion does not exist in your group. Call swarm_board "
    'with {"action": "list"} to see the current discussions.',
    "message_not_found": "This post is unavailable in your group. Read its discussion "
    "to find an available post.",
    "invalid_recipient": "A recipient is not a participant in your group. swarm_state lists "
    "the participants' names.",
    "reply_discussion_mismatch": "The reply target belongs to another discussion. Omit "
    "discussion_id to reply in the target's discussion, "
    "or omit reply_to for a new post.",
    "exact_message_arguments": "To read one message_id, omit discussion_id, cursor, "
    "and limit. No change was applied.",
    "main_membership_required": "Everyone remains in the main discussion. Continue with "
    "other work your task still needs. When no such work is left, end your turn: reply with a "
    "short status and no tool call.",
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
    "swarm_inbox": 'swarm_board has no {action} action. Use {{"action": "read"}} to read the '
    "newest posts of the main discussion. If swarm_inbox is among your Tools, it receives all "
    "your pending Board messages.",
    "swarm_wiki": "swarm_board has no {action} action. Use swarm_wiki to create, find, and edit "
    "shared pages.",
}
# An unknown action that may name one of the Agent's other Tools.
BOARD_OTHER_TOOL = (
    "swarm_board has no {action} action; its actions are list, read, post, create, join and "
    "leave. If {action} is one of your Tools, call it directly as its own Tool, not through "
    "swarm_board. The call was not run."
)
BOARD_ACTION_FIELDS_MERGED = (
    "Nothing was run. action contains merged Tool-call fields instead of one action. "
    "Send action as {action} and {field} as a separate parameter; keep its value and the "
    "other parameters unchanged."
)
REPLY_IS_DISCUSSION = (
    "Nothing was saved. {value} names a discussion, not a post. To post there, use "
    "discussion_id: {discussion} and omit message_id and reply_to. To answer a post, use "
    "its post ID instead."
)
BOARD_FILE_PATH = (
    "swarm_board was not run: it reads and writes Board posts, not files, and has no path "
    "parameter. For the file {path}, use one of your Tools that work with files."
)
INBOX_ONLY_RECEIVE = (
    "swarm_inbox has no {action} action; it only receives your pending Board messages. Call it "
    "without arguments, or with limit."
)
STATE_ONLY_STATUS = (
    "swarm_state has no {action} action; it only shows the participants, their Run activity, "
    "your pending messages, and how Board messages reach you. Share progress, results, or "
    "requests for help on the Board with swarm_board, and end your turn when your task needs "
    "no further work now."
)

# Delivered messages: Inbox results and automatic delivery.
MESSAGES_IN = "In {discussion}:"
INBOX_MORE = "{count} more pending; call swarm_inbox again{arguments} to continue."
DELIVERY_MORE = {
    # Every route delivers while running: the rest follows without any call.
    "automatic_one": "1 more pending; it reaches you automatically, so you need not read it on "
    "the Board.",
    "automatic_many": "{count} more pending; they reach you automatically, so you need not read "
    "them on the Board.",
    "inbox": "{count} more pending; receive them with swarm_inbox.",
    "no_inbox": "{count} more pending.",
}
# A backlog larger than one batch: older posts nobody needs the reader for are skipped.
DELIVERY_SKIPPED = {
    "one": "1 older post in {discussion} was skipped so you receive the newest posts; it is "
    "not from the user, does not address or answer you, and will not be delivered later. To "
    "read it, call swarm_board with {call}",
    "many": "{count} older posts in {discussion} were skipped so you receive the newest posts; "
    "none is from the user or addresses or answers you, and they will not be delivered later. "
    "To read them, call swarm_board with {call}",
    "discussions": "{count} older posts were skipped so you receive the newest posts; none is "
    "from the user or addresses or answers you, and they will not be delivered later. To read "
    "them, call swarm_board with {reads}",
}
DELIVERY_SKIPPED_READ = "{call} for {discussion}"

# How to wait for others, wherever a result shows that nothing is left to receive.
WAIT_FOR_OTHERS = "To wait for others, end your turn: reply with a short status and no tool call"

# swarm_state results.
STATE_PENDING = {
    "none": "No pending messages.",
    "one": "1 message for you; receive it with swarm_inbox.",
    "many": "{count} messages for you; receive them with swarm_inbox.",
    "one_no_inbox": "1 message for you.",
    "many_no_inbox": "{count} messages for you.",
    # Every route delivers while running; this replaces the delivery field.
    "none_automatic": "No pending messages; new ones reach you automatically, also while you "
    "are running.",
    "one_automatic": "1 message for you; it reaches you automatically, also while you are "
    "running, so you need not read it on the Board.",
    "many_automatic": "{count} messages for you; they reach you automatically, also while you "
    "are running, so you need not read them on the Board.",
}
# Follows a "none" pending line when a post can start the reader's next Run; the
# wake field then says which posts do.
STATE_WAIT = f"{WAIT_FOR_OTHERS}."
STATE_ROUTES = {
    "main": "main-discussion posts",
    "discussion": "posts in discussions you joined",
    "ping": "posts that address or answer you",
}
STATE_DELIVERY = {
    "automatic": "{routes} reach you automatically, also while you are running.",
    "when_idle": "{routes} reach you automatically when you are idle.",
    "on_request": "{routes} reach you only through swarm_inbox.",
    "on_request_no_inbox": "{routes} reach you only when you read them with swarm_board.",
}
STATE_WAKE = "{routes} start a Run when you are idle."
STATE_NO_WAKE = "New messages do not start a Run when you are idle."
# Wake pacing holds other posts for up to the longest Quiet period.
STATE_WAKE_PACED = "When you are idle, {routes} start a Run within {minutes} minutes."
STATE_WAKE_ADDRESSED = (
    "When you are idle, posts that address or answer you start a Run at once; {routes} start "
    "one within {minutes} minutes."
)
STATE_PARTICIPANTS = "{count} ({totals})"
STATE_ROSTER_HEADER = "Participants:"
STATE_ROSTER_LINE = "- {name}{you}: {state}"
STATE_ROSTER_YOU = " (you)"
STATE_MORE = "More participants exist. Continue with {call}"

# Notes on a call that ran after a repair the Agent should know about.
LIMIT_CLAMPED = "limit {requested} is above the maximum of {maximum}; used {maximum}."
CURSOR_AS_BEFORE = (
    "cursor {value} is a post ID, so this shows the posts before it. For the newest posts, "
    "omit cursor."
)
BEFORE_OTHER_DISCUSSION = (
    "before {post_id} is a post of {before_discussion}; this shows the posts of {discussion} "
    "before it."
)
FIELD_IGNORED = "{field} is not used by {action} and was ignored."
OFFSET_IGNORED = "offset was ignored: {field} already sets where the page starts."
POST_TITLE_IGNORED = (
    "title was ignored: posts have no title; only create uses one, to name a new discussion."
)
LIST_CURSOR_RESTARTED = "cursor {value} does not continue a list, so this lists from the start."
CREATE_IGNORES_ID = (
    'discussion_id "{value}" names no discussion and was ignored; the new discussion has its '
    "own ID."
)
MESSAGE_DISCUSSION_IGNORED = (
    "discussion_id was ignored: post {post_id} belongs to the discussion shown with it."
)
USER_RECIPIENT = (
    "The user is not a participant and sees every Board post, so the user needs no recipient entry."
)
POST_CLOSE_MATCH = (
    '{field} "{value}" does not exist; this uses post {post_id}, its only close match.'
)
BEFORE_PAST_NEWEST = (
    'before "{value}" names no post yet; the newest post is {newest}, so this shows the '
    "newest posts."
)
MESSAGE_PAST_NEWEST = '{field} "{value}" names no post yet; the newest post is {newest}.'
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
# offset (unadvertised) skips the newest posts; callers often send it hoping for them.
BOARD_PAGE_OFFSET = (
    "Older posts of {discussion}, oldest first ({count} shown). offset {offset} skipped the "
    "newest posts, those after {post_id}. Read the newest posts with {call}"
)
BOARD_PAGE_EMPTY_OFFSET = (
    "No posts shown: offset {offset} skips that many of the newest posts, and {discussion} has "
    "no older ones. Read the newest posts with {call}"
)
# Posts pending for the reader are new to it; delivered and own posts are not.
BOARD_NEW = {
    "one": "{posts} is new to you.",
    "many": "{posts} are new to you.",
    "all": "All {count} posts shown are new to you.",
}
BOARD_NOTHING_NEW = "None of the posts shown is new to you."
BOARD_WAIT = {
    "addressed": f"{WAIT_FOR_OTHERS}; a post that addresses or answers you starts your next turn "
    "at once.",
    "paced": f"{WAIT_FOR_OTHERS}; {{routes}} start your next turn within {{minutes}} minutes.",
}
BOARD_OLDER = "Older posts exist. Continue with {call}"
# The page's character bound (the batch_chars setting) ended it before limit.
BOARD_OLDER_CUT = (
    "Only {count} of {limit} posts fit the page's size limit; older posts exist. Continue "
    "with {call}"
)
BOARD_MORE_DISCUSSIONS = "More discussions exist. Continue with {call}"
BOARD_USER_REQUEST = "Post {post_id} holds the user's request. Read it with {call}"
BOARD_QUEUED = "Queued for {participants}."
BOARD_PARTICIPANTS = {"one": "1 participant", "many": "{count} participants"}
BOARD_QUEUED_NONE = "No other participant receives it; it stays readable on the Board."
BOARD_ADDRESSED = "It reaches {names} in full because it addresses or answers them."
BOARD_ADDRESSED_STOPPED = {
    "one": "{names} has stopped and sees it only if the user resumes their work.",
    "many": "{names} have stopped and see it only if the user resumes their work.",
}
# Outside the main discussion, a post reaches only the discussion's members and those it
# addresses; names without @ reach no one.
BOARD_OUTSIDE = {
    "one": "1 other participant has not joined this discussion and does not receive it; a name "
    "without @ addresses no one.",
    "many": "{count} other participants have not joined this discussion and do not receive it; "
    "a name without @ addresses no one.",
    "one_named": "{names}, the only other participant, has not joined this discussion and does "
    "not receive it; a name without @ addresses no one.",
    "many_named": "{count} other participants have not joined this discussion and do not "
    "receive it, {names} among them; a name without @ addresses no one.",
}
# "@all", its synonyms and "@" before a word that names no participant address no one;
# the note says what readers receive instead, then how to address.
MENTION_NO_ONE = {
    "opening_one": "{mentions} addressed no one, so 1 participant receives only its opening lines.",
    "opening_many": "{mentions} addressed no one, so {count} participants receive only its "
    "opening lines.",
    "main": "{mentions} addressed no one; every participant receives this main-discussion post "
    "anyway.",
    "other": "{mentions} addressed no one.",
}
MENTION_HOW = {
    "opening": "To reach a participant in full, write @ before their name, as in @Name.",
    "address": "To address a participant, write @ before their name, as in @Name.",
}
# Instead of the general rule, for an "@" word that most likely meant a participant.
MENTION_CORRECTED = {
    "possessive": "@ addresses a participant only when no letter follows the name, as in "
    "@{name}'s.",
    "close": "For {written}, did you mean @{name}?",
}
MENTION_USER = (
    "{mentions} addressed no one: the user is not a participant and sees every Board post."
)
BOARD_OPENING = {
    "one": "1 participant receives only its opening lines and the call to read the rest.",
    "many": "{count} participants receive only its opening lines and the call to read the rest.",
}
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
# Beside a list that shows pending posts, when every route delivers while running.
BOARD_PENDING_AUTOMATIC = (
    "Posts pending for you reach you automatically, also while you are running, so you need "
    "not read them on the Board."
)
POST_HEADER_REPLY = "reply to {post_id}"
POST_HEADER_TO = "to {names}"
POST_SHORTENED = "[{count} more characters not shown. Read the whole post with swarm_board {call}]"
# A read page shows a post the reader has seen as only this line; a long main-discussion
# post may have reached it as its opening lines only.
POST_SEEN = "[{count} characters you have seen before. Read the post with swarm_board {call}]"
POST_SEEN_OPENING = (
    "[{count} characters; you received at least its opening lines before. Read the whole post "
    "with swarm_board {call}]"
)
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
POST_NEEDS_TEXT_OR_READ = (
    "post needs text, the message body. To read {post}, use {call}; to answer it, repeat the "
    "call with text. Nothing was saved."
)
RECIPIENT_UNKNOWN = "recipients: {values} {verb} not a participant in your group."
RECIPIENT_SUGGESTIONS = "Did you mean {suggestions}?"
RECIPIENT_ROSTER = "Participants: {roster}."
RECIPIENT_RETRY = (
    'Repeat the call with recipients {corrected}; "all" addresses every other participant. '
    "Nothing was saved."
)
RECIPIENT_CHOOSE = (
    'Repeat the call with recipients chosen from these participants; "all" addresses every other '
    "participant. Nothing was saved."
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
# Tool-call markup a Model leaked into a post or discussion reference.
REFERENCE_MARKUP = (
    '{field} arrived as tool-call markup ({value}) instead of a {kind} ID such as "{example}". '
    "Repeat the call with the {kind} ID alone. The call was not run."
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
CREATE_IN_DISCUSSION = (
    "create opens a new discussion, but discussion_id names the existing discussion "
    "{discussion_id}. To open a new discussion, repeat the call without discussion_id. To add a "
    'message to that discussion, use action post with discussion_id "{discussion_id}" and text. '
    "Nothing was saved."
)
POST_WITH_TWO_TARGETS = (
    "message_id and reply_to name different posts. Repeat the call with only reply_to, set to "
    "the post you answer. Nothing was saved."
)
NOTHING_CHANGED = "Nothing was changed."
FIELD_FOR_OTHER_ACTION = (
    "{action} does not use {field}, so the call may mean another action. Repeat it without "
    "{field}, or use an action that takes {field}: {actions}. The call was not run."
)
OFFSET_INVALID = (
    "offset {value} is not a post count. Send the number of newest posts to skip, 0 or more, "
    "or omit offset to read the newest posts. The call was not run."
)
READ_CURSOR_INVALID = (
    "This cursor is not valid for this read: it is incomplete or belongs to another query. "
    "Omit cursor to read the newest posts, or use before with the oldest post ID you have "
    "to read the posts before it."
)
MESSAGE_WITH_PAGE = (
    "Use message_id to read one post, or before to read the posts before a post, not both."
)
