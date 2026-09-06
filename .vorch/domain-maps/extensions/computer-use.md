# Computer Use Extension

Read this reference for the bundled `computer_use` Extension, its opt-in `computer` Tool, desktop input, observations, and operator stop.

## Ownership and prerequisites

`resources/extensions/computer_use/extension.py` owns validation, live authority checks, observation ownership, dispatch, and registration. `driver.py` owns the persistent Cua MCP connection and emergency hotkey. `windows.py` owns Windows physical display geometry, screenshots, and foreground input through Pillow/Win32. `observations.py` owns original images, coordinates, crops, and bounded results. The manifest remains version 1.0.0 and requires Extension API v5.

`computer` requires explicit opt-in; Project whitelist membership alone does not grant access. Policy is checked after lock admission, connection/session setup, and before subsequent actions. Independent Bash permission remains outside this boundary.

The installed Cua Driver must be stable 0.23.2 or newer. Registration snapshots executable availability; installation requires Extension reload. The Extension does not install or update it. Cua screenshots, including Windows background captures, require `max_image_dimension=0`; Windows foreground Pillow captures are independent of this setting. No source patch or compiler is required.

## Desktop behavior

- Omitted targets select the desktop; `pid`/`window_id` select a window. Desktop observations default to vision; windows default to screenshots plus accessibility elements. Browser windows are ordinary native windows, with no CDP or browser-specific actions in this Tool.
- Input requires `apply=True`; foreground delivery remains the default. `foreground=false` selects Cua window capture and background input, including pixels where supported. Coordinate input must match the capture's foreground setting; zoom preserves it. Window/display geometry is checked around background capture and before pixel input. Desktop background input, background timed holds, and background modifiers refuse before dispatch. There is no automatic foreground retry.
- Windows capture includes all monitors or one selected display. `coordinate`/`to_coordinate` pairs refer to the returned image and map to physical virtual-desktop coordinates, including negative origins; resize uses a screen position and `size` pair. Target selection is derived from window IDs or their omission, with no public scope field. Changed window/display geometry refuses input. DPI awareness is thread-local and restored after use.
- Windows vision skips UIA. Window `som` queries Cua elements without a screenshot, then captures native pixels last; `ax` skips the image. Other platforms retain Cua capture/input and explicitly refuse Windows-only monitor selection and timed input.
- Background window captures use Cua's own image coordinates and capture backend; background vision bounds Cua's required UIA query to one level/element. A target that becomes foreground during a background action is reported and stops the remaining sequence. This detects an observed focus change; it cannot prevent application-driven activation or prove that an unverified background action had an effect.
- Sequences contain up to eight mouse/key/text/wait actions with one final observation. Coordinates use the initial view; only the first step may reference an element. All steps validate before input. Failure reports partial completion; observation failure never authorizes replay.
- `capture_after=false` skips the automatic image after successful input or sequence, preserves the dispatched outcome, and invalidates all prior observations; further input needs a new capture. Partial sequences still attempt an observation. `wait` is interruptible, defaults to one second, is bounded to ten seconds, and captures afterward (only at the end when inside sequence).
- Windows foreground coordinate click/drag/scroll supports held modifiers. They span the whole action, including both clicks of a double-click, and release on completion, failure, or stop. A requested modifier already held by the user refuses before pointer movement. Unsupported modifier routes refuse rather than silently ignoring keys.

## Lifecycle and stop

Internally named Driver sessions belong to Project/Agent/Session/Run. Explicit close, Run completion, and Extension shutdown close only owned sessions. One service lock serializes input. Cancellation ownership tokens prevent late callbacks from interrupting a later call. Interruption kills the owned `cua-driver mcp --direct` worker, never a shared daemon.

Operator stop runs outside the service lock. Windows checks a stop event before short input batches; timed key holds and drag waits are interruptible and release owned keys/buttons on completion, error, and stop. A stopped native adapter is not reused. Already delivered OS events cannot be rolled back.

Stop latches in `<data_dir>/computer-use-stopped` across reload/restart. The user-only `control` operation supplies status/stop/resume; resume needs the current stop token and no active call. `ComputerUseControl.svelte` renders contextual stop/resume icons inside the existing Chat composer through Extension RPC; no app-wide bar is mounted. `EmergencyHotkey` owns a Windows keyboard hook for two physical Esc presses within 600 ms, with key-up between them; repeats and injected input are ignored, and events pass through. A separate worker interrupts input so the hook never waits for driver cleanup. Detection stays armed between calls until the last participating Run ends or explicitly closes, independently of transport session cleanup; a pending stop blocks new Tool input before worker dispatch. Management status distinguishes an active call from ongoing computer control and reports hook availability. Agents cannot resume through `computer`.

## Observations and verification

Captures live under the caller's `tmp/computer-use`. Original PNG limits are 32 MiB / 40 million pixels; overviews use at most 1600 pixels on the long edge and 1.5 million pixels. `original` preserves full resolution and `zoom` crops original pixels. `view_id` binds images to transforms. New views use `view_` plus 12 lowercase base32 characters; view images, originals (`orig_`), and overflow state (`state_`) use exclusive collision-retrying file creation through `core/utils/ids.py`. Mutations invalidate prior observations across Runs; transport loss clears them without input replay. Arbitrary Driver-supplied file paths are never read.

`ToolContext.result_media` carries the displayed image; `presentation_images` exposes the original. Structured elements replace duplicate tree text; text overflow retains a complete state file.

Automatic post-input observations wait one interruptible second after dispatch (once after a sequence), without polling pixels. `capture_after=false` skips that delay too. The result explicitly distinguishes dispatch and an observation from application completion; a fixed delay cannot prove completion. `verify` waits for explicit window/element predicates before capturing; `wait` obtains a later observation for visual outcomes. No observation or verification path replays input.

`tests/resources/extensions/test_computer_use.py` covers permissions, transforms, sequences, Provider contracts, cancellation ownership, transport, and cleanup. `test_computer_use_windows.py` covers native geometry, Unicode events, and interrupted key/drag cleanup through a controlled OS boundary. The Provider probe matrix uses the production definition without sending input.
