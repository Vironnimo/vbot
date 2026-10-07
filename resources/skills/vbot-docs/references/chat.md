# Chat from the shell

`vbot chat` sends one message to an Agent Session, waits until the Run it starts ends, and prints the answer.

```bash
vbot chat [<prompt>] [--agent <agent>] [-c | --session <session-id> | --project <project-id> | --workspace] [--model <provider/model-id>] [--thinking-effort <effort>] [--temperature <0.0-2.0>] [--json]
```

- `<prompt>` is the message. With `-` or without `<prompt>`, the message is read from piped stdin; without `<prompt>` and without piped stdin the command fails with a usage error and sends nothing. Pipe or redirect long or multiline text: `vbot chat --agent reviewer - < notes.md`.
- `--agent` takes an Identity Agent id or `agent@project`; the default is `main`.
- Session choice:
  - Neither `-c` nor `--session`: every call creates a fresh Session.
  - `-c` (`--continue`): continues the Agent's most recently active conversation Session. Sub-Agent, reflection, Cron and Channel Sessions are never selected. Without a conversation Session the command fails before sending.
  - `--session <session-id>`: continues that Session of the Agent. `vbot session list <agent>` lists Session ids.
  - A new Session works in one place for its whole life: by default the Agent's default Project (`vbot agent show` prints it as `default_project`), or its Workspace when it has none. `--project <project-id>` starts it in that registered Project instead, and `--workspace` in the Agent's Workspace. A continued Session keeps working where it started. An `agent@project` address always works in that Project and refuses both flags.
  - `-c`, `--session`, `--project` and `--workspace` cannot be combined.
- `--model`, `--thinking-effort` (`none|minimal|low|medium|high|xhigh|max`) and `--temperature` are saved on the Session and apply to every later Run in that Session, whichever client sends the message. Omitting them keeps the Session's saved values. They never change the Agent's configuration; `vbot agent update` does that.
- A message that starts with `/` is a Built-in Command such as `/status`; its reply is printed instead of an answer and no Run starts.

```bash
vbot chat "Summarize today's calendar"
vbot chat --agent coder@vbot -c "Continue with the next step"
git diff | vbot chat --agent reviewer --model openrouter/anthropic/claude-sonnet-4
vbot chat --agent researcher --session <session-id> --json "List the sources you used"
vbot chat --agent coder --project my-repo "Run the tests and fix failures"
```

## Output and exit codes

- stdout holds only the answer: the text of the Run's final Model step. In a terminal, the text of every Model step streams as it arrives.
- stderr holds progress lines for Tool calls, Tool errors and Provider retries when stderr is a terminal or with `--output human`, then a completion line naming the Session id, Agent and Model, plus a `Continue:` command for the same Session. `--output plain` omits the completion line. Failures print an `[ERROR]` line with the reason and `Next:` inspection commands on stderr in every output mode.
- `--json` prints one JSON object on stdout instead of the answer text: `agent_id`, `session_id`, `run_id`, `status`, `model`, `message`, `tool_calls` (each with `id`, `name`, `arguments`, `is_error`, `error`, `result_preview`), `usage` (token counts summed over the Run), `duration_ms`, `error`, `agent_overrides` (the Session's saved overrides when this call set any, else `null`), `queue_item`, `command`. `status` is one of `completed`, `failed`, `cancelled`, `interrupted`, `queued`, `command`, `unknown`.
- Exit codes: `0` completed Run or handled Built-in Command; `1` failed, cancelled or interrupted Run, queued message, lost connection to the Run, or failed request; `2` usage error; `130` Ctrl-C, which also cancels the Run.

## Busy Sessions and lost connections

One Session runs one Run at a time. When the chosen Session is busy, the message enters that Session's Queue: the command exits `1`, names the queue item, and does not wait for an answer. The queued message still runs after the current Run ends; sending it again queues a second copy.

For the Agent that runs this shell command, `-c` selects the Session of the current Run whenever that Session is its most recently active conversation, so the message waits in the Queue behind the current Run. Use `--session` with another Session id, or omit `-c` for a fresh Session.

When the connection to the Run is lost, the command exits `1` and reports that the Run can still be running. Before sending the message again, check the Session with `vbot chat --agent <agent> --session <session-id> /status`: its `Activity` line reads `running` while a Run is still active, and the command starts no Run.
