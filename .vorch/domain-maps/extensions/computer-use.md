# Computer Use Extension

Read this reference for the bundled `computer_use` Extension: its three opt-in Tools (`computer`, `computer_batch`, `computer_apps`), the access mode and per-Session approvals, screenshots and coordinates, desktop input, and operator stop.

## Overview

The Extension lets an Agent operate the interactive Windows desktop of the vBot server host with the user's real mouse and keyboard (foreground input only). Its Agent interface follows Anthropic's computer tool vocabulary and Claude Code's computer-use server: no ids carry between calls, coordinates always refer to the latest screenshot. The first generation (one `computer` Tool with window targets, view ids, background delivery, element refs and the external Cua Driver) is archived in `archive/computer-use.zip` (`archive/README.md`); nothing of it remains at runtime.

Windows is the only platform today. On other hosts, or when the process has no usable interactive desktop (`DesktopTarget.readiness()`), the Tools stay registered but not ready, with the readiness hint "Computer Use works when the vBot server runs on Windows in a signed-in desktop session."

Decided direction for later work (2026-10, user decision): further targets (VM, second session, Linux Xvfb container) implement the same `DesktopTarget` protocol; a Provider capability may later declare the native Anthropic toolset and map it 1:1 to `computer`. Neither exists yet.

## Terms

### Access mode
The Extension setting `ask_per_app` (toggle "Ask before each app", default off, read live per call). Off: an Agent whose Tool Access Policy allows a Computer Use Tool operates every app. On: it operates only apps the user approved for the Session. User decision 2026-10: no app is treated specially - browsers, terminals, IDEs and vBot itself are ordinary apps; asking is opt-in because remote use (for example over Telegram) cannot answer a WebUI prompt.

### Approval
In ask mode, the user's permission for one application in one Session, given through an access request. Distinct from the Tool opt-in, which must also allow each Tool. Internally `SessionState.grants`.

### Frame
The coordinate space of a display's screenshots: its physical size scaled down (never up) to a long edge <= 1568 and area <= 1,150,000 pixels (`_screens.fit`). Depends only on display geometry, so mapping is deterministic.

## Ownership and source routing

`resources/extensions/computer_use/`:
- `extension.py` - `register`, `ComputerUseService`: readiness, opt-in check per Tool, Session state lookup, call serialization, stop/control, hotkey arming, `run_end`/shutdown release, the three Tool handlers and their result/error envelopes.
- `_tools.py` - descriptions, schemas, argument normalizers (other harnesses' spellings; Agent error tolerance per `tools.md`).
- `_actions.py` - parses one `computer` action into an `Action` (points, seconds, notes).
- `_desktop.py` - `Desktop`: one call's view of the desktop under its `Access`: frame resolution, display selection, screenshot/zoom/cursor, access checks before input, dispatch to the target.
- `_access.py` - `Access` (the call's mode and what it may operate), Session state (approvals, display choice), expiry, app name resolution, access requests as pending inputs.
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
- **Coordinates:** frame pixels of the display shown by the Session's latest screenshot, also when `scale` < 1 returned a smaller image. A display that changed size or disappeared since that screenshot refuses coordinate use (take a new screenshot). Out-of-frame coordinates refuse before anything runs. `zoom` captures the region fresh at physical resolution (fit to frame limits) and never changes the frame.
- **Display choice** is per Session: `screenshot` with `display` (name, 1-based number, `auto`) changes it; `auto` follows the foreground window when it is visible to the Session, else the primary display.
- **Results:** text `content` plus images in `context.result_media` (PNG under the caller's tmp `computer-use` folder) and `add_display_media`. Input actions in `computer` (except `left_mouse_down`, `cursor_position`) settle 0.5 s and attach a screenshot; `wait` attaches one after its pause. `computer_batch` runs 1-30 actions in order with 0.2 s between input steps, validates all items before running any, stops at the first failure (reporting completed steps), keeps the pre-call frame for all coordinates, and attaches a final screenshot after input unless the last item was one. `computer_apps open` waits 1.5 s and attaches a screenshot.
- **Access requests (ask mode):** `computer_apps request` resolves names (case-insensitive exact, then unique contains, else suggestions), skips apps already approved, and raises one pending input (`kind` `computer_access`, `payload.message`, `session_id`) through `api.operations.pending_inputs`; operation `respond` takes `{request_id, response: {action: accept|decline|cancel}}`. Each add/remove publishes a `pending_inputs` change. Unanswered after 300 s it counts as declined; Run cancellation withdraws it. Approvals and the display choice expire 30 minutes after the Session's last Computer Use call and are in memory only (reload/restart clears them). One INFO line per decision with counts, never app names. Without ask mode, `request` returns at once that no approval is needed, and `list`/`open` cover every app.
- **Activity sign** (user decision 2026-10: subtle but visible, like Claude Code and Codex): while an Agent controls the desktop, every display shows an accent glow along its edges and the primary display a pill "vBot is using this computer - Esc Esc to stop" (`DesktopTarget.set_activity`). The service shows it when a call takes the desktop (`computer`, `computer_batch`, `computer_apps open`; not `list`/`request`) and keeps it between calls; it goes away when every Run that used it has ended (`run_end`), on any stop, at shutdown, or after `ACTIVITY_IDLE_SECONDS` (120 s) without a call. On Windows its windows are layered, click-through, never activate, stay out of `windows()`/`window_at`, and use `WDA_EXCLUDEFROMCAPTURE`, so screenshots never contain it.
- **Stop:** operation `control` (`status`/`stop` with `call_id`) and the `control` change keep the shape `ComputerUseControl.svelte` relies on (`webui/chat.md`). Stop, Run cancellation and double Esc set the target's stop event; the target releases keys and buttons it pressed and raises `InputInterrupted`, reported as `computer_use_interrupted` with whether input may have been sent and the completed batch steps. Stop never persists; later calls work normally. The hotkey's keyboard hook starts with the first call and is armed only during an active call.

## Conventions and gotchas

- All target calls run on one dedicated worker thread (`ThreadPoolExecutor(1)`), which is per-monitor DPI aware; an `asyncio.Lock` serializes whole calls, so a batch is atomic across Sessions. `computer_apps list`/`request` do not take the lock, so a waiting access request never blocks Stop.
- `run_end` releases a left button a Run's `left_mouse_down` left held; shutdown releases all input and stops the worker.
- Windows app identity: keys are the lowercased executable path, `exe:<basename>` and `pkg:<package family>`; generic hosts (python, java, node, explorer, mmc, rundll32, ...) get no `exe:` key. UWP frames resolve to their CoreWindow process. Apps with the same display name merge into one app, so an approval by name covers all of its windows. Start-menu discovery (one hidden PowerShell call, cached 60 s, refreshed in the background) can delay the first window lookup up to 20 s. Start names are localized.
- Own windows: the current process and any `vBot*.exe` map to app "vBot" (key `vbot:self`) so they are named like the Start entry; they get no special access treatment. A development Desktop shell running under `python.exe` in another process is named after its executable.
- Every key, chord and typed character is held 20 ms and followed by a 20 ms gap (`_win_input._KEY_HOLD`, `_KEY_GAP`): Windows 11 Notepad and other WinUI text boxes drop or repeat keys whose down and up events arrive together (verified live 2026-10). Typing therefore runs at about 25 characters per second.
- Typing uses `KEYEVENTF_UNICODE`; single-character keys map through the foreground window's keyboard layout and refuse characters needing dead keys. `cmd`/`meta` are refused with a pointer to `ctrl` or `win`.
- Screenshots are written to `<data_root>/tmp/computer-use/shot_*.png` and referenced by the displayed Tool media; nothing removes them yet (the temporary-file lifecycle in `storage.md` covers only its own categories).
