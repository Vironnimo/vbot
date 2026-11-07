# Runtime Extension Host

The Runtime side of the Extension host: `ExtensionHostFactory` and its temporary execution groups, editor catalogs, `inspect_prompt`, Tool/Extension prompt-block contributions, Extension sampling and `resolve_tool_agent`. The Extension kernel and its API live in `extensions.md`.

## Host factory and execution groups

- The host's sampling, credential, attachment and Agent/cwd callables check their exact registration before admitting new work, including when a coroutine is first awaited. Cached hosts therefore fail after disable, registry retirement or Runtime shutdown. Already-admitted calls can finish; Extensions still own shutdown of their arbitrary Python tasks and threads.
- Owner-bound Extension hosts receive temporary execution groups, safe editor catalogs and existing Statistics projections (over the Runtime's shared `statistics_index`) through injection. Whole-roster preflight uses ordinary Model, Tool/settings and Project-ceiling validators off the Event Loop, then rechecks the loaded registration before admission. `_extension_host.py::ExtensionHostFactory` owns these groups and projections with explicit services and live registry/Skill callbacks; it never receives the Runtime object. Source/tests: `_extension_host.py`, `tests/core/runtime/test_runtime_extension_host.py`.
- Extension-owned temporary execution groups receive `chat_loop` through the existing host operations seam, so their Runs expose the same in-flight output and request diagnostics as interactive Chat (`test_runtime_extension_host.py`).
- Editor catalogs project Model reasoning capabilities and context size, plus Tool family/activation metadata, from their existing registry owners so isolated editors can use the shared selectors without inventing capability defaults (`test_runtime_extension_host.py`).
- Editor catalogs also expose System Prompt block metadata. Owner-bound `inspect_prompt` resolves a prospective temporary Agent like an ordinary one, applies Chat's Model-specific Tool routing and the owner's private Tool grants, then returns the combined prompt, rendered block details, effective Tool definitions and the resolved Agent's Model/effort. Inspection creates no Session or Run and rechecks registration identity after asynchronous work (`test_runtime_extension_host.py`).
- Owner-bound transient notifications, page badges and owned pending-approval reads use the injected registry, publisher and existing Session/Run owners; no transport or durable inbox enters Runtime. Public contracts: `extensions/host.md` -> Transient attention and Current authority and pending approvals; coverage: `test_runtime_extension_host_attention.py`.

- Deferred Tool calls upgrade the canonical invocation receipt through `host.defer_tool_call`; final delivery uses the injected `TriggerService`, so whole Tool batches precede completions, idle Sessions wake normally, and user-cancelled origins do not start another Run. Restart recovery only persists an outcome Note for the same live generation. Sessions owns conflict and acknowledgement checks (`extensions/host.md`).

## Prompt blocks and sampling

Native Decision calls are a separate host capability: `_extension_host.py` delegates one item to the injected `DecisionService`, records the declaring Extension's Usage and rechecks registration after the await (`extensions/host.md` -> Native Decisions). It never routes through Chat sampling.

`_shell_review_context.py` reads bounded canonical conversation projections on the Sessions pool for `host.review_context`, retaining human/peer/Parent provenance and known ancestor generations (`extensions/shell-review.md` -> Execution integration). It never promotes Assistant text or a delegated instruction to human consent.

The same helper module supplies `host.input_authority` using the latest canonical input of only the current Run, not the ancestor-rich review projection or the human-filtered latest task (`sessions/history-api.md` -> Bounded conversation projection). A missing live Run is passed as `None`; the owner host rechecks its registration after the await (`extensions/host.md` -> Current authority and pending approvals).

The storage scope adapter and Tool/Extension Prompt-block contributions live together in `_prompt_blocks.py`; bootstrap and reload use the same projection.

Extension sampling (`_extension_sampling.py`) resolves the Agent, Model route and Adapter off the Event Loop. It retains the Adapter and UsageRecorder across awaits, so cancellation and readiness withdrawal still finish Usage accounting and close the acquired Adapter; it records its own exposed Adapter request, and it retains its injected recorder through readiness withdrawal during shutdown.

## Resolving the calling Agent

Receipt restoration revalidates its canonical invocation after resolving Agent,
working Project and Permission Boundary. A move during those awaits repeats
resolution at the original generation's new address; deletion or replacement
returns no context, so an old call cannot inherit the replacement's permissions.

Extension Tool consumers resolve their calling Agent through `ExtensionHost.resolve_tool_agent(context)`. Runtime checks the canonical temporary Session binding against the execution owner before temporary resolution, otherwise uses normal Identity/Project resolution. A context whose Session does not exist is a call outside any Session (Extension management such as MCP `explore`/`invoke`): without an execution owner it resolves the addressed Agent with no Session overrides; with one it fails (`Temporary Tool invocation no longer owns this Session`). MCP policy and sampling share this seam (`tests/core/runtime/test_runtime_mcp_access.py`).
