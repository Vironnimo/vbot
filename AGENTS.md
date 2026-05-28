# Agent Rules

These rules apply to a direct, single-agent workflow in this repository.
Read this before starting any work. These are YOUR rules.

## Session Start

Every session begins by reading these files immediately, before anything else:

1. `.vorch/PROJECT.md`
2. `.vorch/GLOSSARY.md`

Read each file completely, from first line to last line. Do not stop after the
first 100-200 lines. Do not skim. If an agent/custom mode lists additional
required files, read those in full too.

When working on a domain, read its spec from `.vorch/specs/` before changing
that domain. Treat listed specs as a starting point, not a ceiling.

## Operating Mode

The default mode is direct single-agent execution. You own the work end to end:
planning, implementation, tests, documentation, git, and the final handoff.

If the user explicitly asks for the old multi-agent system, follow the files in
`.github/agents/`. Otherwise, fold the useful responsibilities into this single
agent:

- Orchestrator duties: scope the request, keep git clean, commit logical units,
   and keep `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, and `.vorch/specs/`
   current.
- Builder duties: write application code, tests, and UI; follow local patterns;
   run focused quality gates before reporting done.
- Tester duties: when live testing is requested, read `.vorch/TESTER.md`, start
   and stop the app with `python scripts/test-env.py`, use browser automation,
   and capture screenshots for browser-visible claims.

Do not use subagents unless the user explicitly asks for them. If subagents are
forbidden or unavailable, do all work yourself.

## Working Loop

1. Understand: read only the context needed to make a sound local decision.
2. Plan: for non-trivial work, keep a short visible checklist and update it as
    steps complete.
3. Implement: make focused changes that match existing conventions.
4. Verify: run the narrowest meaningful quality gates and fix failures before
    moving on.
5. Update docs/specs: if behavior, contracts, architecture, setup, tests, or UI
    conventions changed, update the relevant `.vorch` docs in the same task.
6. Commit: commit the completed logical unit unless the user tells you not to.
7. Ask: before ending the turn, call `vscode_askQuestions` with concrete next
    choices.

If the user asks only for analysis, review, or a summary, do not edit code
unless they ask you to proceed. Still end with `vscode_askQuestions`.

## Git

You own git in direct mode.

- Use the current branch/worktree unless the user asks for a new one.
- Check `git status --short` before committing.
- Review the diff enough to know what is being committed.
- Commit one logical unit at a time.
- Use Conventional Commit style, for example: `feat(chat): tools added`.
- Include related user-created files in the commit when the user explicitly
   asks for them, such as backups or companion docs.
- Never commit broken code. Never skip verification to "fix it later".
- Never revert user changes unless the user explicitly asks.
- Never use destructive git commands such as `git reset --hard` or
   `git checkout --` without explicit approval.

## Project Docs, Specs, And Glossary

The `.vorch` files are working memory for future agents and must stay current.

Update `.vorch/PROJECT.md` when architecture, module structure, dependencies,
development commands, testing strategy, logging, conventions, or strategic
project context changes.

Update `.vorch/specs/<domain>.md` when a domain's behavior, public contract,
data model, interfaces, constraints, or important gotchas change. If a new
domain appears, create a spec and add it to the Specs index in
`.vorch/PROJECT.md`.

Update `.vorch/GLOSSARY.md` only when the user asks or when a project-specific
term would otherwise be ambiguous for future work. Keep entries concise.

Specs are not optional polish. If code or UI behavior changes and a spec covers
that area, update the spec in the same commit.

## Code Quality

Naming: descriptive names, no abbreviations except standards such as `id`,
`url`, and `db`. Use one human language consistently.

Functions: one thing per function. Keep control flow simple and avoid more than
three levels of nesting.

Imports: stdlib, third-party, local. Blank line between groups. Remove unused
imports.

Constants: no magic numbers or strings. Name meaningful repeated values.

Comments: explain why, not what. Do not leave commented-out code.

Separation of concerns:

- UI: display and user input only, no business logic.
- Business logic: no UI and no direct database/file persistence unless that is
   the module's responsibility.
- Data access: queries and file I/O stay in their own layer.
- API endpoints/delegates: routing and request/response translation only; keep
   domain logic in core modules.

For UI work, respect the existing design system and component patterns. If a
task involves substantial UI, read `.vorch/DESIGN.md` if it exists and activate
the frontend-design skill.

Use `ctx7` to verify external library/framework APIs when current behavior
matters. Do not rely on stale assumptions for third-party APIs.

## Error Handling

| Type | Examples | Action |
|---|---|---|
| Expected | invalid input, not found, timeout, rate limit | Handle locally, log `warn`, return a meaningful response |
| Unexpected | crashes, null refs, broken assumptions | Do not handle; log `error` and rethrow |

Key question: "Did I expect this could happen?" If yes, handle it close to the
origin. If no, rethrow.

- Never silently swallow errors.
- Error messages must be meaningful.
- Retry transient errors only: network failures, HTTP 429, 502, and 503.
- Use max 3 retries with exponential backoff and jitter.
- Do not retry ordinary 4xx responses, auth failures, or validation errors.

For project-specific logging and error patterns, follow `.vorch/PROJECT.md`.

## Security

- Never insert user input directly into SQL, HTML, shell commands, or file
   paths.
- Always use parameterized queries or prepared statements.
- No credentials in code. Use environment variables or the configured data-dir
   `.env`; never commit `.env`.
- No `innerHTML` with user data. Use safe rendering APIs such as `textContent`.
- Validate all input server-side: type, format, length, and range.
- Never log passwords, tokens, API keys, or other secrets.

## Testing And Verification

Write or update tests with the feature. If a bug is fixed, add a test that
would have caught it unless the user explicitly asks for a docs-only change or a
test is not practical.

Use AAA structure: Arrange, Act, Assert. Tests must be independent,
deterministic, and focused on observable behavior.

Run focused gates before reporting done:

```bash
python scripts/quality.py [paths...]
python scripts/quality-frontend.py [paths...]
```

For live UI testing, use only:

```bash
python scripts/test-env.py start [--data-dir <dir>] [--port <port>]
python scripts/test-env.py stop [--data-dir <dir>] [--port <port>]
```

Always stop processes you start. For browser-visible claims, capture and report
screenshot evidence.

## Dependencies

Do not add dependencies speculatively.

If a task appears to require a new dependency:

1. Check whether an existing dependency already covers the need.
2. Ask the user before adding it unless they already approved the dependency.
3. If approved, update manifests and lock files together.
4. Report new dependencies in the completion summary.

## Hard Stop: Required AskQuestions Tool Call

Before ending every assistant turn, call `vscode_askQuestions`.

This is mandatory after every completed task, and at the latest after every
commit. It is also mandatory for explanations, analysis, review, wording
suggestions, status updates, and blocked reports.

The assistant's last action in the turn must be the `vscode_askQuestions` tool
call. A prose answer without that tool call violates this repository's rules.

Immediately before that tool call, explicitly repeat this reminder in the
visible response: after every completed task, and at the latest after every
commit, call `vscode_askQuestions`.

The question must offer concrete next-step choices relevant to the just-completed
work.
