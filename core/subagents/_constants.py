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
    "Current Sub-Agent activity is available at {path}. Read this file if the Sub-Agent's "
    "status or progress becomes relevant."
)
TOP_LEVEL_BACKGROUND_NOTE = (
    "This Sub-Agent is running in the background; vBot monitors it and notifies you "
    "with the result once it finishes. Continue other work, or finish your turn to "
    "wait for a result."
)
TOP_LEVEL_QUEUED_BACKGROUND_NOTE = (
    "This Sub-Agent is queued because its Session is busy with another task; vBot "
    "starts it automatically when the Session is free and notifies you with the "
    "result once it finishes. Continue other work, or finish your turn to wait for a "
    "result."
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

# Cascade policy switch: when True, a parent Run cancellation cascades to every
# sub-agent child including background ones (legacy behaviour). When False,
# only foreground sub-agent spawns (and queued-then-started foreground waits) get
# the cascade; background spawns survive the parent cancel.
# FLIP-BACK: set CASCADE_BACKGROUND_CHILDREN = True to restore the old behaviour.
CASCADE_BACKGROUND_CHILDREN = False
