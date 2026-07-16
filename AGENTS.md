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
| Domain maps (`.vorch/domain-maps/`) | **Orchestrator only** |
| Glossary (`.vorch/GLOSSARY.md`) | **Orchestrator only** |
| Planning & file-scope assignment | **Planner** |
| Application code, tests, and UI | **Builder** |
| Code review | **Reviewer** |
| Web research (tech, libraries, APIs) | **Researcher** |
| Codebase exploration & structured summaries | **Explorer** |
| Bug investigation & root-cause analysis | **Investigator** |

**No agent operates outside their role.** Only the Orchestrator and User touch git. Only the Orchestrator writes `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, and files under `.vorch/domain-maps/`. Every other agent writes only within the scope defined by their role.

## Technical Decisions

When a technical decision involves a trade-off, do not give much weight to development cost. Prefer quality, simplicity, robustness, scalability, and long-term maintainability.

## Architecture

**Few, deep modules** — small interfaces, implementation hidden inside. Module count is a budget: the system must stay small enough to hold in your head.

- Default to extending an existing module. Before adding a new module, layer, or abstraction, name the existing module that could own the capability and why it can't — "no existing owner fits" is a valid answer, "didn't look" is not. An unjustified new module is a defect, not a style nit.
- Deep over wide: one module owning a capability end-to-end beats several shallow ones passing data around.
- Expose what callers need, hide everything else.
- A module is too shallow when its interface is nearly as large as its implementation, when it's mostly pass-through, when it wraps something without adding an abstraction, or when callers must know its internals — fold it back or deepen it.

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

**Retry transient errors**: network failures and HTTP 429/502/503/504 always; HTTP 500 only for idempotent (safely repeatable) requests — never on action-causing POSTs. Max 3 retries, exponential backoff with jitter. Do NOT retry: other 4xx, auth failures, validation errors.

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

When working on a domain, start with its root map at `.vorch/domain-maps/<domain>.md`; root maps are the always-read routing and safety layer. Treat the task's `read:` list as a starting point, not a ceiling, and read additional root maps when ownership or contracts cross domains.

After reading a root map, inspect its `## References` and load only the exact supplementary files whose trigger matches the current task. Never preload a domain's supplementary folder. A supplementary file adds task-specific depth, never replaces its root, and is deliberately absent from the Domain Maps index in `.vorch/PROJECT.md`.

Terminology has two homes: core, cross-cutting terms live in `.vorch/GLOSSARY.md`; a domain's own terms live in a `## Terms` section inside its map. So a domain map is also where that domain's vocabulary is defined — reading the map gives you both the domain and its words.
