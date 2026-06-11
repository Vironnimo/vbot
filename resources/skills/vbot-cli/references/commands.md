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
vbot provider set-key <provider-id> <api-key> [--connection <provider:connection-id>] [--refresh-models]
vbot provider connect <provider-id> --connection <provider:connection-id>
vbot provider connect-status <provider-id> --connection <provider:connection-id>
vbot provider disconnect <provider-id> --connection <provider:connection-id>
```

Use `provider list` before model or agent configuration work to see configured provider connections and whether they are usable. Use `provider status` for one provider or connection. Use `provider set-key` to activate an API-key provider through the server: vBot resolves the configured provider credential key, writes it to the target data-dir `.env`, reloads provider credentials, and prints only the provider connection and credential key name. Add `--refresh-models` to refresh that provider's model catalog immediately after setting the key.

Examples:

```bash
vbot provider status openrouter
vbot provider set-key openrouter <api-key> --refresh-models
vbot provider set-key openai <api-key> --connection openai:api-key
vbot provider list
vbot model refresh openrouter
```

OAuth/subscription connections use the device flow instead of `set-key`:

```bash
vbot provider connect openai --connection openai:subscription
vbot provider connect-status openai --connection openai:subscription
vbot provider disconnect openai --connection openai:subscription
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

`--clear-temperature` and `--clear-thinking-effort` send JSON `null` so the agent inherits current defaults. `--thinking-effort none` is the literal no-reasoning value, not a clear operation. `--allowed-tools` and `--allowed-skills` replace the full allowlist; pass the flag with no values to set an empty list. `--memory-prompt-mode` controls which workspace memory files become prompt-visible; `--custom-system-prompt` toggles the agent's own editable prompt fragments.

## Sessions

```bash
vbot session list <agent-id>
vbot session create <agent-id> [--id <session-id>] [--make-current]
vbot session link-channel <agent-id> <session-id> --channel <channel-id> --conversation <platform-conv-id>
```

Examples:

```bash
vbot session list assistant
vbot session create assistant --make-current
vbot session create assistant --id research-notes
vbot session link-channel assistant research-notes --channel tg-main --conversation 12345
```

`session list` shows session ids, created/last-active timestamps, and the linked source channel when one exists. `session create` without `--id` lets the server generate the id; `--make-current` switches the agent's active session. `session link-channel` routes the session's outbound replies to a platform conversation, such as a Telegram chat id.

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

Target ids are `<provider>/<model>::<connection>` or `local/<id>`; read them from `task-model targets <task-type>` instead of constructing them by hand.

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

## Telegram Channels

```bash
vbot channel add <channel-id> --platform telegram --agent <agent-id> --token-env <ENV_VAR> [--dm-scope <scope>] [--allow <chat-id> ...]
vbot channel update <channel-id> [--platform telegram] [--agent <agent-id>] [--token-env <ENV_VAR>] [--dm-scope <scope>] [--allow <chat-id> ...] [--enabled true|false]
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
vbot channel update tg-work --agent coder --allow 12345 67890 24680
vbot channel enable tg-main
vbot channel status tg-main
```

`channel update` is a partial update: omitted fields remain unchanged. Passing `--allow` replaces the full allowed chat-id list. Use `--enabled true` or `--enabled false` for config-level enabled state; use `channel enable` and `channel disable` for the common on/off operation.

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
vbot cron create <agent-id> --prompt <text> (--cron <cron-expression> | --at <iso-datetime>) [--timezone <iana-timezone>] [--session <session-id>]
vbot cron update <job-id> [--agent <agent-id>] [--prompt <text>] [--cron <cron-expression> | --at <iso-datetime>] [--timezone <iana-timezone>] [--session <session-id>] [--status active|paused|completed]
vbot cron delete <job-id>
vbot cron enable <job-id>
vbot cron disable <job-id>
```

Examples:

```bash
vbot cron list
vbot cron create assistant --prompt "Check the news" --cron "0 9 * * *" --timezone Europe/Berlin
vbot cron create assistant --prompt "Remind me about the deadline" --at 2026-07-01T09:00:00
vbot cron update <job-id> --prompt "Check the news and the weather"
vbot cron update <job-id> --status paused
vbot cron disable <job-id>
vbot cron delete <job-id>
```

`cron create` requires exactly one of `--cron` (recurring) or `--at` (one-time); the CLI derives the schedule type from which flag you pass. `--session` pins the job to a fixed session instead of a job-managed one. `cron list` shows id, agent, status, schedule, next fire time, and a prompt preview — read job ids from there.

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
vbot session list <agent-id>
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
