# Terminal integration audit

Date: 2026-09-07. Inspected revision: `a18e403b220d20d93a04447b9cffed057cdae0bd`.

Scope: reconstruct the current Terminal Tool, operator UI, shared geometry, attachment, and automatic Agent delivery; identify improvements. This audit changes documentation only. Recommendations below are not an approved implementation specification.

## User requirement

The Agent needs a compact, useful terminal resolution independent of the operator UI. The UI should use its available space sensibly without stretching or showing an unnecessarily small crop. Enlarging or maximizing the UI must not by itself expand the terminal content sent to the Agent. Different Agent and UI dimensions are intentional and are not, by themselves, a defect. A rendering defect requires evidence of incorrect content, clipping, wrapping, or input-coordinate mapping. This requirement takes precedence over choosing one shared display size for both consumers.

## What exists today

There is one live PTY/ConPTY process and one canonical column/row grid per Terminal Session. The browser receives sequenced VT output and authoritative ANSI snapshots; the Agent receives a server-rendered text screen and bounded scrollback from that same output. The two consumers use different renderers (xterm and pyte), so sharing the byte stream does not by itself guarantee identical visual interpretation.

| Operation | Current behavior |
| --- | --- |
| Agent `start` | Starts the exact executable and arguments, or the default interactive shell. Uses the Agent's current directory and a 120x32 grid. Automatically attaches to the calling vBot Session. Optional initial text is queued asynchronously until a stable screen or the 15-second deadline, then sent with Enter. |
| UI start | Starts an interactive host shell, defaulting to the server user's home directory. An optional command runs inside that shell. Program completion therefore normally returns to the shell rather than ending the Terminal Session. |
| Agent `list` / `attach` | `list` discovers all retained terminals. `attach` binds an unbound live terminal to the calling Session. Another Session's binding blocks attachment. The successful result is metadata, not the current screen. |
| `detach` | Removes the Session's Tool access and automatic-delivery target. It does not stop the process, resize it, or transfer its cleanup scope. |
| UI interaction | Every running terminal accepts direct keyboard/paste input regardless of origin or attachment. The UI has no attach/detach operation or input ownership mode. Its label shows who started the terminal, not the current attachment. |
| Resize | UI fit, maximize, and window/layout changes can resize the real PTY. The Agent also has `resize`. Bounds are 40-240 columns and 10-80 rows. There is no size owner or separate compact Agent grid. |
| Leaving a Run or view | Already-started terminals remain alive. Closing a tile explicitly stops and forgets it. Natural exits remain inspectable for 30 minutes. Runtime shutdown ends live terminals; launch history and user groups survive, terminals do not. |

Three different relationships matter: immutable starting provenance, the lifecycle scope that can clean up the terminal, and the current Agent attachment. An operator-started terminal has no Agent lifecycle scope even after attachment. An Agent-started terminal keeps that scope when detached and attached elsewhere. Consequently, attachment is not a complete ownership transfer.

Source: `core/tools/terminal.py` (`_handle_start`, `_handle_attach`, `_terminal_summary`); `core/tools/terminal_manager.py` (`_spawn_admitted`, `spawn_for_operator`, `attach`, `detach`, lifecycle cleanup); `webui/src/components/TerminalsView.svelte` (`terminalTarget`, `fitTerminal`, `closeTerminal`).

## When the Agent is informed

Ordinary activity becomes `working`. After two seconds without terminal output it becomes `ready` and creates an `output_settled` attention revision. This is an output-activity observation, not proof that a command finished or a question awaits an answer.

| Event | Delivery behavior |
| --- | --- |
| Agent input, operator input, subsequent spontaneous output while attached | Generally arms delivery at the next quiet boundary. There is no explicit per-terminal subscription choice. |
| Agent start without initial text | Suppresses settled-output wakeups until explicit input or attach, including later startup changes. The initial Tool result can precede the first rendered frame. |
| Repeated identical rendered text | Suppressed using equality of the current screen text. Scrollback, styling, and application task identity are not part of that comparison. |
| Actual resize | Suppresses settled delivery inside a maximum 15-second window; also tries to establish a stable repaint baseline. Further input clears the suppression. See finding 1. |
| Natural process exit / terminal failure | Requests delivery. A command ending inside an operator shell is not a terminal-process exit. |
| Explicit kill or scope cleanup | Suppresses the corresponding exit wakeup. |
| Detach | Removes the delivery target and cancels pending delivery. |

A settled notice embeds the last 20 screen rows after trailing blank rows are removed. It does not embed the whole retained output or a directly usable `screen_revision` for guarded input. `status` provides the full current text screen plus a default 30 prior lines; `lines` does not bound the separate screen field. The screen can therefore grow to 240x80 cells when the UI grows the PTY.

The shared completion coordinator persists notices at the next Model-request boundary of an active Run. If the Session is idle, it can start one internal follow-up Run, coalescing available notices. A user-cancelled origin persists notes without automatically starting another Run. Manual Tool observations acknowledge equivalent pending notices only after their results are persisted. This is useful existing infrastructure and should remain the delivery owner.

Source: `terminal_manager.py` (`_read_terminal`, `_settle_after_quiet`, `_attention_body`); `terminal.py` (`_acknowledge_after_persistence`); `core/automation/automation.py` (`_CompletionDeliveryCoordinator`).

## Findings

### 1. High: a genuine result can lose its wakeup during resize

Reproduction with the production Manager and renderer, a fake PTY, and controlled time:

1. Establish an attached terminal with one durably delivered screen.
2. Resize through the operator surface.
3. Emit a new completion message inside the resize window and let output settle.
4. Advance time by 30 seconds without further terminal output.

Actual: the result is present in the screen and `output_settled` exists, but there are **zero additional deliveries**. The settle task is finished. The grace deadline has no timer that rechecks the suppressed result. Later output may rearm delivery, but a terminal waiting at a final answer or question need not produce any.

Expected improvement: defer and coalesce attention during a repaint, then perform a bounded final comparison. Do not discard a pending observation merely because it overlaps a resize. A continuous output stream and a final quiet result need separate regression scenarios.

Source: `core/tools/terminal_manager.py:1400`, `:1455`, `:1491`. Existing `test_resize_hard_deadline_caps_repaint_suppression` emits more output after the deadline; it does not cover the no-further-output case.

### 2. High: UI sizing can expand Agent context; different dimensions alone are not a defect

The real browser test at `http://127.0.0.1:8422` showed that enlarging the view to 3000x1600 changed the PTY to 239x61. The same production Manager's `_snapshot_data` returns the renderer's complete current `screen` to the Agent. `status(lines=...)` bounds additional scrollback, not that screen. A TUI that fills the larger grid can therefore increase Agent-visible output just because the operator enlarged the UI. Trailing blank cells are trimmed, so the grid size alone does not establish an exact token increase.

Expected improvement: make compact Agent observation independent of UI dimensions. Preserve the user's separate presentation requirements; making the UI dictate the Agent's full screen size is not an acceptable default solution.

A separate live observation used the operator resize RPC to change the PTY from 88x27 to 120x32. This uses the same `_resize_session` implementation as Agent resize. Server dimensions became 120x32, while the browser retained 27 rows and showed `Tile 88x27 · Session 120x32`. The state handler merges metadata without resizing the mounted xterm. This proves a dimension difference, **not** a rendering failure. The audit did not demonstrate an incorrect TUI border, content loss, or incorrect mouse target in this scenario; that part remains a verification gap. It must not be used as a reason to force equal displayed dimensions.

Source: `terminal_manager.py:1202` and `_snapshot_data`; `webui/src/lib/terminalsView.js:534` and its `terminal_state` branch; `TerminalsView.svelte:348` (`writeSnapshot`) and `:423` (`fitTerminal`). Screenshot: `terminal-audit-external-resize.png` records the dimension difference, not a proven rendering defect.

### 3. Medium: automatic font growth is not reversible

Live reproduction: enlarge the browser to 3000x1600, then return to 1280x800 without remounting the view.

Actual: the large view uses 19px text and a 239x61 PTY. Returning to 1280x800 keeps 19px text and reduces the PTY to 88x27. The component starts at 12px but only increases font size when exceeding the grid maximum; no corresponding reduction exists. This unnecessarily reduces visible content after using a large window.

Expected improvement: keep an explicit preferred font size and recompute fitting deterministically in both directions. Apply readable minimum sizing and deliberate overflow/contain behavior for small tiles. Do not use nonuniform stretching to force a fixed TUI into an unrelated aspect ratio.

Source: `TerminalsView.svelte:73`, `:423`. Screenshots: `terminal-audit-baseline.png`, `terminal-audit-wide.png`, `terminal-audit-return-small.png`.

### 4. High: human input does not retire pending Agent startup input

Production Manager reproduction: start with queued initial text `AGENT_TASK`, type `USER_INPUT` through the operator path before the TUI is ready, then render a stable prompt. The observed PTY writes are:

```text
USER_INPUT
AGENT_TASK
<Enter>
```

The pending Agent startup instruction is still sent after the human has begun using the terminal. The Agent input path cancels an earlier initial-input task; the operator input path does not. More generally, attachment authorizes Agent input but does not give either participant exclusive control. `expected_screen_revision` rejects a changed rendered revision, but it is not an ownership token and cannot detect human input that has not yet produced rendered output.

Expected improvement: human intervention should explicitly suspend/retire queued Agent input. Make resuming Agent control deliberate and visible. Keep observation available to both participants. If simultaneous writing remains intentional, document that choice and at least eliminate this delayed-input race.

Source: `terminal_manager.py:894`, `:1028`, `:1118`; `TerminalsView.svelte:314`.

### 5. Medium: following returned scrollback continuations omits older output

Production Tool reproduction: emit 200 numbered lines, request default `status(lines=30)`, and follow every `scrollback.next_request` until null.

| Request start | Returned lines | Next start |
| --- | --- | --- |
| omitted | 139-168 | 109 |
| 109 | 109-138 | 139 |
| 139 | 139-168 | 169 |
| 169 | 169-198 | 199 |
| 199 | 199 | null |

Even including every separately returned screen, **109 of the 200 lines are missing**. The tail page points backward once, while explicit-index pages point forward. The bundled `coding-agents` Skill still describes every continuation as older and refers to a cursor that the Tool no longer accepts.

Expected improvement: use one consistent continuation direction with stable addressing, and align the Skill. Until fixed, complete traversal requires starting explicitly at `start_line: 0` and then following forward continuations. When the 2,000-line retention limit evicts output, current buffer-relative indices shift; concurrent pagination therefore also needs a stable snapshot or sequence contract.

Source: `core/tools/terminal_backend.py:354`, `:374`; `terminal.py:624`; `resources/skills/coding-agents/SKILL.md`, "Monitor without polling blindly". No runtime Skill wording was changed by this audit.

### 6. Medium: attachment is difficult to discover and assess from the UI and Tool

The operator UI has no action to select a target Agent/Session, detach, or transfer an attachment. Its tile label reads immutable starting provenance, so an operator-started terminal still says Manual after attachment. The Agent `list` only says `attachment: other` for another Session; it does not identify that Session. A successful `attach` returns metadata with no screen, so the Agent needs another `status` before it can safely continue. Retained, unbound finished terminals can be listed but cannot be attached for reading because attachment requires a live process.

Expected improvement: show current attachment separately from starting provenance, provide a concrete handoff action with target Session, and return a bounded current screen plus its revision from successful attachment. Keep the process and its geometry intact during handoff. Decide explicitly whether lifecycle cleanup transfers; current attachment does not transfer it.

Source: `terminal.py:429`, `:648`; `terminal_manager.py:655`; `TerminalsView.svelte:914`; `server/rpc/terminal_methods.py` method registry.

## Recommended direction

Keep the shared interactive process, the current VT transport, explicit Session attachment, and the existing completion coordinator. Resolve the presentation layers against the independent Agent/UI sizing requirement above. The architecture already supports shared observation and continuity; the fragile parts are the policies where human viewing, input, geometry, and wakeups intersect.

1. Preserve **compact Agent observation independently of UI presentation**. UI enlargement must not make the Agent receive a larger full screen. Keep a compact Agent grid or a separately bounded Agent representation while the operator presentation uses its available area without distortion. The concrete rendering mechanism must preserve useful content and input-coordinate mapping; unequal dimensions alone neither prove nor disprove that. Do not make the active UI the default authority for the Agent's screen size. Choosing between visual fitting and a distinct Agent projection needs validation against this requirement.
2. Separate **attachment**, **permission to write now**, and **notification subscription**. A human should be able to intervene without racing delayed Agent keystrokes, then deliberately hand control back. A visible attachment/paused state explains why a terminal does or does not wake an Agent.
3. Make wakeups durable observations with a bounded delay. Preserve new output during resize, coalesce noisy updates, and distinguish observation from semantic completion. Generic VT output cannot reliably tell a question from an animation or a finished application task. Specific semantic integrations would be a separate design choice, not a safe inference from `ready`.
4. Reduce redundant Agent calls: attach with observation, revision-bearing notices, and one reliable pagination contract. The Skill currently asks for `status` after every wakeup even though notices already contain a tail, and `wait` also returns a screen snapshot.

Suggested priority: fix lost resize delivery and delayed input first, and validate UI-independent Agent output; then pagination and reversible font sizing; settle the human/Agent handoff and subscription experience before implementing broader control changes.

## Verification and limits

- Scoped non-mutating backend gate passed: format, lint, typing, **123/123 tests** across the selected Terminal Tool/Manager/backend, RPC, and Run-lifetime paths.
- Scoped non-mutating frontend gate passed: formatting, lint, **72/72 tests** for the controller and rendered component. These passing tests do not cover all of the reproduced transitions above.
- Controlled probes used production Manager/renderer/Tool handlers and existing fake PTY/time fixtures. They verified the lost wakeup, continuation gap, missing attach screen, and delayed input sequence. They were disposable experiments; no application or test source changed.
- Live UI: built worktree application at `http://127.0.0.1:8422`, isolated data directory `~/.vbot-terminal-integration-audit`, Chromium through Playwright, real Windows PowerShell/ConPTY. Manual creation and native typing produced `TERMINAL_AUDIT_OK`. Font behavior and differing geometry were inspected visually and through DOM/server dimensions. Different dimensions alone were not treated as a rendering failure. Browser console reported zero warnings/errors.
- Browser closed and test-owned server/fake Provider stopped successfully. The installed product instance was not used. No paid Provider calls, coding-agent TUI task, Raspberry Pi/Linux live validation, or E2E suite run occurred. Platform-specific rendering and semantic completion of external coding CLIs remain unverified.
- Evidence directory: `C:/Users/Viro/.codex/visualizations/2026/09/07/01a07b83-cd36-7af3-8eab-42902a52d98c/`. The four screenshots named above are local artifacts, outside Git.
- Documentation corrections: `DESIGN.md` now explicitly preserves the user requirement for independent UI presentation and compact Agent output, and distinguishes it from the current fit/resize implementation. Its confirmed-Stop wording was corrected to the current optimistic close behavior. The Terminal map had retained inverse-pointer-scaling wording and a misleading complete-history continuation claim. These factual descriptions were corrected. No domain-map index or glossary change was needed.


## Reliability follow-up (2026-09-07)

The audit above describes its named historical revision. This implementation follow-up fixes independent input and delivery defects; it does not implement the separate compact Agent representation, choose a new default grid, change UI fitting/font size, or repair pagination. The proposed fixed-grid fullscreen zoom was rejected by the user and was not implemented. A single ordinary PTY exposes one application layout; the open design decision is whether the compact Agent view should page over that shared content, rather than promise two independently computed application layouts.

| Verifiable requirement | Result and evidence |
| --- | --- |
| Human input supersedes queued Agent startup input and invalidates stale guarded input even without echo. | Pass: manager regressions cover pending initial input, no-echo input, repeated guarded input, and empty-input no-ops. |
| A final answer during resize is delivered without requiring another PTY event. | Pass: fake-clock regressions cover a single final result, repeated identical final output, deferred acknowledgement, and subsequent changes. Delivery is coalesced for 4 seconds with a 15-second cap instead of discarded. |
| Selection attributes count as a screen change while cursor-only refreshes remain quiet. | Pass: renderer comparison and manager delivery tests include identical text with changed inverse-video attributes. Agent results still need a future representation of those attributes. |
| Protocol replies work headlessly and extra viewers do not inject them as human input. | Pass for the supported reports: renderer tests split requests at every character and check cursor, status, attributes, size, mode, and drain-once behavior. A manager test preserves queued initial input while answering headless queries. Component tests intercept viewer replies and focus reports. Unsupported reports remain unsupported independently of browser presence. |
| A real terminal does not inherit an unusable TERM from a service. | Pass: eight inherited/explicit-environment cases, plus the live CLI restart below. The host environment is unchanged. |

Validation: the full backend gate passed 10,908 tests before the additional protocol/startup corrections; the final scoped backend gate passed 130 tests including Tool and RPC coverage after those corrections. The full frontend gate passed 2,263 tests and the production build. Its existing large-chunk build warning remains informational. No Tool description, parameter schema, Skill wording, or runtime reminder wording changed.

Live acceptance used the isolated terminal-polish worktree environment at http://127.0.0.1:8422 with its own data directory and fake Provider. PowerShell accepted a typed command and printed TERMINAL_INPUT_OK. The unmodified installed Codex CLI 0.153.2 initially demonstrated the inherited TERM=dumb warning; after the correction and server restart, it opened its TUI without that warning. Its ordinary update menu was skipped using keyboard input; no installation or Model task was requested. Two browser pages showed the same process. Both recorded zero terminal.input requests during startup/observation; the controlling page then recorded exactly the three deliberate keyboard events used in the menu, while the second viewer continued to record zero. Browser console: zero errors and warnings. Both browser pages were closed and the owned server and fake Provider confirmed stopped.

Screenshot evidence outside the disposable worktree:

- Input: C:/Users/Viro/.codex/visualizations/2026/09/07/01a07b83-cd36-7af3-8eab-42902a52d98c/terminal-polish-input.png
- Initial CLI and reproduced environment warning: C:/Users/Viro/.codex/visualizations/2026/09/07/01a07b83-cd36-7af3-8eab-42902a52d98c/terminal-polish-codex.png
- Second viewer after correction: C:/Users/Viro/.codex/visualizations/2026/09/07/01a07b83-cd36-7af3-8eab-42902a52d98c/terminal-polish-second-viewer.png

Remaining work: independent compact Agent observations and their default budget, correct stable pagination, useful attach observations, representation of TUI selection/cursor state, notification policy for continuously active programs, and the already documented UI font/fit behavior. Real Linux/Pi and arbitrary TUI compatibility were not established by these Windows and fixture checks. The overall terminal-polish request is not complete.
