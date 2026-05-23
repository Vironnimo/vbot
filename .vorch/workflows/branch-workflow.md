# Branch Workflow

One feature branch per task. All work happens in the main working directory.

## Setup

```bash
git checkout -b <type>/<short-description> main
```

Types: `feat` · `fix` · `docs` · `refactor` · `perf` · `test` · `chore`

## Per-Phase Commit

After each phase completes:

```bash
git add <files changed in this phase>
git commit -m "<type>(<scope>): <what this phase accomplished>"
```

**Commit rules:**
- Stage only application files — never the plan file.
- Subject: lowercase, no trailing period, max 72 chars.
- Body (optional): explain *why*, not *what*.
- Breaking change: append `!` → `feat(api)!: rename endpoint`.
- One logical unit = one commit (= one phase).
- **Never commit broken code.**

## Merge & Finalize

Run all quality gates from `.vorch/PROJECT.md` against the full repo. Everything must be green before merging.

```bash
git checkout main
git merge <branch> --no-ff -m "merge: <summary>"
git branch -d <branch>
```
