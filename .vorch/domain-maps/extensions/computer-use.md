# Computer Use Extension

Read this reference for the bundled `computer_use` Extension, its opt-in `computer` Tool, desktop input, observations, and operator stop.

## Ownership and prerequisites

`resources/extensions/computer_use/extension.py` owns validation, live authority checks, observation ownership, dispatch, and registration. `driver.py` owns the persistent Cua MCP connection and emergency hotkey. `windows.py` owns Windows physical display geometry, screenshots, and foreground input through Pillow/Win32. `observations.py` owns original images, coordinates, crops, and bounded results. The manifest remains version 1.0.0 and requires Extension API v5.

`computer` requires explicit opt-in; Project whitelist membership alone does not grant access. Policy is checked after lock admission, connection/session setup, and before subsequent actions. Independent Bash permission remains outside this boundary.

The installed Cua Driver must be stable 0.23.2 or newer. Registration snapshots executable availability; installation requires Extension reload. The Extension does not install or update it. Non-Windows screenshots require Cua's global `max_image_dimension=0`; Windows uses original Pillow captures independently of this setting. No source patch or compiler is required.

## Desktop behavior

- Omitted targets select the desktop; `pid`/`window_id` select a window. Desktop observations default to vision; windows default to screenshots plus accessibility elements. Browser windows are ordinary native windows, with no CDP or browser-specific actions in this Tool.
- Input requires `apply=True`; foreground delivery is the default. Windows pixel input uses physical foreground events and refuses background delivery. Background window element actions remain available through Cua.
- Windows capture includes all monitors or one selected display. Input coordinates refer to the returned image and map to physical virtual-desktop coordinates, including negative origins. Changed window/display geometry refuses input. DPI awareness is thread-local and restored after use.
- Windows vision skips UIA. Window `som` combines a native screenshot with a screenshot-free Cua element query; `ax` skips the image. Other platforms retain Cua capture/input and explicitly refuse Windows-only monitor selection and timed input.
- Sequences contain up to eight mouse/key/text actions with one final observation. Coordinates use the initial view; only the first step may reference an element. All steps validate before input. Failure reports partial completion; observation failure never authorizes replay.

## Lifecycle and stop

Internally named Driver sessions belong to Project/Agent/Session/Run. Explicit close, Run completion, and Extension shutdown close only owned sessions. One service lock serializes input. Cancellation ownership tokens prevent late callbacks from interrupting a later call. Interruption kills the owned `cua-driver mcp --direct` worker, never a shared daemon.

Operator stop runs outside the service lock. Windows checks a stop event before short input batches; timed key holds and drag waits are interruptible and release owned keys/buttons on completion, error, and stop. A stopped native adapter is not reused. Already delivered OS events cannot be rolled back.

Stop latches in `<data_dir>/computer-use-stopped` across reload/restart. The user-only `control` operation supplies status/stop/resume; resume needs the current stop token and no active call. `ComputerUseControl.svelte` owns the global UI through Extension RPC. `EmergencyHotkey` owns Windows Ctrl+Alt+Pause and reports registration failure. Agents cannot resume through `computer`.

## Observations and verification

Captures live under the caller's `tmp/computer-use`. Original PNG limits are 32 MiB / 40 million pixels; overviews use at most 1600 pixels on the long edge and 1.5 million pixels. `original` preserves full resolution and `zoom` crops original pixels. `view_id` binds images to transforms. Mutations invalidate prior observations across Runs; transport loss clears them without input replay. Arbitrary Driver-supplied file paths are never read.

`ToolContext.result_media` carries the displayed image; `presentation_images` exposes the original. Structured elements replace duplicate tree text; text overflow retains a complete state file.

`tests/resources/extensions/test_computer_use.py` covers permissions, transforms, sequences, Provider contracts, cancellation ownership, transport, and cleanup. `test_computer_use_windows.py` covers native geometry, Unicode events, and interrupted key/drag cleanup through a controlled OS boundary. The Provider probe matrix uses the production definition without sending input.
