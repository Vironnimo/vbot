# Google Antigravity subscription

`antigravity:subscription` runs the locally installed Google Antigravity CLI (`agy`) under the server's OS account. The Google AI Studio Provider (`google:api-key`) uses a separate service and billing arrangement. Google's [Antigravity FAQ](https://antigravity.google/docs/faq) excludes embedding the CLI as a third-party Agent backend under personal plans. The [ACP editor integration](https://antigravity.google/docs/ide/extensions/acp/) hosts Google's interactive Agent and has a different contract.

## Login, discovery and quotas

The external Connection is opt-in. Install `agy` and use its own personal login on the server. Executable resolution checks `VBOT_ANTIGRAVITY_PATH`, then PATH and the platform's standard per-user install location. Native probes and inference use a private profile and the operating system's credential store. File-only logins that the CLI cannot find from the private profile are unavailable. vBot does not read token files.

The CLI's JSON `models` command supplies the exact IDs and labels, including distinct effort variants. Discovery skips Google's [Orchestrator workflows](https://antigravity.google/docs/models/#orchestrator), which hand work to another Model inside a turn. Only this installation publishes the catalog. Text and vBot Tools are enabled. Token limits and media support cannot be inferred from the corresponding AI Studio Models.

The zero-inference `/usage` command returns model groups with separate quota buckets. The Connection requires evidence of both five-hour and weekly windows. It omits API and ADC credentials from the child environment and disables AI credit overages in CLI settings. Each turn rechecks the account before inference. The standard `provider usage` report displays a group's available buckets and reset times.

## Native turns

Each Model step gets a disposable private workspace. The custom Agent definition carries the vBot System Prompt. MCP advertises the vBot Tools and their parameter schemas. The pre-execution hook captures the proposed call and denies native execution. Native permission rules independently deny filesystem, command, URL and MCP access. An advertised vBot Tool returns to Chat for permission checks, dispatch and persistence. An unexpected native Tool fails the Model step. The native CLI may still display its own Tools and may supply extra context to the Model.

Chat supplies canonical history again on each step, including Assistant Tool Calls and correlated Tool Results. System instructions stay separate. The transcript omits private Assistant Reasoning. Tool Results use the ordinary vBot rendering. A JSON escape preserves literal `@` without activating the CLI's file references. Native context and caches are transient. Session data remains owned by vBot. The shared Storage owner leases and removes the request workspace, including any temporary CLI state (`../storage/temporary-files.md`).

## Agent-facing text

| Text | Purpose |
| --- | --- |
| `Continue this vBot conversation as the assistant.` | A newly launched CLI must continue from the last message, including after a Tool Result. |
| `Each JSON line is one message in chronological order.` | The CLI receives one input turn containing the transcript. This identifies the boundaries between user messages, Assistant turns and Tool Results. |

## Verification

Google-signed Windows CLI 1.5.12, 2026-11-19. The native login returned 23 selector entries and five quota buckets. Two entries were Orchestrator workflows. A regular vBot Chat Run on `gemini-3.8-flash-medium` read an unpredictable value from its Agent Workspace through the vBot `read` Tool and returned the exact value in two Model steps. A separate live check removed the pre-execution hook and confirmed that the CLI's deny rule prevented its native Tool from writing a file. See the [CLI headless contract](https://antigravity.google/docs/cli/headless/), [CLI changes](https://antigravity.google/docs/changelog?tab=cli) and [personal plan limits](https://antigravity.google/docs/plans).
