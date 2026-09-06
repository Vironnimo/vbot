# Browser Use Extension

Read this reference when changing the bundled `browser_use` Extension or `browser` Tool. Setup and deployment limits are documented in `docs/browser-use.md` (repository-relative).

## Ownership and permission

`resources/extensions/browser_use/extension.py` owns registration, configuration, validation, native transport, browser lifecycle, tab/ref ownership, and media. The existing Extension API, Tool Access Policy, and `ToolContext` media seams supply the integration; this capability has no browser add-on or executable Skill. The API v5 manifest and `requires_opt_in=True` require an explicit Agent grant. Mode `all` and Project whitelist membership alone do not grant it. The handler resolves live Agent and Run restrictions before every backend command, including each form field and observations.

`agent-browser >=0.36.0` is an independently installed server executable. Availability is captured at registration; the version is checked before the first connection. vBot does not install it. General host Bash access remains outside this Tool permission boundary.

## Lifecycle and input invariants

- Backend identities are generated internally and keyed by exact Project/Agent/Session identity. A browser stays connected across Runs in that Session; refs do not. Explicit close, idle cleanup, and Extension shutdown close only that owned backend connection. Native close shuts down a managed browser but only detaches from an external browser. Failed cleanup stays tracked for retry.
- The three live connection settings are managed launch, local existing Chrome auto-connect, or a configured remote CDP URL. Configuration changes retire the old connection before another action; the next call reconnects. Endpoint credentials are resolved through the existing Extension secret setting mechanism.
- Per-session locks serialize managed browsers; a shared lock serializes external browser operations. Cancellation, policy, and config are rechecked after lock admission and before subsequent commands. Already dispatched commands can finish. There are no automatic input retries.
- External connections use native `--pin-tab`. Public tab ids are stable CDP target ids, never list indexes. Before page operations, verify the bound target still exists and is selected; restoring a drifted selection clears refs. A missing target fails rather than falling back to another tab. Another tracked Session's selected external tab cannot be selected or closed.
- Snapshots return compact interactive content by default, bounded to 16000 characters. Only visible backend refs become opaque snapshot-scoped public refs. Every snapshot clears old refs before dispatch, including empty/failed observations. Input clears refs; a new Run cannot reuse them. Resolve the entire fill target list before applying its first field.
- `fill` applies several fields in order without repeated snapshots. A later failure reports completed fields and stops. Failed observation after successful input preserves the successful action and reports an observation error. Failed close-tab replies are reconciled by reading tabs, never by replaying close.
- Native commands are argv arrays sent through one-command batch JSON on stdin, preventing form values from becoming global CLI options. Use the native Windows executable directly, no shell. Seekable temporary files avoid daemon-inherited pipe EOF hangs. Transport has a 45-second timeout and bounded output; ambient browser configuration and credentials are not forwarded. Errors never return raw backend diagnostics.
- Screenshots and managed downloads live below the calling data directory's `tmp/browser-use` tree. Screenshots are bounded PNG files delivered directly through `result_media` and `presentation_images`. Downloads lists only completed, nonsymlink files from the managed browser's download directory. Attached-browser downloads and upload paths belong to the browser host, which can differ from the server.

## Verification routing

`tests/resources/extensions/test_browser_use.py` covers grants, validation, cancellation/revocation between fields, Session/Run isolation, stale refs, selected-tab drift, cleanup, transport, media, and the complete action matrix. Bundled discovery expectations live in `tests/core/runtime/test_runtime.py` and `test_runtime_providers.py`. Generic opt-in policy and registration tests live in `tests/core/tools/test_availability.py` and `tests/core/extensions/test_capabilities.py`.

`scripts/probe_provider_tool_call.py --scenario browser --browser-case ...` uses the production Tool definition for structural Model-call probes without executing browser input. Operator documentation records the separate real-browser checks and the limits of those measurements.
