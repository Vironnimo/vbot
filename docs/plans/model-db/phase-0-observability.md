# Phase 0 — Observability quick wins

> Part of [Model DB plan](README.md). Read the README §0 (operating rules) and `stuff/HANDOFF-model-db.md`
> first. **DB-independent** — can run before or alongside any other phase.

**Goal:** Stop silently discarding two reliable reasoning feedback signals from providers.

**Read:** `.vorch/domain-maps/providers.md`, `.vorch/domain-maps/providers/openai.md`,
`.vorch/domain-maps/chat.md` (Token Usage section).

**Settled — don't redesign:** This is a standalone, small step (handoff → "Observability"). It does
**not** depend on the Model DB and must not introduce any DB concept. Two signals only:

1. **Strict providers return HTTP 400 on an invalid reasoning effort.** Today it's passed through as
   a generic provider error. Evaluate it: when a 400 body indicates the rejected control was a
   reasoning/effort field, log a structured `warn` with the offending effort and model so the snapping
   gap is visible. Do not swallow the error — classification/retry behavior is unchanged
   (`core/utils/http_status.py` stays the policy owner; 400 stays fatal/non-retryable).
2. **Thinking-token counter in `usage`.** When a request was sent with a non-`none` effort but the
   response usage reports **0** reasoning tokens, the effort was effectively swallowed → log a `warn`
   with model + selected effort. Usage normalization is canonical across adapters (providers.md →
   Conventions); add the check where usage is normalized, not per-adapter ad hoc.

## Tasks

- **Locate the spots first.** Find (a) the provider send/error path that turns a 400 into a vBot error
  (start at `core/providers/openai_compatible.py` send + `core/providers/_http_shared.py`
  `classify_http_status`), and (b) the shared usage-normalization point that maps provider `usage`
  into vBot tokens (grep `cache_read_tokens`/`input_tokens` normalization; see providers.md). — files:
  `core/providers/openai_compatible.py`, `core/providers/_http_shared.py` (read), plus wherever usage
  normalization actually lives once located.
- **Surface the 400-on-bad-effort signal** as a structured `warn` (logger `vbot.providers.*`), without
  changing retry/fatal classification. — files: the adapter error path located above; tests in
  `tests/core/providers/test_openai_compatible.py` (or the owning adapter's test).
- **Log the thinking-token mismatch** (non-`none` effort sent, 0 reasoning tokens back). — files: the
  usage-normalization site; tests in the owning adapter/usage test module.

**Done when:**
- A unit test proves a 400 whose body names an invalid reasoning effort produces a structured warning
  (asserted via caplog) and still raises the same fatal provider error as before.
- A unit test proves that effort-sent-but-zero-reasoning-tokens emits a warning, and that a matching
  (non-zero, or effort `none`) case does not.
- `python scripts/quality.py <touched paths>` is green.

**Risks / notes:** Don't log secrets (token values, keys). Don't change which statuses are retryable.
If the 400 body shape varies per provider, key the detection off a substring match on the response
detail and keep it conservative (a false negative — no warning — is fine; a wrong reclassification is
not).
