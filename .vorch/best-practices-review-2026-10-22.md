# Growth and maintainability review - 2026-10-22

Reviewed source checkpoint: `57a9d7f984597b26eb56ccf9ae14fbec3240de50`.

## Recommendation

Keep the existing domain architecture and strengthen its enforcement. The reviewed
code already has meaningful owners, shared persistence and transport boundaries,
bounded worker pools, explicit cancellation handling, and substantial tests.
The findings below concern specific places where those protections do not extend
far enough, or where repeated work grows with unrelated historical data.

There are eleven new findings and one previously recorded capacity decision.
They do not establish a need for a general rewrite, a different database, or
additional service layers. No application changes were made by this review.

## Scope and evidence

Five parallel reviewers covered Runtime/Chat/Runs/Extensions, persistence and
projections, server/access/security, WebUI/accessors, and tests/build/distribution.
The coordinating review checked shared Tool contracts and cross-checked the main
findings. Reviewers read domain maps, production code and relevant tests; isolated
synthetic probes checked selected claims. Existing test suites were not run,
following the repository's review rule.

Evidence below distinguishes reproduced defects, measured work counts, source
traces, and recommendations. Synthetic counts are not production latency or
capacity measurements. No installed instance, credentials, or real user data was
changed. Existing concerns and explicit deferrals were checked against
`FLAGGED.md` and `security-review-2026-10-19.md`.

This was a broad risk-based review, not exhaustive verification of every module.
In particular, it did not perform a full accessibility or Desktop/CLI audit,
Provider-wire compatibility sweep, production load test, recovery/snapshot matrix,
penetration test, or inspection of GitHub organization settings and recent CI runs.

## Findings at a glance

| ID | Finding | Evidence | Recommended order |
| --- | --- | --- | --- |
| BP01 | Archive compensation can discard recovery provenance on an I/O error | Controlled fault probe | First: correctness |
| BP02 | Old WebUI reads can replace newer pushed or acknowledged state | Controlled response-order probe | First: correctness |
| BP03 | Extension loading and attachment reads still block the Event Loop | Timer probe plus source trace | First: responsiveness |
| BP04 | One Extension can exhaust the shared callback workers | Controlled saturation probe | Next: failure isolation |
| BP05 | Full tests and shipped packages use different dependency sets | Workflow trace and local metadata | First: verification |
| BP06 | A single successful retry turns an unknown test failure green | CI/push source and existing flake records | First: verification policy |
| BP07 | Build subprocesses inherit the release-signing secret | Workflow and subprocess trace | First: distribution hardening |
| BP08 | Semantic Recall reprocesses all old messages after an append | Operation-count probe | Next: data growth |
| BP09 | A single-Session Statistics read walks the global Session index | Operation-count probe | Next: data growth |
| BP10 | Channel refresh fans out to every Channel's details | RPC-count probe | Next: client growth |
| BP11 | Rejected RPC method names enter logs without bounds or normalization | Controlled logging probe | First: bounded diagnostics |

### BP01 - Preserve Archive recovery evidence when a path is unavailable

`core/archive/_operations.py:346-353` uses `Path.exists()` to decide whether a
failed archive retained any payload trees. If all checks return false, it calls
`archive_ledger.abandon`. Python 3.14 also returns false when the underlying check
fails with an I/O or permission error; that is not evidence that the tree is gone.
This contradicts the existing strict-filesystem and Archive recovery contracts.

**Evidence:** With `Path.stat` controlled to raise `PermissionError`, Python
3.14.4 returned false from `exists()`, and the actual compensation helper called
`ledger.abandon(7)`. Side effects were mocked. An inaccessible retained payload
therefore loses its pending recovery entry and original rollback provenance.
Later orphan adoption does not provide the same automatic rollback.

**Recommendation:** Use the existing strict check, retain `archiving` on an
unknown filesystem state, and report the failure. Extend the owning compensation
test with this case. No new recovery subsystem is needed. The changed Python
semantics are documented in [pathlib](https://docs.python.org/3.14/library/pathlib.html#querying-file-type-and-status).

### BP02 - Order WebUI snapshots, pushes and mutation responses together

`webui/src/App.svelte:371-389` applies activity and data-store reads without a
freshness guard. Newer activity pushes arrive through
`webui/src/lib/appController.js:305`, and acknowledgement responses update recovery
state at `App.svelte:392`. An older outstanding read can overwrite either.

**Evidence:** A Node probe using the production functions and controlled promises
reproduced both outcomes: a new activity push followed by an older empty read
ended at `[]`; an acknowledged incident followed by an older status read restored
`incident-1`. Agent and Project loads already have ordering protection, so the
problem is inconsistent application of an existing pattern.

**Recommendation:** Give each existing state owner one ordering rule across
reads, events and mutation responses, preferably using server revisions where
available. Invalidate outstanding snapshots when newer state arrives. Test both
response orders at the owner boundary; a request counter that only compares
concurrent reads does not solve the read-versus-push race.

### BP03 - Complete the Event Loop / blocking-work boundary

`core/extensions/extensions.py:241-250` invokes synchronous discovery and
collection from `aload`; `_loading.py:157-191` imports modules and directly calls
`register_fn(api)`. The ordinary reload RPC reaches this through
`server/rpc/extensions_methods.py:69`, `core/runtime/runtime.py:753`, and
`core/extensions/runtime.py:106`.

Separately, `server/_upload_routes.py:80-83` calls `attachment_store.get()` directly
inside an async download handler. `core/attachments/attachments.py:319-321,337-338`
reads a JSON sidecar and checks the blob synchronously.

**Evidence:** A valid synthetic Extension with a 150 ms synchronous registration
delayed a 10 ms loop timer to 158 ms. The attachment path is source-confirmed;
no real disk latency was measured. Shipped Extensions also use synchronous
registration, but their registration durations were not measured.

**Recommendation:** Offload discovery/import/synchronous registration through a
bounded owner-managed worker, while retaining async registration and publication
on the loop. Add an async read entry point at the existing Attachment owner.
Guard loop responsiveness at these public async boundaries. This follows
[Python's blocking-work guidance](https://docs.python.org/3.14/library/asyncio-dev.html#running-blocking-code).

### BP04 - Isolate Extension worker consumption by owner

`core/extensions/_callbacks.py:16-23,43` shares eight workers across synchronous
Extension callbacks. The timeout path at `:95-100` stops waiting, but cannot stop
the running threads. Ordinary Hooks have no hard deadline of their own.

**Evidence:** Eight blocked callbacks from one owner were detached after a
controlled 30 ms timeout. A callback from another owner that would return
immediately could not start until the original calls were released.

**Recommendation:** Add owner-aware admission and a failed/degraded state to the
existing callback executor. One owner must not occupy all shared capacity;
timed-out owners must not accumulate more work on reload while old calls remain.
Only process isolation can forcibly terminate arbitrary hung code, if that later
becomes a requirement. More await timeouts alone cannot reclaim these threads.
This is reliability isolation between trusted Extensions, not a proposed security
sandbox or a change to their trust model.

### BP05 - Validate the dependency combinations that actually ship

`.github/workflows/ci.yml:71-73` resolves `.[dev]` without the package runtime pins;
`scripts/push.py:330` uses the current development interpreter. Package builds
instead install the hashed runtime locks (`scripts/build_windows.py:137-169`,
`scripts/build_linux.py:153-173`). The lock test at
`tests/scripts/test_package_build.py:73-85` checks Python and selected entries,
not complete dependency compatibility. WSL caching at
`scripts/linux/push_tests.sh:53-59` is not keyed by those dependency locks.

**Evidence:** Local installed-package metadata differed from the Windows server
lock for 28 packages, including Starlette 1.0.0 versus 1.6.0, AnyIO 4.13.0 versus
4.15.1, and websockets 17.0.1 versus 17.1. These are local versions, not asserted
versions from a recent CI run. Existing package smokes cover substantial lifecycle
behavior, but do not run the full Chat/Provider/Tool suite. No broken package was
demonstrated.

**Recommendation:** Run behavioral tests against the relevant platform runtime
pins, with separately fixed test dependencies. Automatically check the resolved
runtime set against the package locks, and include locks in environment cache
keys. Preserve the package smokes. See [pip's repeatable-install guidance](https://pip.pypa.io/en/stable/topics/repeatable-installs/).

### BP06 - Keep unknown intermittent failures visible in the gate

`.github/workflows/ci.yml:101-113` and `scripts/push.py:208-222` retry ordinary
pytest failures once in isolation and accept the run when the retry passes.
This applies to every newly failing test, with no classification, explicit
exception list, owner, or expiry. The output attributes the failure to parallel
load without establishing its cause.

**Evidence:** This is direct workflow behavior, also documented in the development
map. `FLAGGED.md` already records three intermittent failures observed on the
review date. A passing retry does not distinguish timing pressure from shared
state, order dependence, or a product race; [pytest documents those causes](https://docs.pytest.org/en/stable/explanation/flaky.html).

**Recommendation:** Keep retries as diagnostics, but make a new unexplained flake
fail the gate. If temporary exceptions are needed, record the exact case, owner,
tracking item and expiry explicitly. This changes an intentional existing policy;
it is not a claim that the retry script implements that policy incorrectly.

### BP07 - Separate package construction from access to the signing key

`.github/workflows/windows-package.yml:75-93` and
`.github/workflows/linux-package.yml:69-71` expose `VBOT_RELEASE_SIGNING_KEY` to
the entire package-build command. `scripts/windows/native_hosts.py:103-114`
copies that environment into build subprocesses; Windows desktop builds execute
a source dependency at `scripts/build_windows.py:165-167`. Linux executes imports
from the assembled runtime before signing (`scripts/build_linux.py:179-194`).

**Evidence:** The environment and subprocess paths are source-confirmed. A
compromised build dependency could read the long-lived signing key as well as
modify that build. No compromise, malicious dependency or actual secret exposure
was observed. Hash-locked inputs reduce substitution risk, but do not restrict
what their code can access when executed.

**Recommendation:** Build and inspect without the key. Sign the completed artifact
in a separate fresh job with a minimal fixed signer environment, without importing
or executing the artifact's runtime. Merely moving the secret to a later step on
the same compromised runner is insufficient. Bind signing to the intended build
identity and artifact digest. This is distinct from the deferred first-install
authenticity decision. [GitHub documents runner access to environment secrets](https://docs.github.com/en/actions/concepts/security/compromised-runners).

### BP08 - Make ordinary Recall appends incremental

`core/recall/_passage_catalog.py:285-315` reloads the complete active Session with
`load_since(None)` whenever its revision changed, rebuilding text passages for
every Message. Lines 370-395 then compare the full passage set. Terminal Runs
nudge the indexer after a 20-second debounce (`semantic_indexer.py:330`), and a
pass enumerates live scopes (`:587-600`).

**Evidence:** An in-memory probe of the actual refresh planner counted 1,001
passage-builder calls for one append to 1,000 Messages, and 10,001 for one append
to 10,000. Repeated small appends therefore accumulate quadratic text-processing
work. Existing vectors are reused: this finding does **not** claim that unchanged
passages are re-embedded or billed again. It applies to Semantic Recall, not the
default lexical-only backend.

**Recommendation:** Use generation-bound cursors for append-only changes, and
project only new canonical entries. Retain full rebuilding for edits, lineage
materialization, restore and invalid cursors; process work in bounded batches.
Extend the existing correctness contract with a count of old entries processed,
instead of a fragile wall-clock threshold.

### BP09 - Scope Statistics reconciliation to the requested data

`core/statistics/index.py:589` calls `_stored_sessions` on every reconcile;
`:682-702` hydrates the complete `stat_sessions` table even when `prune=False`
and only one Session was requested. `core/statistics/statistics.py:397-400` also
loads every Extension Session summary for a Session-tree query. Reads hold the
shared index `RLock` (`index.py:402`). Full reports rediscover every scope at
`statistics.py:439-470`, independently of the requested time window.

**Evidence:** The actual reconcile over in-memory SQLite constructed 1,000 stored
Session objects for a one-Session query in a 1,000-Session index, and 10,000 in a
10,000-Session index. The requested Session was unchanged. No production latency
was inferred from those counts.

**Recommendation:** Fetch requested addresses and relevant generations directly
in SQL, including their specific Extension ownership. Separate global discovery
and pruning from small scoped reads and make it incremental. Keep the existing
SQLite projection and aggregate design; the issue is unnecessary global work,
not an established database-engine limit.

### BP10 - Bound Channel refresh fan-out and fetch details on demand

`webui/src/components/settings/channels/controller.svelte.js:322-353` loads the
Agent/Channel lists, then every Channel's status and full access state, including
collapsed rows. It publishes the refreshed state only after all replies.
`WhatsAppSetup.svelte:26` also polls every three seconds when connected, even when
its setup UI is hidden by `hideWhenConnected`. Polling correctly pauses when the
Settings area or document is hidden.

**Evidence:** The production loader with 50 collapsed Channels generated 102
RPCs: two list requests plus two per Channel. The work is exactly `2 + 2N`, before
WhatsApp polling. A slow detail request delays the whole refresh. This is a
confirmed scaling pattern, not a measured current outage.

**Recommendation:** Include compact row status in the existing Channel list
projection; fetch full access details when a row opens. Restrict setup polling
to active pairing/setup, and share regular status invalidation. Bound any
remaining detail concurrency and allow independent row updates.

### BP11 - Bound and normalize rejected-request diagnostics

`server/rpc/dispatcher.py:58-63` passes any nonempty method string to the logger
unchanged. Lines 45-50 emit a warning on every rejected request, without
aggregation. Normal request-body limits bound individual requests, but there is
no useful method-name or repeated-log bound at this point.

**Evidence:** Two controlled requests with unknown method names of 32,791
characters produced 65,688 log characters and four lines. Embedded newlines
survived, including the production formatting behavior. This allows admitted
clients to amplify or forge log lines and can retain unintended content supplied
in that field. It is not an authentication bypass.

**Recommendation:** Log only registered method names or a fixed invalid/unknown
category; never copy arbitrary unknown names. Aggregate repeated refusals by a
bounded key such as error code and method category using the established logging
pattern. Extend the existing dispatcher privacy tests to cover length, newlines
and repetition, without pinning prose.

## Existing decision to prioritize: global resource admission

The previous security review already leaves global admission open (F7, lines
226-231 and 272-274). This is relevant to growth, not a new discovery here.

The current Run manager limits waiting work for busy Sessions but has no matching
global cap for active Runs of different Sessions (`core/runs/runs.py:309-320,
371-380`). A synthetic probe with `waiting_work_limit=1` admitted 200 active Runs
while reporting zero waiting work. Upload limits likewise bound each file rather
than aggregate bytes waiting for workers. No safe production capacity number was
established in this review.

Choose supported simultaneous work and an explicit overload policy before adding
limits. Put admission at the existing Run/transport owners; preserve fairness and
avoid parent Runs holding every slot while waiting for Sub-Agents. Queueing,
refusal and cancellation need consistent observable semantics.

## Suggested delivery sequence

1. Fix BP01/BP02/BP11 and close BP03's blocking boundaries with focused contract
   tests. These are bounded changes to existing owners.
2. Align tested and shipped dependencies (BP05), decide the flake gate (BP06),
   and isolate signing (BP07). These protect subsequent rapid development.
3. Make Recall and Statistics incremental (BP08/BP09), reduce Channel fan-out
   (BP10), and isolate Extension worker consumption (BP04).
4. Set the already-open global capacity policy using representative workloads;
   validate its admission/fairness behavior deterministically and keep larger
   scale measurements in the existing performance/stress tooling.

For the growth changes, require both semantic equivalence and bounded work at
the owning public interface. Additional rules in documentation alone would leave
the demonstrated gaps in place; a small number of enforceable contracts is the
more useful investment.
