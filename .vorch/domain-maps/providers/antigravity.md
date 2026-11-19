# Google Antigravity subscription

`antigravity:subscription` runs the locally installed Google Antigravity CLI (`agy`) under the server's OS account. The Google AI Studio Provider (`google:api-key`) uses a separate service and billing arrangement. Google's [Antigravity FAQ](https://antigravity.google/docs/faq) excludes embedding the CLI as a third-party Agent backend under personal plans. The [ACP editor integration](https://antigravity.google/docs/ide/extensions/acp/) hosts Google's interactive Agent and has a different contract.

## Login, discovery and quotas

The external Connection is opt-in. Install `agy` and use its own personal login on the server. Executable resolution checks `VBOT_ANTIGRAVITY_PATH`, then PATH and the platform's standard per-user install location. Native probes and inference use a private profile and the operating system's credential store. File-only logins that the CLI cannot find from the private profile are unavailable. vBot does not read token files.

The CLI's JSON `models` command supplies the exact IDs and labels, including distinct effort variants. Discovery skips Google's [Orchestrator workflows](https://antigravity.google/docs/models/#orchestrator), which hand work to another Model inside a turn. Only this installation publishes the catalog. Text and vBot Tools are enabled.

The bundled discovered-only override applies Context limits to listed IDs, including existing installation catalogs. On 2026-11-19, CLI 1.5.12 reported the window of each of 21 exact selectors through ACP `session/update` (`usage_update.size`) and the [status line](https://antigravity.google/docs/cli/statusline/) without inference. Gemini 3.8 Flash, Gemini 4 Argon and Gemini 4 Hydrogen reported 1,048,576 tokens; Claude Opus/Sonnet 5.5 reported 1,000,000; GPT-OSS 120B reported 131,072. Gemini 4 Neon also reported 1,048,576, but the [product documentation](https://antigravity.google/docs/models/#gemini-4-neon) limits it to 512K, so vBot budgets 512,000. Output limits remain unknown. New selectors can be probed by selecting them in a disposable ACP session and waiting for the corresponding usage notification; select a Model with a different window in between because the CLI suppresses unchanged sizes.

The zero-inference `/usage` command returns model groups with separate quota buckets. The Connection requires evidence of both five-hour and weekly windows. It omits API and ADC credentials from the child environment and disables AI credit overages in CLI settings. Each turn rechecks the account before inference. That successful check also updates the passive Chat quota observation (`providers/usage.md`). The standard `provider usage` report displays a group's available buckets and reset times.

## Native turns

The Tool inventory and both hooks run with the server runtime's Python interpreter. On packaged Windows this is the sibling `python.exe`. Launching the server-role executable with script flags instead invokes the server entry point. vBot disables the CLI [background updater](https://antigravity.google/docs/cli/troubleshooting/) in its private child environment because a new profile lacks the updater's debounce marker and its detached version check can open Windows Terminal on every request. Interactive CLI updates keep the user's settings.

Each Model step gets a disposable private workspace. The custom Agent definition carries the vBot System Prompt. MCP advertises the vBot Tools and their parameter schemas. The pre-execution hook captures the proposed call and denies native execution. Native permission rules independently deny filesystem, command, URL and MCP access. The Adapter requires the matching hook receipt and the CLI's terminal denial for every Tool before handing the batch back to Chat. Advertised vBot Tools go through normal permission checks and dispatch. Other Tools return a correlated failure without dispatch, including native names that coincide with a vBot Tool. The CLI can propose built-in Tools even when the custom Agent excludes default components and declares an empty native Tool list. It may also supply extra context to the Model.

Chat supplies canonical history again on each step, including Assistant Tool Calls and correlated Tool Results. System instructions stay separate. The transcript omits private Assistant Reasoning. Tool Results use the ordinary vBot rendering. A JSON escape preserves literal `@` without activating the CLI's file references. Native context and caches are transient. Session data remains owned by vBot. The shared Storage owner leases and removes the request workspace, including any temporary CLI state (`../storage/temporary-files.md`).

The headless stream reports the Model's token counts on the completed `agent_response` step. A native `result` covers the entire turn and can include extra checkpoint work. The Adapter preserves the turn total for accounting and the Model step for the Chat Context anchor. In a live `gemini-3.8-flash-medium` continuation, 2,172 fresh input tokens plus 6,666 cache reads made an 8,838-token prompt. Google's `total_tokens` excluded cache reads, while output already included thinking.

Each name in the vBot MCP inventory comes from the request's Tool list. Chat exposes its fixed `call_tool` proxy for Tools discovered through `load_tools` or announced during the prompt epoch. Existing epochs receive the proxy when a callable Tool is missing from their pinned list (`../chat/request-building.md` -> Fixed proxy transport).

## Agent-facing text

| Text | Purpose |
| --- | --- |
| `Continue this vBot conversation as the assistant.` | A newly launched CLI must continue from the last message, including after a Tool Result. |
| `Each JSON line is one message in chronological order.` | The CLI receives one input turn containing the transcript. This identifies the boundaries between user messages, Assistant turns and Tool Results. |
| `Use the vbot MCP server for tool calls.` / `Answer directly without calling tools.` | Directs the Model to the Tools enabled for this request. CLI 1.5.12 proposed its built-in `manage_task` even with an empty native Tool list. |
| `This tool was blocked before execution.` followed by `Use a tool from the vbot MCP server or answer directly.` or `Answer directly.` | Tells the Model the effect of the blocked call and how to continue with the inventory available on this step. |

## Verification

In separate packaged Chat Runs with CLI 1.5.12 and `gemini-4-hydrogen-high`, a native `manage_task` received a correlated failure and the Run recovered by reading a random file value. An Agent with only an on-demand `read` retrieved a second random value through `load_tools` and `call_tool`. Both Runs completed with the exact expected answer.

On Windows, a copy of the installed native server reproduced the missing guard on `gemini-4-hydrogen-high`. With the packaged Python and updater fixes, the same chat read an unpredictable file value through vBot in two Model steps. A visible-window observer on the test and interactive desktops recorded no console or Terminal window during enrollment or the Run.

Google-signed Windows CLI 1.5.12, 2026-11-19. The native login returned 23 selector entries and five quota buckets. Two entries were Orchestrator workflows. A regular vBot Chat Run on `gemini-3.8-flash-medium` read an unpredictable value from its Agent Workspace through the vBot `read` Tool and returned the exact value in two Model steps. A separate live check removed the pre-execution hook and confirmed that the CLI's deny rule prevented its native Tool from writing a file. See the [CLI headless contract](https://antigravity.google/docs/cli/headless/), [CLI changes](https://antigravity.google/docs/changelog?tab=cli) and [personal plan limits](https://antigravity.google/docs/plans).
