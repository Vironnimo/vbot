# Computer Use Extension

Read this reference for the bundled `computer_use` Extension, the `computer` Tool, driver lifecycle, capture ownership, and native/browser input. The Extension replaces the former bundled Computer Use Skill.

## Ownership and permission

`resources/extensions/computer_use/extension.py` owns the public contract, validation, authority, dispatch, and registration. `driver.py` owns the persistent MCP connection and protocol/version checks; `observations.py` owns original PNGs, bounded observations, image coordinates, and crops. The manifest requires Extension API v5. `computer` requires explicit opt-in; Project whitelist membership alone never grants access. The handler resolves live Agent policy after lock admission, after connection/session setup, and before every subsequent driver action.

The independently installed `cua-driver` must be stable 0.23.2 or newer. Its global `max_image_dimension` must be 0: the Extension checks this prerequisite and never changes it. Stable 0.23.2 on Windows writes this setting globally even when a session label is supplied. Do not silently apply a supposed session override. Registration snapshots executable availability; missing installations require Extension reload after installation. The Extension owns one bounded `cua-driver mcp` process, not the shared daemon. macOS still has upstream app-service prerequisites. Host Bash access remains outside this Tool permission boundary.

## Lifecycle and input invariants

- Driver session names are generated internally and keyed by exact Project/Agent/Session/Run identity. Agents cannot choose another context's driver session. First use starts one; explicit close, Run end, and Extension shutdown close only owned sessions. Failed cleanup remains tracked for shutdown retry.
- One lock serializes operations across Runs; `parallel_safe=False` preserves Run ordering. Cancellation is checked before subsequent driver actions. A dispatched call may finish before cancellation is observed. MCP response waits are bounded to 45 seconds. Transport loss invalidates owned observations and never replays input.
- Input defaults to preview and requires `apply=True`; foreground delivery is explicit. Discovery and dialog inspection are read-only. Browser preparation with an explicit profile requires apply; it never silently converts existing browser preparation to an isolated profile.
- Every applied target input requires a matching observation. Window indices/tokens resolve against that capture. Image coordinates require the current `view_id`. Exact browser target/tab identifiers stay inside their owning driver session. Captures invalidate older observations of that target across Runs; any mutation invalidates all old observations because native windows and browser tabs share a desktop.
- Applied mutations return one fresh observation. A known sequence contains up to eight click/type/key steps: only the first may select a target, later steps operate on focus. Failure stops the sequence and reports completed steps and partial outcome. Observation failure preserves an already-dispatched outcome; it never justifies automatic retry.
- Unknown/inapplicable fields and malformed conditional arguments fail before connection creation. All sequence steps validate before any input. Lock/logout/force-quit shortcuts retain the prototype block list. Driver refusals remain failures.

## Observation boundary

Captures live below the calling data directory's `tmp/computer-use` tree. Original PNGs are retained unchanged (32 MiB / 40 million pixel limits). Automatic overviews use at most 1600 pixels on the long edge and 1.5 million pixels; `original` bypasses resizing and `zoom` crops retained original pixels. `view_id` binds each overview/crop to its pixel transform. Browser coordinates additionally apply the driver's screenshot-to-CSS scale; absent scale refuses coordinate input rather than guessing. Driver-supplied arbitrary image file paths are never read.

`ToolContext.result_media` carries the displayed image; `presentation_images` points to the original for UI review. `ax` actually skips screenshot capture for window/browser targets. Structured window elements replace the duplicate Markdown tree; query/limit reduce capture output. Bounded text overflow keeps a full state file. Primary-display capture is the desktop fallback; it is not a monitor-selection API.

## Verification and upstream limits

`tests/resources/extensions/test_computer_use.py` covers pixel/crop/CSS transforms, the full provider matrix through the real handler, permissions, context isolation, partial sequences, cancellation, refusals, MCP reuse, prerequisite checks, and cleanup. `scripts/probe_provider_tool_call.py --scenario computer --computer-case ...` sends the production definition to Luna without executing desktop input; its matrix contract is checked in `tests/scripts/test_probe_provider_tool_call.py`.

On German Windows, stable 0.23.2 browser preparation currently fails endpoint ownership verification: upstream `platform-windows/src/browser_platform.rs::parse_netstat_loopback_listeners` recognizes English `LISTENING`, while the local OS emits a localized state. Preserve this refusal; do not bypass ownership checks. Native background input is platform/app dependent, and DPI-unaware application captures can be inaccurate in this driver. Fresh observations and explicit foreground/desktop alternatives remain necessary. Browser handlers are covered with deterministic protocol fixtures; successful live semantic-browser control on this host awaits that upstream fix.
