# E2E Test Workflow

An end-to-end test is a sweep of the whole running product: coding agents drive the built WebUI in a real browser against vBot's deterministic fake Provider, work through every journey of `.vorch/workflows/e2e-journeys.md`, look further wherever something seems off, and fix the clear regressions they find. There is no scripted E2E suite. Run this workflow only when the user asks for an E2E test; neither CI nor the release runs it.

You orchestrate: you prepare one isolated environment per journey group, hand the groups to tester subagents running in parallel, triage what they report, fix what is clear and bounded, and report the rest.

## 1. Prepare

Start from the primary checkout on a committed `main` and record the commit you test (`git rev-parse --short HEAD`). Check the prerequisites once: the current Python environment, `webui/node_modules/`, `node` and `python` on `PATH` (the fake Provider, the Tool-cancellation command and the terminal harness need them), and the `playwright-cli` Skill. Do not install missing prerequisites; report them as blocked.

Pick a run id (the date, `YYYY-MM-DD`, plus a suffix when it is taken) and an evidence folder outside every checkout, so it survives the worktree cleanup: `<repo>-data/e2e-runs/<run-id>/` (for `C:/Development/projects/vBot`: `C:/Development/projects/vBot-data/e2e-runs/<run-id>/`).

Give each journey group of the catalog its own worktree, so groups run in parallel without sharing state:

```bash
python scripts/worktree.py create e2e-<group>
```

`create` makes the worktree from `main`, with its own ports, a fresh data directory and Settings seeded for the fake Provider (`scripts/README-worktree.md`). Then start its environment from inside that worktree:

```bash
python scripts/test-env.py start
```

It builds the WebUI, starts the fake Provider declared in the worktree's Settings and then vBot, and prints the URL. Confirm that the output reports the fake Provider as started; without it every Run fails. Record per group the worktree path, URL, data directory and fake Provider port. If startup fails, keep the reported error and log path and report that group as blocked instead of repairing the environment.

Never point a sweep at the installed product's data, the primary `vBot-data/dev` instance or any server you did not start.

## 2. Sweep

Launch one tester subagent per group with the Agent tool, all in one message so they run concurrently. They need no `isolation`: each works in the worktree you made for it and changes no files there. Each brief names the group, its worktree path, URL, data directory, fake Provider port and evidence folder `<run-id>/<group>/`, and tells the tester to read, in full, `.vorch/workflows/e2e-journeys.md` -> General and its group's section, this section and section 3 of `.vorch/workflows/web-test-workflow.md` (browser testing rules).

Rules for testers:

- **Report, never repair.** A tester changes no code, test, journey or Setting beyond what a journey's steps do, and reports every failure.
- **Own browser session.** Use the `playwright-cli` Skill with a session named after the group (`-s=e2e-<group>`), so parallel testers never share a browser. Take a snapshot before interacting; refer to elements by what the snapshot shows, since the catalog names visible labels, not selectors.
- **Order and state.** Run the group's journeys in catalog order: later journeys rely on the cleanup of earlier ones. Start each journey in a new Session unless its steps continue one, and do its cleanup even when it failed.
- **The catalog is the minimum.** When something looks wrong or unexpected beyond the journey's expectations (a flicker, a stale count, a console error, a duplicated message, a confusing state), stay with it: repeat it, vary the input, check the neighboring behavior, reload, and read the browser console and the server log (`<data-dir>/logs/`). Report what you observed, even when no expectation names it.
- **Timing.** Some journeys act inside a window of a few seconds (marked in the catalog). Prepare the next command before you trigger the Run. A missed window is a tester miss, not a failure: repeat the journey in a new Session, and report a failure only when it reproduces.
- **Evidence.** Save screenshots into the group's evidence folder (`playwright-cli screenshot --filename=<path>`), at least one for every pass or failure claim and several across a streaming Run. An RPC response alone does not prove what the WebUI showed.

Each tester reports, per journey: passed, failed or blocked, with screenshot paths. A failure adds the steps to reproduce it, expected and actual behavior, relevant console and server log lines, and the tester's guess whether the product or the journey is wrong. Further observations come in their own list. Before reporting, the tester closes its browser session.

## 3. Triage

Sort every failure and observation, reading the evidence yourself and reproducing it in the group's environment when the report leaves doubt:

- **Regression:** the product breaks a behavior the journey expects and nothing intended that change.
- **Outdated journey:** the product changed on purpose (check `git log` and the owning domain map); the journey, or the fake Provider scenario behind it, is stale.
- **Tester or environment:** a missed window, state leaked from an earlier journey, a missing prerequisite. Repeat the journey cleanly before you dismiss it.
- **Unclear:** the evidence does not decide. Say so; do not guess.

## 4. Fix

Fix in one worktree of its own, never in a sweep worktree: `python scripts/worktree.py create e2e-fixes`.

- **Regressions with a clear, bounded, low-risk fix:** fix them following `AGENTS.md` (Architecture, Testing: extend the test that owns the behavior), one commit per fix.
- **Outdated journeys:** update the journey in `.vorch/workflows/e2e-journeys.md`, and the fake Provider scenario when needed, in their own commit.
- **Everything else** (a fix that is large, risky or needs a product decision, or an unclear cause): do not fix it. Report it, and record each confirmed one in `.vorch/FLAGGED.md` of the primary checkout.

Then start the fix worktree's own environment (`python scripts/test-env.py start` inside it) and repeat every affected journey in the browser. A fix counts only once its journey passes there. Stop that environment and merge: `python scripts/worktree.py merge e2e-fixes`. Push only when the user asks for it.

## 5. Clean up

Inside each sweep worktree run `python scripts/test-env.py stop` and confirm the server and fake Provider stopped; then remove it with `python scripts/worktree.py delete e2e-<group>`, which also removes its data directory. Close any browser session still open. The evidence folder stays.

## 6. Report

Report to the user: the tested commit; per group the passed, failed and blocked journeys; each failure with its classification, reproduction and evidence path; the fixes with their merged commits and the journeys that confirmed them; what is left open, and which items went into `FLAGGED.md`; and product areas that have no journey yet, as suggestions for the catalog.

## Maintaining the catalog

A journey earns its place by proving user-visible behavior that crosses the browser, the server and the fake Provider: live streaming and Run reattachment, several clients on one Session, real terminals, uploads and served media, persistence across reloads, Scheduled Runs that actually fire, configuration that must reach the Provider request. Tool semantics, form validation details and component behavior belong in the pytest and Vitest suites. Product changes do not have to update the catalog; the next sweep reconciles it.

The fake Provider and its Settings fixture (`scripts/fake-provider.mjs`, `scripts/fake-provider-settings.json`, started by `scripts/test-env.py`) serve the sweep and manual live testing alike; `scripts/fake-provider-project/` is the repository fixture of the Project journey. A scenario reads its request as a Model does: Tool results arrive as their plain-text rendering, not as JSON envelopes (`providers/request-policy.md`), and the shell Tool is offered and replayed as `powershell` on Windows and `bash` elsewhere (`tools.md` -> Model Tool names). Key scenario progress on the scenario's own calls and on result text, never on envelope fields or a single shell name. A scenario that neither a journey nor a named manual check uses is removed together with its last journey.
