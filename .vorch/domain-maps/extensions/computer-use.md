# Computer Use Extension

Read this reference for the bundled `computer_use` Extension, the `computer` Tool, driver lifecycle, capture ownership, and desktop input. The Extension replaces the former bundled Computer Use Skill.

## Ownership and permission

`resources/extensions/computer_use/extension.py` owns the entire capability: driver calls, input validation, session ownership, captures, media, and registration. Its manifest requires Extension API v5 and registers `computer` with `requires_opt_in=True`. Shared Tool Access Policy enforces explicit grants; Project whitelist membership alone never grants access. The handler also resolves the current Agent policy before each call and again after starting a driver session, so revocation while waiting cannot lead to another input.

The driver remains an independently installed `cua-driver` executable on the server host. Registration snapshots executable availability; missing installations remain configurable but not ready until Extension reload. The Extension does not install, update, reconfigure, or terminate the shared driver daemon. Host Bash access is outside this Tool permission boundary.

## Lifecycle and input invariants

- Driver session names are generated internally and keyed by exact Project/Agent/Session/Run identity. Agents cannot choose or reuse another context's driver session. First use starts a session; explicit close, Run end, and Extension shutdown close only owned sessions. Failed cleanup remains tracked for shutdown retry.
- One Extension-owned lock serializes desktop operations across Runs; `parallel_safe=False` additionally preserves ordering inside each Run. Cancellation is checked after admission and before subsequent driver actions. An already-dispatched driver call can finish before cancellation is observed.
- Public actions are status, apps, windows, capture, click, type, key, scroll, and close. Input defaults to preview and requires `apply=True` to execute. Foreground delivery is explicit; background is the default.
- Every applied input requires a matching capture for its exact pid/window_id. Bare indices and explicit tokens resolve against that capture, including window ownership. A new capture clears old references even if the new result is empty or fails. Applied or uncertain input invalidates the capture. Input never retries automatically.
- Unknown/inapplicable fields and malformed conditional arguments fail before driver session creation. Lock/logout/force-quit shortcuts retain the prototype's block list. Driver failures report failure rather than successful input.
- Captures live below the calling data directory's tmp/computer-use tree. Model-facing authored paths use forward slashes. Screenshots are bounded to 32 MiB and feed `ToolContext.result_media`; UI previews use `presentation_images`. Accessibility results stay bounded with full-file paths for overflow. Driver-provided arbitrary screenshot paths are never loaded.

## Verification

`tests/resources/extensions/test_computer_use.py` covers driver argument mapping, captures, stale handles, grants, context isolation, cancellation, cleanup, and failure behavior. Generic policy/registration coverage lives in `tests/core/tools/test_availability.py` and `tests/core/extensions/test_capabilities.py`; Project resolution coverage lives in `test_resolver_config_chains.py`. `scripts/probe_provider_tool_call.py --scenario computer --computer-case ...` uses the production definition for the Model call matrix without executing desktop input.
