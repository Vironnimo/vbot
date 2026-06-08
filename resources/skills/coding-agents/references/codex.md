# Codex (`codex`) — non-interactive reference

Headless entrypoint is **`codex exec`** (alias `codex e`): runs a single task to completion,
streams progress to stderr, prints the final message to stdout, and exits. The bare `codex` command
opens the interactive TUI and will hang in an automated call.

## Run

```bash
codex exec "Refactor utils/date.py and keep behavior identical"
cat data.json | codex exec "Convert this to a markdown table"   # prompt arg + piped context
cat prompt.txt | codex exec -                                    # '-' reads the whole prompt from stdin
```

## Output (capture the session id here)

```bash
codex exec "task" --json
```

`--json` emits newline-delimited JSON (JSONL) events. Key events:

- `thread.started` — includes `thread_id` (the session id to resume).
- `item.started` / `item.completed` — agent messages, command runs, file changes.
- `turn.completed` — includes a `usage` block (input/output/reasoning tokens).

Also useful:

```bash
codex exec "task" -o final.txt        # --output-last-message: write the final message to a file
codex exec "task" --output-schema schema.json   # constrain the final output to a JSON Schema
```

## Continue / resume a session

```bash
codex exec resume <SESSION_ID> "next instruction"   # resume a specific session
codex exec resume --last "next instruction"         # resume the most recent session in this directory
codex exec resume --last --all "…"                  # --all also considers sessions from other directories
```

Pattern: run with `--json`, read `thread_id` from the `thread.started` event, then
`codex exec resume <thread_id> "…"`. If you don't want to parse the id, `resume --last` reliably
continues the most recent run from the current working directory.

## Model and reasoning effort

```bash
codex exec "task" --model gpt-5-codex                 # -m, override the configured model
codex exec "task" -c model_reasoning_effort="high"    # reasoning effort via config override (e.g. minimal|low|medium|high)
```

`-c` / `--config <key=value>` is repeatable and sets any config key for this run; reasoning effort is
a config key rather than a dedicated flag.

## Sandbox & approvals — must be non-interactive

An approval prompt with no human to answer it hangs the run. Set the sandbox and approval policy so
it never stops to ask:

```bash
codex exec "task" --sandbox workspace-write --ask-for-approval never
codex exec "task" -s read-only                      # read-only | workspace-write | danger-full-access
codex exec "task" --dangerously-bypass-approvals-and-sandbox   # alias --yolo; full access, use only in a disposable/CI env
```

- `--sandbox` / `-s`: `read-only` | `workspace-write` | `danger-full-access`.
- `--ask-for-approval` / `-a`: `untrusted` | `on-request` | `never` — use `never` for unattended runs
  (paired with a sandbox that already grants what the task needs).

## Other useful flags

```bash
codex exec "task" --skip-git-repo-check    # don't require being inside a git repo
codex exec "task" --ephemeral              # don't persist the session rollout to disk (can't resume it later)
codex exec "task" --ignore-user-config     # skip $CODEX_HOME/config.toml
codex exec "task" --ignore-rules           # bypass user/project execpolicy rule files
```

## Auth for automation

Codex authenticates via `codex login` (interactive — do it beforehand) or `OPENAI_API_KEY` in the
environment. Do not put credentials in the task prompt.

## Minimal multi-step example

```bash
# Step 1 — start, capture thread_id from the JSON event stream
codex exec "Add input validation to the signup form" --json --sandbox workspace-write --ask-for-approval never
#   → parse thread_id from the {"type":"thread.started", ...} line

# Step 2 — resume that session (or use `resume --last` to skip parsing)
codex exec resume <thread_id> "Now write a test for the validation" --sandbox workspace-write --ask-for-approval never
```
