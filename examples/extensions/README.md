# Example extensions

Runnable, copy-pasteable examples of vBot **extensions** — in-process Python
that adds capabilities without forking the app. They are documentation-grade:
copy one into your data directory and it loads on the next start.

## Install

Copy a file (or a whole extension directory) into:

```
<data_dir>/extensions/        # ~/.vbot/extensions/ by default
```

Extensions load at startup. Enable/disable is restart-applied (there is no hot
reload, and disabled extensions are never imported). The agent can trigger a
restart to pick up changes.

## What's here

| File | Capability | Shows |
|---|---|---|
| `guard_bash.py` | hook (`tool_call`) | Refuse destructive shell commands with `Deny` + leave a note |
| `word_count.py` | tool (`register_tool`) | Add a normal agent tool that returns a result envelope |

## How an extension is structured

An extension is a single `.py` file (or a directory/package) whose **name is its
identity**. It exposes one function:

```python
def register(api):
    ...
```

`register(api)` only *declares* — it wires up:

- **hooks** — `api.on(event, handler)` for `run_start`, `before_agent_start`,
  `context`, `tool_call`, `tool_result`, `run_end`
- **tools** — `api.register_tool(name, description, parameters, handler)`
- **recall backends** — `api.register_recall_backend(name, factory)`
- **lifecycle** — `api.on_startup(fn)` / `api.on_shutdown(fn)`

The runtime applies those declarations at the right points during startup;
extensions never touch the live registries directly. Per-extension config
arrives as `api.config` (from `settings.json` → `extensions.config.<name>`), and
`api.logger` is a ready-made `vbot.extensions.<name>` logger.

Decision objects for the `tool_call` hook (`Deny`, `Modify`, `Replace`) import
straight from `core.extensions`.

See `.vorch/specs/extensions.md` for the full contract: every event's
composition rule, identity and the optional `extension.json` manifest, lifecycle
timing, and the trust boundary (extensions share the kernel's trust — there is
no sandbox).
