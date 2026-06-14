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

Loading happens **at startup only**. Enable/disable and config changes are
**restart-applied** — there is no hot reload (handlers may be mid-flight), and a
disabled extension is never imported. The agent can run `vbot server restart` to
apply changes.

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
| `api.on_startup(handler)` / `api.on_shutdown(handler)` | a lifecycle callback (sync or async, no args) |
| `api.config` | your config object from `settings.extensions.config.<name>` (default `{}`) |
| `api.logger` | a ready-made `vbot.extensions.<name>` logger |

## Hooks

Declare a hook with `api.on(event, handler)`. Every handler is called as
`handler(ctx, **payload)` — `ctx` first, then the event's keyword payload.
Handlers may be sync or async. A handler that raises is logged and skipped; it
never aborts the run.

`ctx` is a `HookContext` with `session_id`, `agent_id`, `run_id`, and
`add_note(text)` — append a kernel-internal `<system-reminder>` for the model.

There are exactly six events. Each has a fixed **composition rule** that decides
how return values are used:

| Event | Composition | Payload (after `ctx`) | Return |
|---|---|---|---|
| `run_start` | observer | `session_id, agent_id` | ignored |
| `before_agent_start` | accumulator | `agent, session, messages, run` | `{"system_prompt_append": str}` |
| `context` | pipeline | `messages` | a `list` that replaces the running messages, or `None` |
| `tool_call` | decision pipeline | `tool_name, tool_call_id, input` | `None` / `Modify` / `Deny` / `Replace` |
| `tool_result` | replace pipeline | `tool_name, tool_call_id, input, result` | a full replacement result envelope, or `None` |
| `run_end` | observer | `session_id, agent_id, outcome` | ignored |

`outcome` is `"success"`, `"error"`, or `"cancelled"`. `run_end` runs in a
`finally`, so it always fires.

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

`api.logger` is a `vbot.extensions.<name>` logger through the normal logging
pipeline (no `print`). Both `disabled` and `config` are **restart-applied**.

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
  vbot extensions disable guard_bash   # restart-applied; prints a restart hint
  vbot extensions enable guard_bash
  vbot server restart                  # apply the change
  ```
- **WebUI**: Settings → **Extensions** lists every extension with its status,
  version, capabilities, and failure reason, offers a per-extension
  enable/disable toggle and a raw-JSON config editor, and shows a
  restart-required notice after a change.
- **RPC**: `extensions.list` returns the records; `settings.update` accepts the
  `extensions` section and replies with `"restart_required": true`.

A failed extension never aborts the others — it loads as `failed` with an error
detail you can read in any of the surfaces above.

## Walkthrough: a tool extension from scratch

1. Create `~/.vbot/extensions/word_count.py` with the `word_count` example
   above.
2. Restart: `vbot server restart`.
3. Confirm it loaded: `vbot extensions list` shows
   `word_count  loaded  …  tools: word_count`.
4. Allow the tool on an agent (`allowed_tools`) and ask it to count words — the
   model calls `word_count` like any built-in tool.

To turn the same idea into a hook instead, copy
[`examples/extensions/guard_bash.py`](../examples/extensions/guard_bash.py),
which denies destructive `bash` commands via the `tool_call` decision hook.
