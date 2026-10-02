# Computer Use Extension

Read this reference for the bundled `computer_use` Extension: its three opt-in Tools (`computer`, `computer_batch`, `computer_apps`), per-Session app access, screenshots and coordinates, desktop input, and operator stop.

## Overview

The Extension lets an Agent operate the interactive Windows desktop of the vBot server host with the user's real mouse and keyboard (foreground input only). Its Agent interface follows Anthropic's computer tool vocabulary and Claude Code's computer-use server: no ids carry between calls, coordinates always refer to the latest screenshot. The first generation (one `computer` Tool with window targets, view ids, background delivery, element refs and the external Cua Driver) is archived in `archive/computer-use.zip` (`archive/README.md`); nothing of it remains at runtime.

Windows is the only platform today. On other hosts, or when the process has no usable interactive desktop (`DesktopTarget.readiness()`), the Tools stay registered but not ready, with the readiness hint "Computer Use works when the vBot server runs on Windows in a signed-in desktop session."

Decided direction for later work (2026-10, user decision): further targets (VM, second session, Linux Xvfb container) implement the same `DesktopTarget` protocol; a Provider capability may later declare the native Anthropic toolset and map it 1:1 to `computer`. Neither exists yet.

## Terms

### Grant
A user's permission for one application in one Session, at the tier of the application's category. Distinct from the Tool opt-in (Tool Access Policy), which must also allow each Tool.

### Tier
What a Grant permits: `read` (view only: screenshot, zoom, cursor_position, wait), `click` (adds left/double/triple click, scroll and mouse_move without modifier keys), `full` (everything). Browsers get `read`, terminals and IDEs `click`, every other app `full` (`_access.CATEGORY_TIERS`).

### Frame
The coordinate space of a display's screenshots: its physical size scaled down (never up) to a long edge <= 1568 and area <= 1,150,000 pixels (`_screens.fit`). Depends only on display geometry, so mapping is deterministic.

## Ownership and source routing

`resources/extensions/computer_use/`:
- `extension.py` - `register`, `ComputerUseService`: readiness, opt-in check per Tool, Session state lookup, call serialization, stop/control, hotkey arming, `run_end`/shutdown release, the three Tool handlers and their result/error envelopes.
- `_tools.py` - descriptions, schemas, argument normalizers (other harnesses' spellings; Agent error tolerance per `tools.md`).
- `_actions.py` - parses one `computer` action into an `Action` (tier, points, seconds, notes).
- `_desktop.py` - `Desktop`: one call's view of the desktop: frame resolution, display selection, screenshot/zoom/cursor, access checks before input, dispatch to the target.
- `_access.py` - Session state (grants, display choice), expiry, app name resolution, access requests as pending inputs.
- `_screens.py` - platform-neutral imaging: frames, coordinate mapping, display choice, masking, PNG publishing.
- `_keys.py` - chord parsing to canonical key names (xdotool/Anthropic spellings accepted).
- `target.py` - `DesktopTarget` protocol and value types (`Display`, `AppInfo`, `WindowInfo`, `TargetError`, `InputInterrupted`). The only boundary to the operating system.
- `windows_target.py` with `_win32.py` (bindings, displays, capture, window enumeration), `_win_input.py` (`SendInput`), `_win_apps.py` (app identity, categories, Start-menu discovery, launching) - the Windows target.
- `hotkey.py` - `EmergencyHotkey` (double Esc).
- `skills/computer-use/SKILL.md` - the workflow Skill, discovered like any loaded-Extension Skill; it grants nothing.

Tests: `tests/resources/extensions/computer_use/` (`computer_use_test_support.py` holds `FakeTarget`, `FakeHotkey`, the dispatch `Harness`; `test_computer_use_tools.py`, `_access.py`, `_batch.py`, `_dialects.py`, `test_hotkey.py`, `test_windows_target.py` for pure Windows helpers) and `tests/core/runtime/test_runtime_computer_access.py` (opt-in through runtime dispatch, temporary Agents). No test sends real input or captures the real screen.

## Contracts

- **Two gates.** Each Tool is `requires_opt_in`; the handler re-checks the calling Agent's Tool Access Policy (`resolve_tool_access`, `host.resolve_tool_agent`) and refuses with `tool_not_allowed`. Grants come on top; a call outside a Session is refused (`computer_use_unavailable`) because grants are per Session.
- **Access check before input** (`Desktop._check_access`): every action above `read` requires the foreground window's app and the app under each target point (both drag ends; the pointer position when no coordinate is given) to be granted at a sufficient tier, not to be vBot itself, and not elevated (`target_elevated`; UIPI would drop the input). The taskbar and desktop belong to "File Explorer". With no foreground window, only pointer actions on granted apps proceed. Refusals name the app and the exact `computer_apps` call.
- **Masking:** screenshots and zooms paint windows bottom to top of z-order; a granted window reveals its frame, every other window, vBot's own windows and uncovered pixels are a solid neutral fill. Results name the hidden apps (names, never titles).
- **Coordinates:** frame pixels of the display shown by the Session's latest screenshot, also when `scale` < 1 returned a smaller image. A display that changed size or disappeared since that screenshot refuses coordinate use (take a new screenshot). Out-of-frame coordinates refuse before anything runs. `zoom` captures the region fresh at physical resolution (fit to frame limits) and never changes the frame.
- **Display choice** is per Session: `screenshot` with `display` (name, 1-based number, `auto`) changes it; `auto` follows the foreground window when it is visible to the Session, else the primary display.
- **Results:** text `content` plus images in `context.result_media` (PNG under the caller's tmp `computer-use` folder) and `add_display_media`. Input actions in `computer` (except `left_mouse_down`, `cursor_position`) settle 0.5 s and attach a screenshot; `wait` attaches one after its pause. `computer_batch` runs 1-30 actions in order with 0.2 s between input steps, validates all items before running any, stops at the first failure (reporting completed steps), keeps the pre-call frame for all coordinates, and attaches a final screenshot after input unless the last item was one. `computer_apps open` waits 1.5 s and attaches a screenshot.
- **Access requests:** `computer_apps request` resolves names (case-insensitive exact, then unique contains, else suggestions; vBot is never requestable), skips apps already granted at that tier, and raises one pending input (`kind` `computer_access`, `payload.message`, `session_id`) through `api.operations.pending_inputs`; operation `respond` takes `{request_id, response: {action: accept|decline|cancel}}`. Each add/remove publishes a `pending_inputs` change. Unanswered after 300 s it counts as declined; Run cancellation withdraws it. Grants and the display choice expire 30 minutes after the Session's last Computer Use call and are in memory only (reload/restart clears them). One INFO line per decision with counts and tiers, never app names.
- **Stop:** operation `control` (`status`/`stop` with `call_id`) and the `control` change keep the shape `ComputerUseControl.svelte` relies on (`webui/chat.md`). Stop, Run cancellation and double Esc set the target's stop event; the target releases keys and buttons it pressed and raises `InputInterrupted`, reported as `computer_use_interrupted` with whether input may have been sent and the completed batch steps. Stop never persists; later calls work normally. The hotkey's keyboard hook starts with the first call and is armed only during an active call.

## Conventions and gotchas

- All target calls run on one dedicated worker thread (`ThreadPoolExecutor(1)`), which is per-monitor DPI aware; an `asyncio.Lock` serializes whole calls, so a batch is atomic across Sessions. `computer_apps list`/`request` do not take the lock, so a waiting access request never blocks Stop.
- `run_end` releases a left button a Run's `left_mouse_down` left held; shutdown releases all input and stops the worker.
- Windows app identity: keys are the lowercased executable path, `exe:<basename>` and `pkg:<package family>`; generic hosts (python, java, node, explorer, mmc, rundll32, ...) get no `exe:` key. UWP frames resolve to their CoreWindow process. Apps with the same display name merge into one app, so a grant by name covers all of its windows. Start-menu discovery (one hidden PowerShell call, cached 60 s, refreshed in the background) can delay the first window lookup up to 20 s. Start names are localized.
- Own windows: the current process and any `vBot*.exe` map to app "vBot" (key `vbot:self`). A development Desktop shell running under `python.exe` in another process is not recognized.
- Typing uses `KEYEVENTF_UNICODE`; single-character keys map through the foreground window's keyboard layout and refuse characters needing dead keys. `cmd`/`meta` are refused with a pointer to `ctrl` or `win`.
- Screenshots are written to `<data_root>/tmp/computer-use/shot_*.png` and referenced by the displayed Tool media; nothing removes them yet (the temporary-file lifecycle in `storage.md` covers only its own categories).
