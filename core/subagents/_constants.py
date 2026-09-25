"""Sub-Agent lifecycle policy defaults, event names and response wording."""

DEFAULT_MAX_SUBAGENT_DEPTH = 4
DEFAULT_MAX_SUBAGENTS_PER_TURN = 8
DEFAULT_SUBAGENT_TIMEOUT_MINUTES = 60
SECONDS_PER_MINUTE = 60
SESSION_RESULT_RETRY_ATTEMPTS = 3
SESSION_RESULT_RETRY_DELAY_SECONDS = 0.05
SUBAGENT_STATUS_QUEUED = "queued"
SUBAGENT_SESSION_STARTED_EVENT = "subagent_session_started"
SUBAGENT_STATUS_CHANGED_EVENT = "subagent_status_changed"
SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"
SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"
USER_CANCEL_REASON = "user"
PARENT_AGENT_CANCEL_REASON = "parent_agent"
SUBAGENT_USER_CANCEL_MESSAGE = "Cancelled by the user"
SUBAGENT_SESSION_TITLE_MAX_CHARACTERS = 48
SUBAGENT_ACTIVITY_NOTE_TEMPLATE = (
    "Activity log: {path}. Read it only if the Sub-Agent's progress matters."
)
TOP_LEVEL_BACKGROUND_NOTE = (
    "Running in the background. vBot delivers the result to you automatically when it "
    "finishes, so there is no need to check status. Continue other work, or end your "
    "turn to wait for it."
)
TOP_LEVEL_QUEUED_BACKGROUND_NOTE = (
    "This Sub-Agent is queued because its Session is busy with another task; vBot "
    "starts it automatically when the Session is free and notifies you with the "
    "result once it finishes. Continue other work, or finish your turn to wait for a "
    "result."
)
SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE = (
    "The queued Sub-Agent work was removed from its Session's Queue before it started, "
    "so it did not run. Call subagent again if the work is still needed."
)
SUBAGENT_QUEUED_TIMEOUT_MESSAGE_TEMPLATE = (
    "Sub-agent run timed out after {minutes} minutes while waiting in its busy Session's "
    "Queue; it was removed before it started and did not run."
)
SUBAGENT_START_FAILED_MESSAGE_TEMPLATE = "The queued Sub-Agent work could not start: {error}"
SUBAGENT_TIMEOUT_MESSAGE_TEMPLATE = (
    "The Sub-Agent did not finish within {minutes} minutes, so vBot cancelled it. Its "
    "Session keeps the work done so far; to resume it, {continuation}. Otherwise do the "
    "task yourself."
)
SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE = (
    "Sub-Agents cannot be nested more than {limit} levels deep, so you cannot start "
    "another Sub-Agent; nothing was started. Do this task yourself."
)
SUBAGENT_TURN_LIMIT_MESSAGE_TEMPLATE = (
    "You already started {limit} Sub-Agents in this turn, the limit; nothing was "
    "started. Delegate more after their results arrive, or do this task yourself."
)
# ``target`` is the address the Tool accepts; ``reason`` is the resolver's explanation.
SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE = (
    "Agent {target} cannot run: {reason}. Repeating this call fails the same way until "
    "that is fixed. Delegate to another Agent you are allowed to use instead, or tell "
    "the user that {target} cannot run and why."
)
# ``agent_id`` is the address the Tool accepts: ``agent@project`` for a Project Agent.
SUBAGENT_CONTINUATION_CALL_TEMPLATE = (
    "call subagent with agent_id `{agent_id}`, session_id `{session_id}` and a "
    "continuation message as content"
)
SUBAGENT_INTERRUPTED_WITHOUT_OUTPUT_NOTE_TEMPLATE = (
    "The Sub-Agent Run was interrupted before it produced Assistant output. To continue "
    "the same Session, {continuation}."
)
SUBAGENT_PARTIAL_RESULT_NOTE_TEMPLATE = (
    "Result is partial: the Sub-Agent Run was interrupted{cause}. To continue the same "
    "Session, {continuation}."
)
SUBAGENT_CANCELLED_NOTE_TEMPLATE = (
    "The Sub-Agent Run was cancelled; its Session keeps its history. To resume this work, "
    "{continuation} instead of starting a new Session."
)
SUBAGENT_STATUS_RUNNING_NOTE = (
    "Still running; the result is delivered automatically. Continue other work, or "
    "finish your turn to wait for it. Repeated status calls do not make it finish "
    "faster."
)
SUBAGENT_STATUS_QUEUED_NOTE = (
    "Queued: the Sub-Agent's Session is busy with another task; vBot starts this "
    "work automatically when the Session is free. The result is delivered "
    "automatically. Continue other work, or finish your turn to wait for it."
)
SUBAGENT_STATUS_LIST_NOTE = (
    "Unfinished work delivers its result to you automatically. Continue other work, or "
    "finish your turn to wait for it. Repeated status calls do not make it finish faster."
)

# Call interpretation. ``{call}`` placeholders receive compact JSON argument objects.
SUBAGENT_MISSING_TASK_MESSAGE = (
    'subagent was not run: "content" must carry the task for the Sub-Agent. Call '
    '{"content": "<self-contained task: goal, relevant context, scope, constraints, '
    'expected result>"}. To check delegated work instead, call {"action": "status"}.'
)
SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE = (
    "subagent was not run: it received work id {work_id} but no action. To inspect that "
    "work, call {status_call}; to stop it, call {cancel_call}. To delegate new work, send "
    'the task as "content" without "id".'
)
SUBAGENT_RUN_WITH_WORK_ID_MESSAGE_TEMPLATE = (
    'subagent was not run: "id" names Sub-Agent work {work_id}, so it is unclear whether '
    "to continue that Sub-Agent or to start new work. {continuation} To start new work, "
    'repeat this call without "id".'
)
SUBAGENT_CONTINUE_TRACKED_WORK_TEMPLATE = "To continue its Session, call {call}."
SUBAGENT_CONTINUE_UNTRACKED_WORK_TEXT = (
    "To continue that Sub-Agent's Session, pass the agent_id and session_id from its "
    'result instead of "id".'
)
SUBAGENT_WORK_SESSION_CONFLICT_MESSAGE_TEMPLATE = (
    "subagent was not run: work {work_id} belongs to Session {work_session_id}, but "
    "session_id is {session_id}. To continue work {work_id}, call {call}; to continue "
    'Session {session_id}, repeat this call without "id".'
)
SUBAGENT_IGNORED_WORK_LABEL_NOTE_TEMPLATE = (
    "id {label} was ignored: run assigns the work id above; use that id with status or cancel."
)
SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE = (
    "agent_id {name} is not an Agent id, so a copy of you runs this task, as when "
    "agent_id is omitted."
)
# ``session_id`` is the JSON-quoted value the call sent.
SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE = (
    "No Session {session_id} exists and that value reads as a stand-in, so this task started "
    "a new Session. Continue it with the session_id of this result."
)
SUBAGENT_BACKGROUND_UNAVAILABLE_NOTE = (
    "This call cannot wait for the result: your Sub-Agents always run in the background."
)
SUBAGENT_FOREGROUND_ONLY_NOTE = (
    "This call waited for the result: Sub-Agents started by a Sub-Agent always run in "
    "the foreground."
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
SUBAGENT_SESSION_NOT_FOUND_MESSAGE_TEMPLATE = (
    "No Session {session_id} exists for Agent {target}; nothing was started. {tracked}"
    'To start a new Session, repeat this call without "session_id".'
)
SUBAGENT_SESSION_OWNER_HINT = (
    "If that Session belongs to another Agent, repeat the call with that Agent's agent_id. "
)
SUBAGENT_TRACKED_WORK_TEMPLATE = "Tracked work: {entries}. "
SUBAGENT_NOT_FOUND_TRACKED_MESSAGE_TEMPLATE = (
    "No Sub-Agent work with id {work_id} is tracked for this Session; work stops being "
    "tracked once its result was delivered to you, or when vBot restarts. {tracked}"
)
SUBAGENT_NOT_FOUND_STATUS_MESSAGE_TEMPLATE = (
    "No Sub-Agent work is tracked for this Session, so id {work_id} was not found. Work "
    "stops being tracked once its result was delivered to you, or when vBot restarts; use "
    "the delivered result."
)
SUBAGENT_NOT_FOUND_CANCEL_MESSAGE_TEMPLATE = (
    "No Sub-Agent work is tracked for this Session, so id {work_id} was not found and "
    "nothing was cancelled. Work stops being tracked once it finished and its result was "
    "delivered to you, or when vBot restarts."
)
SUBAGENT_CANCEL_WITHOUT_ID_MESSAGE_TEMPLATE = (
    'cancel needs "id", the work to stop; nothing was cancelled. {tracked}Call {call}.'
)
SUBAGENT_CANCEL_NOTHING_TRACKED_MESSAGE = (
    'cancel needs "id", the work to stop, but no unfinished Sub-Agent work is tracked for '
    "this Session, so there is nothing to cancel."
)

# Cascade policy switch: when True, a parent Run cancellation cascades to every
# sub-agent child including background ones (legacy behaviour). When False,
# only foreground sub-agent spawns (and queued-then-started foreground waits) get
# the cascade; background spawns survive the parent cancel.
# FLIP-BACK: set CASCADE_BACKGROUND_CHILDREN = True to restore the old behaviour.
CASCADE_BACKGROUND_CHILDREN = False
