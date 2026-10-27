"""Sub-Agent policy defaults, event names and Agent-facing wording."""

DEFAULT_MAX_SUBAGENT_DEPTH = 2
DEFAULT_MAX_ACTIVE_SUBAGENTS = 8
DEFAULT_MAX_ACTIVE_SUBAGENTS_TOTAL = 50
SUBAGENT_SESSION_STARTED_EVENT = "subagent_session_started"
SUBAGENT_STATUS_CHANGED_EVENT = "subagent_status_changed"
USER_CANCEL_REASON = "user"
PARENT_AGENT_CANCEL_REASON = "parent_agent"
SUBAGENT_SESSION_TITLE_MAX_CHARACTERS = 48
# Conversational Runs can switch recipient while running, including user Runs
# whose message_parent call restores Parent-directed final answers.
FORWARDED_RUN_KINDS = frozenset({"subagent", "system", "user", "channel"})

# Results of the ``subagent`` Tool.
SUBAGENT_STARTED_NOTE = (
    "Started in the background. Its final answers arrive as new messages. Continue with other "
    "work your task still needs, not the work you gave a Sub-Agent. When no such work is left, "
    "end your turn: reply with a short status and no tool call. Your next turn starts when its "
    "answer arrives."
)
# A note in the Sub-Agent's Session; {secrets} is one line per Secret (core/tools/secrets.py).
SUBAGENT_SECRETS_NOTE_TEMPLATE = (
    "Your Parent Agent passed you these Secrets for this session:\n{secrets}\n"
    "Write a reference where the value belongs; vBot inserts the value there."
)
SUBAGENT_SEND_STEERED_NOTE = (
    "Sent to the running Sub-Agent for its next step. Its final answers return to you."
)
SUBAGENT_SEND_STARTED_NOTE = (
    "Started a new turn with your message. Its final answers return to you."
)
SUBAGENT_SEND_QUEUED_NOTE = (
    "Queued your message for the Sub-Agent's next turn. Its final answers then return to you."
)
SUBAGENT_ACTIVITY_NOTE_TEMPLATE = (
    "Activity log of this Sub-Agent: {path}. Read it only if its progress matters."
)

# Refusals of the ``subagent`` Tool. The parts naming ``background_tasks`` are
# left out while the Agent cannot call it; complete stop calls use compact JSON.
SUBAGENT_MISSING_TASK_MESSAGE = (
    'Nothing was started. run needs "content", a self-contained task: goal, relevant '
    'context, scope, constraints and expected result. Add it and a title in "description", '
    "then repeat the call."
)
SUBAGENT_MISSING_TASK_LIST_TEXT = (
    ' To see your Sub-Agents instead, call background_tasks {"action": "list"}.'
)
SUBAGENT_MISSING_DESCRIPTION_MESSAGE = (
    'Nothing was started. Add "description", a 3-5 word title, and repeat the call with '
    "all other fields unchanged."
)
SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE = (
    "Nothing was started. Sub-Agents cannot be nested more than {limit} levels deep. "
    "Do this task yourself."
)
# Active-limit refusals. ``count`` is the limit with its noun, such as "8 Sub-Agents";
# ``{retry}`` is replaced per action; ``{or_stop}`` is ``SUBAGENT_LIMIT_STOP_TEXT`` or empty.
_SESSION_LIMIT_TEXT = "You already have {count} working, the most you can have at the same time."
_SESSION_LIMIT_CONTINUATION = (
    "A Sub-Agent stops counting once it has answered or was stopped: wait for an answer"
    "{or_stop}, then {retry}, or do this task yourself."
)
SUBAGENT_LIMIT_STOP_TEXT = " or stop one with background_tasks action stop"
_APP_LIMIT_TEXT = "{count} are working across vBot, the most it runs at the same time."
_APP_LIMIT_CONTINUATION = (
    "Do this task yourself, or wait until one of your own working Sub-Agents answers, then {retry}."
)
_NOTHING_STARTED = "Nothing was started."
_IDLE_NOT_SENT = "Nothing was sent. Sub-Agent {id} is idle, so your message would start it again."
SUBAGENT_ACTIVE_LIMIT_MESSAGE_TEMPLATE = (
    f"{_NOTHING_STARTED} {_SESSION_LIMIT_TEXT} "
    + _SESSION_LIMIT_CONTINUATION.replace("{retry}", "delegate again")
)
SUBAGENT_SEND_LIMIT_MESSAGE_TEMPLATE = (
    f"{_IDLE_NOT_SENT} {_SESSION_LIMIT_TEXT} "
    + _SESSION_LIMIT_CONTINUATION.replace("{retry}", "send again")
)
SUBAGENT_APP_LIMIT_MESSAGE_TEMPLATE = (
    f"{_NOTHING_STARTED} {_APP_LIMIT_TEXT} "
    + _APP_LIMIT_CONTINUATION.replace("{retry}", "delegate again")
)
SUBAGENT_SEND_APP_LIMIT_MESSAGE_TEMPLATE = (
    f"{_IDLE_NOT_SENT} {_APP_LIMIT_TEXT} "
    + _APP_LIMIT_CONTINUATION.replace("{retry}", "send again")
)
# ``target`` is the address the Tool accepts; ``reason`` is the resolver's explanation.
SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE = (
    "Nothing was started. Agent {target} cannot run: {reason}. Repeating this call fails "
    "the same way until that is fixed. Delegate to another Agent you are allowed to use "
    "instead, or tell the user that {target} cannot run and why."
)
SUBAGENT_TARGET_CHOICES_TEMPLATE = (
    "Omit agent_id to use your own Agent, or use one of these Agent ids exactly: {targets}."
)
SUBAGENT_NO_TARGET_CHOICES_TEXT = (
    "Omit agent_id to use your own Agent; no additional Agents are available to you."
)
SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE = (
    "Nothing was started. Agent {target} is not available to you as a Sub-Agent. {choices}"
)
SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE = (
    "agent_id {name} is not an Agent id, so your own Agent runs this task, as when "
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
SUBAGENT_NOT_FOUND_MESSAGE_TEMPLATE = (
    "Nothing was sent. No Sub-Agent with id {id} belongs to you. {yours}"
    "Use the id of the intended Sub-Agent from its run result."
)
SUBAGENT_YOURS_TEMPLATE = "Your Sub-Agents: {entries}. "
SUBAGENT_NONE_YOURS_TEXT = "You have no Sub-Agents. "
# ``{or_stop}`` is ``SUBAGENT_NOT_DIRECT_CHILD_STOP_TEMPLATE`` filled in, or empty.
SUBAGENT_NOT_DIRECT_CHILD_MESSAGE_TEMPLATE = (
    "Nothing was sent. Sub-Agent {id} was started by your Sub-Agent {parent_id}, so only "
    "{parent_id} can send it messages. Send your message to {parent_id}{or_stop}."
)
SUBAGENT_NOT_DIRECT_CHILD_STOP_TEMPLATE = (
    ', or stop {id} with background_tasks {{"action": "stop", "id": "{id}"}}'
)
SUBAGENT_SEND_WITHOUT_ID_MESSAGE_TEMPLATE = (
    'Nothing was sent. send needs "id", the Sub-Agent to message. {yours}{next_step}'
)
SUBAGENT_CHOOSE_RECIPIENT_TEXT = (
    "Choose the intended Sub-Agent from this list, then repeat the call with that id and "
    "the same content."
)
SUBAGENT_START_FIRST_TEXT = (
    'Start one with action "run", a title in "description" and a self-contained task in "content".'
)
SUBAGENT_SEND_WITHOUT_CONTENT_MESSAGE_TEMPLATE = (
    'Nothing was sent. send needs "content", the message for Sub-Agent {id}. Add your '
    "message and repeat the call with the same id."
)
# ``{stop}`` is ``SUBAGENT_ID_WITHOUT_ACTION_STOP_TEMPLATE`` filled in, or empty.
SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE = (
    "Nothing was sent. The call supplied Sub-Agent id {id} but no action or message. To "
    'message it, repeat the call with action "send" and your message in "content"{stop}.'
)
SUBAGENT_ID_WITHOUT_ACTION_STOP_TEMPLATE = "; to stop it, call background_tasks {stop_call}"
SUBAGENT_SESSION_MODEL_UNUSABLE_MESSAGE_TEMPLATE = (
    "subagent was not run: Sub-Agent {id} is set to a Model that cannot run: {reason}. "
    'Repeat this call with "model" set to a Model that can run.'
)
SUBAGENT_SESSION_SETTINGS_UNREADABLE_MESSAGE_TEMPLATE = (
    "subagent was not run: Sub-Agent {id} has Agent settings that cannot be read ({reason}), "
    "so it cannot continue. Delegate the work to a new Sub-Agent instead."
)
SUBAGENT_SESSION_PROJECT_MISSING_MESSAGE_TEMPLATE = (
    "subagent was not run: Sub-Agent {id} works in Project {project_id}, which no longer "
    "exists, so it cannot continue. Delegate the work to a new Sub-Agent instead."
)

# Sections vBot delivers to the Parent Agent.
FORWARDED_SECTION_TEMPLATE = (
    "### Sub-Agent {id} — {title}\nrun_id {run_id}. Its turn {outcome}.\n\n{answer}\n\n{facts}"
)
FORWARDED_ANSWER_TEMPLATE = "#### {kind} {message_id}\n\n{content}"
FORWARDED_NO_ANSWER_TEXT = "(The turn ended without an answer.)"
FORWARDED_FAILURE_TEXT_TEMPLATE = "(The turn failed: {error})"
FACTS_RUNNING_TEMPLATE = "Still running for this Sub-Agent: {entries}."
FACTS_NOTHING_RUNNING_TEXT = "Nothing is still running for this Sub-Agent."
# Only when the Parent has other Sub-Agents, so it knows whether more answers follow.
FACTS_SIBLINGS_PENDING_TEMPLATE = "Answers still to come from your other Sub-Agents: {entries}."
FACTS_NO_SIBLINGS_PENDING_TEXT = "No answers from your other Sub-Agents are still to come."
REPLY_TO_USER_NOTICE_TEMPLATE = (
    "### Sub-Agent {id} — {title}\n"
    "The user wrote in this Sub-Agent's Session; its final answers now go to the user. "
    "You can still message it, check its work and stop it. Its answers return to you when "
    "you message it or it messages you."
)
PARENT_MESSAGE_SECTION_TEMPLATE = (
    "### Message from Sub-Agent {id} — {title}\n"
    "Delivery {delivery_id}.\n\n"
    "{content}\n\n"
    "The Sub-Agent continues working; its answer arrives separately. Tell the user about this "
    "message only when it changes what they need to know or decide."
)

# The ``message_parent`` Tool.
MESSAGE_PARENT_QUEUED_NOTE = (
    "Queued for delivery. Your Parent Agent reads it at its next step, or in a new turn if it is "
    "idle. Your final answers now go to your Parent Agent until the user writes again. "
    "Continue your work."
)
MESSAGE_PARENT_DELIVERY_UNAVAILABLE_MESSAGE = (
    "message_parent was not queued: delivery to the Parent Session is unavailable. "
    "Keep the message in your final answer."
)
MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE = (
    "message_parent was not sent: this Session has no Parent Agent. Reply to the user instead."
)
MESSAGE_PARENT_PARENT_GONE_MESSAGE = (
    "message_parent was not sent: your Parent Agent's Session no longer exists. Finish the "
    "task and end your turn with the result."
)
