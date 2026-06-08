# Handoff — Session Search → Hybrid Recall

Working note / plan (not a spec, not committed). Next direction: a **hybrid**
recall backend that fuses literal (SQLite FTS) + semantic (vector) so single
keywords *and* conceptual queries both work.

## Where we are

- **Tool results are opt-in** — done & committed (`01ca9a9`). Default search is
  conversation only (`user, assistant, error, compaction_checkpoint`); `tool`
  only via explicit `roles: ["tool"]`. Context/bookends follow the request's
  roles. Vector snippet now comes from the anchor message, not the chunk
  headline. See `.vorch/specs/recall.md`.
- **Active backend is `vector`** (`~/.vbot/settings.json` → `recall.backend`).
- **Diagnosis confirmed empirically this session:**
  - The vector backend works as designed for *semantic* queries. Test query
    `jemand möchte unterhalten werden, witze oder geschichten` → 10 strong hits
    at distance **0.26–0.33**, matching "Erzähl mir einen Witz / eine Geschichte"
    by meaning, not by literal words.
  - It is structurally **weak for a single keyword**: `bild` → **1** hit at 0.50,
    while a raw grep of the 180 main sessions shows **21** contain "bild" (20 in
    user/assistant text). A lone word gives the embedding too little to anchor
    on, so distances stay diffuse and the `_MAX_DISTANCE = 0.7` floor drops all
    but the one chunk that is *densely* about "Bild".
  - This is the literal-keyword gap a hybrid closes — not a bug, and not caused
    by the tool-opt-in change.

## Goal

One backend that returns **both**: every literal occurrence of a keyword (so
`bild` finds all ~21 sessions) **and** semantically related sessions for
conceptual queries. No change to the two underlying engines — fuse their results.

## What we build on (already exists)

- `core/recall/sqlite_fts.py` — `SqliteFtsRecallBackend`. Literal: FTS5 `trigram`
  → case-insensitive **substring** match (`gpt` matches `gpt4o`). Returns matches
  **per message**, newest-first, re-validated against canonical JSONL. Trigram
  needs ≥3 chars; shorter queries fall back to JSONL scan. Produces
  `message_match_payload`-shaped matches (no `distance`).
- `core/recall/vector.py` — `VectorRecallBackend`. Semantic: per-chunk cosine
  KNN, **deduped to nearest chunk per session**, `_MAX_DISTANCE = 0.7` floor.
  Matches carry `distance` + `chunk_index`. Already role-agnostic index + read-time
  role filtering.
- Both take `RecallBackendContext` (sessions, data_dir, embeddings,
  model_registry, logger) and both inherit `browse`/`scroll` from
  `JsonlSessionRecallBackend` — only `search` differs.
- Registry/config: constants in `core/recall/recall.py`
  (`RECALL_BACKEND_*`, `DEFAULT_RECALL_BACKEND`, `FIRST_PARTY_RECALL_BACKENDS`),
  built in `RecallBackendRegistry.with_builtins()`, selected by
  `Runtime._create_recall_backend` / `reload_recall_backend`
  (`core/runtime/runtime.py`), validated in `core/settings/settings.py` against
  `FIRST_PARTY_RECALL_BACKENDS`, panel list assembled in
  `server/rpc/settings_methods.py`.

## Design — `HybridRecallBackend`

New backend that composes the two existing ones (constructs a
`SqliteFtsRecallBackend` and a `VectorRecallBackend` from the same context) and
overrides only `search`. `browse` (no query) and `scroll` (anchored) have nothing
to fuse → inherit the JSONL behavior like the others.

**Merge strategy (the key decision):**

- **Recommended — literal-first, semantic-augment.** Run the FTS arm and the
  vector arm, dedup to **one entry per session**, then order: all literal-hit
  sessions first (newest-first, as FTS gives them), then semantic-only sessions
  (by ascending distance) to fill up to `limit`. Rationale: matches the user's
  mental model — typing `bild` must surface every "bild" session, never buried
  under a fuzzy semantic hit; semantic just adds conceptual extras. Tag each
  match `source: "literal" | "semantic" | "both"` for transparent rendering;
  when a session hits both, keep the literal payload (its snippet contains the
  exact term the user searched for) but keep the `distance` for display.
- **Alternative — Reciprocal Rank Fusion (RRF).** `score = Σ 1/(k + rank_arm)`,
  k≈60. More principled for mixed queries, but a strong exact match can rank
  below a session that is middling in both arms — worse for the keyword case.
  Use only if literal-first feels too rigid in practice.

**Other decisions:**

- **Dedup unit = session.** FTS is per-message (can repeat a session); collapse
  to the session's best/first message. Vector is already per-session.
- **Over-fetch.** To get `limit` *distinct* sessions after dedup, each arm should
  fetch more than `limit` (FTS returns per-message; vector already over-fetches
  KNN). Cheap refinement; v1 can call each arm with the user `limit` and accept
  occasionally fewer distinct sessions.
- **Keep `_MAX_DISTANCE` on the semantic arm.** Literal covers exact terms, so
  the semantic arm stays pure augmentation — don't loosen the floor here.
- **Graceful degradation (important).** If the vector arm can't run (no embedding
  binding, embed/API failure) → return **literal-only**, no crash. If FTS fails →
  semantic-only or JSONL. Hybrid degrades to whichever arm works; each arm already
  has its own JSONL fallback.
- **Latency.** First search after enabling embeds all sessions *and* builds the
  FTS index (one-time). Steady state: one query-embed API call + local FTS.

**Rendering.** Small hybrid renderer over the merged `matches`: tag
`[literal]`/`[semantic]`/`[both]` and show `distance` for the semantic ones. The
`matches` payload stays the existing shape (semantic entries additionally carry
`distance`/`chunk_index`).

## Implementation steps

1. `core/recall/hybrid.py` — `HybridRecallBackend(JsonlSessionRecallBackend)`;
   build both arms in `__init__`; implement fused `search` (literal-first merge,
   session dedup, `source` tag, graceful degradation) + `render_hybrid_matches`.
2. `core/recall/recall.py` — add `RECALL_BACKEND_HYBRID = "hybrid"`; add to
   `FIRST_PARTY_RECALL_BACKENDS`; register in `with_builtins()`.
3. `core/recall/__init__.py` — export the new name + class.
4. **Decision:** leave `DEFAULT_RECALL_BACKEND = jsonl_scan`, or promote `hybrid`?
   The user switches via settings regardless; don't flip the global default
   without deciding. Their `~/.vbot/settings.json` would move `vector → hybrid`
   (live reload via Settings → Recall panel, or restart).
5. Tests (`tests/core/recall/test_hybrid.py` + update existing):
   - keyword whose vector distance > 0.7 but appears literally → surfaces via the
     literal arm (the `bild` regression, the headline test);
   - purely conceptual query with no literal overlap → semantic-only matches;
   - session hit by both arms → appears once, tagged `both`, literal snippet;
   - no embedding binding → hybrid returns literal-only, no crash;
   - ordering: literal sessions precede semantic-only.
   - **Update `tests/core/recall/test_recall.py:37-40`** — the frozenset equality
     pin must include `hybrid`.
6. `.vorch/specs/recall.md` — add a "Hybrid Backend" section (fusion order, dedup
   unit, cutoff handling, degradation, registration). Read
   `.vorch/workflows/spec-workflow.md` first.

## Open questions

- Literal-first vs RRF (recommend literal-first; revisit if it feels rigid).
- Promote `hybrid` to the default backend, or leave opt-in via settings?
- Surface `distance` in the tool output so weak semantic augments read as weak?
- `_MAX_DISTANCE = 0.7` — once literal covers keywords, is 0.7 still the right
  floor for the semantic arm, or tighten it to keep augments strong?

## Out of scope / unchanged

- Assistant tool-**call** args are still embedded/indexed (intent signal); only
  tool **results** are opt-in. Independent of the hybrid work.
