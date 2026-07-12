# Quality Gates

This document explains how `scripts/quality.py` (backend) and `scripts/quality-frontend.py` (frontend) work: what they run, how they pick which tests to execute, how to read their output, and how to improve them when they hide something you needed.

The short version:

- Run the gate, not the raw tool: `python scripts/quality.py [paths...]` and `python scripts/quality-frontend.py [paths...]`.
- No arguments checks the whole repo; one or more file/dir paths check just those targets (plus their mirrored tests).
- The gate auto-fixes what it can, maps each source file to its tests, filters tool noise, and prints one verdict.
- Reach for raw `pytest`/`ruff`/`mypy`/`vitest`/`eslint`/`prettier` only when you genuinely suspect the gate withheld detail you need — and when it did, improve the gate (see [Improving the gate](#improving-the-gate)) instead of making hand-invocation the habit.

## What the gates are

Two scripts with the same interface, one per side of the codebase. They run the same shape of pipeline — format → lint → type-check → test (→ build, frontend only) — and share their plumbing through `scripts/_quality_common.py`: content-hash snapshots for fix detection, path deduplication, console-encoding setup, and the output-shaping helpers. Each runner supplies only what differs (its file-suffix set, its base directory, its file-vs-directory rule, its tool commands).

The output is the agent contract. It is meant to be read by an agent deciding what to do next, so every step reports a compact status, auto-fixed files are listed explicitly, failures forward the underlying tool output, and the final line states the verdict.

## Running them

```bash
python scripts/quality.py                          # full backend scan
python scripts/quality.py core/runtime/            # one module
python scripts/quality.py core/utils/config.py     # single file (+ its mirrored tests)
python scripts/quality.py core/utils/config.py core/utils/errors.py   # several targets
```

```bash
python scripts/quality-frontend.py                             # full frontend scan
python scripts/quality-frontend.py webui/src/lib/             # one directory
python scripts/quality-frontend.py webui/src/lib/i18n.js      # single file (tests in its dir)
python scripts/quality-frontend.py webui/src/lib/__tests__/i18n.test.js   # one test file
```

Paths are project-root-relative and may be files or directories. Both runners normalize input first (backslash → forward slash, trailing slash stripped) and deduplicate: a file already covered by a directory you also passed is dropped, so `core/utils/ core/utils/config.py` runs `core/utils/` once. The backend runner routes direct files by registered capability: Ruff and mypy own `.py`/`.pyi`, while `pyproject.toml` configures and therefore triggers the full Python pipeline without being passed to a Python source formatter. A direct file with no registered capability aborts before any tool runs. Directories remain mixed scopes whose contents each tool filters itself; a future native module adds its suffixes and tool steps rather than widening Ruff's inputs.

Use the current Python interpreter directly — no virtual environment is assumed. The frontend runner additionally needs `npx` and `npm` on `PATH`; it exits early if they are missing.

## The pipeline

Every step is one external tool run. Steps are one of four kinds, which decides how the status is computed:

- **fix** — auto-fixer. It reports the *action* it took, never a verdict: `FIXED (n files)`, `NO CHANGES` (nothing to fix), or `UNCHANGED` (unfixable issues remain — **not** a failure here; the follow-up lint gate reports those with full detail). Only a harder error (exit ≥ 2) fails the step. `PASS` is reserved for the gate and test steps, so a run that changed nothing never reads as passed.
- **gate** — pass/fail validation (`ruff check`, `mypy`, `eslint`). Nonzero exit fails the gate.
- **test** — the test runner, with a `passed/total` count in the status, or `NO TESTS` when the selected scope has none (never a `PASS` — a run that tested nothing does not read as passed).
- **build** — frontend full-project build; see below.

Backend (`quality.py`), in order: `ruff format` (fix) → `ruff check --fix` (fix) → `ruff check` (gate) → `mypy --pretty` (gate) → `pytest -v --tb=short --timeout=30` (test).

Frontend (`quality-frontend.py`), in order: `prettier --write` (fix) → `eslint --fix` (fix) → `eslint` (gate) → `vitest run --reporter=verbose --passWithNoTests` (test) → `npm run build` (build). All npm commands run with `cwd=webui/`.

On a **full scan** (no paths), the tools target fixed defaults rather than the whole tree indiscriminately:

- Backend: ruff → `.`; mypy → `core/ server/ cli/ desktop/ tests/`; pytest → `tests/`. Full-scan mypy does not include `scripts/`; a scoped Python source path is routed to mypy, while `pyproject.toml` triggers these full defaults.
- Frontend: prettier/eslint/vitest → `src/`; the build step runs only on a full scan.

## Output contract

```text
Quality Gates
=============
ruff format   .... FIXED (0.4s, 2 files)
                    core/utils/config.py
                    core/utils/errors.py
ruff fix      .... NO CHANGES (0.3s)
ruff check    .... PASS (0.2s)
mypy          .... PASS (3.1s)
pytest        .... PASS (2.0s, 41/41)

All gates passed in 6.0s.
```

- Each step prints `label .... STATUS`; a fix step lists every changed file, indented, beneath its line.
- A **failing** step's complete output is reproduced afterward in a `--- label ---` block, so the failure detail is always in the report (pytest/vitest passing-test noise is filtered out first; failure lines are kept verbatim).
- The frontend **build** never fails the gate on warnings. A build that exits `0` but emits stderr (oversized chunks, a11y hints, unused CSS, deprecations) reports `PASS (warnings)` and reproduces the warnings in a `--- build (warnings) ---` block — surfacing them without blocking.
- The final line is either `All gates passed in Xs.` or `N gate(s) failed in Xs.`

## How backend paths map to tests

When you pass a source path, `quality.py` runs the tests that mirror it — the source file itself is never handed to pytest. The rules:

- A `tests` or `tests/...` path is passed through to pytest directly.
- The mirrored packages are `cli`, `core`, `desktop`, `scripts`, `server`. A path whose first segment is none of these selects no tests and prints a `note:` saying so.
- A source **file** `<pkg>/<...>/<file>.py` maps to its exact mirror `tests/<...>/test_<file>.py` **plus** any split-sibling `test_<file>_*.py` in that mirror directory that no more-specific source file owns.
- **Ownership** is by longest matching source stem, with hyphens normalized to `_`. Example: `openai_compatible.py` also runs `test_openai_compatible_oauth.py`, while `openai.py` does **not** — the more specific stem `openai_compatible` owns that split sibling, so the shorter `openai` never swallows it.
- If a source file has no owned mirror test, the whole mirrored **directory** runs instead (with a `note:`), so nearby related tests still execute. If neither the mirror file nor the mirror directory exists, a `note:` says nothing ran.
- A source **directory** maps to `tests/<dir>/` when that exists, else a `note:`.

Two guardrails worth knowing:

- **Bad paths abort with exit 2 before any tool runs.** A nonexistent input path would otherwise make pytest-xdist collect zero items across the whole invocation and silently skip even the valid paths beside it — a green run that tested nothing. Rejecting up front turns a typo into a clear error.
- **Unsupported direct files also abort with exit 2 before any tool runs.** This is capability routing rather than a project-wide Python restriction: a format is safe only after its formatter, linter, type-checker, and test-selection behavior have been registered. Mixed directories remain valid scopes.
- **A scoped run that maps to no tests skips pytest** (`NO TESTS (nothing mirrored)`) rather than falling back to running the entire suite.

## How frontend paths map to tests

`quality-frontend.py` strips a leading `webui/` from each path (all npm commands run inside `webui/`), then resolves each to the Vitest target that actually covers it — the frontend equivalent of the backend source→test mapping:

- An **explicit test file** — a path under a `/__tests__/` directory, or a filename containing `.test.` or `.spec.` — stays file-scoped.
- A **source file** resolves to its mirrored test `<stem>.test.*` / `<stem>.spec.*`, searched in the `__tests__` dir of the file's own directory and each parent up to `src/`. This is what lets a change in `src/components/settings/SettingsProvidersPanel.svelte` find `src/components/__tests__/SettingsProvidersPanel.test.js` one level up — the panels keep their tests in the parent's `__tests__`, not a co-located one.
- If a source file has **no** dedicated mirror test, the nearest ancestor directory that holds any tests runs instead (with a `note:`), so a broader suite still exercises it — e.g. a subcomponent covered only through its view's test.
- A **directory** that holds tests is used as-is; a directory whose tests live one level up maps to that nearest ancestor (with a `note:`).
- An input with no tests anywhere (e.g. a config file outside `src/`) selects no Vitest target and reports `NO TESTS` with a `note:`, instead of falling through to a whole-suite run.

Because a source file resolves to its **actual** mirrored test rather than just its parent directory, a scoped run can no longer silently report a green pass while running zero of the tests that cover the change. Vitest still runs with `--passWithNoTests` as a safety net, and — as on the backend — an input path that does not exist under `webui/` aborts with exit 2 before any tool runs.

## How "auto-fixed files" is detected

The gate does not trust the fixer to report what it changed. Before each fix step it snapshots content hashes of every fixable file under that step's targets; after the step it re-hashes and lists the files whose hash changed. "Fixable" is decided by a suffix set — `.py`/`.pyi` on the backend; a broad web set (`.js`, `.svelte`, `.css`, `.scss`, `.json`, `.md`, `.html`, `.ts`, `.tsx`, `.jsx`, `.cjs`, `.mjs`, `.yaml`, `.yml`) on the frontend — and build/cache directories (`node_modules`, `dist`, `.git`, caches, etc.) are skipped while snapshotting.

This is why the changed-file list is reliable even across tools that print nothing about what they touched, and why it reflects real content changes rather than tool chatter.

## Exit codes

- `0` — all gates passed.
- `1` — at least one gate failed (or, frontend only, `npx`/`npm` was not found on `PATH`).
- `2` — an input path did not exist or a direct file had no registered quality capability; the run aborted before any tool ran.

## Improving the gate

The gates are the contract, so prefer them over calling the underlying tools by hand. If you do fall back to a raw tool and it shows you something the gate did not — a failure that got filtered away, a test that never got mapped, a fix that was not listed — that is a **gate gap**, not just a one-off. Note it in `.vorch/FLAGGED.md` so the gate can be improved to surface it next time.

Where the relevant behavior lives, when you come to fix it:

- **Noise filtering** that could hide a real failure: `filter_pytest_failure_output` / `_is_pytest_progress_nodeid_line` in `quality.py`, `filter_vitest_failure_output` in `quality-frontend.py`.
- **Test selection** that could miss or over-select tests: `translate_to_test_paths` and its `_owned_test_files` / `_owning_source_stem` helpers in `quality.py`; `translate_to_vitest_targets` and its `_find_named_test_files` / `_nearest_ancestor_with_tests` / `_is_explicit_test_file` helpers in `quality-frontend.py`.
- **Backend format routing** that could send a file to the wrong tool or omit a newly supported language: `PYTHON_FILE_SUFFIXES`, `PYTHON_CONFIG_FILES`, and `_unsupported_direct_files` in `quality.py`, plus the pipeline steps that own each registered format.
- **Fix detection** that could miss a changed file: the snapshot suffix sets and ignored-dir sets at the top of each runner, plus `snapshot_target_files` / `changed_snapshot_paths` in `_quality_common.py`.

Their tests live in `tests/scripts/` (`test_quality.py`, `test_quality_frontend.py`, `test__quality_common.py`) — extend those alongside any change so the gate's own behavior stays gated.
