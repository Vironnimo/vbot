# Example extensions

Runnable, copy-pasteable examples of vBot **extensions** — in-process Python that adds capabilities without forking the app. They are documentation-grade: copy one into your data directory and load it with `vbot extensions reload`.

## Install

Copy a file (or a whole extension directory) into:

```
<data_dir>/extensions/        # ~/.vbot/extensions/ by default
```

Extensions load at startup and reload live. Run `vbot extensions reload` after copying or editing one; enable and disable changes also apply live.

## What's here

| File | Capability | Shows |
|---|---|---|
| `guard_bash.py` | hook (`tool_call`) | Refuse destructive shell commands with `Deny` + leave a note |
| `word_count.py` | Tool (`register_tool`) | Add a parallel-safe read-only Tool with closed input and success-data contracts |
| `workflow_command/` | command (`register_command`) + bundled Skill | Start `$workflow` as a same-address follow-up Run from `/workflow [objective]` |

## How an extension is structured

An extension is a single `.py` file (or a directory/package) whose **name is its
identity**. It exposes one function:

```python
def register(api):
    ...
```

`register(api)` only *declares* — it wires up:

- **hooks** — `api.on(event, handler)` for `run_start`, `context`, `tool_call`,
  `tool_result`, `run_end`
- **commands** — `api.register_command(name, description, handler)` for deterministic slash-command entry points and optional follow-up Runs
- **Tools** — `api.register_tool(name, description, parameters, handler, result_schema=..., parallel_safe=False)`; registration compiles the closed canonical input contract, successful `data` is checked against the optional result schema, and calls are serial unless explicitly proven parallel-safe
- **recall backends** — `api.register_recall_backend(name, factory)`
- **prompt blocks** — `api.register_prompt_block(slug, *, default_text=…)` (or
  `render=…`) to add standing content to the System Prompt
- **lifecycle** — `api.on_startup(fn)` / `api.on_shutdown(fn)`

To add text to the System Prompt, declare a **prompt block** — there is no
prompt-append hook (see the author guide for the static vs dynamic split).

The runtime applies those declarations at the right points during startup;
extensions never touch the live registries directly. Per-extension config
arrives as `api.config` (from `settings.json` → `extensions.config.<name>`), and
`api.logger` is a ready-made `vbot.extensions.<name>` logger.

Decision objects for the `tool_call` hook (`Deny`, `Modify`, `Replace`) import
straight from `core.extensions`.

See `.vorch/domain-maps/extensions.md` for the full contract: every event's
composition rule, identity and the optional `extension.json` manifest, lifecycle
timing, and the trust boundary (extensions share the kernel's trust — there is
no sandbox).
