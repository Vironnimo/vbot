# Bootstrap Runs

Bootstrap schedules an internal Agent Run after the server reaches its ready point. It is managed only through the CLI: there is no Bootstrap Agent Tool and no WebUI editor.

## Commands

```bash
vbot bootstrap list
vbot bootstrap create <agent|agent@project> --name "Startup check" --prompt "Check the server" --mode once|always [--session <session-id>]
vbot bootstrap create --current-session --name "Verify restart" --prompt "Check status and logs, then report" --mode once|always
vbot bootstrap update <job-id> [--agent <agent|agent@project>] [--name <name>] [--prompt <prompt>] [--mode once|always] [--session <session-id> | --clear-session]
vbot bootstrap enable <job-id>
vbot bootstrap disable <job-id>
vbot bootstrap delete <job-id>
```

Creation always requires an explicit mode. `once` runs after the next eligible startup and then becomes `completed` or `failed`. `always` runs once after every startup and stays `active`; inspect `last_outcome` and `last_error` for its most recent health. A job created, updated, or enabled in the current process is armed for a future startup and never fires immediately.

Without `--session`, each firing creates a fresh Session. `--session` targets an existing Session. `--current-session` is create-only, must not be combined with an explicit Agent or `--session`, and is available only from Bash inside a vBot Run; it uses the Run's exact Agent/Project/Session context. It is intentionally rejected for a remote CLI target because the injected context belongs to the server hosting the current Run.

Jobs targeting the same Session execute in order; independent Sessions may run concurrently. A fixed-Session Bootstrap can continue an interrupted Run only when vBot classifies the interruption as `process_restart`. This is what lets a post-update verification continue the user conversation without turning ordinary internal Runs into user-message continuations.

Updating execution fields or enabling a paused/failed job rearms it for the next startup. Completed one-shot jobs are immutable history; delete and recreate one when another one-shot is needed. Non-terminal jobs prevent their referenced Agent, Project, or Session from being removed; retarget, complete, or delete the job first.

## Restart and update checks

For a deliberate restart, make the prompt self-contained and idempotent. Tell the Agent what changed, which CLI checks to perform, what constitutes success, and not to repeat the disruptive operation. For `vbot update`, use the exact workflow in `references/server.md`: create with `--current-session`, verify with `bootstrap list`, then start the update.
