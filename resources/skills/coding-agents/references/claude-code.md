# Claude Code (`claude`) — non-interactive reference

Headless entrypoint is **print mode** (`-p` / `--print`): runs the task to completion and exits.
The bare `claude` command opens an interactive TUI and will hang in an automated call.

## Run

```bash
claude -p "Fix the failing test in tests/auth and explain the cause"
cat error.log | claude -p "Diagnose this stack trace"     # pipe stdin as context
```

## Output (capture the session id here)

```bash
claude -p "task" --output-format json
```

`--output-format` accepts `text` (default), `json`, `stream-json`.
The `json` result is a single object that includes `session_id`, `result` (final text),
`is_error`, `num_turns`, `duration_ms`, and `total_cost_usd`. Read `session_id` from it to resume
later. `stream-json` emits one JSON object per event (pair with `--verbose`); `--input-format
text|stream-json` sets the input side for multi-turn streaming.

## Continue / resume a session

| Goal | Command |
|---|---|
| Continue the most recent session in this directory | `claude -c -p "next step"` (`--continue`) |
| Resume a specific session by id or name | `claude -r <session-id> "next step"` (`--resume`) |
| Choose your own session id (must be a valid UUID) | `claude --session-id <uuid> -p "task"` |
| Resume but branch into a new session | `claude -r <id> --fork-session "task"` |

Pattern for multi-step work: run with `--output-format json`, read `session_id`, then pass it to
`-r` on each follow-up call. `--session-id <uuid>` lets you assign the id up front so you don't have
to parse it back.

## Model, effort, fallback

```bash
claude -p "task" --model sonnet          # alias: sonnet | opus, or a full model id
claude -p "task" --effort high           # low | medium | high | xhigh | max (levels depend on model)
claude -p "task" --fallback-model sonnet # auto-fallback if the main model is overloaded (print mode)
```

`--effort` is the "thinking level": more effort = more extended thinking. Available levels depend on
the model.

## Permissions — must be non-interactive

A permission prompt with no human to answer it will hang the run. Pick a mode before running:

```bash
claude -p "task" --permission-mode acceptEdits        # default | acceptEdits | plan | auto | dontAsk | bypassPermissions
claude -p "task" --dangerously-skip-permissions       # == --permission-mode bypassPermissions
```

Scope the tools instead of granting everything:

```bash
claude -p "task" --allowedTools "Read" "Bash(git diff *)"   # run these without prompting
claude -p "task" --disallowedTools "Bash(rm *)"             # deny matching calls
claude -p "task" --tools "Bash,Edit,Read"                   # restrict which built-in tools exist at all
```

## Working directory, limits, agents, MCP

```bash
claude -p "task" --add-dir ../lib ../shared    # extra readable/editable dirs
claude -p "task" --max-turns 6                  # cap agentic turns (print mode); errors when hit
claude -p "task" --max-budget-usd 2.00          # stop after spending this much (print mode)
claude -p "task" --agent reviewer               # use a defined sub-agent / persona
claude -p "task" --agents '{"reviewer":{"description":"Reviews code","prompt":"You are a reviewer"}}'
claude -p "task" --mcp-config ./mcp.json        # load MCP servers (+ --strict-mcp-config to use only these)
claude -p "task" --append-system-prompt "Always use type hints"   # add to the default system prompt
```

Speed/scope helpers: `--bare` skips auto-discovery of hooks/skills/plugins/MCP/memory/CLAUDE.md for
faster scripted starts; `--no-session-persistence` keeps the run off disk (then it can't be resumed).

## Auth for automation

Claude Code uses a Claude subscription login or `ANTHROPIC_API_KEY`. For unattended/CI use, generate
a long-lived token once with `claude setup-token` (interactive, run beforehand) and provide it via
the environment. Do not put credentials in the task prompt.

## Minimal multi-step example

```bash
# Step 1 — start, capture the session id from JSON
claude -p "Add a /health endpoint" --output-format json --permission-mode acceptEdits   # → reads .session_id

# Step 2 — continue that exact session
claude -r <session_id> "Now add a test for it" --output-format json --permission-mode acceptEdits
```
