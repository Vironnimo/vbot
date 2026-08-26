---
name: free-models
description: Conserve budget by delegating suitable Sub-Agent work to free-of-charge Models (OpenRouter free variants, OpenCode Zen free tier) and by helping the user set up a free-capable Provider when none is configured. Use when the user asks to save costs or mentions free models, a low budget, or spending nothing, when many small independent subtasks can run on weaker Models, or before fanning out bulk Sub-Agent work. Do not use for critical, security-sensitive, or irreversible work that needs top-tier quality, and never as a license to downgrade the main conversation's own Model.
---

# Free Models

Free capacity serves **delegated** work. You route suitable Sub-Agent Runs to free
Models through the `subagent` Tool's per-Run `model` override (`<provider>/<model-id>`,
applies only to that Run). You never change the calling Agent's own Model, any stored
configuration, or a Project default through this skill, and never do so silently.

## Non-negotiable rules

1. **Tools gate.** A Sub-Agent without tool calling is broken, not cheap. Propose only
   Models whose catalog record confirms tool support (`vbot model show <id>`), and only
   ids that appear in `vbot model list` — a configured, usable Connection.
2. **No silent paid fallback.** When a free Model fails or returns unusable output,
   rotate to another free Model, wait out rate limits, or report the gap. Moving the
   work to a paid Model spends money and requires the user's explicit consent first.
3. **Quality boundary.** Ask before routing security-sensitive, architectural,
   irreversible, or otherwise high-stakes work to a weaker Model. Saving cents is never
   worth silently degrading an outcome the user cares about.
4. **Privacy boundary.** Free tiers are often paid in data — several free endpoints log
   inputs or train on them. Never put credentials, secrets, or tokens into delegated
   task text. Flag sensitive content to the user and prefer zero-retention, paid, or
   local alternatives for it.
5. **Rate limits are real.** OpenRouter free variants allow 20 requests per minute,
   50 requests per day (raised to 1000 once the account has purchased at least $10 in
   credits, ever); expect HTTP 429 as routine. Spread siblings across different free
   Models instead of stacking parallel Runs on one, and treat exhausted quotas as a
   reason to defer work, not to fall back to paid.
6. **Verify before trusting.** The curated list in `references/free-models.md` is a
   dated hint. Free lineups churn weekly — confirm current free status and availability
   live before relying on any entry (recipe in that file).

## Workflow

1. **Inventory.** Run `vbot provider list` and `vbot model list --task chat` to see
   which Providers are usable. If no free-capable Provider is configured, stop here and
   walk the user through `references/provider-setup.md`; after setup, continue only if
   the user wants that.
2. **Pick candidates.** Read `references/free-models.md` for the curated snapshot and
   the live-verification recipe. Keep only entries that pass both checks right now.
3. **Match task to Model.** Delegate bulk summarization, triage and classification,
   structured extraction, formatting and boilerplate edits, first-pass review notes, and
   documentation drafting to free Models. Keep architecture decisions, security-relevant
   changes, final integration and verification of other output, and anything
   irreversible on the main Model.
4. **Delegate deliberately.** Write self-contained task text — goal, context, scope,
   constraints, expected result. Weaker Models compensate with precision, not intuition;
   spell out what a strong Model would infer. Respect the target Model's context window
   when the task carries large inputs.
5. **Handle failure.** On a failed or empty child Run, classify before retrying — rate
   limit (wait or rotate), capability miss (wrong Model for the job), or quality
   garbage (tighten the task or escalate). Never hammer a rate-limited Model in a
   retry loop.
6. **Report.** State which Runs ran free and on which Models, which stayed on the main
   Model and why, what failed or awaits consent, and the verified result.

## References

Read `references/free-models.md` for the dated curated snapshot, current rate limits,
and how to verify the live free lineup. Read `references/provider-setup.md` before
walking the user through creating an OpenRouter or OpenCode Zen account and connecting
it to vBot.
