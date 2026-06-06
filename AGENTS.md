# Agent Rules

These rules apply to every agent, every task, unconditionally.
**Read this before starting any work. These are YOUR rules.**

## Session Start

Every session begins by reading these files — immediately, before anything else:

1. `.vorch/PROJECT.md`
2. `.vorch/GLOSSARY.md`

**Read each file completely, start to end — don't skim, and don't stop after the first 100–200 lines assuming you have enough.**

Your agent file lists any additional files to read on top of these. Apply the same rule: read them in full.

## Roles & Ownership

| Responsibility | Owner |
|---|---|
| Git (branches, commits, merges) | **Orchestrator + User** |
| Project docs (`.vorch/PROJECT.md`) | **Orchestrator only** |
| Spec files (`.vorch/specs/`) | **Orchestrator only** |
| Glossary (`.vorch/GLOSSARY.md`) | **Orchestrator only** |
| Planning & file-scope assignment | **Planner** |
| Application code, tests, and UI | **Builder** |
| Code review | **Reviewer** |
| Web research (tech, libraries, APIs) | **Researcher** |
| Codebase exploration & structured summaries | **Explorer** |

**No agent operates outside their role.** Only the Orchestrator and User touch git. Only the Orchestrator writes `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, and files under `.vorch/specs/`. Every other agent writes only within the scope defined by their role.

## Code Quality

**Naming:** Descriptive — `getUserById`, not `getU`. No abbreviations except standards (`id`, `url`, `db`). Consistent throughout. One human language, never mixed.

**Functions:** One thing per function. Max 3 levels of nesting.

**Imports:** stdlib → third-party → local. Blank line between groups. Remove unused.

**Constants:** No magic numbers or strings — `MAX_RETRIES = 3`, not `3`.

**Comments:** Explain *why*, not *what*. No commented-out code — that's what git is for.

**Separation of concerns:**
- UI: display + user input only, no business logic
- Business logic: no UI, no direct DB queries
- Data access: queries and file I/O in their own layer
- API endpoints: routing + request/response only, no logic

## Error Handling

| Type | Examples | Action |
|---|---|---|
| **Expected** | invalid input, not found, timeout, rate limit | Handle locally, log `warn`, return meaningful response |
| **Unexpected** | crashes, null refs, broken assumptions | Do NOT handle — log `error`, rethrow |

Key question: "Did I expect this could happen?" Yes → handle. No → rethrow.

- Never silently swallow errors
- Handle as close to origin as possible
- Error messages must be meaningful — "something went wrong" is useless

**Retry transient errors** (network, HTTP 429, 502/503): max 3 retries, exponential backoff with jitter. Do NOT retry: 4xx (except 429), auth failures, validation errors.

For project-specific error patterns, log format, and logging setup → `.vorch/PROJECT.md` (Conventions section).

## Security — non-negotiable

- **Never** insert user input directly into SQL, HTML, shell commands, or file paths
- **Always** use parameterized queries / prepared statements
- **No credentials in code** — environment variables only, never commit `.env`
- **No `innerHTML` with user data** — use `textContent`
- **Validate all input server-side** — type, format, length, range
- **Never log** passwords, tokens, or secrets

## Testing

Write tests **together with the feature** — never skip.

| Type | When | How |
|---|---|---|
| **Unit** | Business logic, validation, calculations | Isolated, deps mocked |
| **Integration** | DB queries, API endpoints | Real test DB / real HTTP server |

**What to test:** happy path · edge cases (null, empty, boundary) · error cases. One logical assertion per test — multiple `assert` calls that verify the same behavior are fine.

**Structure (AAA):** Arrange → Act → Assert.

**Rules:** Tests are independent (no shared state) and deterministic (no random, no real timestamps). If a bug is fixed, add a test that would have caught it.

For project-specific test framework, file naming, fixtures, and coverage targets → `.vorch/PROJECT.md` (Testing section).

## Dependencies

Agents **do NOT install packages themselves.** If a task requires a new dependency:

1. **Check first:** verify no existing dependency already covers the need.
2. **Report it** in your output under `### New Dependencies`:
   ```markdown
   ### New Dependencies
   - `<package-name>` — [why it's needed, what it does]
   ```
3. The **Orchestrator** handles installation and commits lock file changes. No other agent runs install commands.

Do NOT install packages speculatively. Only request what the current task requires.

## Project Context

If a section referenced from `.vorch/PROJECT.md` doesn't exist yet, skip it and proceed with what you have.

When working on a domain, read its spec file from `.vorch/specs/`. Your task will list which specs to read — treat that as a starting point, not a ceiling. Read others if you need them.
