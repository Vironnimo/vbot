# Phase 4 — Snapping against the effective ladder + the small UI

> Part of [Model DB plan](README.md). Read README §0.3 (cost rules — **this phase can trigger
> inference**) and `stuff/HANDOFF-model-db.md` (sections "Reasoning — Steuerung, Quelle, Snapping,
> Replay", "UI"). Depends on Phase 3 (needs real per-model ladders in the data). **Not parallel with
> Phase 5** (shares adapter files). **Parallel-safe with Phase 6.**

**Goal:** Reasoning-effort snapping stops using hardcoded per-adapter constants and snaps against the
**effective (merged) per-model ladder from the DB**. The adapter constant survives only as a floor for
models without a feed ladder. The `/status` view distinguishes the selected effort from what actually
goes on the wire, and the effort control is disabled for non-reasoning models.

**Read:** `.vorch/domain-maps/providers.md`, the provider child maps you touch
(`providers/openai.md`, `providers/openrouter.md`, `providers/mistral.md`,
`providers/github-copilot.md`), `.vorch/domain-maps/models.md`, `.vorch/domain-maps/webui.md`,
`.vorch/DESIGN.md` (for the UI bits).

**Settled — don't redesign (from §1):**
- Phase-1 reasoning scope = **effort/levels only**. `control: budget` and `on_off` are **not** wired on
  the wire here — a budget-only model has empty `levels` and the adapter has nothing to snap; leave it.
- There is **one** effective ladder per model (provider layer wins where it speaks, else canonical).
  Snap against that. Snapping itself already exists (`closest_supported_effort` + `THINKING_EFFORT_ORDER`,
  `core/providers/reasoning.py`) — the change is *what it snaps against*.

## The hardcoded constants this phase retires (snapping → effective ladder, constant → floor)

| File | Constant / method | Today |
|---|---|---|
| `core/providers/openai_compatible.py` | `OPENAI_REASONING_EFFORTS` (L47), `_supported_reasoning_efforts()` (L249, **provider-global**), snap at L236/L680 | hardcoded set |
| `core/providers/openrouter.py` | `OPENROUTER_REASONING_EFFORTS` (L19), snap at L124 | hardcoded set |
| `core/providers/mistral.py` | `MISTRAL_REASONING_EFFORTS` (L28), snap at L127 | hardcoded set |
| `core/providers/openai.py` | `OPENAI_SUBSCRIPTION_REASONING_EFFORTS` (L43), snap at L426 | hardcoded set |
| `core/providers/github_copilot_policy.py` | `allowed_reasoning_efforts`, snap at L268 | already partly catalog-driven |

The key structural change: `_supported_reasoning_efforts()` (and equivalents) is **provider-global**
today — it must become **model-specific**, reading the effective ladder via the already-injected
`model_lookup` (see `model_reasoning_supported(model_lookup, model_id)` in `reasoning.py`, same
pattern). The adapter constant becomes the fallback only when the looked-up model has no ladder.

## Tasks

- **Effective-ladder lookup helper** — add a shared helper in `core/providers/reasoning.py` (alongside
  `model_reasoning_supported`) that returns a model's effective `levels` from the catalog via
  `model_lookup`, or `None` when absent. — files: `core/providers/reasoning.py`; tests:
  `tests/core/providers/test_reasoning.py`.
- **Rewire each adapter's snapping** to be model-specific: use the effective ladder when present, the
  adapter constant as floor otherwise. Do this for openai_compatible, openrouter, mistral, openai
  (subscription), and confirm github_copilot_policy already conforms (fold it to the shared helper if
  it duplicates logic). — files: `core/providers/openai_compatible.py`, `core/providers/openrouter.py`,
  `core/providers/mistral.py`, `core/providers/openai.py`, `core/providers/github_copilot_policy.py`;
  tests: each provider's `tests/core/providers/test_<provider>.py`.
- **`/status`: split selected vs actual effort** — show **Selected thinking effort** and **Actual
  model thinking effort** (= `closest_supported_effort(selected, effective_ladder)`). Resolution must
  be callable **outside** the adapter (the function is already pure). — files: `core/tools/status.py`
  (and `core/chat/commands.py` if the command output is assembled there); tests:
  `tests/core/tools/test_status.py`. All new strings through i18n.
- **Effort control gating in the WebUI** — disable the effort dropdown for non-reasoning models
  (`reasoning.supported == false`) and show only the model's possible options. — files:
  `webui/src/components/chat/ChatHeader.svelte`, `webui/src/lib/i18n.js` (strings); tests:
  `webui/src/components/__tests__/ChatView.test.js` or a focused component test. Run
  `python scripts/quality-frontend.py <paths>`.

**Done when:**
- A test proves snapping uses the **per-model** effective ladder (e.g. OpenRouter `deepseek-v4-pro`
  snaps within `[high, xhigh]`, opencode-go within `[high, max]`) and falls back to the adapter
  constant only when a model has no ladder.
- `/status` reports both selected and actual effort, with actual = the snapped value.
- The effort dropdown is disabled for a non-reasoning model in a component test.
- **One** cheap live confirmation per allowlisted provider (README §0.3): send a tiny prompt with a
  selected effort to `openrouter/deepseek/deepseek-v4-flash`, `opencode-go/deepseek-v4-flash`, and
  `openai/gpt-5.4-mini::subscription`, and confirm no 400-on-bad-effort (cross-checks Phase 0). Tiny
  prompts only; do not sweep models or efforts.
- `python scripts/quality.py <backend paths>` and `python scripts/quality-frontend.py <webui paths>`
  are green.

**Risks / notes:**
- Strict providers throw 400 on an out-of-ladder effort — the whole point of model-specific snapping is
  to prevent that. The live confirmation above is the acceptance check; Phase 0's logging makes a
  regression visible.
- Don't widen scope to budget/on_off wiring — out of scope (README §2).
- `mistral.py` and `openai_compatible.py` are also touched by Phase 5 → these phases must be
  **sequential**, not concurrent. Coordinate ordering with the orchestrator.
