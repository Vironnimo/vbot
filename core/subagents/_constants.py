"""Sub-Agent policy defaults, event names and Agent-facing wording."""

DEFAULT_MAX_SUBAGENT_DEPTH = 4
DEFAULT_MAX_ACTIVE_SUBAGENTS = 8
SUBAGENT_SESSION_STARTED_EVENT = "subagent_session_started"
SUBAGENT_STATUS_CHANGED_EVENT = "subagent_status_changed"
USER_CANCEL_REASON = "user"
PARENT_AGENT_CANCEL_REASON = "parent_agent"
SUBAGENT_SESSION_TITLE_MAX_CHARACTERS = 48
# Run kinds whose final answer reaches the Parent: Runs the Parent started and
# Runs started by deliveries (background commands, terminals, nested Sub-Agents).
FORWARDED_RUN_KINDS = frozenset({"subagent", "system"})

# Results of the ``subagent`` Tool.
SUBAGENT_STARTED_NOTE = (
    "The Sub-Agent works in the background. vBot sends you each of its answers "
    "automatically: at your next step while you are working, otherwise in a new turn. "
    "Continue other work, or end your turn to wait. To give it more instructions, call "
    'subagent with action "send" and this id.'
)
SUBAGENT_SEND_STEERED_NOTE = (
    "The Sub-Agent is working; vBot gives it your message at its next step. Its answer "
    "reaches you automatically."
)
SUBAGENT_SEND_STARTED_NOTE = (
    "The Sub-Agent was idle and started a new turn with your message. Its answer reaches you "
    "automatically."
)
SUBAGENT_SEND_QUEUED_NOTE = (
    "The Sub-Agent is busy; your message starts its next turn when the current one ends. Its "
    "answer reaches you automatically."
)
SUBAGENT_LIST_NOTE = (
    "vBot sends you each answer of a working Sub-Agent automatically; calling list again does "
    "not make it finish faster."
)
SUBAGENT_CANCELLED_NOTE = (
    "Stopped: its current turn, its queued messages, its background commands and terminals, "
    "and its own Sub-Agents. Its Session keeps the history; send it a message to continue."
)
SUBAGENT_ACTIVITY_NOTE_TEMPLATE = (
    "Activity log of this Sub-Agent: {path}. Read it only if its progress matters."
)

# Refusals of the ``subagent`` Tool. ``{call}`` placeholders receive compact JSON.
SUBAGENT_MISSING_TASK_MESSAGE = (
    'subagent was not run: "content" must carry the task for the Sub-Agent. Call '
    '{"description": "<3-5 word title>", "content": "<self-contained task: goal, relevant '
    'context, scope, constraints, expected result>"}. To see your Sub-Agents instead, call '
    '{"action": "list"}.'
)
SUBAGENT_MISSING_DESCRIPTION_MESSAGE_TEMPLATE = (
    'subagent was not run: a new Sub-Agent needs "description", a 3-5 word title that the '
    "user sees. Repeat the call with it: {call}."
)
SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE = (
    "Sub-Agents cannot be nested more than {limit} levels deep, so you cannot start "
    "another Sub-Agent; nothing was started. Do this task yourself."
)
# ``count`` is the limit with its noun, such as "8 Sub-Agents".
_ACTIVE_LIMIT_TEXT = (
    "At most {count} can work at the same time, and that many are working now; yours, theirs "
    "and those of the Agents that delegated to you count together."
)
_ACTIVE_LIMIT_CONTINUATION = (
    "A Sub-Agent stops counting once it has answered or was cancelled: wait for an answer or "
    "stop one with action cancel, then {retry}, or do this task yourself."
)
SUBAGENT_ACTIVE_LIMIT_MESSAGE_TEMPLATE = (
    f"{_ACTIVE_LIMIT_TEXT} Nothing was started. "
    + _ACTIVE_LIMIT_CONTINUATION.replace("{retry}", "delegate again")
)
SUBAGENT_SEND_LIMIT_MESSAGE_TEMPLATE = (
    f"{_ACTIVE_LIMIT_TEXT} Sub-Agent {{id}} is idle, so your message would start it again; "
    "nothing was sent. " + _ACTIVE_LIMIT_CONTINUATION.replace("{retry}", "send again")
)
# ``target`` is the address the Tool accepts; ``reason`` is the resolver's explanation.
SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE = (
    "Agent {target} cannot run: {reason}. Repeating this call fails the same way until "
    "that is fixed. Delegate to another Agent you are allowed to use instead, or tell "
    "the user that {target} cannot run and why."
)
SUBAGENT_TARGET_CHOICES_TEMPLATE = (
    "Omit agent_id to delegate to a copy of yourself, or use one of these Agent ids "
    "exactly: {targets}."
)
SUBAGENT_NO_TARGET_CHOICES_TEXT = (
    "Omit agent_id to delegate to a copy of yourself; no other Agents are available to you."
)
SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE = (
    "Agent {target} is not available to you as a Sub-Agent. {choices}"
)
SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE = (
    "agent_id {name} is not an Agent id, so a copy of you runs this task, as when "
    "agent_id is omitted."
)
SUBAGENT_BACKGROUND_IGNORED_NOTE = (
    "background false was ignored: every Sub-Agent works in the background."
)
SUBAGENT_IGNORED_LABEL_NOTE_TEMPLATE = (
    "id {label} was ignored: vBot assigns each new Sub-Agent its id, shown above."
)
# ``session_id`` is the JSON-quoted value the call sent.
SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE = (
    "No Session {session_id} exists and that value reads as a stand-in, so this task started "
    "a new Sub-Agent."
)
SUBAGENT_SESSION_NOT_SUBAGENT_MESSAGE_TEMPLATE = (
    "subagent was not run: Session {session_id} is not one of your Sub-Agents. {yours}To "
    'start a new Sub-Agent, repeat this call without "session_id".'
)
SUBAGENT_NOT_FOUND_MESSAGE_TEMPLATE = "No Sub-Agent with id {id} belongs to you. {yours}"
SUBAGENT_YOURS_TEMPLATE = "Your Sub-Agents: {entries}. "
SUBAGENT_NONE_YOURS_TEXT = "You have no Sub-Agents. "
SUBAGENT_NOT_DIRECT_CHILD_MESSAGE_TEMPLATE = (
    "Sub-Agent {id} was started by your Sub-Agent {parent_id}, so only {parent_id} can send "
    "it messages; nothing was sent. Send your message to {parent_id}, or stop {id} with "
    "action cancel."
)
SUBAGENT_TAKEN_OVER_MESSAGE_TEMPLATE = (
    "The user took over Sub-Agent {id} by writing in its Session; it works for the user and "
    "takes no messages from you, so nothing was sent. Delegate the work to a new Sub-Agent "
    "instead."
)
SUBAGENT_SEND_WITHOUT_ID_MESSAGE_TEMPLATE = (
    'send needs "id", the Sub-Agent to message; nothing was sent. {yours}Call {call}.'
)
SUBAGENT_SEND_WITHOUT_CONTENT_MESSAGE_TEMPLATE = (
    'send needs "content", the message for Sub-Agent {id}; nothing was sent. Call {call}.'
)
SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE = (
    "subagent was not run: it received Sub-Agent id {id} but no action. To message that "
    "Sub-Agent, call {send_call}; to stop it, call {cancel_call}."
)
SUBAGENT_CANCEL_WITHOUT_ID_MESSAGE_TEMPLATE = (
    'cancel needs "id", the Sub-Agent to stop; nothing was stopped. {yours}Call {call}.'
)
SUBAGENT_NOTHING_TO_CANCEL_MESSAGE_TEMPLATE = (
    "Sub-Agent {id} and its own Sub-Agents are idle and run no background commands or "
    "terminals, so nothing was stopped."
)
SUBAGENT_SESSION_MODEL_UNUSABLE_MESSAGE_TEMPLATE = (
    "subagent was not run: Sub-Agent {id} is set to a Model that cannot run: {reason}. "
    'Repeat this call with "model" set to a Model that can run.'
)
SUBAGENT_SESSION_SETTINGS_UNREADABLE_MESSAGE_TEMPLATE = (
    "subagent was not run: Sub-Agent {id} has Agent settings that cannot be read ({reason}), "
    "so it cannot continue. Delegate the work to a new Sub-Agent instead."
)

# Sections vBot delivers to the Parent Agent.
FORWARDED_SECTION_TEMPLATE = (
    "### Sub-Agent {id} — {title}\n"
    "agent_id {agent_id}, session_id {session_id}. Its turn {outcome}.\n\n"
    "{answer}\n\n"
    "{facts}"
)
FORWARDED_NO_ANSWER_TEXT = "(The turn ended without an answer.)"
FORWARDED_FAILURE_TEXT_TEMPLATE = "(The turn failed: {error})"
FACTS_RUNNING_TEMPLATE = "Still running for this Sub-Agent: {entries}."
FACTS_NOTHING_RUNNING_TEXT = "Nothing is still running for this Sub-Agent."
# Only when the Parent has other Sub-Agents, so it knows whether more answers follow.
FACTS_SIBLINGS_PENDING_TEMPLATE = "Answers still to come from your other Sub-Agents: {entries}."
FACTS_NO_SIBLINGS_PENDING_TEXT = "No answers from your other Sub-Agents are still to come."
TAKEN_OVER_NOTICE_TEMPLATE = (
    "### Sub-Agent {id} — {title}\n"
    "The user took over this Sub-Agent by writing in its Session. vBot no longer sends you its "
    'answers, and subagent action "send" to it is refused. Its earlier answers stay valid.'
)
PARENT_MESSAGE_SECTION_TEMPLATE = (
    "### Message from Sub-Agent {id} — {title}\n"
    "{content}\n\n"
    "The Sub-Agent continues working; its answer arrives separately."
)

# The ``message_parent`` Tool.
MESSAGE_PARENT_SENT_NOTE = (
    "Sent. Your Parent Agent reads it at its next step, or in a new turn if it is idle. "
    "Continue your work."
)
MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE = (
    "message_parent was not sent: this Session has no Parent Agent. Reply to the user instead."
)
MESSAGE_PARENT_TAKEN_OVER_MESSAGE = (
    "message_parent was not sent: the user took over this Session, so you work for the user "
    "and your Parent Agent no longer reads your messages. Reply to the user instead."
)
MESSAGE_PARENT_PARENT_GONE_MESSAGE = (
    "message_parent was not sent: your Parent Agent's Session no longer exists. Finish the "
    "task and end your turn with the result."
)
