"""Curated content, automation and Channel service contracts.

The canonical RPC handlers remain responsible for ownership, target resolution,
locks, persistence and resource publication. Platform-specific Channel settings
are the one dynamic document: its schema comes from ``channel.list.platforms``.
"""

from __future__ import annotations

from .common import (
    JsonObject,
    ServiceDefinition,
    array,
    boolean,
    enum,
    integer,
    nullable,
    obj,
    params,
    service,
    string,
)


def _text(description: str = "") -> JsonObject:
    return {**string(description), "minLength": 1}


_ID = _text("Opaque identifier returned by the corresponding list/create operation.")
_AGENT = _text("Identity Agent id, or agent@project for a Project Agent where supported.")
_PROJECT = _text("Registered Project id.")
_WORKING_PROJECT = nullable(
    _text("Working Project id; null selects the Identity Agent's Workspace. Omission inherits.")
)
_SOURCE = enum("codex", "claude-code", "hermes", "openclaw", "opencode")
_PATH = _text("Local source data directory or exported source file.")
_POSITIVE = integer(1)
_ID_PARAMS = params({"id": _ID}, ("id",))
_AGENT_PARAMS = params({"agent_id": _AGENT}, ("agent_id",))
_RUN_PARAMS = params({"agent_id": _AGENT, "run_id": _ID}, ("agent_id", "run_id"))


def _memory_params(
    properties: JsonObject | None = None, required: tuple[str, ...] = ()
) -> JsonObject:
    result = params(
        {"agent_id": _text("Identity Agent id."), "project_id": _PROJECT, **(properties or {})},
        required,
    )
    result["oneOf"] = [{"required": ["agent_id"]}, {"required": ["project_id"]}]
    if properties and "scope" in properties:
        result["allOf"] = [
            {
                "if": {"required": ["agent_id"]},
                "then": {"properties": {"scope": enum("agent", "user", None)}},
                "else": {"properties": {"scope": enum("project", None)}},
            }
        ]
    return result


_MEMORY_SCOPE = enum("agent", "user", "project")
_MEMORY_ENTRY = {"scope": _MEMORY_SCOPE, "entry_id": _POSITIVE}
_RUN_OVERRIDES = obj(
    {
        "model": nullable(_text("Provider/Model selection; null restores inheritance.")),
        "thinking_effort": enum("none", "minimal", "low", "medium", "high", "xhigh", "max", None),
        "thinking_effort_default": boolean("Use the Provider default instead of a named effort."),
    }
)
_RUN_OVERRIDES["allOf"] = [
    {
        "if": {
            "properties": {"thinking_effort_default": {"const": True}},
            "required": ["thinking_effort_default"],
        },
        "then": {"properties": {"thinking_effort": {"type": "null"}}},
    }
]
_DELIVERY = obj(
    {
        "kind": enum("none", "app", "chat"),
        "channel_id": nullable(_ID),
        "platform_target": nullable(_text("Platform conversation id.")),
        "thread_id": nullable(_text("Optional thread within the conversation.")),
        "platform": nullable(_text("Expected Channel platform; detects a changed platform.")),
    },
    ("kind",),
)
_DELIVERY["allOf"] = [
    {
        "if": {"properties": {"kind": {"const": "chat"}}},
        "then": {
            "required": ["channel_id", "platform_target"],
            "properties": {"channel_id": _ID, "platform_target": _text()},
        },
        "else": {
            "properties": {
                key: {"type": "null"}
                for key in ("channel_id", "platform_target", "thread_id", "platform")
            }
        },
    }
]
_WATCH = obj(
    {
        "command": nullable(
            {**_text("Host shell command in the job's working directory."), "maxLength": 4000}
        ),
        "url": nullable({**_text("Absolute HTTP(S) URL read with GET."), "maxLength": 2048}),
        "always": boolean("Start a Run at every due time instead of only after a changed result."),
    }
)
_WATCH["oneOf"] = [
    {"required": ["command"], "properties": {"command": _text(), "url": {"type": "null"}}},
    {"required": ["url"], "properties": {"url": _text(), "command": {"type": "null"}}},
]
_CRON_EMAIL = obj(
    {
        "channel_id": _text("Email Channel of the target Identity Agent."),
        "senders": nullable(
            {**array(_text("Full sender address or @domain suffix.")), "maxItems": 20}
        ),
        "subject": nullable(
            {**string("Case-insensitive text contained in the subject."), "maxLength": 200}
        ),
    },
    ("channel_id",),
)
_CRON_FIELDS = {
    "agent_id": _AGENT,
    "name": nullable(_text("Display name; omission derives it from the prompt on creation.")),
    "prompt": _text("Instruction used by the scheduled nonhuman Run."),
    "schedule_type": enum("cron", "interval", "once", "event", "webhook", "email"),
    "cron_expression": nullable(_text("Cron expression in the application timezone.")),
    "interval_seconds": nullable(integer(1)),
    "run_at": nullable(
        _text("ISO date-time for a one-time Run; timezone rules are owner validated.")
    ),
    "event_id": nullable(_ID),
    "event_time": nullable(_text("Event edge and optional offset, for example 'start - 30m'.")),
    "repeat": nullable(integer(1)),
    "session_id": nullable(_text("Existing target Session; omission creates Sessions as needed.")),
    "working_project_id": _WORKING_PROJECT,
    "run_overrides": nullable(_RUN_OVERRIDES),
    "delivery": nullable(_DELIVERY),
    "notification_policy": enum("all", "errors", "muted"),
    "watch": nullable(_WATCH),
    "email": nullable(_CRON_EMAIL),
}
_CRON_CREATE = params(_CRON_FIELDS, ("agent_id", "prompt", "schedule_type"))
_CRON_CREATE["allOf"] = [
    {
        "if": {"properties": {"schedule_type": {"const": schedule}}},
        "then": {"required": [field], "properties": {field: schema}},
    }
    for schedule, field, schema in (
        ("cron", "cron_expression", _text()),
        ("interval", "interval_seconds", integer(1)),
        ("once", "run_at", _text()),
        ("event", "event_id", _ID),
        ("email", "email", _CRON_EMAIL),
    )
]
_CRON_CREATE["allOf"].append(
    {
        "if": {"properties": {"schedule_type": {"const": "once"}}},
        "then": {"properties": {"repeat": integer(1)}},
    }
)
_CRON_UPDATE = params(
    {
        **_CRON_FIELDS,
        "id": _ID,
        "name": _text(),
        "cron_expression": _text(),
        "interval_seconds": integer(1),
        "run_at": _text("ISO date-time."),
        "event_id": _ID,
        "event_time": _text("Event edge and optional offset, for example 'end + 10m'."),
        "status": enum("active", "paused"),
    },
    ("id",),
)
_CRON_UPDATE["allOf"] = [
    {
        "if": {"required": ["schedule_type"], "properties": {"schedule_type": {"const": "once"}}},
        "then": {"properties": {"repeat": integer(1)}},
    }
]
_CRON_READ = params(
    {
        "id": _ID,
        "through": nullable(integer(1)),
        "alert_at": nullable(_text("Exact alert timestamp.")),
    },
    ("id",),
)
_CRON_READ["anyOf"] = [
    {"required": ["through"], "properties": {"through": integer(1)}},
    {"required": ["alert_at"], "properties": {"alert_at": _text()}},
]
_BOOTSTRAP_FIELDS = {
    "agent_id": _AGENT,
    "name": nullable(_text()),
    "prompt": _text("Instruction executed as a nonhuman startup Run."),
    "mode": enum("once", "always"),
    "session_id": nullable(_ID),
}
_INPUT_FIELDS = {
    "id": _ID,
    "name": _text("Task input display name."),
    "agent_id": _AGENT,
    "working_project_id": _WORKING_PROJECT,
    "enabled": boolean(),
    "run_overrides": _RUN_OVERRIDES,
}

_EVENT_FIELDS = {
    "title": {**_text(), "maxLength": 200},
    "start": _text("ISO date for all-day events, or ISO local/offset date-time for timed events."),
    "end": _text(
        "Exclusive end in the same date/date-time form as start; omission uses one hour/day."
    ),
    "timezone": _text("IANA timezone for a timed series, such as Europe/Berlin."),
    "description": nullable({**string(), "maxLength": 5000}),
    "location": nullable({**string(), "maxLength": 500}),
    "rrule": nullable(
        {
            **string("RFC 5545 RRULE without its prefix; null/empty stops recurrence."),
            "maxLength": 500,
        }
    ),
}
_CHANNEL_EMAIL_FIELDS = {
    "address": {**_text("Mailbox address."), "maxLength": 254},
    "username": _text("Authentication username; omission uses the address."),
    "imap_host": _text("IMAP host name or IP address."),
    "imap_port": nullable(integer(1, 65535)),
    "smtp_host": _text("SMTP host name or IP address."),
    "smtp_port": nullable(integer(1, 65535)),
    "smtp_security": enum("ssl", "starttls"),
    "folders": {**array(_text("IMAP folder name.")), "minItems": 1},
    "require_sender_authentication": boolean(),
    "trusted_authserv_ids": array(_text("Trusted Authentication-Results authserv-id.")),
}
_CHANNEL_FIELDS = {
    "id": {**_ID, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]*$"},
    "platform": {
        **_text("Installed platform id from channel.list.platforms."),
        "pattern": "^[a-z][a-z0-9_-]*$",
    },
    "agent_id": _text("Identity Agent id; Channels do not target Project Agents."),
    "dm_scope": enum("per_conversation", "main", "per_peer", "per_account_channel_peer"),
    "allowed_chat_ids": array({"anyOf": [_text(), integer()]}),
    "token_env_var": string("Credential reference, never a token value."),
    "enabled": boolean(),
    "response_mode": enum("mention", "all"),
    "mention_patterns": array(_text("Regular expression used for group wake words.")),
    "observe_unaddressed": boolean(),
    "app_token_env_var": string("Slack application credential reference."),
    "server_url": string("Mattermost HTTP(S) server URL without credentials, query or fragment."),
    "email": nullable(obj(_CHANNEL_EMAIL_FIELDS)),
    "settings": {
        **obj(additional=True),
        "description": (
            "Platform-owned fields validated by the selected "
            "channel.list.platforms settings_schema."
        ),
    },
}
_CHANNEL_CREATE = params(
    {
        **_CHANNEL_FIELDS,
        "token": {**_text("Write-only managed bot token or email password."), "writeOnly": True},
        "app_token": {**_text("Write-only Slack application token."), "writeOnly": True},
    },
    ("id", "platform", "agent_id"),
)
_CHANNEL_CREATE["allOf"] = [
    {
        "if": {
            "properties": {"platform": enum("discord", "telegram", "slack", "mattermost", "email")}
        },
        "then": {"oneOf": [{"required": ["token"]}, {"required": ["token_env_var"]}]},
    },
    {
        "if": {"properties": {"platform": {"const": "slack"}}},
        "then": {"oneOf": [{"required": ["app_token"]}, {"required": ["app_token_env_var"]}]},
    },
    {
        "if": {"properties": {"platform": {"const": "email"}}},
        "then": {
            "required": ["email"],
            "properties": {
                "email": obj(_CHANNEL_EMAIL_FIELDS, ("address", "imap_host", "smtp_host"))
            },
        },
    },
]
_ARCHIVE_FILTERS = {
    "kind": nullable(enum("agent", "project", "session", "owner_group", "files")),
    "agent_id": nullable(_AGENT),
    "project_id": nullable(_PROJECT),
}
_ARCHIVE_PURGE = params(
    {
        **_ARCHIVE_FILTERS,
        "entry_ids": {**array(_ID), "minItems": 1, "maxItems": 100},
        "all": boolean(),
    }
)
_ARCHIVE_PURGE["oneOf"] = [
    {
        "required": ["all"],
        "properties": {"all": {"const": True}},
        "not": {"required": ["entry_ids"]},
    },
    {
        "required": ["entry_ids"],
        "properties": {"all": {"const": False}},
        "not": {"anyOf": [{"required": [field]} for field in _ARCHIVE_FILTERS]},
    },
]
_EXTERNAL_FIELDS = {
    "source": _SOURCE,
    "path": nullable(_PATH),
    "offset": integer(0),
    "limit": integer(1, 100),
}

SERVICE_DEFINITIONS: tuple[ServiceDefinition, ...] = (
    service(
        "memory.list",
        "query",
        _memory_params(),
        ("memories",),
        "List an owner's current Memory entries.",
    ),
    service(
        "memory.add",
        "execute",
        _memory_params({"scope": _MEMORY_SCOPE, "content": _text()}, ("scope", "content")),
        ("memories",),
        "Append one Memory entry within the owner's budget.",
    ),
    service(
        "memory.replace",
        "execute",
        _memory_params({**_MEMORY_ENTRY, "content": _text()}, ("scope", "entry_id", "content")),
        ("memories",),
        "Replace a positional Memory entry and retain its revision history.",
    ),
    service(
        "memory.remove",
        "execute",
        _memory_params(_MEMORY_ENTRY, ("scope", "entry_id")),
        ("memories",),
        "Remove a positional Memory entry with history.",
    ),
    service(
        "memory.import",
        "execute",
        _memory_params(
            {
                "scope": _MEMORY_SCOPE,
                "source": _SOURCE,
                "path": _PATH,
                "entries": nullable({**array(_text()), "minItems": 1}),
                "expected_digest": {**_text(), "pattern": "^[0-9a-f]{64}$"},
            },
            ("scope", "source", "path"),
        ),
        ("memories",),
        "Import explicit entries or a source Memory document into one scope.",
    ),
    service(
        "memory.history",
        "query",
        _memory_params({"scope": nullable(_MEMORY_SCOPE), "limit": nullable(integer(1))}),
        ("memories",),
        "Read Memory revisions, newest first, optionally filtered by scope.",
    ),
    service(
        "memory.show",
        "query",
        _memory_params({"revision": nullable(integer(1))}),
        ("memories",),
        "Read Memory at a revision, or its current state when omitted.",
    ),
    service(
        "memory.diff",
        "query",
        _memory_params({"from": integer(1), "to": nullable(integer(1))}, ("from",)),
        ("memories",),
        "Compare a Memory revision to another revision or current state.",
    ),
    service(
        "memory.revert",
        "execute",
        _memory_params({"revisions": {**array(integer(1)), "minItems": 1}}, ("revisions",)),
        ("memories",),
        "Undo selected Memory revisions with canonical conflict checks.",
    ),
    service(
        "cron.list",
        "query",
        params(),
        ("cron",),
        "List user-managed Cron jobs and application timezone.",
    ),
    service(
        "cron.activity",
        "query",
        params(),
        ("cron", "sessions"),
        "Read Cron Run activity and attention state.",
    ),
    service(
        "cron.runs",
        "query",
        params(
            {"id": _ID, "before": nullable(integer(1)), "limit": nullable(integer(1, 50))}, ("id",)
        ),
        ("cron", "sessions"),
        "Page a job's Run history using the returned sequence cursor.",
    ),
    service(
        "cron.create",
        "execute",
        _CRON_CREATE,
        ("cron",),
        "Create a scheduled, event, webhook or email job through normal target validation.",
    ),
    service(
        "cron.update",
        "execute",
        _CRON_UPDATE,
        ("cron",),
        "Patch a job while retaining canonical schedule and target checks.",
    ),
    service("cron.delete", "execute", _ID_PARAMS, ("cron",), "Delete a user-managed Cron job."),
    service("cron.enable", "execute", _ID_PARAMS, ("cron",), "Enable a user-managed Cron job."),
    service("cron.disable", "execute", _ID_PARAMS, ("cron",), "Pause a user-managed Cron job."),
    service(
        "cron.mark_read",
        "execute",
        _CRON_READ,
        ("cron", "sessions"),
        "Mark Runs through a sequence as read and/or acknowledge the exact current alert.",
    ),
    service(
        "cron.acknowledge_alert",
        "execute",
        _ID_PARAMS,
        ("cron",),
        "Acknowledge a Cron job's current failure alert.",
    ),
    service(
        "cron.retry_delivery",
        "execute",
        params(
            {
                "id": _ID,
                "delivery": {
                    **_DELIVERY,
                    "properties": {**_DELIVERY["properties"], "kind": {"const": "chat"}},
                },
            },
            ("id",),
        ),
        ("cron",),
        "Retry blocked messages, optionally rebinding them to a chat destination.",
    ),
    service(
        "bootstrap.list",
        "query",
        params(),
        ("bootstrap",),
        "List startup jobs and their latest Run outcomes.",
    ),
    service(
        "bootstrap.create",
        "execute",
        params(_BOOTSTRAP_FIELDS, ("agent_id", "prompt", "mode")),
        ("bootstrap",),
        "Create a startup job using normal Agent/Session reference checks.",
    ),
    service(
        "bootstrap.update",
        "execute",
        params({**_BOOTSTRAP_FIELDS, "id": _ID, "name": _text()}, ("id",)),
        ("bootstrap",),
        "Patch an existing startup job.",
    ),
    service("bootstrap.delete", "execute", _ID_PARAMS, ("bootstrap",), "Delete a startup job."),
    service("bootstrap.enable", "execute", _ID_PARAMS, ("bootstrap",), "Enable a startup job."),
    service("bootstrap.disable", "execute", _ID_PARAMS, ("bootstrap",), "Disable a startup job."),
    service(
        "input.list",
        "query",
        params(),
        ("inputs",),
        "List Task inputs without revealing their bearer secrets.",
    ),
    service(
        "input.create",
        "execute",
        params(_INPUT_FIELDS, ("name", "agent_id")),
        ("inputs",),
        "Create a Task input with a generated id and nonhuman Run semantics.",
    ),
    service(
        "input.update",
        "execute",
        params(_INPUT_FIELDS, ("id",)),
        ("inputs",),
        "Patch a Task input's target and execution settings.",
    ),
    service(
        "input.delete",
        "execute",
        _ID_PARAMS,
        ("inputs",),
        "Delete a Task input and its associated secret.",
    ),
    service(
        "calendar.window",
        "query",
        params(
            {
                "from": _text("Inclusive ISO date or date-time window start."),
                "to": _text("ISO date or date-time end; date bounds include the whole day."),
            },
            ("from", "to"),
        ),
        ("calendar", "cron"),
        "Read event occurrences and the bounded Cron preview for a window.",
    ),
    service(
        "calendar.create",
        "execute",
        params(_EVENT_FIELDS, ("title", "start")),
        ("calendar",),
        "Create an all-day or timed event, optionally repeating.",
    ),
    service(
        "calendar.update",
        "execute",
        params({**_EVENT_FIELDS, "id": _ID}, ("id",)),
        ("calendar",),
        "Patch an event or occurrence; recurrence and timezone changes require the series id.",
    ),
    service(
        "calendar.delete",
        "execute",
        _ID_PARAMS,
        ("calendar", "cron"),
        "Delete an occurrence, or a whole event and its bound Cron jobs.",
    ),
    service(
        "channel.list",
        "query",
        params(),
        ("channels",),
        "List Channels, health and installed platforms with settings schemas.",
    ),
    service(
        "channel.status",
        "query",
        _ID_PARAMS,
        ("channels",),
        "Read Channel health and recent rejected conversation ids without pairing credentials.",
    ),
    service(
        "channel.access.get",
        "query",
        _ID_PARAMS,
        ("channels",),
        "Read Channel group access and member Tool ceilings.",
    ),
    service(
        "channel.create",
        "execute",
        _CHANNEL_CREATE,
        ("channels", "data_store"),
        "Create a Channel with canonical platform validation and credential rollback.",
    ),
    service(
        "channel.update",
        "execute",
        params(_CHANNEL_FIELDS, ("id",)),
        ("channels",),
        "Patch a Channel; platform and Agent changes use normal lifecycle transitions.",
    ),
    service(
        "channel.delete",
        "execute",
        _ID_PARAMS,
        ("channels",),
        "Stop and delete a Channel and its transport state.",
    ),
    service(
        "channel.enable",
        "execute",
        _ID_PARAMS,
        ("channels",),
        "Enable and start a Channel through canonical adapter supervision.",
    ),
    service("channel.disable", "execute", _ID_PARAMS, ("channels",), "Disable and stop a Channel."),
    service(
        "channel.set_token",
        "execute",
        params(
            {"id": _ID, "token": {**_text(), "writeOnly": True}, "slot": enum("bot", "app")},
            ("id", "token"),
        ),
        ("channels", "data_store"),
        "Replace a Channel credential without revealing it and restart its adapter if necessary.",
    ),
    service(
        "archive.list",
        "query",
        params(
            {
                **_ARCHIVE_FILTERS,
                "limit": nullable(integer(1, 200)),
                "cursor": nullable(
                    obj(
                        {
                            "archived_at": _text(
                                "Canonical UTC timestamp copied from next_cursor."
                            ),
                            "entry_id": _ID,
                        },
                        ("archived_at", "entry_id"),
                    )
                ),
            }
        ),
        ("archive",),
        "Page archived entries with their restore and user-folder facts.",
    ),
    service(
        "archive.show",
        "query",
        params({"entry_id": _ID, "session_limit": nullable(integer(1, 500))}, ("entry_id",)),
        ("archive",),
        "Read archived entry details and its bounded Session listing.",
    ),
    service(
        "archive.restore",
        "execute",
        params({"entry_id": _ID, "target_id": nullable(_ID)}, ("entry_id",)),
        ("archive", "agents", "projects", "sessions"),
        "Restore an archived Agent, Project or Session under normal lifecycle guards.",
    ),
    service(
        "archive.purge",
        "execute",
        _ARCHIVE_PURGE,
        ("archive",),
        "Permanently purge explicit entries or eligible matches; bulk mode preserves user folders.",
    ),
    service(
        "learning.changes",
        "query",
        _RUN_PARAMS,
        ("memories", "skills"),
        "Read the Memory and Skill changes attributable to one Run.",
    ),
    service(
        "learning.undo",
        "execute",
        _RUN_PARAMS,
        ("memories", "skills"),
        "Undo one Run's learning changes together with canonical busy/conflict checks.",
    ),
    service(
        "librarian.overview",
        "query",
        params(),
        ("skills",),
        "Read Librarian availability and recent passes across Agents.",
    ),
    service(
        "librarian.status",
        "query",
        _AGENT_PARAMS,
        ("skills",),
        "Read an Identity Agent's Librarian schedule, passes and last changes.",
    ),
    service(
        "librarian.run",
        "execute",
        _AGENT_PARAMS,
        ("skills",),
        "Start a Librarian pass through its normal availability and Run admission checks.",
    ),
    service(
        "librarian.undo",
        "execute",
        params(
            {"agent_id": _AGENT, "started_at": _text("Exact started_at value of a retained pass.")},
            ("agent_id", "started_at"),
        ),
        ("skills",),
        "Undo a retained Librarian pass across its Skill owners.",
    ),
    service(
        "external.sources",
        "query",
        params(),
        (),
        "Discover supported local external history sources.",
    ),
    service(
        "external.list",
        "query",
        params({**_EXTERNAL_FIELDS, "query": nullable(_text("Text filter."))}, ("source",)),
        (),
        "Page saved external conversations without running their originating application.",
    ),
    service(
        "external.show",
        "query",
        params(
            {
                **_EXTERNAL_FIELDS,
                "source_session_id": _ID,
                "query": nullable(_text()),
                "char_offset": integer(0),
            },
            ("source", "source_session_id"),
        ),
        (),
        "Read saved external conversation content with entry and character pagination.",
    ),
    service(
        "external.memories",
        "query",
        params({**_EXTERNAL_FIELDS, "char_offset": integer(0)}, ("source",)),
        (),
        "Read external Memory documents with entry and character pagination.",
    ),
    service(
        "external.import_session",
        "execute",
        params(
            {
                "source": _SOURCE,
                "path": nullable(_PATH),
                "source_session_id": _ID,
                "agent_id": _AGENT,
                "working_project_id": _WORKING_PROJECT,
                "make_current": boolean(),
            },
            ("source", "source_session_id", "agent_id"),
        ),
        ("sessions",),
        "Import saved history into a new Session with source provenance, without starting a Run.",
    ),
)

RPC_ONLY_FIELDS: dict[str, dict[str, str]] = {}

SERVICE_EXCLUSIONS = {
    "cron.webhook_secret": (
        "Reveals a bearer credential; Extensions use owned triggers or application services."
    ),
    "cron.rotate_webhook_secret": (
        "Returns the replacement bearer credential; no redacted rotation contract exists."
    ),
    "input.secret": "Reveals a Task input bearer credential rather than a product data projection.",
    "input.rotate_secret": (
        "Returns the replacement Task input bearer credential; no redacted result exists."
    ),
    "channel.admin.grant": (
        "Operator-owned trust assignment; this handler has no Extension authority contract."
    ),
    "channel.admin.revoke": (
        "Operator-owned trust assignment; this handler has no Extension authority contract."
    ),
    "channel.identity.set": (
        "Assigns the trusted transport self identity; reserved for the operator/platform workflow."
    ),
    "channel.member_tools.set": (
        "Changes the group member Tool ceiling; reserved for operator access policy workflows."
    ),
    "channel.whatsapp.setup": (
        "Installs local WhatsApp bridge dependencies as part of the operator pairing workflow."
    ),
    "channel.whatsapp.pair": (
        "Starts or resets account pairing; reserved for the operator credential workflow."
    ),
    "channel.whatsapp.status": (
        "Includes a pairing QR credential; channel.status provides credential-free health."
    ),
}
