"""Canonical RPC envelopes and Extension schemas for conversation domains.

RPC and Extension admission share these fields; explicitly reasoned RPC-only
parameters keep human input and execution grants out of Extension calls. Domain
handlers retain state-dependent checks and value normalization.
"""

from __future__ import annotations

from core.memory import MEMORY_PROMPT_MODES
from core.settings import ALLOWED_THINKING_EFFORTS
from core.settings.agent_defaults import MAX_TEMPERATURE, MAX_TOP_P, MIN_TEMPERATURE, MIN_TOP_P

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


def _nonempty(description: str = "") -> JsonObject:
    return {**string(description), "minLength": 1}


_ID = _nonempty("Existing entity id; the owning service validates its scope and existence.")
_AGENT = _nonempty("Identity Agent id, or a Project Team address such as coder@my-project.")
_ADDRESS = {"agent_id": _AGENT, "session_id": _ID}
_EXPECTED_INCARNATION = _nonempty("Creation stamp of the Identity Agent the caller opened.")
_ADDRESS_REQUIRED = ("agent_id", "session_id")
_STRINGS = array(string())
_NAMES = array(_nonempty())
_TEMPERATURE = {"type": "number", "minimum": MIN_TEMPERATURE, "maximum": MAX_TEMPERATURE}
_TOP_P = {"type": "number", "minimum": MIN_TOP_P, "maximum": MAX_TOP_P}
_THINKING = enum(*sorted(ALLOWED_THINKING_EFFORTS))
_TOOL_NAME = {**_nonempty(), "not": {"const": "*"}}
_TOOL_NAMES = array(_TOOL_NAME)

PERMISSION_RULES = obj(
    {
        "read": nullable(enum("anywhere", "work_area")),
        "write": nullable(enum("anywhere", "work_area", "scratch")),
        "commands": nullable(enum("any", "listed")),
        "web": nullable(enum("any", "listed")),
        "allow": nullable(array(string("Permission Rule, e.g. Read({workdir}/docs/**)."))),
        "deny": nullable(array(string("Permission Rule, e.g. Shell(git push)."))),
    }
)
TOOL_LOADING = obj({"on_demand": boolean(), "always_loaded": _TOOL_NAMES})
TOOL_ACCESS = {
    "oneOf": [
        obj(
            {
                "mode": enum("selected"),
                "allowed": _TOOL_NAMES,
                "denied": _TOOL_NAMES,
                "granted": _TOOL_NAMES,
            },
            ("mode", "allowed"),
        ),
        obj(
            {"mode": enum("all", "none"), "denied": _TOOL_NAMES, "granted": _TOOL_NAMES}, ("mode",)
        ),
    ]
}
COMPACTION_POLICY = obj(
    {
        "enabled": boolean(),
        "trigger": nullable(
            {
                "oneOf": [
                    obj(
                        {
                            "type": enum("context_ratio"),
                            "threshold": nullable(
                                {"type": "number", "exclusiveMinimum": 0, "maximum": 1}
                            ),
                            "tokens": nullable(integer(1)),
                        }
                    ),
                    obj({"type": enum("input_tokens"), "tokens": nullable(integer(1))}, ("type",)),
                ]
            }
        ),
        "strategy": nullable(
            {
                "oneOf": [
                    obj(
                        {
                            "type": enum("summary_tail"),
                            "tail_tokens": nullable(integer(1)),
                            "summary_model": nullable(
                                string("Model binding, or null for inheritance.")
                            ),
                        }
                    ),
                    obj({"type": enum("continuation")}, ("type",)),
                ]
            }
        ),
    },
    ("enabled", "trigger", "strategy"),
)
_CONTEXT_COMMANDS = nullable(
    array(
        {
            **_nonempty("One shell command line run when a Session starts."),
            "maxLength": 4000,
            "pattern": r"^[^\r\n]+$",
        }
    )
)
_PLACEMENT = {
    "oneOf": [
        obj({"kind": enum("chat", "helper")}, ("kind",)),
        obj({"kind": enum("private"), "owner_id": _ID}, ("kind", "owner_id")),
    ]
}
# Tool-specific settings are deliberately extensible: Extensions own their own
# nested configuration. The built-in special cases still have useful schemas.
_TOOLS = obj(
    {
        "bash": nullable(obj({"allowed_env": _STRINGS})),
        "subagent": nullable(obj({"allowed_agents": _STRINGS}, additional=True)),
    },
    additional=True,
)
_TOOLS["description"] = "Per-Tool settings keyed by Tool name; Extension Tools own their values."

AGENT_FIELDS = {
    "name": nullable(string()),
    "description": nullable(string()),
    "model": string("Model binding; an empty value inherits the shared default."),
    "fallback_models": {**_STRINGS, "maxItems": 5},
    "memory_prompt_mode": enum(*MEMORY_PROMPT_MODES),
    "project_memory_enabled": boolean(),
    "temperature": nullable(_TEMPERATURE),
    "top_p": nullable(_TOP_P),
    "thinking_effort": nullable(_THINKING),
    "tool_access": nullable(TOOL_ACCESS),
    "allowed_skills": _STRINGS,
    "excluded_skills": array({**_nonempty(), "not": {"const": "*"}}),
    "tools": _TOOLS,
    "custom_system_prompt_enabled": boolean(),
    "compaction_policy": nullable(COMPACTION_POLICY),
    "tool_loading": nullable(TOOL_LOADING),
    "librarian_enabled": boolean(),
    "permission_rules": nullable(PERMISSION_RULES),
    "permission_profile": nullable(_nonempty("Named permission profile.")),
    "placement": _PLACEMENT,
}
OPERATOR_AGENT_FIELDS = frozenset({"permission_rules", "permission_profile", "tool_access"})
_SERVICE_AGENT_FIELDS = {
    name: schema for name, schema in AGENT_FIELDS.items() if name not in OPERATOR_AGENT_FIELDS
}
_AGENT_UPDATE_FIELDS = {
    **AGENT_FIELDS,
    "expected_incarnation": _EXPECTED_INCARNATION,
    "current_session_id": nullable(_ID),
    "workspace": nullable(string("Absolute Workspace path, or null to use the default.")),
    "root_project_id": nullable(_ID),
    "copy_workspace_identity_files": boolean(),
    "subagent_allow": _STRINGS,
    "context_commands": _CONTEXT_COMMANDS,
}
_AGENT_UPDATE = {
    **params(
        {
            "id": _ID,
            **{
                name: schema
                for name, schema in _AGENT_UPDATE_FIELDS.items()
                if name not in OPERATOR_AGENT_FIELDS
            },
        },
        ("id",),
    ),
    "not": {"required": ["tools", "subagent_allow"]},
}
_PROJECT_FIELDS = {
    "cwd": _nonempty("Existing repository directory; its files remain owned by the repository."),
    "display_name": nullable(string()),
    "default_agent": nullable(_ID),
    "default_model": nullable(_nonempty("Configured Model binding.")),
    "default_temperature": nullable(_TEMPERATURE),
    "default_thinking_effort": nullable(_THINKING),
    "default_top_p": nullable(_TOP_P),
    "sources": array(
        obj(
            {
                "id": _nonempty("Source catalog id or folder:<path>; unknown ids remain inert."),
                "enabled": boolean(),
                "agent_paths": nullable(_NAMES),
            },
            ("id",),
        )
    ),
    "model_mappings": obj(additional=_nonempty("Configured vBot Model binding.")),
    "auto_load": nullable(_NAMES),
    "auto_instructions": boolean(),
    "folder_instructions": boolean(),
    "memory_enabled": boolean(),
    "skills_bundled_enabled": _NAMES,
    "skills_global_enabled": _NAMES,
    "skills_project_disabled": _NAMES,
}
_PROJECT_OVERRIDES = {
    "model": _nonempty("Configured Model binding."),
    "temperature": _TEMPERATURE,
    "top_p": _TOP_P,
    "thinking_effort": _THINKING,
    "compaction_policy": COMPACTION_POLICY,
    "tool_access": nullable(TOOL_ACCESS),
    "tool_loading": TOOL_LOADING,
    "project_memory_enabled": boolean(),
    "permission_rules": PERMISSION_RULES,
}
_SESSION_OVERRIDE_FIELDS = {
    "model": _nonempty("Configured Model binding."),
    "temperature": _TEMPERATURE,
    "top_p": _TOP_P,
    "thinking_effort": enum(*sorted(ALLOWED_THINKING_EFFORTS - {""})),
    "thinking_effort_default": boolean(),
}
_SESSION_FILTERS = {
    name: boolean()
    for name in (
        "include_subagents",
        "include_memory_reflections",
        "include_skill_reflections",
        "include_cron",
        "include_channels",
        "split_pinned",
    )
}
_LIST_CURSOR = obj(
    {
        **_ADDRESS,
        "last_activity_at": {
            **string("Canonical UTC timestamp from next_cursor."),
            "format": "date-time",
        },
    },
    ("last_activity_at", *_ADDRESS_REQUIRED),
)
_INSPECTION_CURSOR = obj(
    {
        "history_id": _ID,
        "generation": _ID,
        "revision": integer(0),
        "summary": boolean(),
        "run_id": nullable(_ID),
        "before": integer(0),
        "floor_sequence": integer(0),
    },
    ("history_id", "generation", "revision", "summary", "run_id", "before", "floor_sequence"),
)
_HISTORY_FIELDS = {
    "agent_id": _AGENT,
    "session_id": nullable(_ID),
    "include_inherited": boolean(
        "Include the history inherited from a fork's source (default true)."
    ),
    "limit": nullable(integer(1, 500)),
    "before": nullable(_ID),
    "after": nullable(_ID),
}
_CONTENT_BLOCK = {
    "oneOf": [
        obj({"type": enum("text"), "text": string()}, ("type", "text")),
        obj(
            {
                "type": enum("media"),
                "attachment_id": string(),
                "filename": string(),
                "media_type": string(),
                "image_reference": nullable(integer(1)),
                "transcription": nullable(_nonempty()),
            },
            ("type", "attachment_id", "filename", "media_type"),
        ),
        obj(
            {
                "type": enum("file"),
                "attachment_id": string(),
                "filename": string(),
                "media_type": string(),
            },
            ("type", "attachment_id", "filename", "media_type"),
        ),
        {
            **obj(
                {
                    "type": enum("file_mention"),
                    "path": string(),
                    "status": enum("inlined", "too_large", "not_text", "missing"),
                    "text": nullable(string()),
                    "size_bytes": nullable(integer()),
                },
                ("type", "path", "status"),
            ),
            "if": {"properties": {"status": {"const": "inlined"}}},
            "then": {"required": ["text"], "properties": {"text": string()}},
            "else": {"properties": {"text": {"type": "null"}}},
        },
    ]
}
_CHAT_INPUT = params(
    {**_ADDRESS, "content": {"anyOf": [_nonempty(), array(_CONTENT_BLOCK)]}},
    (*_ADDRESS_REQUIRED, "content"),
)
_SKILL_SCOPE = {
    **string("Writable Skill home: global or agent:<Identity Agent id>."),
    "pattern": r"^(global|agent:[^@/\\]+)$",
}
_SKILL = {"scope": _SKILL_SCOPE, "name": _nonempty("Skill package name.")}
_SKILL_REQUIRED = ("scope", "name")

SERVICE_DEFINITIONS: tuple[ServiceDefinition, ...] = (
    service("agent.list", "query", params(), ("agents",), "List Identity Agents in user order."),
    service(
        "agent.get",
        "query",
        params({"id": _ID, "expected_incarnation": _EXPECTED_INCARNATION}, ("id",)),
        ("agents",),
        "Read an Identity Agent's effective configuration.",
    ),
    service(
        "agent.create",
        "execute",
        params({"id": _ID, **_SERVICE_AGENT_FIELDS}, ("id",)),
        ("agents",),
        "Create an Identity Agent through the canonical Agent store.",
    ),
    service(
        "agent.update",
        "execute",
        _AGENT_UPDATE,
        ("agents", "sessions"),
        "Update Agent configuration; Tools and subagent_allow cannot be sent together.",
    ),
    service(
        "agent.rename",
        "execute",
        params({"id": _ID, "new_id": _ID}, ("id", "new_id")),
        ("agents", "sessions", "channels", "cron", "inputs"),
        "Rename an Identity Agent and retarget references under lifecycle guards.",
    ),
    service(
        "agent.delete",
        "execute",
        params({"id": _ID, "permanent": boolean()}, ("id",)),
        ("agents", "sessions", "archive"),
        "Archive an Agent; permanent also purges its archive.",
    ),
    service(
        "agent.reorder",
        "execute",
        params(
            {"agent_ids": _STRINGS, "expected_revision": integer(0)},
            ("agent_ids", "expected_revision"),
        ),
        ("agents",),
        "Store the complete Agent order against the last observed order_revision.",
    ),
    service(
        "agent.soul.get",
        "query",
        params({"id": _ID, "expected_incarnation": _EXPECTED_INCARNATION}, ("id",)),
        ("agents",),
        "Read SOUL.md content and its optimistic-concurrency revision.",
    ),
    service(
        "agent.soul.set",
        "execute",
        params(
            {
                "id": _ID,
                "content": string(),
                "revision": string(),
                "expected_incarnation": _EXPECTED_INCARNATION,
            },
            ("id", "content", "revision"),
        ),
        ("agents",),
        "Replace SOUL.md only when the supplied revision is current.",
    ),
    service(
        "agent.suggest_description",
        "execute",
        params({"id": _ID, "expected_incarnation": _EXPECTED_INCARNATION}, ("id",)),
        (),
        "Ask the Agent's Model to draft a description without saving it.",
    ),
    service(
        "project.list",
        "query",
        params({"include_scan": boolean()}),
        ("projects",),
        "List Projects in user order, optionally including cached source scans.",
    ),
    service(
        "project.show",
        "query",
        params({"project_id": _ID}, ("project_id",)),
        ("projects", "skills"),
        "Read a Project and refresh its source and Skill scan.",
    ),
    service(
        "project.detect",
        "query",
        params({"cwd": nullable(_ID)}),
        ("projects",),
        "Detect supported Project sources in an existing directory.",
    ),
    service(
        "project.add",
        "execute",
        params(_PROJECT_FIELDS, ("cwd",)),
        ("projects",),
        "Register a repository as a Project, deriving its stable id from its display name.",
    ),
    service(
        "project.set",
        "execute",
        {
            **params(
                {"project_id": _ID, **_PROJECT_FIELDS, "context_commands": _CONTEXT_COMMANDS},
                ("project_id",),
            ),
            "minProperties": 2,
        },
        ("projects", "skills"),
        "Update at least one Project field and invalidate derived catalogs.",
    ),
    service(
        "project.reorder",
        "execute",
        params(
            {"project_ids": _STRINGS, "expected_revision": integer(0)},
            ("project_ids", "expected_revision"),
        ),
        ("projects",),
        "Store the complete Project order against the last observed order_revision.",
    ),
    service(
        "project.rm",
        "execute",
        params(
            {
                "project_id": _ID,
                "copy_rooted_agent_identity_files": boolean(),
                "permanent": boolean(),
            },
            ("project_id",),
        ),
        ("projects", "agents", "sessions", "archive"),
        "Archive a Project and its Sessions without deleting its repository.",
    ),
    service(
        "project.set_override",
        "execute",
        {
            **params(
                {
                    "project_id": _ID,
                    "agent_id": _ID,
                    "field": enum(
                        *(name for name in _PROJECT_OVERRIDES if name not in OPERATOR_AGENT_FIELDS)
                    ),
                    "value": {"description": "Value matching the selected override field."},
                },
                ("project_id", "agent_id", "field", "value"),
            ),
            "allOf": [
                {
                    "if": {"properties": {"field": {"const": field}}},
                    "then": {"properties": {"value": schema}},
                }
                for field, schema in _PROJECT_OVERRIDES.items()
            ],
        },
        ("projects", "agents", "skills"),
        "Override one Project Team Agent setting.",
    ),
    service(
        "project.clear_override",
        "execute",
        params(
            {
                "project_id": _ID,
                "agent_id": _ID,
                "field": enum(
                    *(name for name in _PROJECT_OVERRIDES if name not in OPERATOR_AGENT_FIELDS)
                ),
            },
            ("project_id", "agent_id", "field"),
        ),
        ("projects", "agents", "skills"),
        "Clear one Project Team Agent override and resume inheritance.",
    ),
    service(
        "session.list",
        "query",
        {
            **params(
                {
                    "agent_id": _AGENT,
                    "agent_ids": {**_STRINGS, "maxItems": 100},
                    "parent_session_id": nullable(_ID),
                    "parent_agent_id": nullable(_AGENT),
                    "working_project_id": nullable(_ID),
                    "limit": nullable(integer(1, 100)),
                    "cursor": nullable(_LIST_CURSOR),
                    "required_session": nullable(obj(_ADDRESS, _ADDRESS_REQUIRED)),
                    **_SESSION_FILTERS,
                }
            ),
            "allOf": [
                {"not": {"required": pair}}
                for pair in (
                    ["agent_id", "agent_ids"],
                    ["agent_id", "working_project_id"],
                    ["agent_ids", "working_project_id"],
                )
            ],
        },
        ("sessions", "runs"),
        "Page Session summaries with canonical address and source filters.",
    ),
    service(
        "session.project_groups",
        "query",
        params({"limit": nullable(integer(1, 20)), **_SESSION_FILTERS}),
        ("sessions", "projects", "runs"),
        "Read recent Sessions grouped by Working Project.",
    ),
    service(
        "session.activity_list",
        "query",
        params({"agent_ids": _STRINGS}, ("agent_ids",)),
        ("sessions", "runs"),
        "Read completion activity for a batch of Agent addresses.",
    ),
    service(
        "session.get",
        "query",
        params(_ADDRESS, _ADDRESS_REQUIRED),
        ("sessions",),
        "Read one exact Session's summary, or null when it does not exist.",
    ),
    service(
        "session.show",
        "query",
        params(
            {
                "session_id": _ID,
                "agent_id": nullable(_AGENT),
                "summary": boolean(),
                "cursor": nullable(_INSPECTION_CURSOR),
                "limit": nullable(integer(1, 100)),
                "run_id": nullable(_ID),
                "entry": nullable(integer(0)),
                "offset": nullable(integer(0)),
            },
            ("session_id",),
        ),
        ("sessions", "runs"),
        "Inspect a Session transcript; ambiguous Session ids require the Agent address.",
    ),
    service(
        "session.change_stats",
        "query",
        params({**_ADDRESS, "include_subagents": boolean()}, _ADDRESS_REQUIRED),
        ("sessions", "runs"),
        "Read file-change totals for a Session and optionally its Sub-Agents.",
    ),
    service(
        "session.create",
        "execute",
        params(
            {
                "agent_id": _AGENT,
                "session_id": nullable(_ID),
                "make_current": boolean(),
                "working_project_id": nullable(_ID),
                "agent_overrides": nullable(obj(_SESSION_OVERRIDE_FIELDS)),
            },
            ("agent_id",),
        ),
        ("sessions", "agents"),
        "Create a Session without granting Computer execution access.",
    ),
    service(
        "session.delete",
        "execute",
        params({**_ADDRESS, "permanent": boolean()}, _ADDRESS_REQUIRED),
        ("sessions", "agents", "archive"),
        "Archive an idle, unreferenced Session; optionally purge it.",
    ),
    service(
        "session.fork",
        "execute",
        params({**_ADDRESS, "target_agent_id": nullable(_AGENT)}, _ADDRESS_REQUIRED),
        ("sessions",),
        "Fork a Session, optionally into another Agent, preserving fork provenance.",
    ),
    service(
        "session.rename",
        "execute",
        params({**_ADDRESS, "title": string()}, _ADDRESS_REQUIRED),
        ("sessions",),
        "Set the Session title; empty or absent title restores automatic naming.",
    ),
    service(
        "session.set_pinned",
        "execute",
        params({**_ADDRESS, "pinned": boolean()}, (*_ADDRESS_REQUIRED, "pinned")),
        ("sessions",),
        "Pin or unpin a Session for all accessors.",
    ),
    service(
        "session.set_fast_mode",
        "execute",
        params({**_ADDRESS, "enabled": boolean()}, (*_ADDRESS_REQUIRED, "enabled")),
        ("sessions",),
        "Set the speed preference for subsequent Runs in this Session.",
    ),
    service(
        "session.set_agent_overrides",
        "execute",
        params(
            {
                **_ADDRESS,
                "agent_overrides": {
                    **obj(
                        {key: nullable(value) for key, value in _SESSION_OVERRIDE_FIELDS.items()}
                    ),
                    "minProperties": 1,
                },
                "remember": boolean(),
            },
            (*_ADDRESS_REQUIRED, "agent_overrides"),
        ),
        ("sessions", "agents", "projects"),
        "Set Session Model settings; null clears a field and remember also saves Agent defaults.",
    ),
    service(
        "session.set_compaction_policy",
        "execute",
        params({**_ADDRESS, "policy": nullable(COMPACTION_POLICY)}, _ADDRESS_REQUIRED),
        ("sessions",),
        "Replace the full Session Compaction Policy; null or omission clears it.",
    ),
    service(
        "session.link_channel",
        "execute",
        params(
            {**_ADDRESS, "channel_id": _ID, "platform_conv_id": _ID},
            (*_ADDRESS_REQUIRED, "channel_id", "platform_conv_id"),
        ),
        ("sessions", "channels"),
        "Link an Identity Agent's Session to its Channel conversation.",
    ),
    service(
        "session.mark_read",
        "execute",
        {
            **params(
                {
                    **_ADDRESS,
                    "run_id": _ID,
                    "run_ids": {**_STRINGS, "minItems": 1, "maxItems": 200},
                    "through": _ID,
                },
                _ADDRESS_REQUIRED,
            ),
            "oneOf": [
                {"required": ["run_id"]},
                {"required": ["run_ids"]},
                {"required": ["through"]},
            ],
        },
        ("sessions",),
        "Acknowledge completed Runs by exact id or through a displayed Run.",
    ),
    service(
        "session.notification_status",
        "query",
        params({**_ADDRESS, "run_id": _ID}, (*_ADDRESS_REQUIRED, "run_id")),
        ("sessions", "runs", "cron"),
        "Check whether a completed Run is eligible for notification.",
    ),
    service(
        "session.notification_policy",
        "execute",
        params(
            {**_ADDRESS, "policy": enum("all", "errors", "muted")}, (*_ADDRESS_REQUIRED, "policy")
        ),
        ("sessions",),
        "Set a Session's notification policy.",
    ),
    service(
        "chat.commands",
        "query",
        params(
            {
                "agent_id": _AGENT,
                "session_id": nullable(_ID),
                "working_project_id": nullable(_ID),
                "reply_surface": enum("webui", "tui"),
            }
        ),
        ("skills", "agents", "projects", "extensions"),
        "List available Slash Commands and Skill suggestions for a Session or draft.",
    ),
    service(
        "chat.history",
        "query",
        params(_HISTORY_FIELDS, ("agent_id",)),
        ("sessions", "runs"),
        "Read paged Chat History with canonical continuation cursors.",
    ),
    service(
        "chat.timeline",
        "query",
        params({**_HISTORY_FIELDS, "child_limit": nullable(integer(1, 200))}, ("agent_id",)),
        ("sessions", "runs"),
        "Read the presented Chat timeline and optional Tool children.",
    ),
    service(
        "chat.search",
        "query",
        params(
            {
                **_ADDRESS,
                "query": {**_nonempty(), "maxLength": 256},
                "include_inherited": _HISTORY_FIELDS["include_inherited"],
            },
            (*_ADDRESS_REQUIRED, "query"),
        ),
        ("sessions",),
        "Search the displayed Session for User and Assistant messages, newest first.",
    ),
    service(
        "chat.timeline_children",
        "query",
        params(
            {
                **_ADDRESS,
                "run_id": _ID,
                "before": _ID,
                "history_generation": _ID,
                "limit": nullable(integer(1, 200)),
            },
            (*_ADDRESS_REQUIRED, "run_id", "before", "history_generation"),
        ),
        ("sessions", "runs"),
        "Page a Run's timeline children against an exact History generation.",
    ),
    service(
        "chat.tool_call",
        "query",
        params(
            {**_ADDRESS, "run_id": _ID, "tool_id": _ID, "history_generation": _ID},
            (*_ADDRESS_REQUIRED, "run_id", "tool_id", "history_generation"),
        ),
        ("sessions", "runs"),
        "Read the full arguments and result of a timeline Tool row.",
    ),
    service(
        "chat.run_result",
        "query",
        params({**_ADDRESS, "run_id": _ID}, (*_ADDRESS_REQUIRED, "run_id")),
        ("sessions", "runs"),
        "Read the final response of an exact completed Run.",
    ),
    service(
        "chat.reflections",
        "query",
        params(_ADDRESS, _ADDRESS_REQUIRED),
        ("sessions", "runs"),
        "Read Reflection Runs associated with a Session.",
    ),
    service(
        "chat.send",
        "execute",
        _CHAT_INPUT,
        ("sessions", "runs"),
        "Submit nonhuman SYSTEM input to an existing idle Session and await its result; "
        "no commands.",
    ),
    service(
        "chat.stream",
        "execute",
        _CHAT_INPUT,
        ("sessions", "runs"),
        "Start nonhuman SYSTEM input in an existing idle Session and return its Run; no commands.",
    ),
    service(
        "chat.cancel",
        "execute",
        params(
            {"run_id": _ID, "reason": nullable({**_ID, "not": enum("user", "parent_agent")})},
            ("run_id",),
        ),
        ("runs", "sessions"),
        "Cancel an active Run by id.",
    ),
    service(
        "chat.stop_all",
        "execute",
        params(_ADDRESS, _ADDRESS_REQUIRED),
        ("runs", "sessions", "commands", "terminals"),
        "Stop all work in a Session and its Sub-Agent tree.",
    ),
    service(
        "chat.work",
        "query",
        params(_ADDRESS, _ADDRESS_REQUIRED),
        ("runs", "sessions", "commands", "terminals"),
        "Inspect stoppable work in a Session subtree.",
    ),
    service(
        "chat.command_output",
        "query",
        params({"session_id": _ID, "command_id": _ID}, ("session_id", "command_id")),
        ("commands",),
        "Read recent clean output and status of a Session background command.",
    ),
    service(
        "chat.control_run",
        "execute",
        {
            **params(
                {
                    **_ADDRESS,
                    "run_id": _ID,
                    "action": enum("compact", "background_tool"),
                    "tool_call_id": _ID,
                },
                (*_ADDRESS_REQUIRED, "run_id", "action"),
            ),
            "if": {"properties": {"action": {"const": "background_tool"}}},
            "then": {"required": ["tool_call_id"]},
            "else": {"not": {"required": ["tool_call_id"]}},
        },
        ("runs", "sessions"),
        "Request compaction or move an active Tool call into the background.",
    ),
    service(
        "chat.queue_list",
        "query",
        params(_ADDRESS, _ADDRESS_REQUIRED),
        ("queue", "runs"),
        "List public queued input for an exact Session.",
    ),
    service(
        "chat.queue_remove",
        "execute",
        params({**_ADDRESS, "item_id": _ID}, (*_ADDRESS_REQUIRED, "item_id")),
        ("queue", "runs"),
        "Remove public queued input unless steering is already in flight.",
    ),
    service(
        "chat.queue_steer",
        "execute",
        params({**_ADDRESS, "item_id": _ID}, (*_ADDRESS_REQUIRED, "item_id")),
        ("queue", "runs"),
        "Deliver existing queued input into its active Run without changing its provenance.",
    ),
    service(
        "subagent.inspect",
        "query",
        params({**_ADDRESS, "id": _ID}, (*_ADDRESS_REQUIRED, "id")),
        ("sessions", "runs"),
        "Inspect Sub-Agent work identified within its parent Session.",
    ),
    service(
        "subagent.tree",
        "query",
        params(_ADDRESS, _ADDRESS_REQUIRED),
        ("sessions", "runs"),
        "List a Session's current Sub-Agent descendants breadth first.",
    ),
    service(
        "tool.list",
        "query",
        params(),
        ("extensions", "settings"),
        "List registered public Tools with readiness and configuration annotations.",
    ),
    service(
        "skill.list",
        "query",
        params(),
        ("skills",),
        "List active global Skills and invalid-package diagnostics.",
    ),
    service(
        "skill.read",
        "query",
        params({"scope": _SKILL_SCOPE}, ("scope",)),
        ("skills",),
        "Read editable Skill documents in a writable home.",
    ),
    service(
        "skill.inspect",
        "query",
        params({"id": _ID, "expected_incarnation": _EXPECTED_INCARNATION}, ("id",)),
        ("skills",),
        "Inspect an exact Skill package id from the inventory.",
    ),
    service(
        "skill.inventory",
        "query",
        params({"include_usage": boolean()}),
        ("skills", "statistics"),
        "Inventory every Skill source, including disabled packages and optional usage.",
    ),
    service(
        "skill.usage",
        "query",
        params(),
        ("skills", "statistics"),
        "Read activation use aggregated by current Skill package.",
    ),
    service(
        "skill.history",
        "query",
        params(
            {"scope": _SKILL_SCOPE, "name": nullable(_ID), "limit": integer(1, 500)}, ("scope",)
        ),
        ("skills",),
        "List revisions of a writable Skill home or one package.",
    ),
    service(
        "skill.archived",
        "query",
        params({"scope": _SKILL_SCOPE}, ("scope",)),
        ("skills",),
        "List archived packages in a writable Skill home.",
    ),
    service(
        "skill.create",
        "execute",
        params(
            {**_SKILL, "content": _nonempty("Full SKILL.md document."), "source": nullable(_ID)},
            (*_SKILL_REQUIRED, "content"),
        ),
        ("skills",),
        "Create a validated Skill package with nonhuman Extension provenance.",
    ),
    service(
        "skill.update",
        "execute",
        params(
            {
                **_SKILL,
                "content": _nonempty("Full SKILL.md document."),
                "source": nullable(_ID),
                "expected_incarnation": _EXPECTED_INCARNATION,
            },
            (*_SKILL_REQUIRED, "content"),
        ),
        ("skills",),
        "Replace a Skill document with nonhuman Extension provenance.",
    ),
    service(
        "skill.delete",
        "execute",
        params({**_SKILL, "expected_incarnation": _EXPECTED_INCARNATION}, _SKILL_REQUIRED),
        ("skills",),
        "Archive a Skill package through the canonical authoring service.",
    ),
    service(
        "skill.revert",
        "execute",
        params(
            {"scope": _SKILL_SCOPE, "revisions": {**array(integer(1)), "minItems": 1}},
            ("scope", "revisions"),
        ),
        ("skills",),
        "Revert selected Skill revisions atomically, rejecting conflicting later changes.",
    ),
    service(
        "skill.restore",
        "execute",
        params({"scope": _SKILL_SCOPE, "archive_id": _ID}, ("scope", "archive_id")),
        ("skills",),
        "Restore a Skill package from its writable home's archive.",
    ),
    service(
        "skill.purge",
        "execute",
        params({"scope": _SKILL_SCOPE, "archive_id": _ID}, ("scope", "archive_id")),
        ("skills",),
        "Permanently remove an archived Skill package.",
    ),
    service(
        "skill.install",
        "execute",
        params(
            {
                "scope": _SKILL_SCOPE,
                "source": _nonempty("Package URL or local source path."),
                "path": _ID,
                "ref": _ID,
                "replace": boolean(),
                "dry_run": boolean(),
                "return_choices": boolean(),
                "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            ("scope", "source"),
        ),
        ("skills",),
        "Inspect or install a Skill package while preserving its source document.",
    ),
    service(
        "skill.write_file",
        "execute",
        params(
            {
                **_SKILL,
                "path": _nonempty("Safe package-relative supporting-file path."),
                "content": string(),
            },
            (*_SKILL_REQUIRED, "path", "content"),
        ),
        ("skills",),
        "Write a UTF-8 supporting file in a Skill package.",
    ),
    service(
        "skill.remove_file",
        "execute",
        params(
            {**_SKILL, "path": _nonempty("Safe package-relative supporting-file path.")},
            (*_SKILL_REQUIRED, "path"),
        ),
        ("skills",),
        "Remove a supporting file from a Skill package.",
    ),
    service(
        "skill.set_disabled",
        "execute",
        params(
            {"id": _ID, "disabled": boolean(), "expected_incarnation": _EXPECTED_INCARNATION},
            ("id", "disabled"),
        ),
        ("skills", "projects"),
        "Enable or disable an exact inventoried Skill package.",
    ),
    service(
        "skill.share",
        "execute",
        {
            **params(
                {"agent_id": _ID, "name": _ID, "shared": boolean(), "receivers": _NAMES},
                ("agent_id", "name", "shared"),
            ),
            "if": {"properties": {"shared": {"const": True}}},
            "then": {"required": ["receivers"], "properties": {"receivers": {"minItems": 1}}},
        },
        ("skills",),
        "Share or unshare an Identity Agent's private Skill with selected Identity Agents.",
    ),
)

# These fields belong to the Accessor envelope, but must never be exposed by the
# Extension facade. Every exception is explicit so new shared fields become
# visible to Extensions without maintaining a second accepted-field list.
_CHAT_RPC_ONLY_FIELDS = {
    "new_session": (
        "Extensions submit SYSTEM input to existing Sessions; "
        "Accessors own human new-Session admission."
    ),
    "input_origin": (
        "Speech and live-voice provenance describes human Accessor input, "
        "not Extension SYSTEM input."
    ),
    "file_mentions": (
        "Accessor file mentions snapshot files into human input; "
        "Extensions supply explicit content."
    ),
    "reply_surface": (
        "Accessor response routing must not turn Extension SYSTEM input "
        "into a human command surface."
    ),
    "widget_states": (
        "Widget return state records a human interaction and cannot be claimed by an Extension."
    ),
    "ui_context": "Only the human Accessor can attach its current view to a specialist Session.",
    "expected_generation_id": "Binds Accessor input to the Session generation the user opened.",
    "input_id": (
        "Durable Accessor input receipts belong to human submission; "
        "Extension SYSTEM Runs use their own admission."
    ),
}
_AGENT_OPERATOR_FIELDS = dict.fromkeys(
    OPERATOR_AGENT_FIELDS,
    "Agent Permission Rules and Tool access are operator-owned configuration.",
)
_INLINE_RULE_GUARD = {
    "require_no_permission_profile": "Inline rule editors can require an unbound Agent.",
}
_REVIEWED_RULES = {
    "expected_profile_rules": "Reviewed permission changes require the approved effective rules.",
}
RPC_ONLY_FIELDS: dict[str, dict[str, str]] = {
    "agent.create": {**_AGENT_OPERATOR_FIELDS, **_REVIEWED_RULES},
    "agent.update": {**_AGENT_OPERATOR_FIELDS, **_INLINE_RULE_GUARD, **_REVIEWED_RULES},
    "project.set_override": {**_INLINE_RULE_GUARD, **_REVIEWED_RULES},
    "project.clear_override": _REVIEWED_RULES,
    "chat.send": dict(_CHAT_RPC_ONLY_FIELDS),
    "chat.stream": dict(_CHAT_RPC_ONLY_FIELDS),
    "session.create": {
        "execution": (
            "Computer execution grants require an authenticated human principal; "
            "Extensions cannot create grants."
        ),
    },
}

SERVICE_EXCLUSIONS = {
    "session.create_app_chat": "The WebUI starts a user-facing specialist conversation.",
    "session.create_side_chat": "The side-chat composer opens a fork for its first send.",
    "chat.cancel_tool_call": (
        "Tool cancellation sets human-stop authority and user-cancelled Tool results; "
        "Extensions use neutral Run cancellation instead."
    ),
    "skill.set_pinned": "Only a human writer may change Skill pin protection.",
    "session.set_execution": "Computer execution grants require an authenticated human principal.",
    "chat.edit": "Replaces human input and submits a human rerun; Extensions use SYSTEM input.",
    "chat.queue_update": "Edits human content while retaining human provenance; accessor-only.",
    "chat.stop_command": "Records a user stop and delivers a user-stop notice; accessor-only.",
}
