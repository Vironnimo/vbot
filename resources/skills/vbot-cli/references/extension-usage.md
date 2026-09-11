# Using bundled Extensions

Read only the section relevant to the requested operation: [Swarm](#swarm), [MCP](#mcp) or [Computer Use](#computer-use). For creating Extensions, read [extensions.md](extensions.md); for settings and enable/disable, read [configuration.md](configuration.md#extensions).

## Swarm

The bundled Swarm Extension contributes the **Swarms** page. Create a profile,
select configured Models and participant counts, choose an explicit Project or
working directory, and select ordinary Tools. An empty ordinary Tool selection
still allows the three private coordination Tools in participant Sessions.
Enter a goal and start from the page, or use `/swarm <profile-slug> "goal"`.
The Command supplies only the entered goal; it does not import source Session
history or attachments.

Participants use separate durable Sessions and coordinate as peers on a public
Board. Pings are public posts addressed to participant ids. Delivery mode and
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
Swarm's Board and participant Sessions. Its profile remains available for new
Swarms. Failed deletion can be retried; a partially deleted Swarm cannot Resume.

## MCP

The bundled MCP Extension exposes schema-described management operations. Use `vbot extensions mcp operations` to inspect its management surface, `save --stdin` to configure a connection on the vBot server machine, `grant <id> --agent <address>` for access, and `test` followed by an Agent-scoped `invoke` to verify it. Tools, Resources/templates, Prompts, completion, subscriptions, logging, progress, sampling, roots, elicitation, OAuth, and historical task results pass through the connection. Configuration grants do not override an Agent's explicit Tool denials or a Project's Tool Whitelist. Read [mcp.md](mcp.md) for the full installation and diagnosis workflow.

Each MCP connection presents one stable Tool with search, describe, call, and read actions. Remote Tool schemas are loaded in ordinary results only when requested; catalog changes do not add hundreds of definitions to the Model request. CLI automation uses `vbot extensions mcp explore <id> --agent <address> --action <action>` for the same workflow. Large results are preserved completely in files and returned as bounded receipts with `result_id` and `result_file`; JSON Pointer reads, pages, and field projection expose selected data. The [MCP reference](mcp.md) documents setup, discovery, and result handling. Existing context prefixes remain unchanged by discovery; actual prompt-cache hits and pricing remain Provider-dependent.

The MCP row in Settings -> Connections -> Extensions also provides manual connection management: add/edit local programs or HTTP/SSE servers, select Agent grants, enter referenced credentials in a write-only dialog, enable/disable, test, and remove connections. Capabilities & access shows the discovered Tools, server guidance, Prompts, and effective access for granted Agents; inspection does not execute application Tools. Connection edits are explicitly saved and may interrupt the current connection. A connection test verifies the server; verify application-level access separately through the intended Agent. CLI operations remain available for automated setup, including `inspect <id>` for the same read-only inspection.

Begin Agent discovery with an unfiltered search: application Tools appear first, alongside server guidance and available Prompts. Searches rank matching words; an empty result supplies a browse action because an application may expose an indirect capability through a general-purpose Tool. Search continuation arguments and saved-result reads preserve access to larger catalogs and guidance without inflating every Model request.

## Computer Use

The bundled `computer_use` Extension supplies the opt-in `computer` Tool on the vBot server host. Install published stable Cua Driver 0.23.2 or newer, reload Extensions, and enable `computer` under the Agent's Tools & Skills. Project Agents also need the Project whitelist and Tool override. Windows uses existing Pillow and Windows APIs for physical input and screenshots; no compiler, custom Driver build, or browser debugging setup is required. Other platforms use Cua and require `max_image_dimension=0`.

The Extension bundles a `computer-use` Skill with guidance for target selection, drawing, dialogs, recovery, and saving. Start with `windows`, then `capture` the selected `pid`/`window_id`. On Windows, `monitors` discovers displays; `monitor` selects one and omission captures all displays when no window or view is targeted. Browser windows use the same mouse and keyboard actions as other applications.

`move`, `click`, `type`, `key`, `scroll`, and `drag` execute by default. Set `apply: false` for a preview without input. Windows timed key holds and drags accept `duration_ms` up to 2000; background drags also support duration and default to 250 ms. Window element actions, menus, launch, resize, and verification remain available.

Window capture and control use `foreground: false` by default. Cua uses window-directed input and its own window images; its `max_image_dimension` must be zero to preserve original coordinates. A `view_id` also supplies the target and delivery setting when omitted. Use `foreground: true` explicitly when foreground input is needed, capturing with that setting first. Support depends on the application: an independent cursor overlay is not an independent Windows input session, and some applications can activate themselves. An observed target activation is reported and stops remaining sequence steps. The Extension never retries a background action in the foreground automatically. Desktop input, timed holds, and held modifiers use the shared mouse/keyboard.

Coordinates use `coordinate: [x, y]`; drag and zoom also take `to_coordinate: [x, y]`. A valid `view_id` is sufficient to identify a captured window, including for zoom; repeating its window ids is optional. Resize takes a screen position and `size: [width, height]`. Without a view or window ids, input selects the desktop. Windows foreground coordinate click, drag, and scroll accept `modifiers`, for example `["ctrl", "shift"]`; held keys release after the action or Stop.

Input and `sequence` capture automatically after a one-second observation delay. This does not prove application completion and does not poll for changing or quiet pixels. Use `verify` for a concrete window/element condition or `wait` for a later image. Set `capture_after: false` to skip the final image and delay when unnecessary; capture again before further input. Partial sequences still attempt an observation. `wait` pauses for `duration_ms` (default 1000, maximum 10000) and then captures; within a sequence it adds no intermediate screenshot. Stop interrupts waits immediately.

Observations default to screenshots without element lists. Request `mode: som` for a screenshot plus compact window elements, or `mode: ax` for elements only; supplying `query` or `limit` also requests elements. These options apply to the observation after input, avoiding a separate capture when the next step needs named controls. Overviews are bounded to 1600 pixels on the long edge and 1.5 million pixels. Originals remain available through `resolution: original` or native-resolution `zoom` crops. Zoom preserves recent parent/crop views until new capture or input retires them. Use coordinates from the displayed image. Negative monitor origins and physical DPI coordinates are handled internally; changed geometry requires a fresh capture. Foreground captures reject other foreground applications rather than mislabeling their pixels.

`sequence` executes up to eight known mouse/key/text actions with one final observation. Set `view_id` once on the sequence to share it across coordinate steps. Only the first step may use an element reference. Results report completed/total steps, numbered effects, and the stopped step on failure, partial effect, or a suspected no-op. Zero-completion failures retain their recovery observation in `artifacts`. Windows modal dialogs are returned with their actual target, so the next action can address the dialog directly. Keep sequences short when the layout may change; uncertain input never retries automatically.

On Windows, press **Esc twice within 600 ms**, releasing it between presses, to interrupt the active Computer Use call from any foreground application. Detection is armed only while a call runs. Single presses, key auto-repeat, and injected Agent keystrokes do not trigger interruption. The listener passes keys through and dispatches interruption outside the keyboard hook. A pending double-Esc ends remaining steps of that call; delayed callbacks cannot interrupt its successor.

Chat's existing composer shows a **Stop computer control** icon during an active call and disables it while that call is stopping. There is no paused state or release control. Stop addresses the invocation shown by the latest status response, so a stale UI request cannot interrupt a later call. Stop, Tool/Run cancellation, and double-Esc interrupt the owned input worker, release held keys/buttons, and report interruption to the Agent through the Tool result. Completed sequence steps remain visible. Later Tool calls remain available, including after reload or restart; no stop state is persisted. Already delivered input cannot be rolled back.

The Extension version remains 1.0.0. References: [Cua installation](https://cua.ai/docs/how-to-guides/driver/install), [Windows input](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput), and [Pillow screenshots](https://pillow.readthedocs.io/en/stable/reference/ImageGrab.html).
