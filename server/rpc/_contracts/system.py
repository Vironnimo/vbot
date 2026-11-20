"""Explicit application, configuration and operational service declarations.

Each entry has been checked against its canonical RPC owner. The companion
exclusions are a deliberate contract, not a wildcard fallback for new methods.
"""

from core.model_tasks.constants import SUPPORTED_TASK_TYPES
from core.models.models import MODEL_TASK_ORDER
from core.performance import (
    MAX_HEAP_TOP,
    MAX_HISTORY_NAMES,
    MAX_HISTORY_WINDOWS,
    MAX_RECORDING_SECONDS,
)
from core.statistics import REPORT_SECTIONS

from .common import (
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
from .conversation import PERMISSION_RULES
from .settings import MODEL_TASKS, SETTINGS_PATCH, SETTINGS_UPDATE, TASK_OPTIONS

RPC_ONLY_FIELDS = {
    "provider.custom_save": {
        "api_key": "Provider credentials are configured through the operator credential surface.",
    },
    "model.refresh_db": {
        "expected_resources_dir": (
            "Packaged system catalogs are maintained by the serving checkout's operator."
        ),
    },
}

_STRING = string()
_ID = {**string(), "minLength": 1}
_STRINGS = array(_STRING)
_TIMESTAMP = {**string("ISO 8601 timestamp with an explicit UTC offset."), "format": "date-time"}
_CONNECTION = {"provider_id": _ID, "connection_id": _ID, "account": nullable(_ID)}
_MODEL_FILTER = nullable({"anyOf": [_STRING, _STRINGS]})
_CUSTOM_CAPABILITIES = obj(
    {
        "vision": boolean(),
        "tools": boolean(),
        "json_mode": boolean(),
        "reasoning": boolean(),
        "input_modalities": _STRINGS,
        "output_modalities": _STRINGS,
        "supported_parameters": _STRINGS,
        "supported_voices": _STRINGS,
        "task_types": array(enum(*MODEL_TASK_ORDER)),
        "task_options": {
            **obj(additional=True),
            "description": "Task-owned option definitions, validated by the Provider catalog.",
        },
    }
)
_CUSTOM_MODEL = obj(
    {
        "name": _ID,
        "context_window": nullable(integer(1)),
        "max_output_tokens": nullable(integer(1)),
        "capabilities": _CUSTOM_CAPABILITIES,
    }
)
_CUSTOM_PROVIDER = obj(
    {
        "id": {**_ID, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
        "name": _ID,
        "adapter": enum("openai_compatible"),
        "base_url": {
            **_ID,
            "format": "uri",
            "description": "HTTP(S) API base URL without credentials, query or fragment.",
        },
        "auth": enum("api_key", "none"),
        "models_endpoint": nullable(_ID),
        "defaults": {
            **obj(
                {
                    "max_tokens": integer(1),
                    "temperature": {"type": "number"},
                    "top_p": {"type": "number"},
                },
                additional=True,
            ),
            "description": "Adapter-owned request defaults; additional JSON fields are retained.",
        },
        "models": obj(additional=_CUSTOM_MODEL),
        "wire": nullable(
            {
                **obj(additional=True),
                "description": (
                    "Provider-owned wire profile; the canonical wire parser validates its "
                    "versioned format. Null removes the profile."
                ),
            }
        ),
    },
    ("id", "name", "adapter", "base_url"),
)
_PROMPT_SCOPE = nullable(
    {
        "oneOf": [
            obj({"type": enum("default"), "agent_id": enum(None, "")}),
            obj({"type": enum("agent"), "agent_id": _ID}, ("type", "agent_id")),
        ]
    }
)
_SCOPE = {"scope": _PROMPT_SCOPE}

SERVICE_DEFINITIONS: tuple[ServiceDefinition, ...] = (
    service("activity.list", "query", params(), (), "Read current background activity."),
    service(
        "activity.dismiss",
        "execute",
        params({"id": _ID}, ("id",)),
        (),
        "Dismiss a finished background activity for all clients.",
    ),
    service("application.changelog", "query", params(), (), "Read bundled stable release notes."),
    service(
        "application.update_status",
        "query",
        params(),
        ("application",),
        "Read the installation's update availability.",
    ),
    service(
        "client.list",
        "query",
        params(),
        ("clients",),
        "List connected app clients and their presence.",
    ),
    service(
        "computers.list",
        "query",
        params(),
        ("computers",),
        "Discover registered Computers and their public capabilities.",
    ),
    service(
        "connection.list",
        "query",
        params(),
        ("providers",),
        "List Provider Connections without credentials.",
    ),
    service(
        "connection.set_enabled",
        "execute",
        params(
            {"provider_id": _ID, "connection_id": _ID, "enabled": boolean()},
            ("provider_id", "connection_id", "enabled"),
        ),
        ("providers", "models"),
        "Enable or disable an existing Provider Connection.",
    ),
    service(
        "provider.custom_list",
        "query",
        params(),
        ("providers",),
        "List Custom Provider definitions and revision tokens, without API keys.",
    ),
    service(
        "provider.custom_save",
        "execute",
        params({"provider": _CUSTOM_PROVIDER, "expected_revision": _ID}, ("provider",)),
        ("providers", "models"),
        (
            "Create or replace a secret-free Custom Provider. Use expected_revision "
            "for optimistic concurrency; configure credentials through the "
            "operator's credential surface."
        ),
    ),
    service(
        "provider.custom_delete",
        "execute",
        params({"provider_id": _ID}, ("provider_id",)),
        ("providers", "models", "data_store"),
        "Delete a Custom Provider through its canonical owner, including its stored credential.",
    ),
    service(
        "provider.connection_status",
        "query",
        params(_CONNECTION, ("provider_id", "connection_id")),
        ("providers",),
        "Read a Connection's authentication status without returning credentials.",
    ),
    service(
        "provider.disconnect",
        "execute",
        params(_CONNECTION, ("provider_id", "connection_id")),
        ("providers",),
        "Disconnect a Provider account or disable an external Connection.",
    ),
    service(
        "provider.routing_options",
        "query",
        params({"provider_id": enum("openrouter"), "model_id": nullable(_ID)}, ("provider_id",)),
        ("providers", "models"),
        "Read available OpenRouter routing providers for all Models or one Model.",
    ),
    service(
        "provider.usage",
        "query",
        params({"connections": nullable(array(_ID))}),
        ("providers",),
        "Read selected Connections' current upstream usage windows and limits.",
    ),
    service(
        "provider.usage_history",
        "query",
        params(
            {
                "since": nullable(string("ISO 8601 timestamp; missing offset means UTC.")),
                "until": nullable(string("ISO 8601 timestamp; missing offset means UTC.")),
            }
        ),
        ("providers",),
        "Read retained Provider usage observations in an optional time window.",
    ),
    service(
        "provider.usage_history.clear",
        "execute",
        params(),
        ("providers",),
        "Delete retained Provider usage observations; Session statistics are unchanged.",
    ),
    service(
        "model.list",
        "query",
        params(
            {
                "provider_id": nullable(_STRING),
                **dict.fromkeys(
                    (
                        "capability",
                        "capabilities",
                        "task",
                        "tasks",
                        "task_type",
                        "task_types",
                        "input_modality",
                        "input_modalities",
                        "output_modality",
                        "output_modalities",
                        "query",
                    ),
                    _MODEL_FILTER,
                ),
                "min_context_window": nullable(integer(0)),
                "sort": enum("name", "usage"),
            }
        ),
        ("models", "providers"),
        (
            "Find usable Models by capability, task, modalities and text, ordered by "
            "name or recent usage."
        ),
    ),
    service(
        "model.get",
        "query",
        params({"model": _ID}, ("model",)),
        ("models", "providers"),
        "Read one Model's catalog facts and effective Connection wire profiles.",
    ),
    service(
        "model.favorites",
        "query",
        params(),
        ("model_favorites",),
        "Read the shared favorite Model ids.",
    ),
    service(
        "model.set_favorite",
        "execute",
        params({"model": _ID, "favorite": boolean()}, ("model", "favorite")),
        ("model_favorites",),
        "Set a Model's shared favorite state.",
    ),
    service(
        "model.forget_wire_facts",
        "execute",
        params({"model": _ID, "connection": nullable(_ID)}, ("model",)),
        ("models",),
        "Forget learned wire facts for a Provider or Model, optionally limited to one Connection.",
    ),
    service(
        "model.refresh_db",
        "execute",
        params({"provider_id": nullable(_ID), "target": enum("runtime")}),
        ("models",),
        (
            "Refresh the running application's Model catalog. Editing packaged "
            "system resources is not part of this service."
        ),
    ),
    service(
        "settings.values",
        "query",
        params(),
        ("settings",),
        "Read effective public Settings without raw or secret values.",
    ),
    service(
        "settings.get",
        "query",
        params(),
        ("settings",),
        "Read the Settings editor projection and available configuration choices.",
    ),
    service(
        "settings.catalog",
        "query",
        params({"prefix": nullable(_STRING)}),
        ("settings",),
        "Discover public Settings paths with their types, defaults and current values.",
    ),
    service(
        "settings.get_path",
        "query",
        params({"path": _ID, "allow_missing": boolean()}, ("path",)),
        ("settings",),
        "Read one public Settings path with its effective/configured value and provenance.",
    ),
    service(
        "settings.patch",
        "execute",
        SETTINGS_PATCH,
        ("settings",),
        (
            "Atomically set or unset public Settings paths; canonical validation and "
            "live apply effects are preserved."
        ),
    ),
    service(
        "settings.update",
        "execute",
        SETTINGS_UPDATE,
        ("settings",),
        (
            "Apply structured Settings sections with optional base values for "
            "compare-and-set. Individual section schemas describe sparse versus "
            "complete updates."
        ),
    ),
    service(
        "permissions.validate",
        "query",
        params(
            {
                "permission_rules": {
                    "description": (
                        "Candidate Permission Rules or null. Invalid candidates are accepted "
                        "intentionally: the result contains all editor diagnostics. "
                        "x-candidate-schema describes the valid shape."
                    ),
                    "x-candidate-schema": nullable(PERMISSION_RULES),
                },
                "agent_id": _ID,
                "new_agent": boolean(),
            },
            ("permission_rules",),
        ),
        ("settings", "agents"),
        (
            "Validate and preview Permission Rules without saving or granting "
            "access; semantic errors are returned as editor diagnostics."
        ),
    ),
    service(
        "permissions.session",
        "query",
        params(
            {"agent_id": _ID, "session_id": _ID, "working_project_id": nullable(_ID)}, ("agent_id",)
        ),
        ("sessions", "agents", "settings"),
        "Describe a Session's effective rules, inherited ceilings and stored grants.",
    ),
    service(
        "permissions.pending",
        "query",
        params(),
        (),
        "Read pending Tool approval requests; only the authenticated operator may answer them.",
    ),
    service(
        "statistics.report",
        "query",
        params(
            {
                "since": nullable(_TIMESTAMP),
                "until": nullable(_TIMESTAMP),
                "timezone": nullable(string("IANA timezone name.")),
                "sections": nullable({**array(enum(*REPORT_SECTIONS)), "minItems": 1}),
                "period": boolean(),
            }
        ),
        ("statistics",),
        (
            "Read selected Statistics report sections in a time window or the shared "
            "measurement period."
        ),
    ),
    service(
        "statistics.period",
        "query",
        params(),
        ("statistics",),
        "Read the shared measurement period.",
    ),
    service(
        "statistics.period.start",
        "execute",
        params(),
        ("statistics",),
        "Start a new shared measurement period without changing recorded usage.",
    ),
    service(
        "statistics.period.clear",
        "execute",
        params(),
        ("statistics",),
        "Clear the shared measurement start without deleting usage.",
    ),
    service(
        "statistics.run_activity",
        "query",
        params({"since": _TIMESTAMP, "until": _TIMESTAMP}, ("since", "until")),
        ("statistics",),
        "Read bounded Run activity overlapping a required time window.",
    ),
    service(
        "statistics.session_usage",
        "query",
        {
            **params(
                {
                    "agent_id": _ID,
                    "session_id": _ID,
                    "session_ids": {**array(_ID), "minItems": 1, "maxItems": 20},
                }
            ),
            "oneOf": [
                {"required": ["session_id"], "not": {"required": ["session_ids"]}},
                {"required": ["session_ids"], "not": {"required": ["session_id"]}},
            ],
        },
        ("statistics",),
        "Read a Session tree's usage, or select up to twenty Session trees by id.",
    ),
    service("task_model.settings", "query", params(), ("settings",), "Read Task Model bindings."),
    service(
        "task_model.update",
        "execute",
        params(
            {
                "model_tasks": MODEL_TASKS,
                "base": nullable(obj({"model_tasks": MODEL_TASKS}, ("model_tasks",))),
            },
            ("model_tasks",),
        ),
        ("settings",),
        "Update Task Model bindings through their owner, optionally checking previous bindings.",
    ),
    service(
        "task_model.list_targets",
        "query",
        params({"task_type": enum(*sorted(SUPPORTED_TASK_TYPES))}, ("task_type",)),
        ("models", "providers"),
        "Discover usable local and Provider Task Model targets.",
    ),
    service(
        "task_model.status",
        "query",
        params({"task_type": enum(*sorted(SUPPORTED_TASK_TYPES))}, ("task_type",)),
        ("settings", "models"),
        "Read whether one bound Task Model is configured and usable.",
    ),
    service(
        "task_model.options",
        "query",
        params(
            {"task_type": enum(*sorted(SUPPORTED_TASK_TYPES)), "target": nullable(_ID)},
            ("task_type",),
        ),
        ("settings", "models"),
        "Discover a Task Model target's complete option schema, defaults and effective values.",
    ),
    service(
        "task_model.patch_options",
        "execute",
        params(
            {
                "task_type": enum(*sorted(SUPPORTED_TASK_TYPES)),
                "set": TASK_OPTIONS,
                "unset": array(_ID),
            },
            ("task_type",),
        ),
        ("settings",),
        "Set or unset target-owned options; overlapping or empty changes are rejected.",
    ),
    service(
        "task_model.local_setup_status",
        "query",
        params({"target": {**_ID, "pattern": "^local/"}}, ("target",)),
        (),
        "Read one local Task Model engine's installation state.",
    ),
    service(
        "task_model.local_setup_install",
        "execute",
        params({"target": {**_ID, "pattern": "^local/"}}, ("target",)),
        (),
        "Start or join the fixed installation of one local Task Model engine.",
    ),
    service(
        "task_model.local_setup_cancel",
        "execute",
        params({"target": {**_ID, "pattern": "^local/"}}, ("target",)),
        (),
        "Cancel a local Task Model engine installation while retaining completed downloads.",
    ),
    service(
        "task_model.local_memory_status",
        "query",
        params(),
        (),
        "Read loaded local Task Models and memory use.",
    ),
    service(
        "task_model.local_unload",
        "execute",
        params({"target": {**_ID, "pattern": "^local/"}}, ("target",)),
        (),
        "Unload a local Task Model when its owner can release it.",
    ),
    service(
        "speech.local_memory_status",
        "query",
        params(),
        (),
        "Read loaded local Speech Models and memory use.",
    ),
    service(
        "speech.local_unload",
        "execute",
        params({"target": _ID}, ("target",)),
        (),
        "Unload one local Speech Model.",
    ),
    service(
        "speech.prepare_transcription",
        "execute",
        params(),
        (),
        "Prepare the bound local speech-to-text Model for an upcoming transcription.",
    ),
    service(
        "recall.status", "query", params(), (), "Read semantic Recall indexing status and coverage."
    ),
    service(
        "recall.rebuild_index",
        "execute",
        params(),
        (),
        "Rebuild the current embedding-space index from canonical passages.",
    ),
    service(
        "live.status",
        "query",
        params(),
        ("settings",),
        "Read live voice binding readiness without starting an Accessor media session.",
    ),
    service(
        "filesystem.list",
        "query",
        params(
            {
                "path": nullable(_STRING),
                "root": _ID,
                "include_files": boolean(),
                "prefix": nullable(_STRING),
            }
        ),
        (),
        "List a server directory or root locations; root confines relative traversal.",
    ),
    service(
        "filesystem.create_directory",
        "execute",
        params({"path": _STRING, "name": _ID, "root": _ID}, ("path", "name")),
        (),
        "Create one directory without modifying an existing entry.",
    ),
    service(
        "filesystem.create_file",
        "execute",
        params({"path": _STRING, "name": _ID, "root": _ID}, ("path", "name")),
        (),
        "Create one empty file without modifying an existing entry.",
    ),
    service(
        "files.list",
        "query",
        params(
            {
                "agent_id": _ID,
                "session_id": _ID,
                "working_project_id": nullable(_ID),
                "directory": nullable(_STRING),
            },
            ("agent_id",),
        ),
        (),
        "Read file-mention candidates for an Agent's Session or draft Working Project.",
    ),
    service(
        "prompt.list",
        "query",
        params(_SCOPE),
        (),
        "Read System Prompt block definitions, content and available scopes.",
    ),
    service(
        "prompt.preview",
        "query",
        params(
            {
                **_SCOPE,
                "agent_id": _ID,
                "session_id": _ID,
                "working_project_id": nullable(_ID),
                "include_tools": boolean(),
            }
        ),
        (),
        (
            "Render an Agent's effective System Prompt and optionally Tool "
            "definitions; agent_id is required unless scope selects an Agent."
        ),
    ),
    service(
        "prompt.update",
        "execute",
        params({**_SCOPE, "id": _ID, "content": _STRING}, ("id", "content")),
        (),
        "Replace one System Prompt block through the live prompt owner.",
    ),
    service(
        "prompt.reset",
        "execute",
        params({**_SCOPE, "id": _ID}, ("id",)),
        (),
        "Reset one System Prompt block to its built-in content.",
    ),
    service(
        "prompt.set_layout",
        "execute",
        params(
            {
                **_SCOPE,
                "layout": array(
                    obj({"id": _ID, "enabled": boolean(), "source": nullable(_STRING)}, ("id",))
                ),
            },
            ("layout",),
        ),
        (),
        "Set ordered System Prompt blocks and their enabled states.",
    ),
    service(
        "prompt.create_block",
        "execute",
        params(
            {**_SCOPE, "slug": _ID, "content": nullable(_STRING), "position": nullable(integer(0))},
            ("slug",),
        ),
        (),
        "Create a custom System Prompt block at an optional zero-based position.",
    ),
    service(
        "prompt.remove_block",
        "execute",
        params({**_SCOPE, "id": _ID}, ("id",)),
        (),
        "Remove a custom System Prompt block.",
    ),
    service(
        "prompt.reset_layout",
        "execute",
        params(_SCOPE),
        (),
        "Restore the built-in System Prompt block layout.",
    ),
    service(
        "terminal.list",
        "query",
        params(),
        ("terminals",),
        "Read Terminal Sessions, groups and launch history.",
    ),
    service(
        "terminal.programs",
        "query",
        params(),
        ("terminals",),
        "Read running programs of Terminal Sessions.",
    ),
    service(
        "terminal.read",
        "query",
        params({"terminal_id": _ID}, ("terminal_id",)),
        ("terminals",),
        "Read a Terminal Session's bounded screen snapshot.",
    ),
    service(
        "terminal.group.create",
        "execute",
        params({"name": _ID}, ("name",)),
        ("terminals",),
        "Create a Terminal group.",
    ),
    service(
        "terminal.group.rename",
        "execute",
        params({"group_id": _ID, "name": _ID}, ("group_id", "name")),
        ("terminals",),
        "Rename a Terminal group.",
    ),
    service(
        "terminal.group.delete",
        "execute",
        params({"group_id": _ID}, ("group_id",)),
        ("terminals",),
        "Delete a Terminal group, preserving its Terminal Sessions.",
    ),
    service(
        "terminal.group.order",
        "execute",
        params({"group_id": _ID, "order": array(_ID)}, ("group_id", "order")),
        ("terminals",),
        "Set the order of Terminal Sessions in a group.",
    ),
    service(
        "terminal.move",
        "execute",
        params({"terminal_id": _ID, "group_id": nullable(_ID)}, ("terminal_id", "group_id")),
        ("terminals",),
        "Move a Terminal Session into a group or unpin it with null.",
    ),
    service(
        "terminal.kill",
        "execute",
        params({"terminal_id": _ID}, ("terminal_id",)),
        ("terminals",),
        "Terminate a Terminal Session through its owner.",
    ),
    service(
        "terminal.forget",
        "execute",
        params({"terminal_id": _ID}, ("terminal_id",)),
        ("terminals",),
        "Forget a completed Terminal Session.",
    ),
    service(
        "portal.close",
        "execute",
        params({"id": _ID}, ("id",)),
        ("portals",),
        "Close an existing Portal without issuing a browser access capability.",
    ),
    service("log.list", "query", params(), (), "List available application log files."),
    service(
        "log.read",
        "query",
        params({"file": _ID, "before": integer(1)}, ("file",)),
        (),
        "Read a bounded page from an application log.",
    ),
    service(
        "debug.status",
        "query",
        params(),
        ("settings", "debug_traces"),
        "Read diagnostic recording state.",
    ),
    service(
        "debug.trace_list",
        "query",
        params(),
        ("debug_traces",),
        "List sanitized Model trace metadata when debug mode is enabled.",
    ),
    service(
        "debug.trace_get",
        "query",
        params({"trace_id": _ID}, ("trace_id",)),
        ("debug_traces",),
        "Read one sanitized Model trace when debug mode is enabled.",
    ),
    service(
        "debug.trace_clear", "execute", params(), ("debug_traces",), "Delete retained Model traces."
    ),
    service(
        "debug.speech_clear",
        "execute",
        params(),
        ("debug_traces",),
        "Delete retained Speech recordings.",
    ),
    service(
        "debug.model_probe",
        "execute",
        params({"provider_id": _ID, "connection_id": _ID}, ("provider_id", "connection_id")),
        ("debug_traces",),
        (
            "Probe a Connection's catalog endpoint and record a sanitized diagnostic "
            "trace when debug is enabled."
        ),
    ),
    service(
        "performance.snapshot",
        "query",
        params(),
        (),
        "Read runtime performance gauges, histograms and Event Loop stalls.",
    ),
    service(
        "performance.history",
        "query",
        params(
            {
                "since": _TIMESTAMP,
                "until": _TIMESTAMP,
                "limit": integer(1, MAX_HISTORY_WINDOWS),
                "names": {**array(_ID), "maxItems": MAX_HISTORY_NAMES},
            }
        ),
        (),
        "Read bounded retained performance summaries.",
    ),
    service(
        "performance.heap",
        "query",
        params({"top": integer(1, MAX_HEAP_TOP)}),
        (),
        "Read a bounded heap type census, without object contents.",
    ),
    service(
        "performance.recording_list",
        "query",
        params({"limit": integer(1, 100)}),
        (),
        "List retained performance recordings.",
    ),
    service(
        "performance.recording_start",
        "execute",
        params({"label": _ID, "max_seconds": integer(1, MAX_RECORDING_SECONDS)}),
        (),
        "Start the shared bounded performance recording.",
    ),
    service(
        "performance.recording_stop",
        "execute",
        params(),
        (),
        "Stop and persist the shared performance recording.",
    ),
    service(
        "data_store.status",
        "query",
        params({"verify_search": boolean()}),
        ("data_store",),
        "Read canonical database and snapshot health, optionally checking the search projection.",
    ),
    service(
        "data_store.snapshot_create",
        "execute",
        params({"reason": enum("manual", "rpc", "update", "recovery")}),
        ("data_store",),
        "Create a consistent data snapshot through the shared snapshot barrier.",
    ),
    service(
        "data_store.incident_acknowledge",
        "execute",
        params({"incident_id": _ID}, ("incident_id",)),
        ("data_store",),
        "Acknowledge a diagnosed recovery incident.",
    ),
    service(
        "data_store.unregister",
        "execute",
        params({"name": _ID}, ("name",)),
        ("data_store",),
        "Unregister an inactive Extension database with canonical retirement safeguards.",
    ),
    service(
        "managed_service.get",
        "query",
        params({"id": enum("searxng")}, ("id",)),
        (),
        "Read the installation-owned search service's state and available actions.",
    ),
    service(
        "managed_service.apply",
        "execute",
        params(
            {
                "id": enum("searxng"),
                "action": _ID,
                "plan_id": nullable(_STRING),
                "allow_prerequisites": boolean(),
            },
            ("id", "action"),
        ),
        (),
        (
            "Apply an action advertised by managed_service.get, preserving "
            "installation plan admission and Settings conflict checks."
        ),
    ),
    service(
        "setup.get",
        "query",
        params(),
        ("setup",),
        "Read shared first-setup progress, observing completed configuration.",
    ),
    service(
        "setup.update",
        "execute",
        params({"step": enum("provider", "model", "components", "services")}, ("step",)),
        ("setup",),
        "Record the current first-setup step.",
    ),
    service("setup.finish", "execute", params(), ("setup",), "Mark shared first setup complete."),
    service(
        "extensions.list",
        "query",
        params(),
        ("extensions",),
        "Discover Extension state, public configuration and capability diagnostics.",
    ),
    service(
        "extensions.reload",
        "execute",
        params(),
        ("extensions", "skills", "commands"),
        (
            "Reload the Extension layer; a successfully committed mutation keeps its "
            "result even if this owner retires."
        ),
    ),
)

SERVICE_EXCLUSIONS = {
    "auth.login_ticket": (
        "Issues an authentication capability for a signed-in Device; requires "
        "that request's principal."
    ),
    "devices.list": "Device administration requires a signed-in request principal.",
    "devices.rename": "Device administration requires a signed-in request principal.",
    "devices.revoke": "Revoking a Device requires a signed-in request principal.",
    "devices.revoke_others": "The identity of the Device to keep comes from the signed-in request.",
    "devices.logout": "Logs out the calling Device; an Extension has no Device identity.",
    "devices.pending": "Pairing inspection requires a signed-in request principal.",
    "devices.approve": "Pairing approval is an authenticated operator decision.",
    "devices.deny": "Pairing denial is an authenticated operator decision.",
    "computers.remove": "Computer trust removal requires an authenticated operator principal.",
    "application.update_continuation": (
        "Private installation control protocol, authorized by a control token "
        "and durable handoff ticket."
    ),
    "application.update_handoff_mint": (
        "Private update handoff bound to a live Bash Tool-call token."
    ),
    "provider.connect": (
        "Starts an interactive account authentication flow and returns a login "
        "code; use the operator authentication surface."
    ),
    "provider.set_key": "Writes global Provider credentials; use the operator credential surface.",
    "provider.unset_key": (
        "Removes global Provider credentials; use the operator credential surface."
    ),
    "secrets.list": (
        "Global credential-store administration is separate from public product Services."
    ),
    "secrets.set": "Writes global environment credentials; use the operator credential surface.",
    "secrets.unset": "Removes global environment credentials; use the operator credential surface.",
    "agent_secrets.list": (
        "Agent Secret management includes operator-owned grants and pending "
        "requests, separate from integration data."
    ),
    "agent_secrets.create": "Creates an operator-owned Secret and its Agent access policy.",
    "agent_secrets.update": "Changes an operator-owned Secret or its Agent access policy.",
    "agent_secrets.delete": "Deletes an operator-owned Secret and its grants.",
    "agent_secrets.reveal": "Reveals Secret material to the operator.",
    "agent_secrets.answer": "Answers a human Secret request and grants the requesting Run access.",
    "agent_secrets.give": "Grants Secret material to an Agent as a human decision.",
    "agent_secrets.decline": "Resolves an operator's pending Secret request.",
    "agent_secrets.source_entries": "Browses an operator-owned credential source.",
    "agent_secrets.source_lock": "Controls an operator-owned credential source's locked state.",
    "agent_secrets.source_unlock": "Unlocks an operator-owned credential source with secret input.",
    "settings.get_raw": (
        "Raw persisted Settings bypass the public, secret-free Settings "
        "contract; use settings.values or settings.get_path."
    ),
    "settings.set_service_key": (
        "Writes global web-service credentials; use the operator credential surface."
    ),
    "permissions.resolve": "Tool approval resolution represents a human consent decision.",
    "skill.install_cli": "Private remote Skill install bound to one live shell call.",
    "permissions.review_cli": "Private CLI authorization bound to one live shell call.",
    "permissions.set_session_rules": (
        "Widening or replacing a Session's rules requires an authenticated operator principal."
    ),
    "permissions.remove_session_grants": (
        "Editing a user's Session Grants requires an authenticated operator principal."
    ),
    "permissions.profiles": "The profile picker and its rules are served through operator RPCs.",
    "permissions.save_profile": "Only the operator can define or change Agent permission profiles.",
    "permissions.delete_profile": "Only the operator can delete Agent permission profiles.",
    "permissions.clear_session_ceiling": (
        "Lifting inherited limits requires an authenticated operator principal."
    ),
    "file.preview_open": (
        "Mints a browser file-delivery capability bound to the requesting principal."
    ),
    "file.preview_revision": (
        "Consumes a browser file-delivery capability; not a transport-independent file API."
    ),
    "portal.list": (
        "Includes browser Portal capabilities bound to the requesting principal; "
        "portal.close remains public."
    ),
    "terminal.start": (
        "Launches an operator Terminal with operator execution authority; "
        "Extension execution uses its declared capabilities."
    ),
    "terminal.input": (
        "Injects operator input into a Terminal; exposing it would falsely "
        "attribute an Extension action to the operator."
    ),
    "live.start": (
        "Starts an Accessor-owned media session whose stream requires a live owner socket."
    ),
    "live.stop": "Controls an Accessor-owned media session; use its owner transport.",
    "voice.wakeword_command": "Dispatches a spoken Desktop wakeword command on the user's behalf.",
    "live.ui_result": (
        "Answers a specific Accessor's live UI request and must remain bound to that owner."
    ),
    "speech.local_setup_restart": (
        "Restarts the server as the operator's installation follow-up; not a product data mutation."
    ),
    "performance.client_report": (
        "Accepts measurements attributed specifically to the WebUI transport; "
        "Extensions must not impersonate it."
    ),
    "debug.speech_list": (
        "Returns authenticated audio-delivery URLs tied to the HTTP Accessor, "
        "not portable recording data."
    ),
    "extensions.set_secret": (
        "Writes an Extension's declared secret through the operator credential surface."
    ),
    "extensions.operation": (
        "Legacy management-operation transport; cross-Extension calls use "
        "explicitly exported Services."
    ),
    "extensions.contribution_operation": (
        "UI contribution dispatch is bound to its registration epoch and "
        "declared operations; cross-Extension calls use Services."
    ),
    "extensions.contribution_sources": (
        "Batched browser source reads are registration-bound presentation transport; "
        "cross-Extension calls use explicitly exported Services."
    ),
    "extensions.contributions": (
        "Projects browser Pages with principal-bound asset capabilities; query "
        "declared Services for portable data."
    ),
    "extensions.pages": "Projects browser Pages with principal-bound asset capabilities.",
    "extensions.requests": (
        "Extension interaction requests belong to the management/interaction transport."
    ),
    "extensions.page_service": (
        "Page-to-Service bridge checks that Page's registration and allowlist; "
        "backend Extensions call Services directly."
    ),
    "extensions.page_run": (
        "Page-owned temporary Run projection belongs to its registration-bound browser bridge."
    ),
    "extensions.page_history": (
        "Page-owned temporary Session history belongs to its registration-bound browser bridge."
    ),
    "extensions.page_session": "The registered page resolves only its own participant Sessions.",
    "extensions.page_open_specialist": "Starts a chat with the current page's registered profile.",
    "extensions.page_tool_call": (
        "Page-owned Tool-call projection belongs to its registration-bound browser bridge."
    ),
    "extensions.page_timeline_children": (
        "Page-owned timeline projection belongs to its registration-bound browser bridge."
    ),
    "extensions.page_cancel_tool": (
        "Page-owned Tool cancellation belongs to its registration-bound browser bridge."
    ),
}
