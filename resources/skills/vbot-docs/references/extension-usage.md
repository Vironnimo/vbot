# Using bundled Extensions

Read only the section relevant to the requested operation: [Swarm](#swarm), [MCP](#mcp) or [Computer Use](#computer-use). For creating Extensions, read [extensions.md](extensions.md); for settings and enable/disable, read [configuration.md](configuration.md#extensions).

## Swarm

The bundled Swarm Extension contributes the **Swarms** page. Create a profile,
select configured Models and participant counts, choose an explicit Project or
working directory, and select Tools. The Swarm section of Tools & Skills exposes
Board, Inbox, State and Wiki individually. These private Tools start
enabled; an empty ordinary Tool selection does not disable them. Use their
switches or None to disable them. Changes apply to future Runs.
The profile's Compaction section can set one Compaction Policy for every
participant; while it is off, participants use the global Compaction settings.
Enter a goal and start from the page, or use `/swarm <profile-slug> "goal"`.
The Command supplies only the entered goal; it does not import source Session
history or attachments.

The original request is pinned on the Board. Participants first receive a message
asking them to read it and discuss the request together before implementation;
they decide together when to start the work. If Board access is disabled, the
initial message carries the original request directly.

The shared Wiki is available through `swarm_wiki` in participant Sessions and the
Wiki tab on each Run. Create, search, read and edit free Markdown pages; view their
history, restore earlier versions, and recover deleted pages. Replacing a whole
page requires the revision it is based on, and a passage edit applies only while
its passage still matches, so concurrent edits cannot silently overwrite one
another. Share page links on the Board when others should notice them. Wiki
edits themselves do not send messages or wake participants. CLI users can inspect
the equivalent `wiki` management operation with per-operation help.

Participants use separate durable Sessions and coordinate as peers on a public
Board. Their pings are public posts addressed to participants by name or id, or
to "all" other participants. Delivery mode and
permission to wake idle participants are independent profile settings. Every
automatic wake carries actual Board messages, including in pull mode; Inbox
remains available for manual reads and batch overflow. Participants keep their
assigned names; the state Tool is read-only. Profile edits apply to future
Swarms; applying live communication changes records a revision and old/new values
for inspection.

Participant status comes from Run execution: idle, running, failed, cancelled,
or interrupted. Agents cannot set it. Ending a reply normally returns a
participant to idle while retaining its Session for later Board messages.
Progress, results and requests for help belong on the Board. If everyone is idle,
the Swarm rests without further Model calls; it is not automatically completed.

Stop closes admission and drains owned execution. Resume continues inactive
participants in their existing Sessions; Activity also offers Resume for one
selected inactive participant, including after a failed Run. Usage comes from
canonical Statistics. Disable/reload retains history; interrupted execution never
restarts itself.

After Stop, **Delete Swarm** opens a confirmation and permanently removes that
Swarm's Board, Wiki, retained coordination history and participant Sessions. Its profile remains available for new
Swarms. Failed deletion can be retried; a partially deleted Swarm cannot Resume.

## MCP

The bundled MCP Extension exposes schema-described management operations. Use `vbot extensions operations mcp` to inspect its management surface, `save --stdin` to configure a connection on the vBot server machine, the intended Agent or Swarm Tool switch for access, and `test` followed by an Agent-scoped `invoke` to verify it. Tools, Resources/templates, Prompts, completion, subscriptions, logging, progress, sampling, roots, elicitation, OAuth, and historical task results pass through the connection. MCP connections require explicit Tool opt-in, including in All Tools mode. Explicit Tool denials and Project ceilings still apply. Read [mcp.md](mcp.md) for the full installation and diagnosis workflow.

Each MCP connection presents one stable Tool with search, describe, call, and read actions. Remote Tool schemas are loaded in ordinary results only when requested; catalog changes do not add hundreds of definitions to the Model request. CLI automation uses `vbot extensions run mcp explore <id> --agent <address> --action <action>` to search, describe, or call; its job result contains the complete payload. Inside a Session, large results stay with their Tool Result and return as bounded receipts with `result_id` and a `read` action; JSON Pointer reads, pages, and field projection expose selected data. The [MCP reference](mcp.md) documents setup, discovery, and result handling. Existing context prefixes remain unchanged by discovery; actual prompt-cache hits and pricing remain Provider-dependent.

The MCP row in Settings -> Connections -> Extensions also provides manual connection management: add/edit local programs or HTTP/SSE servers, enter referenced credentials in a write-only dialog, enable/disable, test, and remove connections. Capabilities & access shows the discovered Tools, server guidance, Prompts, and guidance for enabling the connection in Agent or Swarm Tool settings; inspection does not execute application Tools. Connection edits are explicitly saved and may interrupt the current connection. A connection test verifies the server; verify application-level access separately through the intended Agent. CLI operations remain available for automated setup, including `inspect <id>` for the same read-only inspection.

Begin Agent discovery with an unfiltered search: application Tools appear first, alongside server guidance and available Prompts. Searches rank matching words; an empty result supplies a browse action because an application may expose an indirect capability through a general-purpose Tool. Search continuation arguments and saved-result reads preserve access to larger catalogs and guidance without inflating every Model request.

## Computer Use

The bundled `computer_use` Extension lets an Agent operate the Windows desktop of the vBot server host with the user's real mouse and keyboard. It needs no extra installation; it works when the server runs on Windows in a signed-in desktop session. It supplies three opt-in Tools, `computer` (screenshots, zoom, clicks, typing, keys), `computer_batch` (several actions in one call) and `computer_apps` (list apps and displays, bring an app to the front, request approval, hand the computer back). Enable them under the Agent's Tools & Skills; Project Agents also need the Project whitelist and Tool override. The bundled `computer-use` Skill teaches the workflow.

By default, an Agent allowed to use these Tools operates every application without asking. The Extension setting "Ask before each app" (`ask_per_app`) makes the user approve applications per Session instead: an Agent asks with `computer_apps` `request`, the request appears in the vBot app's request dialog, and the Agent waits for the answer for up to five minutes. In that mode, screenshots show apps without approval as gray boxes, input is refused unless the foreground window and the window under the pointer belong to an approved app, and approvals end 30 minutes after the Session's last Computer Use call or when the server restarts or the Extension reloads. In both modes, applications running as administrator cannot receive input.

While an Agent controls the desktop, an orange frame lines the screens; it lasts from the Agent's first desktop call until the Agent hands the computer back with `computer_apps` `release`, its reply ends, or two minutes pass without a call. To stop the Agent at any time during that period, use the Stop control in the Chat composer or press Esc twice within 600 ms anywhere. Stop interrupts a running action, releases held keys and buttons, and removes the frame; the Agent cannot take the computer again until your next message. Input already delivered cannot be undone.
