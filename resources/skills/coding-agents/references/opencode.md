# OpenCode (`opencode`) — non-interactive reference

Headless entrypoint is **`opencode run`**: executes a prompt without launching the TUI. The bare
`opencode` command opens the interactive TUI and will hang in an automated call.

## Run

```bash
opencode run "Add pagination to the users list endpoint"
opencode run "Summarize the changes in this diff" < changes.diff
```

## Output (capture the session id here)

```bash
opencode run "task" --format json
```

`--format` accepts `default` (formatted text) or `json` (raw JSON events). Use `json` to read the
session id and structured result instead of scraping prose. `--thinking` additionally surfaces the
model's thinking blocks. List existing sessions with `opencode session list` to recover an id.

## Continue / resume a session

```bash
opencode run -c "next instruction"          # --continue: continue the most recent session
opencode run -s <session-id> "next step"    # --session: continue a specific session id
```

Pattern: run with `--format json`, capture the session id, then pass it with `-s`/`--session` on the
next call. `-c`/`--continue` continues the most recent session when you don't need a specific id.
Note: opencode's non-interactive session continuation has had rough edges across versions — verify
that the follow-up actually attached to the intended session, and prefer an explicit `--session <id>`
over `--continue` when correctness matters.

## Model, agent, reasoning effort

```bash
opencode run "task" --model anthropic/claude-sonnet-4-5   # -m, always provider/model form
opencode run "task" --agent build                         # select a defined agent
opencode run "task" --variant <name>                      # provider-specific reasoning-effort variant
```

- `--model` / `-m`: `provider/model` (e.g. `openai/gpt-5`, `anthropic/claude-sonnet-4-5`). List
  options with `opencode models`.
- `--agent`: selects an agent. Agents are defined in `opencode.json` under the `agent` key, or as
  markdown files in the opencode agent config directory (frontmatter: `mode: primary|subagent`,
  `model`, `reasoningEffort`). Define a high-effort agent there and select it with `--agent` when you
  want a fixed reasoning effort rather than a per-run `--variant`.
- `reasoningEffort` (e.g. `low|medium|high`) is set per agent in config, not as a top-level run flag.

## Headless server (robust orchestration)

For multi-step orchestration, run a persistent server and attach runs to it:

```bash
opencode serve --port 4096                          # start the server (long-lived, run in background)
opencode run --attach http://localhost:4096 "task"  # send a run to the running server
```

This keeps one engine alive across calls and is more reliable than re-spawning the CLI for each step.

## Auth for automation

OpenCode authenticates via `opencode auth login` (interactive — do it beforehand) or provider
environment variables / its config. Do not put credentials in the task prompt.

## Minimal multi-step example

```bash
# Step 1 — start, capture the session id from JSON output
opencode run "Create a CLI flag --dry-run for the deploy script" --format json --agent build
#   → read the session id from the JSON events

# Step 2 — continue that session
opencode run -s <session-id> "Now document the flag in the README" --format json --agent build
```
