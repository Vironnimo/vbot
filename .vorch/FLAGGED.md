# Flagged Concerns

Append-only log of deferred concerns. Newest at the bottom. Don't reorganize.

---

## 2026-06-07 — Compaction: deferred robustness/edge cases

Found during a review of `core/compaction/`. The main bug (every compaction
re-summarized the full raw history instead of only the delta since the last
checkpoint) was fixed the same day. The items below were deliberately **not**
fixed and are recorded here.

### 1. Small context windows (< ~32k) are not actively supported

**Decision:** Assume a context window of at least 32k tokens. We don't want to
lock smaller models out, but we don't actively support them either.

**Why it can break below ~32k:** Auto-compaction triggers when
`input_tokens / context_window >= threshold` (default `threshold = 0.8`). After
compacting, the next request is roughly `summary + preserved_tail`, where the
preserved tail targets `tail_tokens` (default `15_000`). If `tail_tokens` is
larger than `threshold * context_window`, the preserved tail *alone* already
sits above the trigger threshold — so the very next turn triggers compaction
again, but there is nothing left to remove. Worked example: a 16k model has a
trigger point at `0.8 * 16000 = 12800` tokens, but the tail target is `15000`,
so compaction can never bring usage back under the threshold → it re-fires every
turn (each firing is an LLM call) without ever helping.

At 32k this does not happen: trigger point `25600`, tail `15000` → after
compaction we sit around `15000 + summary`, comfortably under the threshold.

**Residual edges that exist even at 32k+ (low likelihood, left unguarded):**

- **`tail_tokens` is a floor, not a cap.** `find_tail_boundary` always preserves
  *at least* the whole most-recent turn before checking the budget
  (`core/compaction/compaction.py`, the `boundary_index = start_index` line runs
  before the `>=` check). So a single turn with very large tool output can push
  the preserved tail far past `tail_tokens`. There is no clamp of `tail_tokens`
  against the context window anywhere.
- **Empty-delta compaction does a redundant LLM call.** If compaction is invoked
  but nothing has been added since the last checkpoint's boundary
  (`pre_tail_messages` is empty), the strategy still calls the summary model with
  `"(no history before boundary)"` and re-emits essentially the previous summary.
  Harmless, but a wasted call. Not expected at 32k (the trigger math above means
  there is always a real delta to fold in), so left unguarded.

**If we ever want to support small windows:** clamp the effective tail to
something like `min(tail_tokens, ~0.5 * context_window)`, and skip compaction
when the projected result would still be above the threshold or when the delta
is empty.

### 2. Final-response auto-compaction rebuilds request messages then throws them away

`core/chat/chat.py`: when a turn ends with a final assistant message and no tool
calls, the loop calls `_maybe_auto_compact(...)` and assigns the result to
`messages` — but the very next line is `return assistant_message`, so that
rebuilt `messages` list is never used.

**Why it's wasteful:** the turn is already over, so the only thing that needs to
happen at this point is persisting the checkpoint (`session.append(checkpoint)`),
which sets up the *next* turn. But `_maybe_auto_compact` also runs the full
`_build_request_messages(...)` rebuild — re-assembling the system prompt and
re-running attachment resolution — purely to produce a list that the caller
immediately discards. It's CPU work with no effect.

Contrast with the **mid-tool** call site, where the rebuilt `messages` *is*
needed: there the loop continues and sends another provider request, so it must
continue with the compacted (smaller) message list. There the rebuild is
correct and necessary.

**Possible cleanup:** split the two cases so the final-response path only
persists the checkpoint and skips the request rebuild.

### 3. Token source differs between the two auto-compaction trigger points

The "are we over the threshold?" check uses a different token number depending on
where it runs:

- **After a final assistant response:** it uses the provider's *real* reported
  `usage.input_tokens` — accurate.
- **After a mid-turn tool-result cycle:** the provider hasn't reported usage for
  the next request yet, so it falls back to the local heuristic
  `estimate_messages_tokens(messages)` (`core/utils/tokens.py`, ~4 chars/token).

**Why it matters:** the heuristic ignores tool/function schemas and other
provider-side overhead, so it tends to *under*-count. The practical effect is
that mid-turn compaction can trigger a bit later than the real context pressure
would warrant — i.e., the threshold behaves slightly differently mid-turn vs.
end-of-turn. Not wrong, just inconsistent. Acceptable as long as the heuristic
stays conservative-ish; worth revisiting only if mid-turn overflows show up.

### 4. The summary is injected as a second consecutive `user` message

When a checkpoint exists, `_build_request_messages` (`core/chat/chat.py`) emits
the summary as a synthetic `role: "user"` message wrapped in `<system-reminder>`
tags, placed immediately before the preserved tail. The tail itself always
starts on a `user` message (boundary invariant). So the provider request
contains **two `user` messages in a row** (summary, then the boundary turn).

**Why to keep an eye on it:** some provider wire protocols (notably Anthropic's
Messages API) historically expected strictly alternating user/assistant roles.
Most adapters tolerate or merge consecutive same-role messages, and this codebase
already injects notes/system-reminders as synthetic user messages elsewhere, so
it's *probably* fine — but it has not been explicitly verified for the summary
injection path against every adapter. If an adapter ever rejects consecutive
user turns, this is where it would surface. Worth a one-time check against the
Anthropic adapter rather than a fix.
