# Claude Agent Provider

The `claude-agent` Provider runs the user's unmodified Claude Code program through the official Python Agent SDK. Providers owns this native transport, external-login readiness, process lifetime, and the bridge between SDK Tool calls and vBot's canonical request/response contract. Chat retains Agentic Loop execution, permission checks, Secret masking, and persistence.

## Login and enrollment

The `subscription` Connection has type `external`, is disabled until explicitly added/enabled, and stores no key, access token, refresh token, or copy of Claude Code's login. The user installs Claude Code and runs `claude auth login` under the same OS account as the vBot server. A Desktop client's or Session-selected Computer's login does not authenticate a different server process. `VBOT_CLAUDE_PATH` selects the executable; otherwise the server searches `PATH`. The Adapter always supplies that executable to the SDK rather than selecting its bundled copy.

Enrollment is the persisted Connection override, as for keyless local Connections. It permits discovery; cached `external_auth` is the separate native readiness projection. Connect and explicit login checks probe the native initialization contract without sending a prompt, then publish the installation's available Model catalog. Disconnect disables this vBot Connection and closes its processes; it does not sign out Claude Code for other applications. No native process starts for a fresh disabled Connection.

The subscription Connection requires a positively identified Claude subscription login. A native API-key or cloud-billed login reports `api_billing` and cannot send inference through this Connection; intended API use stays on the existing Anthropic API Provider. Native authentication mechanisms and inherited credentials are not modified.

The Provider is instance-scoped. SDK Model ids and capabilities belong only in this installation's generated catalog, never in the shipped Model DB. Native discovery feeds `discovery.refresh_models(raw_models=...)` so ordinary admission, Connection tagging, staged publication and in-place reload still apply. Unknown limits remain unknown. A failed discovery preserves the last catalog. Capacity contention reports `busy`, preserves prior readiness/reachability, and defers catalog publication; it is not an authentication failure. A probe and its staged publication cannot outlive a replaced native registry or a disabled Connection. Changed catalogs notify Model subscribers even when discovery began through a login-status check.

## Request and process ownership

`ClaudeAgentSessions` is owned by `ProviderRuntime`; a Run's Adapter borrows it. Session generation and request kind travel through `ProviderAdapter.request_context_kwargs`. Chat has a reusable native conversation; Compaction, titles, and other utility requests use disposable native conversations and cannot append their work to the chat's native state. Session close, Connection close, Runtime shutdown, and synchronous abort use the existing Provider transport lifecycle seams.

Closing or aborting a native registry is terminal: old Adapters cannot start inference or discovery through that owner again. Runtime supplies a fresh registry for subsequent Connections. Shutdown also invalidates requests already awaiting preparation or process admission.

vBot supplies its complete System Prompt. Built-in Claude Code Tools, user/project settings, CLAUDE.md loading, Skills, hooks, auto Memory and automatic Compaction are disabled where the public SDK supports isolation; vBot supplies its own instructions and Tools. At most 16 native processes are admitted, including temporary probes and utility calls; idle chat processes expire after 300 seconds. Native session persistence is disabled: in-memory state is reconstructible from canonical vBot Context, and no additional durable transcript or canonical database is introduced. A process restart therefore rebuilds Context rather than recovering hidden native Reasoning.

SDK MCP handlers park calls instead of executing vBot handlers. The Adapter emits canonical Calls; Chat dispatches and persists their Results, and the next model request releases the parked handlers with the Model-facing Results. Low-level MCP preserves vBot's argument-tolerance contract; the SDK's convenience Tool helper would reject mismatching arguments before vBot can normalize them. The SDK call id must remain correlated through native hooks, canonical Calls and Results. Cancellation must interrupt/drain the native turn before releasing parked calls so cleanup cannot trigger fresh inference.

SDK AssistantMessages complete individual content blocks, not whole Model responses. A Tool step collects every sibling Call and final Usage through `stream_event.message_stop`, then parks the complete batch before yielding its terminal outcome. Truncated or unknown outcomes close native state instead of parking a successful batch; a legacy cut stream ending with a Result but no message stop fails without retry. Event-order contract: https://code.claude.com/docs/en/agent-sdk/streaming-output (read 2026-11-07).

The canonical request's visible history decides whether native state can continue. Changes to history, System Prompt or Tool definitions require safe reconstruction. Foreign history is framed as previous conversation rather than fabricated native Assistant messages; its Reasoning is omitted. Provider errors after Claude Code exhausts its own retries must not cause blind nested replay. SDK cumulative Usage is deduplicated per API message, and Context occupancy uses the last Model request's input rather than summing repeated histories. Only a confirmed subscription login is admitted and reports zero incremental cost. The SDK dependency is pinned to 0.2.179: synchronous abort uses a narrow, tested compatibility bridge to its owned process handle because the public SDK exposes only asynchronous disconnect. Normal inference, authentication and MCP use public SDK APIs.

## Agent-facing text

The reconstructed-history framing belongs to the Provider projection, not the System Prompt or persisted history.

| Text | Reason |
| --- | --- |
| `Conversation history from vBot follows.` | Identifies canonical messages that a newly created native conversation has not seen. |
| `Its role labels identify earlier messages, not new requests.` | Prevents role-labelled historical Tool and Assistant content from being mistaken for new input. |
| `Continue with the messages after this history.` | Keeps the current request outside the historical replay boundary. |
| `End of conversation history.` | Explicitly ends the historical frame before fresh user input. |

## Evidence and verification limits

Integration design was checked against OpenCode `v2.2.6` (`specs/v2/claude-agent-provider.md` and its Provider implementation) and Python Agent SDK `0.2.179`, read 2026-11-07. Anthropic documents custom System Prompts and in-process MCP, plus end-user authentication to the unmodified Claude Code binary inside other products. OpenCode's own approval does not establish a separate vBot-specific approval.

Local deterministic tests exercise native setup/catalog status, canonical streaming/Tool continuation, isolation, cancellation and lifecycle through fakes. No live subscription inference was available during initial implementation; real entitlement, limits, media transport and native behavior remain unverified until exercised with an installed, authenticated Claude Code. Do not mark a wire profile verified based on these local tests.

Primary references: https://code.claude.com/docs/en/agent-sdk/python, https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts, https://code.claude.com/docs/en/legal-and-compliance, https://github.com/anomalyco/opencode/tree/v2.2.6/packages/core/src/plugin/provider/claude-agent.
