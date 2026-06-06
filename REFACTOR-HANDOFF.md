# Refactor Handoff — Large-File Decomposition

**Status:** in progress (Wave 1 done; Wave 2 through ChatView done) · **Owner:** Julian · **Started:** 2026-06-06
**Next action:** continue Wave 2 → decompose `api.js` as described under "Planned UI
decomposition" below. Keep `api.js` as the compatibility facade and preserve every
existing export.

This document is self-contained: a fresh session should be able to continue from it
alone. Read it top to bottom before touching code.

> **⚠ WORKFLOW — ONE PANEL PER SESSION (user decision 2026-06-06).** Do **not** extract
> all remaining SettingsView panels in one go. Each session: extract **exactly one** panel
> from the "Remaining panels — one per session" queue below, get the full frontend gate
> green (`python scripts/quality-frontend.py` → vitest 525/525 · build PASS), update this
> handoff (check the panel off, advance "CONTINUE HERE" to the next one, record line
> counts), commit, and stop. The user starts the next session for the next panel.

---

## 0. Orientation (read before doing anything)

This is **vBot**, a local-first agent harness (async Python kernel + FastAPI + Svelte
WebUI + CLI + pywebview desktop). Before working:

1. Read `.vorch/PROJECT.md` — architecture, layers, conventions, quality-gate commands.
2. Read the spec for whatever domain you are about to touch, from `.vorch/specs/`
   (index is in `PROJECT.md`). E.g. WebUI work → `.vorch/specs/webui.md`.
3. This is a **pure structural refactor** effort: move code to separate concerns,
   **never change behavior**. Tests are the safety net — keep them green, and prefer
   not to edit tests (if a test reaches into internals, see the recipe in §5).

Conventions that matter here (from `PROJECT.md`):
- DI via constructor `__init__`; interfaces via `typing.Protocol`. No globals/singletons.
- stdlib → third-party → local imports; remove unused.
- Backend Python 3.11+, ruff + mypy + pytest gated. Frontend Svelte (JS, no TS),
  Vitest. Use the current Python interpreter directly (no venv assumptions).

---

## 1. Goal

Several files have grown past a maintainable size. Split each one along genuine
concern boundaries so no single file carries multiple unrelated responsibilities.

## 2. Working threshold & principle

- Soft limit is **~1000 lines per file** (`PROJECT.md` now says 1000). Files
  comfortably under ~1000 are **not** targets. The split is about separating concerns,
  not hitting a number.
- **Deep modules, few not many** (`PROJECT.md` convention): each domain keeps *one*
  public main file (the deep interface). Internal concerns move into private sibling
  modules; the package `__init__.py` re-exports what callers need. **Do not** fragment
  into many shallow files — extract cohesive units only.

## 3. Out of scope

- **`core/runtime/runtime.py` (~1030)** — long but *flat*: ~210 lines are trivial
  lazy-getter DI `@property` wiring. Splitting hurts the DI overview. **Excluded by
  user decision — do not touch.**
- **Test files** (e.g. `tests/server/test_rpc.py` 4849, `test_chat_loop.py` 3536) — not
  a separate campaign. Tests mirror source and split along with it.
- **Borderline 900–1000 files** (`openai_compatible.py` 977, `channels.py` 940,
  `telegram.py` 912, `settings/validation.py` 910, `SystemPromptView.svelte` 995,
  `CronView.svelte` 968, `LogsView.svelte` 936) — under threshold. Watch list only;
  trim opportunistically if already editing them (Boy-Scout rule).

---

## 4. Per-file process — Definition of Done (FOLLOW EVERY TIME)

For each target file, in order:

1. **Read the file fully** and map its concerns (classes, function clusters, helpers).
2. **Find external dependents** before cutting — see §5 for the exact grep recipe.
   Know what the package `__init__.py` re-exports and who imports module internals.
3. **Extract cohesive units** into private sibling modules. Keep the main file as the
   deep public interface. Preserve every public import path.
4. **Keep `__init__.py` surface identical** (or only widen it). Re-export anything
   callers imported. (The project's "no legacy compatibility" rule is about *data
   formats* — it does not excuse breaking live import paths.)
5. **Update the domain spec — MANDATORY after every refactor.** Follow
   `.vorch/workflows/spec-workflow.md` (the Orchestrator agent, `.opencode/agents/orchestrator.md`,
   owns specs; that workflow is the standard). Spec rules that bite here:
   - A spec is **decision-useful working notes for agents**, *not* architecture
     documentation, API reference, or a file-layout inventory.
   - A pure internal refactor (no behavior/contract/boundary change) usually needs
     **little or no** spec change. Do **not** paste a "module layout" inventory.
   - Add only what changes an agent's decisions: the **import-path boundary** ("import
     from the package, internals are private") and a concise **"where new code goes"**
     decision rule if the split created a new home for a concern (e.g. "new settings
     normalizers go in `settings_normalizers.py`, not `storage.py`").
   - Every claim must be backed by the source you just wrote. Update the Specs index in
     `PROJECT.md` only if a spec is created/renamed/removed (not for in-place edits).
6. **Quality gate green** (see §5 for commands). No behavior change.
7. **Update this handoff**: check the box, record before→after line counts, the new
   files, and the verification result. Keep "START HERE" pointing at the next item.
8. **Commit the item.** Once the gate is green and the handoff is updated, commit the
   refactor as its own logical commit (one per target file / wave item, plus the
   updated handoff). Use a `refactor(<domain>): …` message. This step is mandatory —
   do not leave finished work uncommitted for the next session to discover.

Commit cadence: scaffolding (plan + threshold) and each refactor item get their own
commit on `main`, matching this handoff's structure. Do not bundle multiple wave items
into one commit. Branch only if the user asks.

---

## 5. Verification recipe (commands & how-to)

**Find who depends on a package before splitting it** (bash tool; adjust the name):
```bash
# external importers of the package
grep -rnE 'from core\.<pkg>|import core\.<pkg>|core\.<pkg>\.' --include='*.py' . \
  | grep -v '/core/<pkg>/' | grep -v '__pycache__'
# what the package __init__ re-exports
cat core/<pkg>/__init__.py
# does any TEST import the concrete module (not the package) or patch its members?
grep -nE 'core\.<pkg>\.<module>' tests -r
```
If a test does `import core.<pkg>.<module> as m` and patches `m.<name>`, the split must
**keep that module exposing `<name>`** (re-export it). That lets you split with **zero
test changes** — preferred, because unchanged tests prove behavior is preserved.

**Quality gates** (each runs format → lint → type-check → test):
```bash
python scripts/quality.py core/<pkg>/            # backend module
python scripts/quality-frontend.py webui/src/... # frontend path
```
The backend gate runs the *mirrored* tests only. Also run the **indirect consumers**
you found in the grep, e.g.:
```bash
python -m pytest tests/core/prompts tests/core/runtime tests/server/test_rpc.py -q
```
Finish with an import smoke test:
```bash
python -c "import core.runtime.runtime, server.app; print('ok')"
```

## 6. Reusable split recipe (learned in Wave 1)

- **Pure stateless helpers** (validators/normalizers that only touch args + constants)
  → a free-function module (e.g. `settings_normalizers.py`). Move the constants they
  use with them. Caller methods change `self._x(...)` → `x(...)`.
- **A stateful collaborator** (owns paths/IO) → a class injected via `__init__` and
  delegated to (e.g. `PromptFragmentStore`). The main class keeps thin delegator
  methods so the public API is unchanged.
- **Shared low-level primitives** (atomic temp-file writes, a logger, a background-task
  error logger) → a tiny shared module (`atomic.py`) or the lower-layer module, imported
  by both halves. Avoid duplicating.
- **Errors** → per-package `errors.py` (matches `core/providers/errors.py`,
  `core/chat/errors.py`). Re-import into the main module so old paths still resolve.
- **Circular imports**: a sibling importing another sibling is fine as long as the
  `__init__.py` imports the leaf modules *before* the main module, and you use direct
  submodule imports (`from core.x.leaf import Y`). Lower layers must not import upward.
- **Re-export-only-for-tests**: if a name must stay on a module but is otherwise unused
  there, use `from x import name as name` (ruff treats it as an intentional re-export;
  avoids F401).

---

## 7. Primary targets (> ~1000 lines)

| File | Lines | Core problem |
|---|---|---|
| `webui/src/components/SettingsView.svelte` | 4475 | ~10 independent settings panels in one component |
| `webui/src/components/ChatTimeline.svelte` | 2523 | rendering + scroll logic + date grouping |
| `core/chat/chat.py` | 2486 | data model + tool dispatch + orchestrator + model resolution + events |
| `webui/src/components/AgentsView.svelte` | 2025 | agent list + form + detail in one view |
| `webui/src/lib/chatState.js` | 1927 | session/run state **+** full timeline projection |
| `webui/src/components/DebugView.svelte` | 1700 | trace list + detail + filters |
| `webui/src/components/ChatView.svelte` | 1594 | |
| `webui/src/lib/api.js` | 1227 | RPC client for all domains in one module |
| `cli/main.py` | 1109 | 350-line `parse_args` + dispatchers + output formatters |
| `webui/src/components/ChatComposer.svelte` | 1075 | |
| `core/chat/chat.py`, `core/storage/storage.py`, `core/subagents/subagents.py` | — | (storage & subagents DONE — see Wave 1) |

## 8. Execution order (waves)

Ordering: **low risk + clean seam + good test coverage first**, central/risky last.

- [ ] **`api.js` (1227)** ◀── START HERE
- [ ] **`ChatComposer.svelte` (1075)**

### Planned UI decomposition (surveyed 2026-06-06)

Use this as the starting plan, then re-check dependents and tests before each cut.
Preserve the current parent component/API import surfaces throughout.

5. **`api.js` (1227; 33 unit tests).**
   - Keep `api.js` as the compatibility facade. Extract generic RPC/error/URL/JSON
     transport primitives, Run/server/log event subscriptions, and binary
     attachment/speech HTTP into cohesive private modules.
   - Group thin RPC wrappers by real domains only where the group is substantial
     (Chat/session/queue, channels/providers/settings, debug/logs/automation); re-export
     every existing symbol from `api.js`. Do not create one shallow file per method.

6. **`ChatComposer.svelte` (1075; 16 component tests).**
   - Extract pure trigger detection/filtering/insertion helpers first, then the
     attachment upload/content-block conversion lifecycle if the component remains over
     threshold.
   - Keep textarea focus/resize, keyboard submission, voice recording, and top-level
     send orchestration together unless a later cohesive voice-control child is clearly
     warranted. This is borderline size, so stop once it is comfortably under 1000.

### Wave 3 — central, well-tested core (do last)

- [ ] **`cli/main.py` (1109)** — `parse_args` alone is ~350 lines of argparse →
  `cli/parser.py`. `print_*` / `_*_output_lines` formatters → `cli/output.py`.
  `dispatch_*` stay or → `cli/dispatch.py`. Spec: `.vorch/specs/cli.md`.
