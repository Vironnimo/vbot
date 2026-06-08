## Plan: Hybrid Recall Backend (literal + semantic fusion)

**Goal:** A new opt-in `hybrid` recall backend that fuses SQLite-FTS literal matches with vector semantic matches in one `search`, so a single keyword surfaces *every* literal occurrence (within the limit budget) while conceptual queries still get semantically related sessions.

**Context:** The active `vector` backend is excellent for conceptual queries but structurally weak for lone keywords — empirically `bild` returned 1 hit at distance 0.50 while 21 sessions contain the word, because a single token gives the embedding too little to anchor on and the `_MAX_DISTANCE = 0.7` floor drops all but the densest chunk. This is the known literal-keyword gap of dense embeddings, not a bug. The fix is the industry-standard hybrid: run both engines unchanged and fuse their results. Approach and merge strategy were reviewed and agreed with the user; this plan supersedes the working note in `handoff.md`.

**Merge strategy — decided: literal-first, semantic-augment** (RRF rejected). Literal hits come first because the user's mental model is "type `bild`, find all bild sessions" — an exact keyword match must never be buried under a fuzzy semantic hit. The semantic arm is pure augmentation that fills remaining budget with conceptually-related sessions. RRF was rejected: a strong exact match can rank below a session that is middling in both arms, which is exactly wrong for the keyword case.

**Key design decisions (settle before coding):**

- **Source classification keys on the `distance` field, not on which arm produced a match.** A match carries `distance` iff it came from a *real* semantic KNN hit; FTS matches never carry it, and the vector arm's own JSONL fallback (no embedding binding / embed failure) returns matches *without* `distance`. So: `distance` present → semantic; absent → literal. This makes graceful degradation fall out for free — no binding ⇒ the vector arm returns distance-less fallback matches ⇒ they classify as literal ⇒ hybrid output is effectively literal-only, no special-casing, no crash.
- **Dedup unit = session.** FTS is per-message (can repeat a session); collapse to the session's first match in FTS order. Vector is already per-session. Per session: literal payload = first FTS match (its snippet contains the exact term the user typed); `distance` = the vector match's distance if that session also hit semantically.
  - session in FTS **and** vector(with distance) → `source: "both"`, keep **literal** payload, attach `distance` for display.
  - session in FTS only (or only distance-less vector fallback) → `source: "literal"`.
  - session in vector(with distance) only → `source: "semantic"`, use the vector match payload (anchor-message snippet) as-is.
- **Over-fetch both arms (in v1, not deferred).** FTS returns per-message, so calling it with the user `limit` can yield matches from only a few sessions and starve the literal group — defeating the headline goal. Both arms are called with an inflated limit via `dataclasses.replace(request, limit=request.limit * _FETCH_MULTIPLIER + _FETCH_MARGIN)`; hybrid dedups to distinct sessions and truncates to the user `limit` at merge time (mirrors the vector backend's existing internal `_CHUNK_FETCH_MULTIPLIER + _KNN_FETCH_MARGIN`).
- **Ordering respects `sort` only within the literal group.** FTS already orders candidates by `request.sort` (newest/oldest). The literal+both group keeps that order; the semantic-only group is **always** distance-ascending regardless of `sort` (recency ordering would scramble the only meaningful relevance signal semantic hits have). Merged list = literal/both group (per `sort`) then semantic group (per distance), truncated to `limit`.
- **Semantic arm stays pure.** Keep `_MAX_DISTANCE = 0.7` on the vector arm unchanged — literal now covers exact terms, so the floor isn't loosened. (Tightening it is a post-merge tuning question, see Out of scope.)
- **`<3`-char queries work unchanged.** Trigram needs ≥3 chars; shorter terms make the FTS arm fall back to its own JSONL substring scan (still correct literal matching), and the vector arm still embeds the query. No hybrid-specific handling needed — but pinned by a test.
- **Default is NOT changed.** `DEFAULT_RECALL_BACKEND` stays `jsonl_scan` (user decision). `hybrid` is opt-in via Settings → Recall (live reload) or `~/.vbot/settings.json`.

**Scope:**
- In: new `HybridRecallBackend` + `render_hybrid_matches`; registry/constant/export wiring; settings validation & panel coverage (automatic via the frozenset); tests; `recall.md` spec section.
- Out: changing the default backend; modifying `SqliteFtsRecallBackend` or `VectorRecallBackend` internals; tuning `_MAX_DISTANCE`; surfacing new tool-schema fields in `session_search.py` (payload shape is unchanged — semantic entries just additionally carry `distance`/`chunk_index`, as they already do under the `vector` backend).

**Assumptions & Constraints:**
- Result payload shape stays the existing search shape: `{ content, matches, truncated, searched_sessions, total_candidate_sessions, request }`. Each match is a `message_match_payload`; semantic ones additionally carry `distance`/`chunk_index`; hybrid adds a `source` key per match.
- Both arms construct from the same `RecallBackendContext` (`SqliteFtsRecallBackend(context)`, `VectorRecallBackend(context)`); both inherit `browse`/`scroll` from `JsonlSessionRecallBackend`. Hybrid overrides only `search`; `browse`/`scroll` inherit the JSONL behavior (nothing to fuse).
- Adding the constant to `FIRST_PARTY_RECALL_BACKENDS` auto-covers settings validation (`core/settings/settings.py:183`) and the Recall panel list (`server/rpc/settings_methods.py:197`) — both derive from the frozenset. No edits to those files.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Backend + renderer | `HybridRecallBackend.search` fuses both arms; `test_hybrid.py` green |
| M2 | Wired & selectable | `hybrid` registered, exported, validates, appears in panel; `test_recall.py` pin updated |
| M3 | Documented | `recall.md` Hybrid Backend section |

### Phase Breakdown

#### Phase 1: Hybrid backend + renderer (with tests)
**Goal of this phase:** A working `HybridRecallBackend` that composes the two arms and implements the fused `search`, plus its own renderer, fully tested in isolation (constructed directly, not via registry).
**Can run in parallel with:** none (foundation for the rest).

- Create `HybridRecallBackend(JsonlSessionRecallBackend)`: in `__init__(context)` build `self._fts = SqliteFtsRecallBackend(context)` and `self._vector = VectorRecallBackend(context)`; inherit `browse`/`scroll`. — read: [.vorch/specs/recall.md], files: [core/recall/hybrid.py]
- Implement fused `search`: over-fetch both arms via `dataclasses.replace(request, limit=…)`; group matches by session; classify `source` by presence of `distance` (literal/semantic/both per the decisions above); keep the literal payload for literal/both and attach `distance`; order literal-group by `sort` then semantic-group by ascending distance; truncate to `request.limit` and set `truncated`; populate `searched_sessions`/`total_candidate_sessions` from the arms (both derive from the same candidate set). — files: [core/recall/hybrid.py]
- Implement `render_hybrid_matches(request, matches, *, truncated)`: tag each line `[literal]` / `[semantic]` / `[both]` and show `distance` (4 dp) for entries that carry one; reuse the existing match-line formatting style from `render_message_matches` / `render_vector_matches`. — files: [core/recall/hybrid.py]
- Tests (same phase, constructed directly with a tmp data dir + fake embedding service following the existing vector/fts test fixtures): — files: [tests/core/recall/test_hybrid.py]
  - keyword whose semantic distance > 0.7 but appears literally → surfaces via the literal arm (the `bild` regression; the headline test);
  - purely conceptual query, no literal overlap → semantic-only matches, each tagged `semantic` with a `distance`;
  - session hit by both arms → appears **once**, tagged `both`, carries the literal snippet **and** a `distance`;
  - no embedding binding (vector arm falls back) → hybrid returns literal-only (all `source: "literal"`), no crash;
  - ordering: literal/both sessions precede semantic-only sessions; literal group honors `sort`, semantic group is distance-ascending;
  - over-fetch: when one session has many literal message-hits, distinct *other* literal sessions still fill the budget (not starved by one session's repeats);
  - 2-char query → still merges (FTS arm via its JSONL fallback + semantic arm), no empty result.

**Dependencies:** none.
**Done when:** `python scripts/quality.py core/recall/hybrid.py tests/core/recall/test_hybrid.py` is green and all listed cases pass.

#### Phase 2: Registry / constant / export wiring + pin update ⚡
**Goal of this phase:** `hybrid` is a first-class first-party backend: registered, exported, validated, and shown in the Recall panel.
**Can run in parallel with:** Phase 3 (no file overlap).

- Add `RECALL_BACKEND_HYBRID = "hybrid"`; add it to `FIRST_PARTY_RECALL_BACKENDS`; register it in `RecallBackendRegistry.with_builtins()` (`registry.register(RECALL_BACKEND_HYBRID, HybridRecallBackend)`, local import like the others). — files: [core/recall/recall.py]
- Export `RECALL_BACKEND_HYBRID` and `HybridRecallBackend` and add both to `__all__`. — files: [core/recall/__init__.py]
- Update `tests/core/recall/test_recall.py`: extend the `FIRST_PARTY_RECALL_BACKENDS` frozenset-equality assertion (≈ lines 37–40) to include `RECALL_BACKEND_HYBRID`; rename `test_registry_with_builtins_registers_all_three_backends` (it now covers four) — its body already compares against `FIRST_PARTY_RECALL_BACKENDS`, so only the name needs fixing. — files: [tests/core/recall/test_recall.py]

**Dependencies:** Phase 1 (`HybridRecallBackend` must exist to import/register).
**Done when:** `python scripts/quality.py core/recall/ tests/core/recall/` is green; registry `names()` includes `hybrid`; `settings.update({recall:{backend:"hybrid"}})` validates (no edits to `settings.py`/`settings_methods.py` needed — confirm both still derive the list from the frozenset).

#### Phase 3: Spec — Hybrid Backend section ⚡
**Goal of this phase:** `recall.md` documents the new backend factually.
**Can run in parallel with:** Phase 2 (different files).

- Read `.vorch/workflows/spec-workflow.md` first. Add a "Hybrid Backend" section to `recall.md`: composes FTS + vector arms; fusion order (literal-first per `sort`, semantic-augment by ascending distance); session dedup unit; `source` classification keyed on `distance` presence; over-fetch; `_MAX_DISTANCE` unchanged on the semantic arm; graceful degradation (distance-less ⇒ literal); registration in `FIRST_PARTY_RECALL_BACKENDS`; default unchanged (`jsonl_scan`). Update the backend-names list near the top of the spec. — read: [.vorch/workflows/spec-workflow.md, .vorch/specs/recall.md], files: [.vorch/specs/recall.md]

**Dependencies:** Phase 1 (final design must be settled).
**Done when:** `recall.md` has a Hybrid Backend section consistent with the shipped code; the backend list in PROJECT.md's spec index needs no change (recall spec already listed).

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Vector arm's no-binding fallback double-scans JSONL (FTS also scans) | High (only in degraded path) | Low | Accept in v1 — degraded path only; sessions dedup so output is correct, just redundant work. |
| Over-fetch multiplier too small ⇒ literal group still starved for very repetitive sessions | Low | Med | Mirror vector's proven multiplier+margin; the repetitive-session test pins the behavior; tunable constant if it shows up. |
| First `hybrid` search builds *both* indexes (FTS + full embed) ⇒ heavier one-time latency than either alone | Certain | Low | Inherent and one-time; steady state is one query-embed + local FTS. Note in spec. |
| `searched_sessions`/`total_candidate_sessions` ambiguous across two arms | Low | Low | Both arms derive from the same candidate set; report the candidate count and the larger searched count; cosmetic only. |

### Open / deferred (not blocking v1)
- **Tighten `_MAX_DISTANCE` for the semantic arm** once literal covers keywords (e.g. 0.5–0.6 so augments read as strong). Decision: keep 0.7 in v1, **measure** on real data, revisit as a follow-up — do not guess-tune now.
- **Promote `hybrid` to default** — explicitly out for now (user decision: default stays `jsonl_scan`).
