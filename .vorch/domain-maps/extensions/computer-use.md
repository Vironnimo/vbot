# Computer Use Extension

Read this reference for the bundled `computer_use` Extension: its three opt-in Tools (`computer`, `computer_batch`, `computer_apps`), the access mode and per-Session approvals, screenshots and coordinates, desktop input, and operator stop.

## Overview

The Extension lets an Agent operate the interactive Windows desktop of the vBot server host with the user's real mouse and keyboard (foreground input only). Actions follow Anthropic's computer vocabulary; coordinates use the pixels of a returned image selected by `screenshot_id`, including scaled screenshots and zooms. Screenshots initially show the foreground window; display view remains available. The first generation (one `computer` Tool with background delivery, element refs and the external Cua Driver) is archived in `archive/computer-use.zip` (`archive/README.md`); the current implementation does not use it.

Windows is the only platform today. On other hosts, or when the process has no usable interactive desktop (`DesktopTarget.readiness()`), the Tools stay registered but not ready, with the readiness hint "Computer Use works when the vBot server runs on Windows in a signed-in desktop session."

Decided direction for later work (2026-10, user decision): further targets (VM, second session, Linux Xvfb container) implement the same `DesktopTarget` protocol; a Provider capability may later declare the native Anthropic toolset and map it 1:1 to `computer`. Neither exists yet.

## Terms

### Access mode
The Extension setting `ask_per_app` (toggle "Ask before each app", default off, read live per call). Off: an Agent whose Tool Access Policy allows a Computer Use Tool operates every app. On: it operates only apps the user approved for the Session. User decision 2026-10: no app is treated specially - browsers, terminals, IDEs and vBot itself are ordinary apps; asking is opt-in because remote use (for example over Telegram) cannot answer a WebUI prompt.

### Approval
In ask mode, the user's permission for one application in one Session, given through an access request. Distinct from the Tool opt-in, which must also allow each Tool. Internally `SessionState.grants`.

### Desktop control
The period in which Agents hold the desktop, shown by the activity sign and stoppable by the user. It begins with the first call that takes the desktop (`computer`, `computer_batch`, `computer_apps open`) and ends when every Run in it has released (`computer_apps release`) or ended (`run_end`), after `CONTROL_IDLE_SECONDS` (120 s) without a call, at shutdown, or on a user stop. Identified by `control_id`. Not a vBot Session.

### Frame
One published image's pixel space and its physical desktop rectangle (`_screens.Frame`). Images fit a long edge <= 1568 and area <= 1,150,000 pixels, then apply optional `scale`. Each axis maps from the actual returned pixel size, including rounding, to the recorded rectangle. Agents do not undo scaling, crop offsets or monitor origins.

## Ownership and source routing

`resources/extensions/computer_use/`:
- `extension.py` - `register`, `ComputerUseService`: readiness, opt-in check per Tool, Session state lookup, call serialization, desktop control and stop, hotkey arming, `run_end`/shutdown release, the three Tool handlers and their result/error envelopes.
- `_tools.py` - descriptions, schemas, argument normalizers (other harnesses' spellings; Agent error tolerance per `tools.md`).
- `_actions.py` - parses one `computer` action into an `Action` (points, seconds, notes).
- `_desktop.py` - `Desktop`: one call's view of the desktop under its `Access`: frame resolution, display selection, screenshot/zoom/cursor, access checks before input, dispatch to the target.
- `_access.py` - `Access` (the call's mode and what it may operate), Session state (approvals, capture choice, bounded image references), expiry, app name resolution, access requests as pending inputs.
- `_screens.py` - platform-neutral imaging: frames, coordinate mapping, display choice, masking, PNG publishing.
- `_keys.py` - chord parsing to canonical key names (xdotool/Anthropic spellings accepted).
- `target.py` - `DesktopTarget` protocol and value types (`Display`, `AppInfo`, `WindowInfo`, `TargetError`, `InputInterrupted`). The only boundary to the operating system.
- `windows_target.py` with `_win32.py` (bindings, displays, capture, window enumeration), `_win_input.py` (`SendInput`), `_win_apps.py` (app identity, shell surfaces, Start-menu discovery, launching), `_win_overlay.py` (`ActivityOverlay`, the activity sign) - the Windows target.
- `hotkey.py` - `EmergencyHotkey` (double Esc).
- `skills/computer-use/SKILL.md` - the workflow Skill, discovered like any loaded-Extension Skill; it grants nothing.

Tests: `tests/resources/extensions/computer_use/` (`computer_use_test_support.py` holds `FakeTarget`, `FakeHotkey`, the dispatch `Harness`; `test_computer_use_tools.py`, `_access.py`, `_batch.py`, `_dialects.py`, `test_hotkey.py`, `test_windows_target.py` for pure Windows helpers) and `tests/core/runtime/test_runtime_computer_access.py` (opt-in through runtime dispatch, temporary Agents). No test sends real input or captures the real screen.

## Contracts

- **Permission.** Each Tool is `requires_opt_in`; the handler re-checks the calling Agent's Tool Access Policy (`resolve_tool_access`, `host.resolve_tool_agent`) and refuses with `tool_not_allowed`. In ask mode, approvals come on top. A call outside a Session is refused (`computer_use_unavailable`): display choice and approvals are per Session.
- **Check before input** (`Desktop._check_access`): every input action requires a window under each target point (both drag ends; the pointer position when no coordinate is given), and that window and the foreground window must not be elevated (`target_elevated`; UIPI would drop the input). In ask mode their apps must also be approved (`access_required`, naming the app and the exact `computer_apps request` call). The taskbar and desktop belong to "File Explorer". With no foreground window, only pointer actions proceed.
- **Masking (ask mode only):** screenshots and zooms paint windows bottom to top of z-order; an approved app's window reveals its frame, every other window and uncovered pixels are a solid neutral fill. Results name the hidden apps (names, never titles). Without ask mode nothing is masked.
- **Coordinates:** image-local pixels, with the top-left at `[0, 0]`. `screenshot_id` selects an image returned in the same Session; omission selects the latest image. Coordinate/region actions and `cursor_position` need a prior observation. The Session keeps at most 64 image mappings, without image bytes; eviction, Session expiry or restart makes a reference unavailable. Unknown or other-Session references never fall back to a different image. Changes to display layout or the captured foreground window's geometry refuse before input; each batch step rechecks its mapping before acting. Window images also refuse pointer input into a covering app, while menus/dialogs of the same app remain usable. Bounds use the actual image size. `zoom` captures its selected region fresh and returns a new image mapping; nested zooms compose through physical rectangles. Its bottom-right corner is exclusive. `screenshot_id` does not select keyboard focus.
- **Capture choice** is per Session. Initial `view` is `window`: capture the visible desktop rectangle of the foreground window and its visible owned popups, across displays when needed. With no usable approved foreground window, capture the chosen display instead, still applying masking. `view: display` captures a whole monitor. Setting `display` (name, 1-based number, `auto`) also selects display view; `auto` follows the visible foreground window's monitor, else the primary display. `view: window` restores window capture and automatic monitor choice; combining it with an explicit `display` refuses. Input and wait screenshots retain the capture choice.
- **Results:** text `content` plus images in `context.result_media` (PNG under the caller's tmp `computer-use` folder) and `add_display_media`. Each image's text gives its actual dimensions and `screenshot_id` (the generated PNG basename, without its suffix). Input actions in `computer` (except `left_mouse_down`, `cursor_position`) settle 0.5 s and attach a screenshot; `wait` attaches one after its pause. `computer_batch` runs 1-30 actions in order with 0.2 s between input steps, resolves every explicit/default image reference and validates all items before running any, stops at the first failure (reporting completed steps), and attaches a final screenshot after input unless the last item was one. Images returned inside the batch never change a later step's coordinate basis. `computer_apps open` waits 1.5 s and attaches a screenshot.
- **Access requests (ask mode):** `computer_apps request` resolves names (case-insensitive exact, then unique contains, else suggestions), skips apps already approved, and raises one pending input (`kind` `computer_access`, `payload.message`, `session_id`) through `api.operations.pending_inputs`; operation `respond` takes `{request_id, response: {action: accept|decline|cancel}}`. Each add/remove publishes a `pending_inputs` change. Unanswered after 300 s it counts as declined; Run cancellation withdraws it. Approvals, capture choice and image references expire 30 minutes after the Session's last Computer Use call and are in memory only (reload/restart clears them). One INFO line per decision with counts, never app names. Without ask mode, `request` returns at once that no approval is needed, and `list`/`open` cover every app.
- **Activity sign** (user decision 2026-10: subtle but visible, like Claude Code and Codex): while an Agent controls the desktop, every display shows an accent glow along its edges and the primary display a pill "vBot is using this computer - Esc Esc to stop" (`DesktopTarget.set_activity`). It is shown exactly while desktop control lasts (Terms), including between calls. On Windows its windows are layered, click-through, never activate, stay out of `windows()`/`window_at`, and use `WDA_EXCLUDEFROMCAPTURE`, so screenshots never contain it.
- **Stop** (user decision 2026-10: the user must be able to stop at any moment the sign shows, not only during a call): operation `control` returns `{available, active, stopping, hotkey_available, control_id?}`, where `active` means desktop control; `stop` with the current `control_id` and double Esc end desktop control as the user. That interrupts a running call, hides the sign, disarms the hotkey, and marks every Run in control as stopped: their later calls that take the desktop are refused (`computer_use_interrupted`, "Nothing was done") until the Run ends, so the user's next message may take control again. Run cancellation and shutdown interrupt only the running call. An interrupt sets the target's stop event; the target releases keys and buttons it pressed and raises `InputInterrupted`, reported as `computer_use_interrupted` with whether input may have been sent and the completed batch steps. Every control change publishes a `control` change. The hotkey's keyboard hook starts with the first call that takes the desktop and is armed with the `control_id` for the whole desktop control.
- **Release:** `computer_apps release` ends the calling Run's part in desktop control and releases a left button it still holds; the sign goes when no other Run is in control. A later call in the same Run takes control again. Without control it reports that there was nothing to release.

## Conventions and gotchas

- All target calls run on one dedicated worker thread (`ThreadPoolExecutor(1)`), which is per-monitor DPI aware; an `asyncio.Lock` serializes whole calls, so a batch is atomic across Sessions. `computer_apps list`/`request` do not take the lock, so a waiting access request never blocks Stop.
- `run_end` ends the Run's desktop control, clears its stopped mark, and releases a left button its `left_mouse_down` left held; shutdown releases all input and stops the worker.
- Windows app identity: keys are the lowercased executable path, `exe:<basename>` and `pkg:<package family>`; generic hosts (python, java, node, explorer, mmc, rundll32, ...) get no `exe:` key. UWP frames resolve to their CoreWindow process. Apps with the same display name merge into one app, so an approval by name covers all of its windows. Start-menu discovery (one hidden PowerShell call, cached 60 s, refreshed in the background) can delay the first window lookup up to 20 s. Start names are localized.
- Own windows: the current process and any `vBot*.exe` map to app "vBot" (key `vbot:self`) so they are named like the Start entry; they get no special access treatment. A development Desktop shell running under `python.exe` in another process is named after its executable.
- Every key, chord and typed character is held 20 ms and followed by a 20 ms gap (`_win_input._KEY_HOLD`, `_KEY_GAP`): Windows 11 Notepad and other WinUI text boxes drop or repeat keys whose down and up events arrive together (verified live 2026-10). Typing therefore runs at about 25 characters per second.
- Typing uses `KEYEVENTF_UNICODE`; single-character keys map through the foreground window's keyboard layout and refuse characters needing dead keys. `cmd`/`meta` are refused with a pointer to `ctrl` or `win`.
- Screenshots are written to `<data_root>/tmp/computer-use/shot_*.png` and referenced by the displayed Tool media; nothing removes them yet (the temporary-file lifecycle in `storage.md` covers only its own categories).

## Agent-facing text

Changed guidance and its purpose (coordinate failures observed in recent Paint Runs, 2026-10):

| Text | Reason |
| --- | --- |
| `Take a screenshot first...foreground window by default.` | Establishes an observable coordinate basis and explains the smaller initial view. |
| `Coordinates are pixels [x, y] in the image selected by screenshot_id...including zoom images.` | Prevents the old conflict between visible image pixels and full-display frame coordinates. |
| `When available, use computer_batch...and computer_apps...` | Each companion Tool has its own opt-in; family membership does not make it callable. |
| `Select images returned before this call...latest image from before the batch.` | Makes default selection and explicit multiple-image batches predictable. |
| `Images returned inside a batch are for inspection after it ends.` | Prevents interpreting later coordinates against an image the Agent has not observed yet. |
| `Image to measure coordinate, start_coordinate and region in...cursor_position...` | Defines which actions consume the reference, including both drag endpoints. |
| `It selects coordinates, not keyboard focus.` | Refuses a reference supplied on a keyboard action instead of silently typing into an unintended focus. |
| `screenshot: window...display...Keeps this choice...` | Teaches deliberate full-desktop inspection and persistence through input screenshots. |
| `zoom...bottom-right corner excluded...own screenshot_id and coordinates.` | Explains crop edges and supplies a directly usable close-up instead of Agent-side arithmetic. |
| `Coordinates use the returned image's pixels.` | Optional `scale` changes the coordinate space together with the image. |
| `Setting display selects display view...For window view, omit display.` | Preserves deliberate monitor selection without a conflicting window selector. |
| Skill: `verify that the intended control, color, text or drawing actually changed` | Dispatch success did not establish task success in the observed Runs. |

Reference failures (`invalid_arguments`) state the unavailable/changed image, that no input was sent for the action, and the recovery: take a new screenshot and use its reference. Bounds failures name the selected image and its pixel size. Conflicting window/display selection and a keyboard action carrying `screenshot_id` refuse before execution. Owning tests: `test_computer_use_tools.py` (local/nested image mapping, window/popups, scope, geometry, bounds and misuse), `test_computer_use_batch.py` (pre-call references, upfront refusal and partial failure), `test_computer_use_dialects.py` (representation repair). Deterministic dispatch tests do not establish fresh-Model task success; a repeated Paint control-selection comparison remains a separate usability evaluation.
