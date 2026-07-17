# Web Live-Test Workflow

Use this workflow to verify the running WebUI in a real browser. This is live acceptance testing, not Vitest or component-test authoring.

## 1. Establish the Scope

Read the WebUI domain map and the supplementary reference selected by its task trigger, then inspect the current implementation for the feature under test. Treat the assigned behavior as the test scope; do not rely on a static whole-application checklist because the WebUI changes frequently.

Identify which claims require working Provider credentials before starting. Never expose or copy credentials into commands, reports, screenshots, or logs. If required credentials are unavailable, test the remaining behavior and report the credential-dependent claims as unverified.

## 2. Prepare the Live Environment

Use the current checkout or worktree's configured target. Do not hard-code the product default port and do not point development testing at the installed product data directory.

Before running the environment helper, confirm that the current Python environment is ready and `webui/node_modules/` already exists. Do not install dependencies during live testing; report the missing prerequisite as blocked.

Check the resolved server status before startup and record whether the target is already running. Prefer an explicit isolated port and data directory when the test changes state or needs a newly built server. A pre-existing server is not owned by the test and must remain running afterward.

Start the environment from the repository root:

```bash
python scripts/test-env.py start
```

The helper builds the current WebUI, starts the server, waits for the vBot health contract, and prints the resolved URL. Use exactly that URL for browser testing. If startup fails, preserve the reported error and log path and report the test as blocked rather than repairing the environment as part of the test.

## 3. Test in the Browser

Activate the `playwright-cli` skill for every browser interaction and follow its session-management rules. Take a snapshot before interacting so selectors and visible state come from the current page rather than assumptions.

Exercise the assigned happy path, meaningful empty and error states, boundary behavior, navigation, and any relevant asynchronous updates. After actions that depend on RPC, SSE, or WebSocket delivery, wait for and inspect the resulting browser state. An API response alone does not prove that the WebUI behaved correctly.

Check the visible result as a user would: content, interaction feedback, layout, responsive behavior when relevant, focus and keyboard behavior when relevant, and the absence of unintended content. Do not infer visual correctness solely from DOM text or snapshots.

Capture at least one screenshot path for every browser-visible pass or failure claim. For streaming behavior, capture multiple observations across the Run rather than only its final state. When verifying that content stays hidden, capture the relevant view and explicitly report its absence.

## 4. Report Evidence

Report the resolved URL and environment, the exact flows tested, passed behavior, failed behavior, and anything left unverified. Every failure includes reproducible steps, expected behavior, actual behavior, severity, and the relevant screenshot path. Distinguish a product failure from a blocked prerequisite or missing credential.

## 5. Clean Up

Close every Playwright browser session first. If the test started the server, stop the same resolved environment from the repository root:

```bash
python scripts/test-env.py stop
```

Confirm that a test-owned server stopped. Leave any pre-existing server running and leave no browser or test-owned server processes behind.
