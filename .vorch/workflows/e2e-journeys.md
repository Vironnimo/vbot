# E2E Journeys

The journey catalog of `.vorch/workflows/e2e-test-workflow.md`: what every E2E sweep proves at least. Each group runs in its own environment, its journeys in the order listed. Steps name visible labels; take them from the current snapshot when the wording has moved. Exact texts in quotes are what the fake Provider or the WebUI must show.

## General

**Fake Provider.** `scripts/fake-provider.mjs` answers from markers in the user's message (`E2E_STREAM`, `E2E_SLOW`, ...), sometimes from the Model name or the System Prompt. Its plain answer is "Fake provider response.". Things that surprise testers:

- A Tool scenario advances by counting its own Tool results in the whole Session history, so the same marker a second time in one Session jumps to its final text. Use a new Session per Tool journey.
- Model `fake/e2e-fallback::default` answers "Fallback provider response." to every prompt. A `/model` override is stored on the Agent, not the Session: reset it (`/model reset`) at the end of the journey that set it.
- While a System Prompt block containing `E2E custom provider context 5821` exists, almost every text answer becomes "Custom System Prompt reached the Provider.". Journey G4 removes it.
- Once a Session contains `E2E_COMMAND_CONTINUE`, any later Run whose input carries vBot's interruption notice ("previous turn") answers "Continued interrupted work completed.".
- Request bodies above 1 MiB are refused; keep Sessions short.
- Speech and image endpoints accept only their journey's exact input (T3).

**WebUI.** "New session" in the Sessions drawer opens a draft; a Session row appears only with its first message. The drawer floats over the timeline: close it before clicking into the timeline. The Chat also has a visually hidden status region that repeats each answer and error, so page text shows them twice; check the timeline itself. A Tool row shows the Tool name and a status dot (done, error, cancelled); the shell Tool's row may read `bash` on Windows too. Before clicking a row in a list, move the pointer away; a tooltip can cover the row above.

**RPC for setup and cleanup.** Prerequisites such as an extra Agent go through RPC, not the UI: `POST <url>/api/rpc` with `{"method": "...", "params": {...}}` from the local machine needs no login and answers `{"ok": ..., "result": ...}`. Delete what a journey created at its end, also after a failure.

## Group chat - Runs and the timeline

### C1 Streaming and reattachment

1. New Session, send `E2E_STREAM Please demonstrate streaming`. Expect "Fake provider streaming response." to arrive in word chunks, then no "· Running" and "New session" enabled.
2. New Session, send `E2E_SLOW Follow this Run across a reload and a second tab` (about 16 s: "Slow response started." plus 80 " still running" chunks). After the first chunk, reload. Expect the prompt exactly once, "Cancel run" visible and the chunk count still rising.
3. Open a second tab at `<url>/#chat`. It opens the same Session by itself: the prompt once and "Cancel run".
4. When the Run ends, both tabs lose "Cancel run" and show the prompt once, one answer, exactly 80 " still running" chunks: nothing duplicated by replay, nothing lost.
5. New Session, send `E2E_SLOW Preserve this completed turn exactly once`. After the first chunk set the browser offline (`playwright-cli run-code "async page => page.context().setOffline(true)"`) and click "Agents". Wait about 18 s, go online, wait for "Connected", open Chat. Expect the prompt once, exactly one assistant message, no "· Running".

### C2 Stop, errors, fallback and resuming

1. New Session, send `E2E_SLOW Keep streaming until cancelled`; after "Slow response started." click "Cancel run" (window: about 16 s). Expect "Cancelled", the partial text kept, the "Cancel run" button gone, and no "Interrupted work retained" text or "Discard" button.
2. New Session, send `E2E_ERROR Return the configured failure`. Expect exactly "Provider error: 400 E2E provider failure" with an "ERROR" label and no Continue or Discard controls. A 400 must not switch to the fallback Model.
3. New Session, send `E2E_FALLBACK Exercise model fallback` (vBot may retry first; allow 45 s). Expect "Fallback provider response." and exactly once the notice "fake/e2e-primary::default was unavailable. Switched to fake/e2e-fallback::default for this Run.". Reload: the prompt, the answer and the notice each still exactly once.
4. New Session, send `E2E_SLOW E2E_COMMAND_CONTINUE retain this interrupted work`; after "Slow response started." type `/stop` and click the send button (now "Queue message"). `/stop` runs at once. Expect "Run cancelled." and "Cancelled".
5. Send `Resume after cancellation`. Expect "Continued interrupted work completed.".
6. Send `E2E_ERROR E2E_COMMAND_CONTINUE simulate a Provider interruption`, then `Resume after provider interruption`. Expect the provider error, then "Continued interrupted work completed." a second time. Throughout: no "Interrupted work retained", no Continue or Discard buttons.

### C3 Queue

Window: the first Run lasts about 8 s; type the queued messages and edits quickly, or repeat in a new Session.

1. New Session, send `E2E_QUEUE_ACTIVE Keep this Run active`. While it runs, queue three messages with "Queue message": `E2E_QUEUE_FIRST Original first queued message`, `E2E_QUEUE_REMOVED This message must never run`, `E2E_QUEUE_THIRD Final queued message`.
2. In "Queued messages": three items, FIRST on top. Edit FIRST to `E2E_QUEUE_FIRST Edited first queued message` and save; remove REMOVED. Expect two items, the edited text shown.
3. Expect the panel to empty within 20 s and the timeline to show, in order: the edited FIRST, "First queued response.", THIRD, "Third queued response.". `E2E_QUEUE_REMOVED` and "Removed queued message unexpectedly ran." appear nowhere.

### C4 Scroll follow

1. New Session, send one message: `E2E_STREAM Build enough real layout for scroll ownership.` followed by 45 lines `Seed line N: preserved viewport content.` (N = 1..45). The timeline must now be taller than its viewport.
2. Send `E2E_SLOW Keep growing while I read above.`; once it streams, scroll up about 650 px with the mouse wheel over the timeline. Expect the view to stay where you read (moving less than about 8 px over a second) while content grows below.
3. Scroll down to the end. Expect the view to stick to the bottom while the Run keeps growing and after it completes.

### C5 Attachment and Compaction

1. New Session. Create a text file `e2e-attachment.txt` containing `attachment payload 7319` and attach it. Expect the file name and "Attached file" before sending. Send `E2E_ATTACHMENT Inspect the attached text file`. Expect "Fake provider received the attachment content." and the file still shown on the message.
2. New Session, send `E2E_COMPACTION_SEED Store the deterministic archive marker` ("Archived obsidian record 8642."), then `/compact`. Expect a note beginning "Context compacted" within 30 s.

### C6 Website preview

New Session, send `Create a small website for the preview demo.`. Expect five `apply_patch` rows done and the answer "Your website is ready. Open it here:" with a link to `index.html`. Click the link: it opens in the opposite area's Preview, the page renders with its style and image, its "Try it" button counts up, its internal link (`pages/about.html`) works inside the Preview, and the WebUI tab itself does not navigate away.

## Group tools - Tools through the Agentic Loop

### T1 Runtime Tools

New Session, send `E2E_TOOL_RUNTIME Exercise the runtime tools`. Expect "Runtime tools completed." and the rows `status`, shell and `terminal` done; the opened shell row contains `e2e-shell-output`.

### T2 Tool cancellation and Tool errors

1. New Session, send `E2E_TOOL_CANCEL Cancel only the long-running tool` and click "Cancel running tool call" within 15 s (later, the command finishes by itself and the journey passes falsely). Expect the shell row's dot cancelled, its details saying the user stopped it, then "Tool cancellation handled.". The Run itself is not cancelled (no "· Cancelled").
2. New Session, send `E2E_TOOL_MISSING_FILE Exercise tool failure recovery`. Expect the `read` row with an error dot whose details say the file was not found, then "Missing file error handled.".

### T3 Generated media

New Session, send `E2E_TOOL_MEDIA Generate deterministic local media artifacts`. Expect:

- `generate_speech` done, its details containing "E2E synthesized speech", and a "Speech audio" player whose source is `/api/speech/artifacts/<id>`; fetching it returns `audio/mpeg`.
- `generate_image` done, its details containing the prompt, and an image `img_<id>.png` (1x1 pixel) served from `/api/files/...` as `image/png`.
- The answer "Generated media tools completed." with a file link to the image.

### T4 Sub-Agent

Setup: RPC `agent.create {"id": "e2e-worker", "name": "E2E Worker"}`.

New Session with Main, send `E2E_TOOL_SUBAGENT Delegate to the worker`. Expect first "Sub-agent started; awaiting automatic delivery.", then, without any user action, a new Run answering "Sub-agent tool completed." (allow 45 s). The "Subagent" row is done; "Open Sub-Agent Session" opens the child Session showing "Fake sub-agent result.".

Cleanup: `agent.delete {"id": "e2e-worker"}`.

### T5 Tool access

Setup: RPC `agent.create {"id": "tool-restricted", "name": "Tool Restricted"}`.

1. Agents -> Tool Restricted -> "Tool access": "Deselect all", then check `status`. Expect "Saved", `status` checked, the shell and `apply_patch` unchecked.
2. New Session with that Agent, send `E2E_TOOL_CATALOG Verify the restricted Provider Tool catalog`. Expect "Restricted Tool catalog verified." (the fake Provider says "Unexpected Tool catalog." when the offered Tools are wrong), a `status` row done and no shell or `apply_patch` row.

Cleanup: `agent.delete {"id": "tool-restricted"}`.

### T6 Memory changes and Session search

1. New Session, send `E2E_TOOL_MEMORY Exercise memory changes`. The fake Provider adds the user Memory "User works in UTC+2.", replaces it with UTC+1 and removes it again. Expect three `memory` rows done, each showing its change in the details, and "Memory changes completed.". The user Memory ends as it was.
2. New Session, send `E2E_TOOL_SESSION_SEARCH Find the runtime Session`. Expect a `session_search` row done whose details list the T1 Session, and "Session search completed.".

### T7 Statistics

Open Statistics. Expect on Overview a positive "Runs" count and `fake/e2e-primary` among "Top Models"; under "Costs & tokens" positive Model calls and input tokens for `fake/e2e-primary`; under "Tools & skills" -> "Per Tool" positive calls for `status`, `terminal` and the shell, with a rejected rate of 0.0% for `status` and `terminal`.

## Group sessions - commands and Session management

### S1 Slash commands and Sessions

1. New Session. Type `/`: suggestions open with `help` and `rename`. Type `/sta` and pick `status`: it runs at once, and the "Command output" shows "Fake Primary".
2. `/help`: the output lists "Slash commands:" and `/handoff`, and does not list `/continue`.
3. Send `E2E_STREAM Start the command Session`, then `/rename E2E Slash Command Session`. Expect "Session renamed to E2E Slash Command Session." and the title in the Sessions drawer.
4. `/new`: an empty draft ("No messages yet"), no new row in the drawer, the renamed Session still listed.
5. Click the renamed Session: it becomes active, with no "Viewing a past session" banner and no "Return to current session". Send `E2E_STREAM Continue the selected earlier Session`: the answer lands in that same Session.

### S2 Session menu

1. Create two Sessions, sending `E2E_STREAM Seed the deletion landing` and `E2E_STREAM Seed the managed Session`. In the drawer, on the active row open "Session actions" -> Rename, enter `E2E Managed Session`.
2. Session actions -> "Compaction Policy": "Session override" is off; turn it on and save. Reopen: it is on. Cancel.
3. Session actions -> Delete -> confirm. Expect the drawer to stay open, the row gone and the other seeded Session active. After a reload the deleted Session is still gone.

### S3 Model control

New Session: `/model fake/e2e-fallback::default` ("Model set to fake/e2e-fallback::default."), `Confirm the temporary Model selection` ("Fallback provider response."), `/model reset` ("Model reset."), `E2E_STREAM Confirm the inherited Model` ("Fake provider streaming response.").

### S4 Agent move and handoff

Setup: RPC `agent.create {"id": "e2e-command-agent", "name": "E2E Command Agent"}`.

1. New Session with Main, send `E2E_STREAM Session content before move`.
2. `/agent e2e-command-agent Continue after moving this Session`. Expect the Agent picker to show "E2E Command Agent", the earlier message still there (same Session), and "Fake provider response." to the task text.
3. `/handoff agent:main Preserve the deterministic E2E handoff brief`. Expect the picker to show Main, a fresh Session with the brief "E2E handoff brief 9182.", then "E2E handoff received by target Agent.".

Cleanup: `agent.delete {"id": "e2e-command-agent"}`.

### S5 Learning and reflection

1. New Session, open the Sessions drawer and keep it open. Send `/learn E2E_LEARN_COMMAND author the deterministic test Skill`. Expect the command toast (not the timeline) "Created private Skill e2e-learned-command.".
2. Send `E2E_CLEAN_LEARNED_COMMAND remove the test Skill`. Expect "Learned command Skill cleaned up." This removes the Skill again; do not skip it.
3. In "Session list filters" turn on "Skill reflections" and note the row count. Send `/reflect E2E reflection focus`. Expect the toast "E2E reflection completed." and one more row, marked as a reflection.

## Group config - Agents, Settings, Projects and Schedules

### G1 Navigation and Logs

1. Open the root URL: title and heading "vBot", "Connected". Click every section of the navigation (Chat, Agents, Terminals, Projects, Settings, System Prompt, Schedules, Statistics, Logs): each sets its hash (`#chat`, `#agents`, `#terminals`, `#projects`, `#settings`, `#system-prompt`, `#cron`, `#statistics`, `#logs`) and shows its region. Back, Back, Forward lands on Statistics, Schedules, Statistics.
2. Logs: search `vbot.server Server started` (at least one entry, the first containing it); level INFO (entries show INFO); order "Oldest first"; then search `no-e2e-log-entry-can-match-5821`: "No entries match the current filters".

### G2 Agent lifecycle

Agents -> "Create agent": id `e2e-agent`, name `E2E Agent`. Expect "Agent created." and the new row active. Rename it to `E2E Agent Updated`; wait for "Saved", reload: the name stays. "Workspace & advanced" -> "Delete agent": the dialog names the Agent; expect "Agent moved to the Archive." and the row gone.

### G3 Memory mode and Agent prompt scope

Setup: RPC `agent.create {"id": "memory-prompt-agent", "name": "Memory Prompt Agent"}` and `agent.create {"id": "prompt-agent", "name": "Prompt Agent", "custom_system_prompt_enabled": true}`.

1. New Session with Memory Prompt Agent, send `E2E_MEMORY_PROMPT_SEED Store deterministic pinned Memory` ("Memory prompt seed stored.", `memory` row done).
2. Agents -> Memory Prompt Agent -> "Context & Memory": set Memory to "Off" ("Saved"). New Session: `E2E_MEMORY_PROMPT_CHECK Check the disabled Memory context` -> "Pinned Memory stayed out of the Provider prompt.".
3. Set Memory to "Agent notes (MEMORY.md)". New Session: `E2E_MEMORY_PROMPT_CHECK Check the enabled Memory context` -> "Pinned Memory reached the Provider.".
4. System Prompt -> "Edit blocks", scope "Prompt Agent" -> "New block" (a browser prompt dialog: answer `agent_only`), content `Agent-only E2E prompt 3307`, save. Switch the scope to "Default": `user:agent_only` is not listed there.
5. New Session with Prompt Agent: `E2E_PROMPT_SCOPE_CHECK Inspect this Agent` -> "Agent-scoped System Prompt reached the Provider.". New Session with Main, same message with `Inspect Main` -> "Agent-scoped System Prompt stayed out of the Provider prompt.".

Cleanup: remove the `agent_only` block, delete both Agents.

### G4 Custom System Prompt block

System Prompt -> "Edit blocks" -> "New block" (answer `e2e_notes`), content `E2E custom provider context 5821`, save. Reload: the content is kept. New Session with Main, send `Confirm the active custom context` -> "Custom System Prompt reached the Provider.". Remove the block (the dialog warns "This cannot be undone."): it is gone. Remove it even when the journey fails; it hijacks later answers.

### G5 Custom Provider

1. Settings -> Providers -> "Add custom": id `Invalid ID`, endpoint `relative-endpoint`, save -> "Provider id must use lowercase letters and digits in hyphen-separated segments.". Id `e2e-invalid` -> "Enter an absolute HTTP(S) endpoint URL.". Cancel.
2. "Add custom": id `e2e-custom`, name `E2E Dynamic Provider`, endpoint `http://127.0.0.1:<fake Provider port>/v1`, API key `e2e-secret-key-9412`, Model discovery path cleared. "Add Model": wire id `e2e-live`, display name `E2E Live Model`, context window 65536, max output 2048, task types `chat, text_output`, supported parameters `temperature`; Tools on, JSON mode and Reasoning turned on. Save -> "Custom Provider saved.", the card offers "Disable connection e2e-custom:default".
3. The key appears nowhere outside the Secret Store: not in any RPC response (the save, `provider.custom_list`), not in `settings.json`, and not in plain text in `secrets.json`. `model.list` shows `e2e-custom/e2e-live` with the entered limits and capabilities.
4. New Session with Main: `/model e2e-custom/e2e-live::default`, then `E2E_CUSTOM_PROVIDER_LIVE Exercise the newly created Provider` -> "Dynamic Custom Provider response.".
5. Edit the Provider: the id is locked, the replace-key field empty. Rename to `E2E Dynamic Provider Updated` and the Model to `E2E Live Model Updated`, save. New Session: `/status` shows "Model display name: E2E Live Model Updated"; `E2E_CUSTOM_PROVIDER_LIVE Confirm the edited Provider remains active` answers as before. `/model reset`.
6. Delete the Provider (the dialog says existing Model references are kept and become unavailable): "Custom Provider deleted.", the card and its `settings.json` entry are gone.

### G6 Debug settings

Settings -> search `trace limit` -> open "Trace limit". Turn on "Enable debug mode", set the trace limit to 73 and leave the field (number fields save on blur); wait for the save, not an older "Saved". Reload: debug on, 73. Set 50, turn debug off: the field hides. Reload: debug off, 50 kept.

### G7 Project Sources

1. Projects -> "Add project": repository path = absolute path of `scripts/fake-provider-project` in this worktree, display name `Mixed Source E2E Project`. Expect "Project added.", Team members `open-e2e-worker` and `claude-e2e-reviewer` (the latter from a nested folder), Skills `open-e2e-skill` and `claude-e2e-skill` both on.
2. Chat: in the Agent picker, expand the Project and pick `open-e2e-worker`. New Session: `E2E_PROJECT_AGENT_CONTEXT Verify the selected repository Agent prompt` -> "OpenCode Project Agent context reached the Provider.".
3. Project -> "Sources": switch off "OpenCode · Agents". Expect only `claude-e2e-reviewer` in the Team while both Skills stay on (the Skills Source is independent).
4. Chat with `claude-e2e-reviewer`: `E2E_PROJECT_AGENT_CONTEXT Verify the switched repository Agent prompt` -> "Claude Project Agent context reached the Provider.".
5. Remove the Project ("Repository management" -> Remove): "Project moved to the Archive", "No projects yet".

### G8 Schedules

1. Schedules shows "No scheduled runs yet". "Create schedule": name `E2E Morning Schedule`, prompt `E2E scheduled prompt`, repeats Daily (default), time 06:00 -> the cron expression reads `0 6 * * *`. Save: an active row with that expression.
2. Edit to `E2E Updated Schedule`, `E2E updated prompt`, 07:30 (`30 7 * * *`), save. Reload: the row and its values are kept.
3. Disable it: the row reads Paused and the switch offers Enable. "Technical details" -> Delete -> "Delete E2E Updated Schedule permanently?" -> Delete: "No scheduled runs yet".
4. Chat: open the Sessions drawer, turn on the filter "Schedule runs" and keep the drawer open. Create a one-time schedule for Main with prompt `E2E_SCHEDULED_RUN report back` about a minute ahead (in the form, or RPC `cron.create {"agent_id": "main", "name": "E2E One-time Delivery", "prompt": "E2E_SCHEDULED_RUN report back", "schedule_type": "once", "run_at": "<ISO time a few seconds ahead>"}`).
5. When it fires, its Session row appears in the open drawer without a reload; it shows "Scheduled Run delivered 4471.". Schedules shows the job Completed with "Succeeded". Delete it.

## Group terminals - real terminals

Before and after the group, stop and forget every terminal (Terminals view, or RPC `terminal.list`, `terminal.kill`, `terminal.forget`). On Windows use `python`, elsewhere `python3`.

### X1 Default shell

Terminals -> "New terminal" -> "Start terminal" without a command. Expect a live terminal in the focused tile, target "Manual", running the platform default shell (Windows: pwsh, else Windows PowerShell, else cmd; POSIX: `$SHELL`). Set its title (PowerShell `$Host.UI.RawUI.WindowTitle = 'E2E-DEFAULT-SHELL'`, cmd `title E2E-DEFAULT-SHELL`); the tile title follows. Type `Write-Output VBOT-DEFAULT-SHELL-READY` (POSIX `printf 'VBOT-DEFAULT-SHELL-READY\n'`): the marker appears.

### X2 Command inside the shell

Start a terminal with this command (quote it for the shell as needed):

```text
python -u -c "import sys;set_title=lambda value:(sys.stdout.write('\x1b]0;'+value+'\x07'),sys.stdout.flush());set_title('E2E-INNER');print('READY-INNER',flush=True);[print('INNER:'+line.rstrip(),flush=True) for line in sys.stdin]"
```

Expect "READY-INNER", target "Manual", and `message-for-inner` echoed as `INNER:message-for-inner`. Press Ctrl+C: the program ends and the shell survives and answers the marker command of X1.

### X3 Several terminals

1. Start ALPHA: the X2 program with labels `ALPHA` that, one second after `READY-ALPHA`, sets the title `E2E-ALPHA active` and prints `BACKGROUND-ALPHA` (add `threading.Timer(1,lambda:(set_title('E2E-ALPHA active'),print('BACKGROUND-ALPHA',flush=True))).start()` after the READY print and import `threading`). Then start BRAVO, the X2 program with label `BRAVO`.
2. Two tile bars, the newest first; BRAVO focused, titled `E2E-BRAVO`; `message-for-bravo` echoed.
3. Select ALPHA: its title changed to `E2E-ALPHA active` while in the background, its output has `BACKGROUND-ALPHA` and nothing of BRAVO; `message-for-alpha` echoed. Back to BRAVO: only its own output.
4. Navigate to Projects and back: both terminals remain, BRAVO selected with its output.
5. Close BRAVO: its tile goes, ALPHA is selected and still echoes `alpha-survived-bravo-stop`. Close ALPHA: no terminals left.
