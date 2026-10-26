# Writing vBot Extensions

An Extension is Python code that runs inside vBot's process and adds hooks, Commands, Tools, prompt blocks, settings, pages and more, all declared through one `register(api)` function. Extensions run with the full rights of vBot itself, without a sandbox: install only code you would run by hand.

This covers creating and changing an Extension. Settings and enable/disable of an installed one: [configuration.md](configuration.md#extensions); MCP: [mcp.md](mcp.md); Swarm and Computer Use: [extension-usage.md](extension-usage.md); a page or Extension-owned Sessions: [extension-pages.md](extension-pages.md) after this file.

Paths beginning with `core/`, `resources/` or `webui/` are relative to the `vbot_root` that `vbot home` prints on the server's computer.

Contents: [Build and verify](#build-and-verify), [Install](#install-and-reload), [register(api)](#the-entry-point-registerapi), [Hooks](#hooks), [Tools](#tools), [Commands](#slash-commands), [Recall backends](#recall-backends), [Channel buttons](#channel-interaction-handlers), [Settings](#settings-schema), [Prompt blocks](#prompt-blocks), [Lifecycle](#lifecycle-startup-and-shutdown), [Config, logging, secrets](#config-and-logging), [Manifest and API versions](#manifest-extensionjson), [Managed operations](#managed-operations), [Databases](#extension-databases), [Tool result payloads](#tool-result-payloads), [Tool summary](#tool-summary), [Tool changes in a Session](#tool-changes-in-a-session), [Explicit Tool permission](#explicit-tool-permission). Read only the sections the task needs.

## Build and verify

1. Run `vbot home` on the server's computer and `vbot extensions list`. Done when you know the `data_dir` and the installed Extensions. When you manage a remote server, keep the same `--host`/`--port` or `--url` on every later call; a local `vbot home` does not describe it.
2. Pick the template below that fits, read it, and copy only that file or directory into `<data_dir>/extensions/`, nested files included. Read a template with the `skill` Tool (`name` `vbot-docs`, `file_path` such as `assets/extensions/word_count.py`), or copy it from `<vbot_root>/resources/skills/vbot-docs/assets/extensions/`. Give a new Extension a name no other Extension uses; keep the name when you update one. Install only the Extension the user asked for.
3. Declare only the capabilities you need in `register(api)`. Acquire resources in startup and release them in shutdown. Put a workflow into a bundled Skill, standing instructions for the Model into a prompt block, and callable behavior into a Tool.
4. Run `vbot extensions reload`, then set declared fields with `vbot extensions set <name> <field> ...`, secrets through `--stdin`. Done when `vbot extensions list` shows the Extension loaded with the intended capabilities and no diagnostics. A failure in any other Extension also makes `reload` fail.
5. Test the real behavior and its failures in a disposable context: a Tool with valid and invalid input through the real Tool registry, checking its result envelope; a hook with affected and unaffected calls; a Command with its outcome and follow-up Run. Loading alone proves no behavior.
6. Before a live call, check the intended Agent's access: its Tool policy, a Project's Tool ceiling, explicit opt-in grants and whether a bundled Skill is allowed. When the Extension owns resources or background work, check that reload and disable clean them up.

| Template | Use when | Read/copy |
|---|---|---|
| Word count | Adding a parallel-safe Tool with input validation and a success-data schema | [word_count.py](../assets/extensions/word_count.py) |
| Bash guard | Intercepting Tool calls with `Deny` and a System Reminder; illustrative, not a security boundary | [guard_bash.py](../assets/extensions/guard_bash.py) |
| Workflow Command | Starting a Skill as a follow-up Run in the same Agent/Session/Project address | [workflow_command/](../assets/extensions/workflow_command/) |

## Install and reload

Extensions live in the data directory's `extensions/` folder; the Settings path `extensions.directories` adds further folders. vBot scans only the direct children of each folder, and an Extension's file or directory name is its identity everywhere: settings, CLI and WebUI.

| Shape | Layout | Manifest |
|---|---|---|
| Single file | `<root>/<name>.py` | none |
| Package | `<root>/<name>/__init__.py` | optional |
| Directory | `<root>/<name>/extension.py` | optional |

Nothing needs a server restart. Config values and secrets are read on every call. Code, added or removed Extensions and enabling go through a reload, `vbot extensions reload`: it rebuilds every Extension from disk, the same end state as a fresh server start, and cycles every loaded Extension through shutdown and startup. Disabling stops only that Extension. A disabled Extension is not imported. A failing Extension loads as `failed` with its error and never stops the others.

## The entry point: `register(api)`

Every Extension exposes one `register(api)`, synchronous or asynchronous; an asynchronous one must finish within 10 seconds.

```python
def register(api):
    api.on("tool_call", my_hook)
    api.register_tool("word_count", "Count words.", PARAMETERS, my_tool)
```

`register` and the module import only declare: no connections, files or tasks there. vBot applies the declarations at the right moment and never lets Extensions touch its registries directly.

| Call | Declares |
|---|---|
| `api.on(event, handler)` | a hook handler for one event |
| `api.register_command(name, description, handler, *, argument="optional", catalog_result="notice", execution_mode="serialized", argument_execution_mode=None, unavailable_surfaces=(), argument_hint="")` | a slash Command |
| `api.register_tool_family(family_id, label)` | a Tool Family owned by this Extension |
| `api.register_tool(name, description, parameters, handler, *, summary=None, internal=False, catalog_visible=True, requires_opt_in=False, display=None, ready=None, readiness_hint=None, result_schema=None, parallel_safe=True, open_input_schema=False, unadvertised_parameters=None, argument_normalizer=None, family=None, announce_changes=())` | an Agent Tool |
| `api.register_page(page_id, title, entry, *, icon="network")` | an Extension page ([extension-pages.md](extension-pages.md)) |
| `api.register_recall_backend(name, factory)` | a Session recall backend |
| `api.register_prompt_block(slug, *, default_text=None, render=None, requires_tool=None)` | a System Prompt block |
| `api.register_interaction_handler(prefix, handler)` | a Channel button handler |
| `api.register_settings(fields)` | a settings schema |
| `api.on_startup(handler)` / `api.on_shutdown(handler)` | a lifecycle callback (sync or async, no arguments) |
| `api.config` | the config at register time, from `settings.extensions.config.<name>` (default `{}`) |
| `api.get_config()` | the current config, read on every call |
| `api.resolve_credential(key)` | a credential, read on every call: process environment first, then the Secret Store |
| `api.logger` | the `vbot.extensions.<name>` logger |

## Hooks

`api.on(event, handler)` calls `handler(ctx, **payload)`, synchronous or asynchronous. A handler that raises is logged and skipped; it never stops the Run. `ctx` is a `HookContext` with `session_id`, `agent_id`, `run_id` and `add_note(text)`, which adds a System Reminder for the Model.

| Event | Composition | Payload (after `ctx`) | Return |
|---|---|---|---|
| `run_start` | observer | `session_id, agent_id` | ignored |
| `context` | pipeline | `messages` | a `list` that replaces the messages, or `None` |
| `tool_call` | decision pipeline | `tool_name, tool_call_id, input` | `None` / `Modify` / `Deny` / `Replace` |
| `tool_result` | replace pipeline | `tool_name, tool_call_id, input, result` | a complete replacement result envelope, or `None` |
| `run_end` | observer | `session_id, agent_id, outcome` | ignored |

`outcome` is `"success"`, `"error"` or `"cancelled"`; `run_end` always fires. Standing System Prompt text is a [prompt block](#prompt-blocks), not a hook; use the `context` hook only for changes that depend on the current messages.

```python
from core.extensions import Deny, Modify, Replace


def guard(ctx, *, tool_name, tool_call_id, input):
    if tool_name != "bash":
        return None  # proceed unchanged
    if "rm -rf /" in input.get("command", ""):
        ctx.add_note("guard blocked a destructive command.")
        return Deny(reason="Refused: destructive command.")
    return None
```

- `None` proceeds unchanged.
- `Modify(input)` replaces the arguments and continues the pipeline.
- `Deny(reason)` stops it; the Tool does not run, and the Agent receives a `tool_call_denied` failure naming your Extension.
- `Replace(result)` stops it and skips the Tool; `result` must be a valid envelope from `tool_success` or `tool_failure`, or it is dropped.

A `tool_result` handler returns a complete replacement envelope, checked against the envelope and the Tool's success-data schema, or `None`; there is no patching.

## Tools

An Extension Tool is a normal Tool: it appears in the Model's Tool definitions, follows the Agent's Tool policy, and its handler `(context, arguments)` returns the same envelope as built-in Tools: `tool_success(data)` or `tool_failure(code, message)` from `core.tools`, both `{ok, error, data, artifacts}`. Keep the description short and sufficient for correct use; it costs Model context on every request.

- vBot compiles the input schema at registration. Before validation it fixes common unambiguous Model encodings in a copy of the arguments; `argument_normalizer` adds your own rewrite of the raw arguments, such as aliases. It then validates successful `data` against `result_schema`.
- The root `properties` are the complete parameter list: vBot refuses unknown top-level arguments before the handler and drops unknown ones whose value is null or empty, so the handler checks only conditional rules, such as the fields one action requires. A schema accepts undeclared arguments only through `additionalProperties` or `patternProperties`. Fixed-shape objects declare `additionalProperties: false`, unless `open_input_schema=True` sends a schema without it to the Model.
- `unadvertised_parameters` maps extra root parameters the handler accepts to their schemas without showing them to the Model, such as options from older conversation history.
- Sibling calls run in parallel; set `parallel_safe=False` only when calls of the Tool must not overlap.
- `ready` is a cheap, I/O-free predicate; while it returns false, the Tool stays registered but hidden from Agents, for example until its credential is set. `readiness_hint` explains the precondition.
- `catalog_visible=False` hides a Tool from Tool catalogs, capability lists and Project Tool selection. It grants nothing: ordinary policy still applies.
- A Tool name that collides with a built-in or an earlier loaded Extension's Tool is skipped with a diagnostic in `vbot extensions list`; so is an invalid Tool contract. The Extension's other capabilities stay active.
- When two or more Tools form one capability the user recognizes, declare a Family with `api.register_tool_family(family_id, label)` and pass `family=` on each Tool. Family ids are local to the Extension. A Family only groups the Tools in configuration screens; it grants nothing.

```python
def register(api):
    api.register_tool_family("weather", "Weather")
    api.register_tool(
        "weather_now", "Read current weather.", NOW_PARAMETERS, read_now, family="weather"
    )
    api.register_tool(
        "weather_forecast", "Read a forecast.", FORECAST_PARAMETERS, read_forecast, family="weather"
    )
```

The [word-count template](../assets/extensions/word_count.py) shows an open input schema, the handler's own unknown-field check and a closed success-data schema. To teach an Agent a workflow, write a Skill instead of a Tool.

## Slash commands

`api.register_command` adds a Command to the same dispatcher as the Built-in Commands, for Chat, Channels, RPC, `/help` and WebUI autocomplete. A Command returns a result at once, reports a state change, or starts a follow-up Run, for example one that activates a Skill bundled with the Extension.

```python
from core.chat import CommandFeedback, CommandOutcome, CommandRun


async def start_workflow(context, argument):
    prompt = "$workflow"
    if argument:
        prompt = f"{prompt}\n\nUser objective:\n{argument}"
    run = await context.start_run(prompt)
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="notice", text="Workflow started."),
        runs=(CommandRun(role="follow_up", run=run),),
    )


def register(api):
    api.register_command(
        "workflow",
        "Start the bundled workflow Skill.",
        start_workflow,
        argument="optional",
        argument_hint="optional: what the workflow should achieve",
    )
```

`argument_hint` (API 20) tells how to use the command, in plain words rather than usage notation; the WebUI shows it greyed after the typed command, when the user already chose the command, while the description says what it does in the `/` list.

The handler, synchronous or asynchronous, receives `(context, argument)`: `argument` is the trimmed text after the Command, or `None`. It returns a `CommandOutcome` whose `command` equals the declared name. `ExtensionCommandContext` offers `agent_id`, `session_id`, `project_id`, `reply_surface`, `report_change(...)` and `await start_run(content, *, internal=False)`, which starts the Run at the same address and reply surface.

| Option | Values and meaning |
|---|---|
| `argument` | `"none"`, `"optional"` or `"required"`; controls recognition and autocomplete |
| `catalog_result` | `"notice"`, `"detail"` or `"state_change"`; how clients show the result |
| `execution_mode` | `"immediate"` or `"serialized"`; serialized Commands wait behind active Session work |
| `argument_execution_mode` | a different mode when an argument is present |
| `unavailable_surfaces` | any of `"webui"` and `"channel"` |

Names are lowercase, without `/`, of letters, digits, hyphens and underscores, starting with a letter or digit. Built-in Commands win collisions, then the earlier loaded Extension; a skipped declaration becomes a diagnostic. A Skill of the same name stays reachable as `$skill-name`. A Command removed or replaced while a call waits in the Queue answers that it is no longer available. A handler that raises or returns an invalid outcome is logged and answers with a neutral failure.

Bundle each Skill as `<extension>/skills/<skill-name>/SKILL.md` with its helper files. Loaded Extensions add their Skills to the global Skill pool; an Agent still needs them allowed, and a Project needs the global Skill in its selection. A user's global Skill of the same name wins. `$workflow` does not bypass the Skill policy. The [workflow_command/](../assets/extensions/workflow_command/) template declares API v2, bundles the `workflow` Skill and starts it as a follow-up Run.

## Recall backends

`api.register_recall_backend(name, factory)` adds a Session recall backend; `factory` takes a `RecallBackendContext` and returns a `RecallBackend`. The name is lowercase snake_case and no built-in name; the user selects it with the Settings path `recall.backend`. Implement `search_capabilities() -> RecallSearchCapabilities` and `search_page(request: RecallSearchRequest) -> RecallSearchPage`: declare the supported controls and result unit, apply the request's scope, excluded Sessions, roles and time range before ranking, and return canonical Message or Passage references with deterministic pagination and snapshot validation. Prefer an async `search_page`, so I/O can be cancelled. Read the protocol in `core/recall/recall.py` before you implement one.

## Channel interaction handlers

A Channel message can carry buttons. `api.register_interaction_handler(prefix, handler)` handles taps on buttons whose callback data is `"<prefix>:<payload>"`, in vBot's process without a Model call. The prefix contains no `:`, and the first Extension to claim a prefix owns it. `run` and `chk` are reserved by vBot.

```python
from core.extensions import InteractionButton


async def _toggle(event, responder):
    # The tapped message's keyboard arrives with every tap; rebuild and edit it.
    new_rows = [
        [
            InteractionButton(label=flip(b.label) if b.data == event.data else b.label, data=b.data)
            for b in row
        ]
        for row in event.buttons
    ]
    await responder.edit(buttons=new_rows)
    await responder.answer()  # stop the tapper's spinner (empty = silent ack)


def register(api):
    api.register_interaction_handler("pick", _toggle)
```

- `event` (`InteractionEvent`) carries `platform`, `channel_id`, `chat_id`, `user_id`, `message_id`, `data` (the tapped payload), `buttons` (the message's current keyboard as rows of `InteractionButton`) and optionally `text`, `user_display_name` and `thread_id`. vBot stores no button state; compute the edit from the event.
- `responder.answer(text=None, *, alert=False)` acknowledges the tap: no text silently, a text as a toast, or as a dialog with `alert=True`. `responder.edit(*, text=None, buttons=None)` rewrites the message. Without `answer()`, and after a handler error, which is logged, vBot acknowledges the tap itself.
- Send buttons with the `channel_send` Tool's `buttons` parameter (rows of `{label, data}`) or an adapter's `send(..., buttons=...)`. Only Telegram supports buttons; callback data holds at most 64 bytes. For a list the user ticks and submits, `channel_send` has a `checklist` parameter that needs no Extension.
- A `run:<payload>` button wakes the Agent instead of an Extension. Sent through `channel_send` from an Identity Agent Session, its tap starts at most one Run in that Session, the reply goes to Telegram, and later Telegram messages continue there until `/new`; when that Session was deleted, the tap reports it unavailable. From a Project Session, the tap goes to the Channel's current Identity Session. Emit only `run:<payload>`; never build the value vBot puts on the Telegram message.

## Settings schema

Declare user-editable configuration once with `api.register_settings(fields)`; the WebUI then shows a form under Settings > Extensions, and single-file Extensions can use it too.

```python
def register(api):
    api.register_settings(
        [
            {
                "key": "url",
                "type": "text",
                "label": "Server URL",
                "default": "http://homeassistant.local:8123",
            },
            {"key": "verbose", "type": "toggle", "label": "Verbose logging", "default": False},
            {"key": "token", "type": "secret", "label": "Access token", "env_key": "HASS_TOKEN"},
        ]
    )
```

| Key | Rule |
|---|---|
| `key` | required, `^[a-z][a-z0-9_]*$`, unique |
| `type` | required: `text`, `number`, `toggle` or `secret` |
| `label` | required, non-empty |
| `description` | optional hint under the field |
| `default` | optional, matching the type (`text` str, `number` int or float, `toggle` bool); not allowed for `secret` |
| `required` | optional bool, default `False` |
| `env_key` | required for `secret` (`^[A-Z][A-Z0-9_]*$`), not allowed for other types |

A violation, or a second `register_settings` call, raises `ValueError` and the Extension loads as `failed`. On save, vBot refuses unknown keys, secret fields in the config, wrong types and empty required fields. A secret field is write-only in the WebUI and saved on its own.

## Prompt blocks

`api.register_prompt_block` adds a block to the System Prompt; the user can reorder and disable it in the System Prompt tab. Pass exactly one of:

- `default_text`: a static block; the user can also edit its text.
- `render`: a function `render(context) -> str` that builds the text when the System Prompt is built. The user cannot edit it, and when it raises, only this block is left out. `context` holds the Agent and Run state but no messages.

```python
def register(api):
    api.register_prompt_block(
        "house_style",
        default_text="Prefer SI units and ISO 8601 dates.",
    )

    def render_quota(context):
        return f"Daily quota remaining: {remaining_calls()}."

    api.register_prompt_block("quota", render=render_quota)
```

The block's id is `extension:<slug>`; it appears only while your Extension is loaded. `requires_tool` with a Tool name, or a prefix ending in `*` such as `"weather_*"`, shows the block only while the Agent's Tool list contains a matching Tool (API 9). Use one slug per block; a slug another Extension took first is skipped with a diagnostic.

## Lifecycle: startup and shutdown

```python
def register(api):
    api.on_startup(open_resources)  # once serving begins, with the event loop running
    api.on_shutdown(close_resources)  # during shutdown, reload and disable
```

Both take no arguments. Async startup handlers run on the server's event loop and can start background tasks; keep blocking I/O out of them. Synchronous handlers run in worker threads. CLI commands that serve nothing fire no startup. A failing handler is logged and the others still run.

Acquire connections, tasks and file handles in startup, release them in shutdown, and make both idempotent: every reload runs shutdown and startup of every loaded Extension, not only the changed one.

## Config and logging

`api.config` is the config at register time; use it for decisions inside `register()`. In handlers, read `api.get_config()`, which reflects a change from the settings form on the next call.

```python
def register(api):
    def call_home(context, arguments):
        url = api.get_config().get("url", "http://homeassistant.local:8123")  # live
        ...

    api.register_tool("ha_ping", "Ping Home Assistant.", PARAMS, call_home)
```

`api.logger` writes to vBot's logs; never use `print` and never log a secret.

### Secrets

A `secret` field's value lives encrypted in the Secret Store under the `env_key` you declared, never in `settings.json`. Read it on every call with `api.resolve_credential(env_key)`:

```python
def register(api):
    api.register_settings(
        [
            {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
        ]
    )

    def call_home(context, arguments):
        token = api.resolve_credential("HASS_TOKEN")  # process env, then the Secret Store
        if not token:
            return tool_failure("not_configured", "Set the token in Settings → Extensions.")
        ...
```

- You choose the credential name; prefix a new one with your Extension's name (`MYEXT_API_KEY`) so it cannot clash with another.
- A variable of the same name in the server's process environment wins over the stored value; when a secret seems not to change, check the environment.

## Manifest: `extension.json`

A package or directory Extension can add an `extension.json`; it is never required.

```json
{
  "name": "Bash Guard",
  "version": "1.2.0",
  "description": "Refuses obviously destructive shell commands.",
  "api_version": 3
}
```

The identity stays the directory name; `name` is only shown. vBot refuses an Extension whose `api_version` is above its own, currently 22. Declare the version that added what you use: 3 Tool Families; 4 managed operations and live Tool catalogs; 5 `requires_opt_in`; 6 pages and `catalog_visible`; 7 [databases](#extension-databases); 8 [Tool result payloads](#tool-result-payloads); 9 `requires_tool` on prompt blocks; 10 listing and archiving owned Session groups; 11 OAuth browser redirects (`host.oauth_redirects`, see `core/extensions/oauth_redirects.py`); 12 server folder listing for pages; 13 [Tool summaries](#tool-summary); 14 [Tool-change announcements](#tool-changes-in-a-session); 15 `config_documents`, `host.resolve_tool_agent_async` and Tool-call receipts; 16 windowed Run steps for pages; 17 Secret Sources; 18 `host.resolve_permissions`; 19 bounded Tool-call previews and `history.tool_call` for pages; 20 `argument_hint` on slash Commands; 21 shared Computer routing (`host.computers`); 22 Run-bound programmatic Tool calls (`ToolContext.programmatic`, `call_tool`, `tool_calls_available`).

`config_documents` maps file patterns in your Extension's state directory to `module:function` validators, for example `"config_documents": {"connections.json": "config:validate_connections_file"}`. A validator receives the file's absolute `Path` and returns a `core.config_validation.JsonValidationReport`. `vbot doctor config` loads only that module, without your `__init__`, `register` or startup, also while the Extension is disabled; keep the module independent of registration.

## Managed operations

`api.operations.register` adds management operations with a JSON Schema; vBot validates their arguments and offers them through `vbot extensions operations <name>` and `vbot extensions run <name> <operation>`. An operation that takes a credential declares `secret=True`, and the CLI then requires its JSON on `--stdin`. `api.operations.replace_tools` publishes a complete live Tool catalog. The declarations and host types are in `core/extensions/operations.py`.

Append an async startup callback to `api.operations.startup`; it receives the Extension's `ExtensionHost`: `state_dir` (a private directory for persistent files), `open_database(...)`, `load_result_payload(context, payload_id)`, `catalog()` (configured choices) and `publish_change(resource, ids, revision)`, which refreshes your [page](extension-pages.md).

- From async handlers, `await host.resolve_tool_agent_async(context)` returns the invoking Agent, for Tool policy or sampling.
- For a remote task that outlives the call, take `receipt = await host.capture_tool_call(context)` and store it with the task's id in your database; `None` means the call has no Session. After a restart, `await host.restore_tool_call(receipt)` returns the original call's current context, following Session moves, or `None` when nothing needs delivery. Resume the remote task by its id without sending the operation again, and deliver the outcome with `await host.deliver_tool_outcome(receipt, content)`: it adds one note for the Session's next Run without starting a Run. Quote external content as JSON, escape angle brackets and write no reminder tags. Repeating the same content is ignored; different content for the same receipt is refused.

## Extension databases

Keep SQLite data in databases vBot manages, never in SQLite files you open yourself (API 7). In a callback appended to `api.operations.startup`, `await host.open_database(name, schema_sql, migrations=(), retired_indexes=())` returns a `Database` at `<data_dir>/extension-data/<extension-id>/<name>.db`. `name` and the Extension id are lowercase letters, digits, `_` and `-`, starting with a letter or digit, otherwise it raises `ValueError`; each name opens once. vBot includes these databases in its data snapshots and restores.

```python
from core.extensions.databases import Database

SCHEMA = """
CREATE TABLE notes (
  id INTEGER PRIMARY KEY,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
"""


class Notes:
    def __init__(self) -> None:
        self.database: Database | None = None

    async def start(self, host) -> None:
        self.database = await host.open_database("notes", SCHEMA)

    async def add(self, body: str, created_at: str) -> None:
        def insert(connection):
            connection.execute(
                "INSERT INTO notes (body, created_at) VALUES (?, ?)", (body, created_at)
            )

        await self.database.write_async(insert)

    async def bodies(self) -> list[str]:
        def select(connection):
            rows = connection.execute("SELECT body FROM notes ORDER BY id")
            return [row["body"] for row in rows]

        return await self.database.read_async(select)


notes = Notes()


def register(api):
    api.operations.startup.append(notes.start)
```

- From async code use `await database.write_async(fn)` and `await database.read_async(fn)`; `database.write(fn)` and `with database.read() as connection:` block the thread. `fn` receives a `sqlite3.Connection` whose rows support `row["column"]`. A write function runs in one transaction and is retried as a whole while the database is busy, so keep other side effects out of it. Read connections are read-only.
- Change the schema only additively: new tables, indexes, or columns that are nullable or have a default go into `schema_sql`, and vBot adds them on the next open. Never rename, retype or drop columns or tables, and never change an existing table's constraints; vBot refuses to open a database whose objects differ from their declaration. To remove an index, delete it from `schema_sql` and list its name in `retired_indexes`. Fill new structures with `Migration(name, apply=fn)` from `core.extensions.databases`: `apply(connection)` runs once per database and must be idempotent; a new database records it without running it.
- Set `breaks_older=True`, as in `Migration(name, breaks_older=True, apply=fn)` (`apply` can be omitted), when earlier versions of your Extension would misread the data afterwards, such as a new status value their code does not handle or a column whose meaning changed. An earlier version that lacks the migration then fails in `host.open_database` with `DatabaseError` and leaves the data unchanged; going back to it means restoring a data snapshot from before the change, losing everything written since. Leave it `False` when earlier versions can ignore the change, such as a new table or column they never read. Keep every released migration declared under its name: a version that drops a `breaks_older` migration refuses its own database.
- Name the columns in every `SELECT` and `INSERT`, because a later version can add columns. Check values that can grow, such as statuses, in code instead of CHECK constraints.
- A handle belongs to the current registration. Shutdown handlers can still use it; then vBot closes it, also on reload and disable. Open it again in the next startup, and never close it yourself.
- A deleted Extension's databases stay registered and in data snapshots. When the user no longer needs that data, `vbot data-store unregister ext.<extension-id>.<name> --yes` releases each one (`data-store.md`).

## Tool result payloads

A Tool can keep a large result with its Tool Result in Session history instead of in files (API 8). When `context.result_payloads_available` is true, `payload_id = context.attach_result_payload(value)` stores the JSON-serializable `value` with this call's result and returns an id to put in the result you return; attach only while the call runs. Outside a Session, such as in management operations and the CLI, it is false and `attach_result_payload` raises `RuntimeError`; return the value inline then. vBot stores the payload only when the handler returns a valid result, not when it raises or its Run is cancelled.

A later Tool call in the same Session reads it with `await host.load_result_payload(context, payload_id)`, passing its own `ToolContext`. It returns `None` when the id is unknown, belongs to another Extension, or its Tool Result is not in that Session's current history. Forks read the payloads they inherited. Apply your own access checks after loading.

## Tool summary

An Agent that loads Tools on demand sees most Tools only as `name: summary` until it loads one. Pass `summary="..."` to `api.register_tool(...)`, or the same field in a live Tool catalog, when the description's first sentence does not say what the Tool is for or is longer than about 160 characters: one sentence of at most 200 characters for the Model. Without it, vBot uses the description's first sentence (API 13).

## Tool changes in a Session

A Session keeps the Tool definitions it started with until its next Compaction. When your Tool changes, for example in a newly published live catalog, Sessions that already know it learn of the change only for the parts you list in `announce_changes`: `"description"`, `"parameters"` or `"parameters.<name>"`; vBot then sends them the new definition (API 14). Leave out changes your handler can explain when it is called, such as an option the current configuration does not support.

## Explicit Tool permission

`requires_opt_in=True` on `api.register_tool(...)`, or in a live Tool catalog, makes a configurable Tool need an explicit grant per Agent (API 5); not for internal or Session-scoped Tools. Grants live in the Agent's `tool_access.granted`: All mode does not grant them, selected mode needs the Tool in `allowed` and granted, and None and `denied` still win. A Project's Tool ceiling never implies a grant; grant it to a Project Agent through its Tool override. This is Tool authorization in vBot; it does not sandbox an Agent's shell or the Extension's own code. MCP connections use the same opt-in.
