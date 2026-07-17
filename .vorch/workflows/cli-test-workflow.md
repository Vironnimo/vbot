# CLI Live-Test Workflow

Use this workflow to verify the running CLI as a user or automation client would. This is live acceptance testing, not pytest authoring.

## 1. Establish the Scope

Read the CLI domain map, then inspect the current command help and the implementation relevant to the assigned behavior. Discover the current command surface through `python cli/main.py --help` and the applicable nested `--help`; do not rely on a static command inventory in this workflow.

Classify each command in scope before running it. Server lifecycle, Desktop launch, update, autostart, and doctor have local effects; the management areas use the running server's RPC contract. Commands that update the checkout, operating-system autostart, credentials, real user data, or another external system must be explicitly in scope and tested only in an appropriate isolated environment.

## 2. Isolate the Target

Use the current development checkout or worktree's configured target, or an explicit temporary data directory and free port when the scenario requires clean state. Never point development tests at the installed product data directory or an unrelated running instance.

Check the resolved server status before startup and record whether the target is already running. For RPC-backed commands, start the exact target through the CLI and retain its resolved URL, data directory, log path, and ownership state for verification and cleanup. Prefer an explicit isolated port and data directory for mutations. Do not build the WebUI unless the assigned CLI behavior depends on WebUI availability.

```bash
python cli/main.py server start [--host HOST] [--port PORT] [--data-dir DIR]
```

If the current Python environment or required existing dependencies are unavailable, report the test as blocked. Do not install dependencies during live testing.

## 3. Exercise the Command

Invoke commands through `python cli/main.py` with the same arguments a user or automation would provide. For every case, record the exact command, stdout, stderr when present, and the process exit code.

Cover the assigned happy path and the meaningful failures for that command: missing or invalid input, absent resources, target conflicts, unavailable server, and repeated execution when idempotency matters. Verify mutation results with the corresponding read command or another public contract; successful text alone does not prove that state changed correctly.

Check that output is deterministic and actionable: success and failure are explicit, identifiers and resolved targets are shown where needed, secrets are never printed, and the exit code matches the documented outcome. Verify that RPC-backed commands do not silently fall back to direct file mutation and that local commands do not accidentally target a remote or unrelated instance.

Do not run destructive or machine-changing commands merely to broaden coverage. Test `update`, `autostart`, credential changes, destructive confirmations, Desktop launch, or real Channel/Provider effects only when the assigned scope and environment make that effect intentional.

## 4. Report Evidence

Report the target environment, exact commands and exit codes, passed behavior, failed behavior, and anything left unverified. Every failure includes reproducible steps, expected behavior, actual behavior, severity, and the relevant output or log path. Distinguish a product failure from a blocked prerequisite or unsafe environment.

## 5. Clean Up

Stop the exact server target only if the test started it:

```bash
python cli/main.py server stop [--host HOST] [--port PORT] [--data-dir DIR]
```

Confirm that a test-owned server stopped, close any Desktop process started by the test, and remove only temporary state created specifically for the test. Leave any pre-existing server running and leave no test-owned server or client processes behind.
