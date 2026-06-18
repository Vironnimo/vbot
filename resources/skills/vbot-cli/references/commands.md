# vBot CLI Command Reference

Use `vbot` as the command name after the package is installed with `pip install -e .` or `pip install -e ".[dev]"`.

Primary identifiers are positional arguments (`vbot agent show assistant`, `vbot channel remove tg-main`); secondary parameters are flags.

## Targeting

Most commands accept these options:

```bash
--host 127.0.0.1
--port 8420
--data-dir ~/.vbot
```

Use them consistently when the user is working with a non-default instance:

```bash
vbot server status --host 127.0.0.1 --port 9000 --data-dir ./dev-data
vbot model list --host 127.0.0.1 --port 9000 --data-dir ./dev-data
```

## Server Lifecycle

```bash
vbot server start
vbot server stop
vbot server restart
vbot server status
```

Only server lifecycle commands can operate without an already-running vBot server. `start` refuses to launch over a non-vBot process on the target port.

## Config

```bash
vbot config
vbot config get <key>
vbot config set <key> <json-or-string-value>
vbot doctor settings [--data-dir <path>]
vbot doctor config [--data-dir <path>]
```

Examples:

```bash
vbot config get server_port
vbot config set server_port 9000
vbot config set skill_directories '["C:/Users/Viro/skills"]'
vbot config set extension_directories '["C:/Users/Viro/vbot-extensions"]'
vbot config set defaults '{"agent":{"temperature":0.4}}'
vbot config set debug '{"enabled": true}'
vbot doctor settings
vbot doctor config
```

`config set` parses JSON values first and falls back to plain strings. Quote JSON as one shell argument.
`doctor settings` validates the target data-dir `settings.json` locally and prints file/path diagnostics; it does not require a running server. `doctor config` validates the full user-editable runtime JSON bundle: settings, agents, channels, and cron jobs.

## Providers

```bash
vbot provider list
vbot provider status <provider-id> [--connection <provider:connection-id>]
vbot provider set-key <provider-id> <api-key> [--connection <provider:connection-id>] [--account <account-id>] [--refresh-models]
vbot provider unset-key <provider-id> [--connection <provider:connection-id>] [--account <account-id>]
vbot provider connect <provider-id> --connection <provider:connection-id> [--account <account-id>]
vbot provider connect-status <provider-id> --connection <provider:connection-id> [--account <account-id>]
vbot provider disconnect <provider-id> --connection <provider:connection-id> [--account <account-id>]
```

Use `provider list` before model or agent configuration work to see configured provider connections and whether they are usable. Use `provider status` for one provider or connection. Use `provider set-key` to activate an API-key provider through the server: vBot resolves the configured provider credential key, writes it to the target data-dir `.env`, reloads provider credentials, and prints only the provider connection and credential key name. Add `--refresh-models` to refresh that provider's model catalog immediately after setting the key. `provider unset-key` removes the key from the data-dir `.env`; credentials coming from the process environment are out of its reach and stay usable.

A connection can hold multiple credential **accounts** (named slots; the default slot is `default`). `--account <account-id>` targets a named slot on all five credential commands; account ids are 1-32 characters of lowercase letters, digits, or underscores. Named API-key accounts persist under the derived env key `<BASE>__<ACCOUNT>` (for example `OPENAI_API_KEY__WORK`). `provider list` and `provider status` print each connection's accounts with usable state and source.

Examples:

```bash
vbot provider status openrouter
vbot provider set-key openrouter <api-key> --refresh-models
vbot provider set-key openai <api-key> --connection openai:api-key
vbot provider set-key openai <api-key> --account work
vbot provider unset-key openai --account work
vbot provider list
vbot model refresh openrouter
```

OAuth/subscription connections use the device flow instead of `set-key`; add `--account <account-id>` for an additional login on the same connection:

```bash
vbot provider connect openai --connection openai:subscription
vbot provider connect-status openai --connection openai:subscription
vbot provider disconnect openai --connection openai:subscription
vbot provider connect openai --connection openai:subscription --account personal
```

`connect` starts the flow and prints `user_code`, the verification URL, and the expiry. Relay the code and URL to the user; the server polls for completion in the background. Use `connect-status` to check `connected=` and `flow_active=` afterwards. `set-key` rejects OAuth connections, and `connect` rejects API-key connections.

## Agents

```bash
vbot agent list
vbot agent show <agent-id>
vbot agent create <agent-id> <display-name> [--model <provider/model-id>] [--fallback-model <provider/model-id>] [--temperature <0..2>] [--thinking-effort <effort>] [--memory-prompt-mode off|agent|agent_user] [--custom-system-prompt true|false] [--allowed-tools <tool> ...] [--allowed-skills <skill> ...]
vbot agent update <agent-id> [--name <display-name>] [--model <provider/model-id>] [--fallback-model <provider/model-id>] [--temperature <0..2>] [--clear-temperature] [--thinking-effort <effort>] [--clear-thinking-effort] [--memory-prompt-mode off|agent|agent_user] [--custom-system-prompt true|false] [--allowed-tools <tool> ...] [--allowed-skills <skill> ...] [--current-session-id <session-id>]
vbot agent delete <agent-id>
```

Examples:

```bash
vbot agent list
vbot agent show assistant
vbot agent create coder Coder --model openai/gpt-5.2 --allowed-tools '*' --allowed-skills '*'
vbot agent update coder --temperature 0.4 --thinking-effort high
vbot agent update coder --memory-prompt-mode agent_user --custom-system-prompt true
vbot agent update coder --allowed-tools read_file edit_file --allowed-skills debugging vbot-cli
vbot agent update coder --clear-temperature --clear-thinking-effort
vbot agent delete old-agent
```

Supported `--thinking-effort` values:

```text
none
minimal
low
medium
high
xhigh
max
```

`--model` and `--fallback-model` accept plain `<provider>/<model-id>` values or pinned forms with `::<connection>[:<account>]` (for example `openai/gpt-5.2::api-key:work`) to bind the agent to a specific connection and credential account. `--clear-temperature` and `--clear-thinking-effort` send JSON `null` so the agent inherits current defaults. `--thinking-effort none` is the literal no-reasoning value, not a clear operation. `--allowed-tools` and `--allowed-skills` replace the full allowlist; pass the flag with no values to set an empty list. `--memory-prompt-mode` controls which workspace memory files become prompt-visible; `--custom-system-prompt` toggles the agent's own editable prompt fragments.

## Projects

```bash
vbot project add <path> [--name <display-name>] [--default-agent <agent-id>] [--default-model <provider/model-id>] [--auto-load <file> ...]
vbot project list
vbot project show <project-id>
vbot project set <project-id> [--cwd <path>] [--name <display-name>] [--default-agent <agent-id>] [--default-model <provider/model-id>] [--auto-load <file> ...]
vbot project rm <project-id>
```

Examples:

```bash
vbot project add ./my-repo --name vbot --default-agent orchestrator --auto-load AGENTS.md docs/guide.md
vbot project list
vbot project show vbot
vbot project set vbot --default-agent builder
vbot project set vbot --cwd ./moved-repo
vbot project rm vbot
```

A project points vBot at a repo directory (`cwd`) and exposes the agents discovered in that repo (its **team**). `project add` needs only the repo path; `--name` sets the display name (and derives the project id), `--default-agent`/`--default-model` set project defaults, and `--auto-load` lists repo files folded into project agent prompts (pass the flag with no values to clear it on `set`).

`project add` and `project show` print the **scan preview**: the team plus a report of anything unclean under what exists (bad or unconfigured model, slug collision, unslugifiable name). An empty folder is a valid project with an empty team and a clean report. `project show` re-scans the repo live, so the team reflects the current repo. `project set --cwd` re-points the repo and re-scans.

`project rm` archives the project's runtime anchor (never the repo) and prints the archive path. It is blocked while a project agent has an active or queued run (`project_busy`) or a cron job points at a project agent (`project_in_use`); clear the run or retarget/delete the cron job first.

## Sessions

```bash
vbot session list <agent>
vbot session create <agent> [--id <session-id>] [--make-current]
vbot session link-channel <agent-id> <session-id> --channel <channel-id> --conversation <platform-conv-id>
```

Examples:

```bash
vbot session list assistant
vbot session list orchestrator@vbot
vbot session create assistant --make-current
vbot session create orchestrator@vbot --id research-notes
vbot session link-channel assistant research-notes --channel tg-main --conversation 12345
```

The `<agent>` argument of `session list` and `session create` takes a bare identity agent (`assistant`) or a project agent in the address form `agent@projekt` (`orchestrator@vbot`). A bare agent is unchanged identity behavior; `agent@projekt` opens the session under that project, against its scanned team. `session list` shows session ids, created/last-active timestamps, and the linked source channel when one exists. `session create` without `--id` lets the server generate the id; `--make-current` switches the agent's active session. `session link-channel` routes the session's outbound replies to a platform conversation, such as a Telegram chat id.

## Models

```bash
vbot model list
vbot model refresh [<provider-id>]
```

Examples:

```bash
vbot model refresh
vbot model refresh openrouter
vbot model list
```

Omitting the provider id refreshes all refreshable providers.

## Task Models

```bash
vbot task-model list
vbot task-model targets <task-type>
vbot task-model options <task-type> <target-id>
vbot task-model set <task-type> <target-id> [--options <json-object>]
vbot task-model clear <task-type>
```

Supported `<task-type>` values:

```text
image_generation
speech_to_text
text_embedding
text_to_speech
video_generation
```

Target ids are `<provider>/<model>::<connection>` or `local/<id>`; read them from `task-model targets <task-type>` instead of constructing them by hand. An optional trailing `:<account-id>` pins a credential account (`openai/gpt-4o-mini-tts::api-key:work`); `task-model targets` lists targets connection-level, so append the account part yourself when needed.

Examples:

```bash
vbot task-model list
vbot task-model targets speech_to_text
vbot task-model options text_to_speech openai/gpt-4o-mini-tts::api-key
vbot task-model set text_to_speech openai/gpt-4o-mini-tts::api-key --options '{"voice": "alloy"}'
vbot task-model set text_embedding openai/text-embedding-3-small::api-key
vbot task-model clear image_generation
```

`--options` must be one JSON object passed as a single shell argument; check the valid keys with `task-model options` first. `task-model set` updates only the given task type and leaves other bindings unchanged.

## Skills

```bash
vbot skill list
```

The output includes loadable skills and an `invalid skills:` section when diagnostics exist.

## Tools

```bash
vbot tool list
```

Use this to inspect public registered tools. Internal system-managed tools are omitted by the server.

## Extensions

```bash
vbot extensions list
vbot extensions enable <extension-name>
vbot extensions disable <extension-name>
```

`list` shows loaded, failed (with the error), and disabled extensions plus each one's
contributed capabilities (hooks, tools, recall backends, startup/shutdown). `enable` and
`disable` edit the `extensions` settings section and are **restart-applied** — extensions
are never hot-reloaded, so the command prints a restart hint and you must run
`vbot server restart` for the change to take effect.

## Prompts

```bash
vbot prompt list
vbot prompt update <fragment-name> --content <text>
vbot prompt update <fragment-name> --file <path>
vbot prompt reset <fragment-name>
vbot prompt preview <agent-id>
```

Examples:

```bash
vbot prompt list
vbot prompt update tools.md --file ./tools.md
vbot prompt reset skills.md
vbot prompt preview assistant
```

`prompt list` shows editable fragments, modified state, and variable placeholders. `prompt update` sends replacement content through server RPC; use `--file` for multi-line content. `prompt preview` prints token metadata and the rendered System Prompt for one agent.

## Logs

```bash
vbot log list
vbot log read <daily-log-name>
```

Examples:

```bash
vbot log list
vbot log read 2026-05-11.log
```

`log list` shows daily log files newest-first. `log read` takes a file name exactly as listed (`<date>.log`) and returns parsed entries and a cursor for live-tail handoff.

## Messaging Channels

```bash
vbot channel add <channel-id> --platform telegram|discord --agent <agent-id> --token-env <ENV_VAR> [--dm-scope <scope>] [--allow <platform-chat-id> ...]
vbot channel update <channel-id> [--platform telegram|discord] [--agent <agent-id>] [--token-env <ENV_VAR>] [--dm-scope <scope>] [--allow <platform-chat-id> ...] [--enabled true|false]
vbot channel list
vbot channel status <channel-id>
vbot channel enable <channel-id>
vbot channel disable <channel-id>
vbot channel remove <channel-id>
```

Examples:

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN --allow 12345
vbot channel add tg-work --platform telegram --agent assistant --token-env TELEGRAM_WORK_BOT_TOKEN --dm-scope per_peer --allow 12345 67890
vbot channel add dc-main --platform discord --agent assistant --token-env DISCORD_BOT_TOKEN --allow 123456789012345678
vbot channel update tg-work --agent coder --allow 12345 67890 24680
vbot channel enable tg-main
vbot channel status tg-main
```

`channel update` is a partial update: omitted fields remain unchanged. Passing `--allow` replaces the full allowed chat-id list. Use `--enabled true` or `--enabled false` for config-level enabled state; use `channel enable` and `channel disable` for the common on/off operation.

Telegram allowlist entries are chat ids. Discord entries are channel/thread ids, not guild ids; enable the Message Content Intent in the Discord Developer Portal before starting the channel.

Supported `--dm-scope` values:

```text
per_conversation
main
per_peer
per_account_channel_peer
```

## Cron Jobs

```bash
vbot cron list
vbot cron create <agent> --prompt <text> (--cron <cron-expression> | --at <iso-datetime>) [--timezone <iana-timezone>] [--session <session-id>]
vbot cron update <job-id> [--agent <agent>] [--prompt <text>] [--cron <cron-expression> | --at <iso-datetime>] [--timezone <iana-timezone>] [--session <session-id>] [--status active|paused|completed]
vbot cron delete <job-id>
vbot cron enable <job-id>
vbot cron disable <job-id>
```

Examples:

```bash
vbot cron list
vbot cron create assistant --prompt "Check the news" --cron "0 9 * * *" --timezone Europe/Berlin
vbot cron create builder@vbot --prompt "Nightly build" --cron "0 2 * * *"
vbot cron create assistant --prompt "Remind me about the deadline" --at 2026-07-01T09:00:00
vbot cron update <job-id> --prompt "Check the news and the weather"
vbot cron update <job-id> --agent builder@vbot
vbot cron update <job-id> --status paused
vbot cron disable <job-id>
vbot cron delete <job-id>
```

`cron create` requires exactly one of `--cron` (recurring) or `--at` (one-time); the CLI derives the schedule type from which flag you pass. The `<agent>` argument (and `cron update --agent`) takes a bare agent or the `agent@projekt` address form to target a project agent; firing such a job runs in that project. `--session` pins the job to a fixed session instead of a job-managed one. `cron list` shows id, target (in `agent@projekt` form for a project target, bare for an identity target), status, schedule, next fire time, and a prompt preview — read job ids from there.

## Debug

```bash
vbot debug status
vbot debug traces
vbot debug trace <trace-id>
vbot debug clear
vbot debug probe <provider-id> --connection <provider:connection-id>
```

Examples:

```bash
vbot debug status
vbot debug probe openai --connection openai:api-key
vbot debug traces
vbot debug trace <trace-id>
vbot debug clear
```

`probe` fetches the provider's models endpoint with the connection's credentials and prints status, duration, and a model preview; the full raw response is stored as a trace and read with `debug trace <trace-id>`. `traces`, `trace`, and `probe` fail while debug mode is disabled; enable it with `vbot config set debug '{"enabled": true}'`. `status` and `clear` always work.

## Verification Pattern

After every change, run a read command from the same area:

```bash
vbot config get <key>
vbot doctor settings
vbot doctor config
vbot agent show <agent-id>
vbot agent list
vbot project list
vbot project show <project-id>
vbot session list <agent>
vbot channel status <channel-id>
vbot channel list
vbot provider list
vbot provider status <provider-id>
vbot provider connect-status <provider-id> --connection <provider:connection-id>
vbot model list
vbot task-model list
vbot cron list
vbot skill list
vbot tool list
vbot prompt list
vbot log list
vbot debug status
vbot server status
```
