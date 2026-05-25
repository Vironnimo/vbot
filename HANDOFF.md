# Handoff

## Subagent Improvement Candidates

Current status: first-pass `SubAgentCoordinator` is implemented in `core/subagents/`; the remaining candidates below are the next subagent improvement options.

### 1. UI Visibility

Show child run state directly in the parent timeline instead of only offering a session link. Useful details include queued/running/done/failed status, latest answer preview, and important child tool-call activity.

Why it matters:
- Subagents are otherwise a black box from the parent conversation.
- Better visibility makes delegation easier to trust and debug.

Tradeoff:
- The timeline can become noisy when many subagents run.
- The UI needs careful summarization so nested activity stays readable.

### 2. SubAgentCoordinator

Status: first pass implemented. `core/tools/subagent.py` is now a registration/schema wrapper, while `core/subagents/` owns coordination and batch tracking.

Move sub-agent orchestration out of `core/tools/subagent.py` into a typed service. The tool wrapper should stay limited to schema, display metadata, and registration.

Why it matters:
- The tool module currently owns validation, queueing, cancellation, batch tracking, result lookup, and ChatLoop startup details.
- A coordinator gives subagents a real domain boundary and makes future UI, persistence, and testing work easier.

Tradeoff:
- This is mostly structural at first, so it has little immediate user-visible payoff.
- It touches a sensitive path near Runs, queues, ChatLoop, and compaction behavior, so the refactor must stay narrow.

### 3. Durable Parent-Child Tracking

Persist or reconstruct parent-child links: parent run, child agent, child session, child run, queued state, and result state.

Why it matters:
- The current `SubAgentBatchTracker` is process-local; restart loses batch state.
- Durable links make subagent history, UI, and debugging more reliable.

Tradeoff:
- Adds storage state and cleanup rules.
- Needs careful schema/design work before implementation.

### 4. Blocking Mode Policy

Decide whether `blocking: true` should remain a normal tool option, get tighter timeout guidance, or be discouraged in favor of non-blocking plus `subagent_result`.

Why it matters:
- Blocking can tie up the parent run for a long time.
- Non-blocking delegation is usually a better fit for parallel work.

Tradeoff:
- Some workflows genuinely want an immediate child result.
- Over-restricting blocking mode can make simple delegation less convenient.

### 5. Prompt And Tool Contract

Clarify agent guidance for when to use blocking, what `queued` means, when to call `subagent_result`, and how many subagents are reasonable.

Why it matters:
- Tool behavior is only useful if models use it correctly.
- A clearer contract can improve behavior without large code changes.

Tradeoff:
- Prompt changes can have broad behavioral side effects.
- Too much guidance can reduce useful flexibility.

### 6. Observability And Status

Expose active and queued subagents through a debug/status surface or API.

Why it matters:
- Developers need to see what subagent work is alive, queued, done, or stuck.
- This helps diagnose long-running or cancelled delegation chains.

Tradeoff:
- Adds another API/UI surface to maintain.
- Less urgent if parent timeline visibility becomes good enough.
