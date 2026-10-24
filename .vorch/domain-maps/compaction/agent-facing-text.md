# Compaction Agent-facing Text

Why each sentence of the Agent-facing text the Compaction domain owns exists: the Compaction instructions in `resources/prompts/compaction.md`, `compaction-manual.md`, `compaction-continuation.md` and `compaction-continuation-manual.md`, and the checkpoint texts `COMPACTION_REFERENCE_PREFIX`, `COMPACTION_USER_QUOTE_PREFIX` and `SKILL_COMPACTION_GUIDANCE` in `core/compaction/compaction.py`. No reasons are recorded yet for the existing sentences; add a row whenever you change or add one. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text.

| Text | Reason |
|---|---|
| `[... {omitted} characters omitted from this Tool result for Compaction ...]` | Marks clipped Tool observations in a rescue-only request, preserving head and tail without presenting their concatenation as complete output. |
| `Some older conversation exchanges were omitted because the Provider rejected the Compaction request's size.` | Tells the Summary Model that the rescue view no longer contains all exchanges it would otherwise assume it summarized. |
| `Summarize only the remaining evidence; do not infer omitted details.` | Prevents filling missing older exchanges with invented facts after whole-exchange omission. |
| `Some older conversation exchanges were omitted from this summary's input after the Provider rejected its size.` | Deterministically records incomplete source coverage in a successful checkpoint; the Summary Model cannot silently omit this limitation. |
| `The original Session history is unchanged.` | Distinguishes summary-input omission from deletion of the canonical conversation, which remains available for later recall. |
