# Writing vBot Extensions

An **extension** is in-process Python that adds capabilities to vBot without
forking the app. One extension is the unit of discovery, identity, config, and
enable/disable; it can contribute several **capabilities** — hooks, tools, and
recall backends — through a single `register(api)` entry point.

This is the author guide. For the precise internal contract (composition rules,
dispatch internals) see [`.vorch/domain-maps/extensions.md`](../.vorch/domain-maps/extensions.md);
for runnable samples see [`examples/extensions/`](../examples/extensions/).

> **Trust boundary.** Extensions run in-process with the **same trust as the
> kernel** — arbitrary Python, no sandbox, no permission system. Only install
> extensions you would run by hand. This is intentional: vBot is a single-user,
> technical-user tool.

`API_VERSION` is currently **1**. The extension API is vBot's first public
surface; it is designed conservatively and is not yet declared stable.

## Install and discovery

Copy a file or directory into the data directory's `extensions/` folder
(`~/.vbot/extensions/` by default), or add extra roots via
`settings.json` → `extension_directories`. Only the **immediate children** of
each root are scanned. The extension's **name is its filesystem name** — that is
its identity everywhere (settings, CLI, the WebUI panel).

Three entry-point shapes are accepted:

| Shape | Layout | Manifest? |
|---|---|---|
| Single-file module | `<root>/<name>.py` | no |
| Package module | `<root>/<name>/__init__.py` | optional |
| Directory fallback | `<root>/<name>/extension.py` | optional |

Everything applies **live** — no restart. Config-value and secret changes are read
live per call; enabling, disabling, or editing an extension's code takes effect
through the [extension reload](#reloading-extensions) (enabling and the explicit
`extensions reload` both rebuild the whole layer from disk, disabling deactivates
just that one extension). A disabled extension is never imported until it is
enabled and the layer reloads.

## The entry point: `register(api)`

Every extension exposes one function. It may be sync or async (async
`register()` is awaited before any declaration goes live):

```python
def register(api):
    api.on("tool_call", my_hook)
    api.register_tool("word_count", "Count words.", PARAMETERS, my_tool)
```

`register(api)` **only declares**. Nothing runs at import time; the runtime
applies your declarations at the correct bootstrap points (tools late, recall
backends early, hooks after every extension has registered). Extensions never
touch the live registries directly.

The `api` object (`ExtensionAPI`) offers:

| Call | Declares |
|---|---|
| `api.on(event, handler)` | a hook handler for one event |
| `api.register_tool(name, description, parameters, handler, *, internal=False, display=None)` | an agent tool |
| `api.register_recall_backend(name, factory)` | a session-recall backend |
| `api.register_prompt_block(slug, *, default_text=None, render=None)` | a System Prompt block |
| `api.register_interaction_handler(prefix, handler)` | a channel button-tap handler (see [Channel interaction handlers](#channel-interaction-handlers)) |
| `api.register_settings(fields)` | a settings schema (see [Settings schema](#settings-schema)) |
| `api.on_startup(handler)` / `api.on_shutdown(handler)` | a lifecycle callback (sync or async, no args) |
| `api.config` | your config **snapshot** from `settings.extensions.config.<name>`, taken at register time (default `{}`) |
| `api.get_config()` | your config read **live** per call — reflects a UI change without a restart |
| `api.resolve_credential(key)` | resolve a credential **live** per call (process env, then the data-dir `.env`) |
| `api.logger` | a ready-made `vbot.extensions.<name>` logger |

## Hooks

Declare a hook with `api.on(event, handler)`. Every handler is called as
`handler(ctx, **payload)` — `ctx` first, then the event's keyword payload.
Handlers may be sync or async. A handler that raises is logged and skipped; it
never aborts the run.

`ctx` is a `HookContext` with `session_id`, `agent_id`, `run_id`, and
`add_note(text)` — append a kernel-internal `<system-reminder>` for the model.

There are exactly five events. Each has a fixed **composition rule** that decides
how return values are used:

| Event | Composition | Payload (after `ctx`) | Return |
|---|---|---|---|
| `run_start` | observer | `session_id, agent_id` | ignored |
| `context` | pipeline | `messages` | a `list` that replaces the running messages, or `None` |
| `tool_call` | decision pipeline | `tool_name, tool_call_id, input` | `None` / `Modify` / `Deny` / `Replace` |
| `tool_result` | replace pipeline | `tool_name, tool_call_id, input, result` | a full replacement result envelope, or `None` |
| `run_end` | observer | `session_id, agent_id, outcome` | ignored |

`outcome` is `"success"`, `"error"`, or `"cancelled"`. `run_end` runs in a
`finally`, so it always fires.

To add **standing text to the System Prompt**, do not use a hook — declare a
**prompt block** instead (see [Prompt blocks](#prompt-blocks)). The prompt block
is positioned in the prompt layout, gated, and user-overridable; a hook would
have to rebuild the prompt every turn. Use the `context` hook only for
**per-request, message-dependent** changes (the kind that needs to inspect the
running `messages`).

### The `tool_call` decision hook

Import the decision objects from `core.extensions`:

```python
from core.extensions import Deny, Modify, Replace

def guard(ctx, *, tool_name, tool_call_id, input):
    if tool_name != "bash":
        return None                       # proceed unchanged
    if "rm -rf /" in input.get("command", ""):
        ctx.add_note("guard blocked a destructive command.")
        return Deny(reason="Refused: destructive command.")
    return None
```

- `None` — proceed unchanged (the common case).
- `Modify(input)` — replace the tool arguments; the pipeline continues, and the
  tool runs with the modified input.
- `Deny(reason)` — stop the pipeline; the tool does not run. Chat builds a
  `tool_call_denied` failure envelope naming your extension.
- `Replace(result)` — stop the pipeline and skip execution; `result` must be a
  valid result envelope (use `tool_success` / `tool_failure`) or it is dropped.

`tool_result` is a **full-replace** pipeline: return a complete replacement
envelope (re-validated) or `None` to leave it unchanged — there is no patching.

## Tools

`api.register_tool` mirrors the built-in `ToolRegistry.register`. A registered
extension tool is a **normal tool**: it appears in provider tool definitions and
is filtered by an agent's `allowed_tools` like any other. The handler signature
`(context, arguments)` and the result envelope are identical to built-ins.

```python
from core.tools import tool_failure, tool_success

PARAMETERS = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "Text to count."}},
    "required": ["text"],
}

def word_count(context, arguments):
    text = arguments.get("text")
    if not isinstance(text, str):
        return tool_failure("invalid_arguments", "`text` must be a string.")
    return tool_success({"word_count": len(text.split())})

def register(api):
    api.register_tool(
        "word_count",
        "Count whitespace-separated words in a piece of text.",
        PARAMETERS,
        word_count,
    )
```

A tool name that **collides** with a built-in or another extension's tool is
skipped (built-in wins; between two extensions the first-loaded wins) and the
skip is recorded as a non-fatal diagnostic visible in `vbot extensions list` and
the WebUI panel. Keep descriptions short — every tool enlarges the system
prompt.

(Tools are code that does one thing. To teach the agent a *workflow*, write a
Skill instead.)

## Recall backends

`api.register_recall_backend(name, factory)` adds a session-recall backend
(`factory` is `RecallBackendContext -> RecallBackend`). The name must be
lowercase snake_case and must not collide with a built-in. Once registered, a
backend becomes selectable via `settings.recall.backend` (Settings → Recall).
See [`.vorch/domain-maps/recall.md`](../.vorch/domain-maps/recall.md) for the backend
protocol.

## Channel interaction handlers

A channel message can carry inline **buttons**; tapping one produces a channel
interaction event. `api.register_interaction_handler(prefix, handler)` lets your
extension handle those taps **deterministically in-process** — no LLM run, no
agent wake-up — which is what makes a tap fast and free.

Routing is by **callback-data prefix**. A button's callback data is
`"<prefix>:<payload>"` (the `prefix` never itself contains `:`); your handler
receives every tap whose data begins with `"<prefix>:"`. A prefix already
claimed by an earlier-loaded extension is skipped and diagnosed on your
extension's record (first-wins, the same policy as a tool-name collision) — one
prefix, one owner.

```python
from core.extensions import InteractionButton

async def _toggle(event, responder):
    # event.buttons is the message's current keyboard (rows of InteractionButton),
    # event.data is the tapped button's callback payload. Rebuild the keyboard and
    # edit it back — the state lives in the message, there is no server-side store.
    new_rows = [
        [
            InteractionButton(label=flip(b.label) if b.data == event.data else b.label,
                              data=b.data)
            for b in row
        ]
        for row in event.buttons
    ]
    await responder.edit(buttons=new_rows)
    await responder.answer()            # stop the tapper's spinner (empty = silent ack)

def register(api):
    api.register_interaction_handler("chk", _toggle)
```

The handler is called `handler(event, responder)`:

- `event` (`InteractionEvent`) carries `platform`, `channel_id`, `chat_id`,
  `user_id`, `message_id`, `data` (the tapped payload), `buttons` (the message's
  current keyboard as rows of `InteractionButton`), and optional `text`,
  `user_display_name`, `thread_id`. There is **no server-side persistence**: the
  platform delivers the current message + keyboard with every tap, so recompute
  the edit from the event.
- `responder` (`InteractionResponder`) is the reply channel: `await
  responder.answer(text=None, *, alert=False)` acknowledges the tap (an empty
  `text` is a silent ack; a non-empty one shows a toast, or a modal when
  `alert=True`), and `await responder.edit(*, text=None, buttons=None)` rewrites
  the tapped message's text and/or keyboard.

Every tap is acknowledged **exactly once** — if your handler does not call
`answer()`, the channel sends a fallback ack so the tapper's spinner always
stops. A handler that raises is logged and swallowed (fail-open), then the
fallback ack still runs.

To **send** buttons, use the `channel_send` tool's `buttons` parameter (rows of
`{label, data}`), or a channel adapter's `send(..., buttons=...)`. Telegram is
the only platform with interactive support today; callback data is capped at 64
bytes. The bundled **checklist** extension (prefix `chk`) is a ready reference
handler that flips a leading ⬜↔✅ on the tapped button.

## Settings schema

An extension can declare a **settings schema** so the WebUI renders a real form for it (Settings → Extensions), instead of the raw-JSON editor. Declare it at register time — the same declaration pattern as hooks, tools, and recall backends — so single-file extensions (which carry no manifest) can use it too:

```python
def register(api):
    api.register_settings([
        {"key": "url", "type": "text", "label": "Server URL",
         "default": "http://homeassistant.local:8123"},
        {"key": "verbose", "type": "toggle", "label": "Verbose logging", "default": False},
        {"key": "token", "type": "secret", "label": "Access token", "env_key": "HASS_TOKEN"},
    ])
```

Each field is one dict. The v1 types are `text`, `number`, `toggle`, and `secret`:

| Key | Rule |
|---|---|
| `key` | required, `^[a-z][a-z0-9_]*$`, unique within the schema |
| `type` | required, one of `text` / `number` / `toggle` / `secret` |
| `label` | required, non-empty string |
| `description` | optional string (shown as a hint under the field) |
| `default` | optional; **forbidden** for `secret`; must match the type (`text`→str, `number`→int/float, `toggle`→bool) |
| `required` | optional bool, default `False` |
| `env_key` | **required** for `secret` (`^[A-Z][A-Z0-9_]*$`); **forbidden** on any other type |

Any violation raises `ValueError` inside `register()`, so the extension loads as `failed` with a message naming the bad field (the same policy as an invalid prompt block). Calling `api.register_settings` twice also raises — declare exactly one schema.

**What the WebUI renders:** a `text` field is a text input (its `default` is shown as the placeholder), `number` a number input, `toggle` a checkbox, and `secret` a write-only password input with a Set/Not-set indicator and Save/Clear actions. Non-secret fields save together through a single "Save settings" button; a secret saves on its own (see [Secrets](#secrets-in-env)).

**Server-side validation on save:** for a loaded extension with a schema, the `settings.update` RPC validates the submitted config against the schema before persisting — unknown keys are rejected, a key naming a `secret` field is rejected (secrets live in `.env`, never in config), types must match, and a `required` non-secret field must be present and non-empty. An extension **without** a schema keeps the raw-JSON pass-through.

**Live values.** A non-secret setting change (URL, a toggle, a number) is read **live** on the next tool call — no restart. Read your config through `api.get_config()` rather than the register-time `api.config` snapshot (see [Config and logging](#config-and-logging)). Enabling, disabling, or reloading a whole extension also applies live (see [Reloading extensions](#reloading-extensions)).

## Prompt blocks

To add standing content to the **System Prompt**, declare a block with
`api.register_prompt_block`. The vBot System Prompt is built from ordered blocks;
your block joins them — positioned in the layout, gated, and (for a static block)
user-overridable from the System Prompt tab. This is the right tool for "always
tell the model X"; do not try to append it from a hook.

Pass **exactly one** of:

- `default_text` — a **static** block. Its text is editable through the System
  Prompt override cascade, so a user can tweak or disable it per scope.
- `render` — a **dynamic** block: a build-time function `render(context) -> str`
  that returns the text. It is not user-editable, and if it raises, only that one
  block is dropped (the run is never aborted). `context` carries the agent and
  run state but **no conversation messages** — message-dependent content belongs
  in the `context` hook, not here.

```python
def register(api):
    # A static block the user can edit/reorder/disable in the System Prompt tab.
    api.register_prompt_block(
        "house_style",
        default_text="Prefer SI units and ISO 8601 dates.",
    )

    # A dynamic block computed at prompt-build time.
    def render_quota(context):
        return f"Daily quota remaining: {remaining_calls()}."

    api.register_prompt_block("quota", render=render_quota)
```

Your block's id is `extension:<slug>` and its owner is `extension:<name>`, so it
renders **only while your extension is loaded**. Declare several blocks by using
distinct slugs. A slug that collides with another extension's block is resolved
first-loaded-wins with a non-fatal diagnostic (same policy as tool names). The
block list refreshes whenever extensions reload — no per-request cost.

## Lifecycle: startup and shutdown

```python
def register(api):
    api.on_startup(open_resources)     # fires once serving begins (loop running)
    api.on_shutdown(close_resources)   # fires during runtime shutdown
```

Both may be sync or async and take no arguments. Startup handlers fire on the
live serving event loop, so they may schedule background tasks. Accessors that
never serve (CLI local commands) do not fire startup. Both phases fail-open per
handler.

**Contract (a reload leans on this).** `import` and `register(api)` must be
**side-effect-free** — they only *declare*. Acquire resources (connections, tasks,
file handles) in a **startup** handler and release them in a **shutdown** handler,
and make both **idempotent**. This matters because an [extension
reload](#reloading-extensions) cycles **every** loaded extension through
shutdown+startup, not just the one that changed: a startup handler that assumes it
runs once, or a shutdown handler that leaks, will misbehave across reloads.

## Config and logging

Per-extension config arrives as `api.config` — the object under
`settings.json` → `extensions.config.<name>` (default `{}`):

```json
{
  "extensions": {
    "disabled": ["some_old_extension"],
    "config": { "guard_bash": { "deny": ["rm -rf /"] } }
  }
}
```

```python
def register(api):
    deny = api.config.get("deny", [])
    api.logger.info("guard_bash loaded with %d patterns", len(deny))
```

`api.config` is a **snapshot** taken at register time — use it for structural decisions inside `register()`. For values you read while running (in a hook or tool handler), use `api.get_config()` instead: it re-reads the persisted config **per call**, so a change a user makes in the settings form takes effect on the next call without a restart. Enabling, disabling, and reloading the whole extension also apply live (see [Reloading extensions](#reloading-extensions)).

```python
def register(api):
    def call_home(context, arguments):
        url = api.get_config().get("url", "http://homeassistant.local:8123")  # live
        ...
    api.register_tool("ha_ping", "Ping Home Assistant.", PARAMS, call_home)
```

`api.logger` is a `vbot.extensions.<name>` logger through the normal logging pipeline (no `print`). Never log a secret value.

### Secrets (in `.env`) {#secrets-in-env}

A `secret` field's value is stored in the data directory's `.env` (`~/.vbot/.env`) under the `env_key` you declared — **never** in `settings.json`. Read it live per call with `api.resolve_credential(env_key)`:

```python
def register(api):
    api.register_settings([
        {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
    ])

    def call_home(context, arguments):
        token = api.resolve_credential("HASS_TOKEN")  # process env, then .env
        if not token:
            return tool_failure("not_configured", "Set the token in Settings → Extensions.")
        ...
```

- **Write-only in the UI.** The form shows only whether the secret is set; the stored value is never displayed. Saving or clearing it writes `.env` and refreshes the resolver immediately (no restart).
- **You choose the `.env` key explicitly.** There is no derived naming — established keys keep their names (`HASS_TOKEN` stays `HASS_TOKEN`). To avoid clashing with another extension or an app credential, **prefix new keys** with your extension name (e.g. `MYEXT_API_KEY`).
- **Process-env precedence.** A key present in the process environment **wins** over the `.env` value the UI writes. So if the same key is exported in the environment that launched vBot, that stale value overrides what a user sets in the form — worth knowing when a secret "won't change".

## Manifest (optional): `extension.json`

Directory/package extensions may add an `extension.json` to enrich identity
(single-file extensions can't, and don't need to). It is never required:

```json
{
  "name": "Bash Guard",
  "version": "1.2.0",
  "description": "Refuses obviously destructive shell commands.",
  "api_version": 1
}
```

Identity stays the **directory name**; the manifest `name` is display-only. An
`api_version` greater than the app's `API_VERSION` fails the extension at load
with a clear message (forward-compatibility guard).

## Managing extensions

Extensions surface through the normal management flow:

- **CLI** (an accessor — everything goes through server RPC):
  ```bash
  vbot extensions list                 # loaded / failed / disabled + capabilities
  vbot extensions reload               # rebuild the whole layer from disk (live)
  vbot extensions disable guard_bash   # applied live
  vbot extensions enable guard_bash    # applied live (reloads the layer)
  ```
- **WebUI**: Settings → **Extensions** lists every extension with its status,
  version, capabilities, and failure reason, offers a "Reload extensions" button
  and a per-extension enable/disable toggle, and — for a schema'd extension — a
  real settings form (raw-JSON editor as the fallback for schema-less ones).
  Secret fields are write-only. Every change applies live; there is no restart
  notice.
- **RPC**: `extensions.list` returns the records (with any declared settings
  schema and live secret state); `extensions.reload` rebuilds the layer and
  returns the same shape; `settings.update` accepts the `extensions` section (all
  changes apply live); `extensions.set_secret` writes/clears a secret.

A failed extension never aborts the others — it loads as `failed` with an error
detail you can read in any of the surfaces above.

## Reloading extensions

Everything an extension change touches applies **live** — you never restart the
server to pick up an extension. There are two live paths:

- **Config values and secrets** are read per call (`api.get_config()` /
  `api.resolve_credential()`), so a settings-form save takes effect on the next
  call with no further action.
- **Code, structure, and enable/disable** go through the **extension reload** — a
  full, restart-equivalent rebuild of the whole extension layer from disk in the
  running process. Trigger it explicitly with `vbot extensions reload`, the
  WebUI "Reload extensions" button, or the `extensions.reload` RPC. **Enabling**
  an extension runs the same rebuild for you; **disabling** deactivates just that
  one extension.

A reload picks up edited code of loaded extensions (including submodules of
package extensions), extensions you added to or deleted from a scan root,
previously `failed` extensions whose code you fixed, and boot-disabled extensions
you enabled — the end state is exactly what a fresh server start would produce.
Because a reload cycles **every** extension through shutdown+startup, keep those
handlers idempotent (see [Lifecycle](#lifecycle-startup-and-shutdown)). Edit a
`.py` file, then run `vbot extensions reload` to apply it.

## Walkthrough: a tool extension from scratch

1. Create `~/.vbot/extensions/word_count.py` with the `word_count` example
   above.
2. Load it: `vbot extensions reload`.
3. Confirm it loaded: `vbot extensions list` shows
   `word_count  loaded  …  tools: word_count`.
4. Allow the tool on an agent (`allowed_tools`) and ask it to count words — the
   model calls `word_count` like any built-in tool.

To turn the same idea into a hook instead, copy
[`examples/extensions/guard_bash.py`](../examples/extensions/guard_bash.py),
which denies destructive `bash` commands via the `tool_call` decision hook.
