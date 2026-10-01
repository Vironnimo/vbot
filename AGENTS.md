# AGENTS.md

## Load context for the task

At session start, read these core files completely unless their current contents are already in context. Load them once; re-read affected sections when they change:

- `.vorch/PROJECT.md` - project context
- `.vorch/GLOSSARY.md` - project-specific terms

Before working in or discussing a domain, read its root map under `.vorch/domain-maps/` (index in PROJECT.md), plus adjacent maps when ownership or contracts cross domains. Use them to locate owners, contracts, source, and tests. The task's `read:` list is a starting point, not a ceiling. Follow the root map's `## References` only for supplementary files whose triggers match the task; do not preload a supplementary folder. Supplementary files never replace the root map or enter the Domain Maps index.

Before creating, editing, or auditing a domain map or supplementary file, read `.vorch/workflows/domain-map-workflow.md` in full unless its current contents are already in context. Ordinary map reading does not require that workflow.

## Interpret documentation

Distinguish explicit user requirements and engineering contracts from descriptions of current behavior, examples, and proposals. Follow applicable requirements and contracts; a documented implementation or past choice is not automatically a constraint on new work. Existing behavior and design may be revised within the user's requested scope without a documentation exception.

Source code establishes what is implemented, not what ought to be implemented. Verify behavior claims against source and relevant tests. Correct stale descriptions; when code conflicts with an explicit requirement or contract, address the discrepancy rather than rewriting the requirement to match the code. Keep observations, requirements, and proposed changes identifiable in the docs.

## Communication with the user

### Develop ideas together

Match discussion depth to the task's uncertainty and consequences. Understand the intended outcome; treat suggested implementation choices as hypotheses unless the user establishes them as constraints.

For consequential open decisions, actively consider different approaches before forming a recommendation. Bring forward meaningful options, including possibilities the user may not know to ask about. Explain their main advantages, disadvantages, and the assumptions that favor each, then give a reasoned recommendation tied to the user's intended outcome. Use these perspectives to discover better solutions and build shared understanding; invite the user to challenge the framing or suggest another direction.

For substantial or uncertain work, first outline the major areas, dependencies, and open questions. Surface missing capabilities and consequential assumptions before exhausting implementation details. Discuss the most important unresolved decision next, keeping each exchange focused. Summarize settled and open points when that helps the user assess the whole undertaking.

For small, clear changes, a brief explanation is enough. Do not manufacture options, require a planning round, or reopen settled decisions without new evidence. Clarify unresolved choices that materially affect the outcome; settle routine implementation details within the authorized scope.

Exploratory discussion alone does not authorize implementation. Once the user requests implementation, proceed with established decisions; revisit them only when new evidence materially changes the approach.

### Preserve task continuity and state next steps

Keep the active objective, earlier decisions, and unfinished commitments in view. A side task does not replace the main task unless the user changes direction.

Always tell the user what comes next in its own paragraph, even if it is only one sentence. After a detour, first briefly report what was done and where the main task stands. Resume authorized implementation; for an ongoing discussion, name the open point and invite the user to continue there.

Before declaring completion, check the full agreed scope across the conversation, not just the latest subtask. Respect explicit pauses, cancellations, and changes of direction.

### State the expected outcome before implementation

Before implementation, briefly state the concrete, observable outcome you understand the user wants. One sentence is enough for a small change; use a short list when several outcomes need tracking. Base it on the request and established context; do not invent requirements, numerical targets, or scope.

Check completed work against those outcomes. Report what was achieved, material deviations or remaining gaps, and relevant verification results in the conversation. Create a separate verification report only when requested.

### Present Agent-facing text changes

For runtime Agent-facing text, explain the meaningful changes in the conversation and show the relevant proposed wording verbatim. Include the corresponding current wording when comparison helps the user assess the change.

For larger changes, keep the conversation focused on the passages that materially affect Agent behavior. Make the complete changes available through the source-file diff. Do not create additional files solely to present the review. Show complete wording when the user requests it.

Runtime Agent-facing text includes System Prompt blocks, Tool descriptions, Skill instructions, and System Reminders supplied by vBot to an Agent or Model. Repository governance and development documentation are outside this review rule.

### Use established terminology

Use vBot's established terms as defined in the glossary and domain maps: Tools, Run, Session, Queue, Skill, Provider, and so on. Keep these terms untranslated.

## Agent-facing text quality

Runtime Agent-facing text must be self-contained for a fresh Agent that has not read project documentation. Mention only concepts the Agent can observe or act on, explain available behavior and valid next actions, and avoid hidden implementation categories.

## Architecture & code

**Few, deep modules:** Prefer clear ownership, small interfaces, and hidden implementation. Extend an existing owner when the capability belongs there. Before introducing a new responsibility, public interface, layer, or abstraction, identify the closest existing owner and explain why the new boundary is warranted. Avoid pass-through layers and abstractions that expose their internals or add little beyond their interface.

Judge complexity by responsibilities, coupling, and what callers must understand, not file count alone. Tests, documentation, components, and internal file splits do not need a new-owner justification unless they introduce an architectural boundary. Split files when it improves clarity and cohesion; do not overload an existing file merely to avoid creating one.

**Technical Decisions** — when making technical decisions, do not give much weight to development cost. Instead, prefer quality, simplicity, robustness, scalability, and long-term maintainability.

## Testing

Tests guard behavior against regressions from concurrent and later work. Every test also costs runtime and maintenance for all later tasks, so keep the suite a curated set of behavior contracts, not a record of every change.

**Write tests for contracts, not for changes.**

- Test behavior at the owning module's public interface: contracts, persisted formats, protocols, Tool-visible behavior. Test an internal helper directly only when it carries its own contract that is impractical to reach through that interface.
- A bug fix extends the existing test that owns the behavior, with a new parameter case or assertion. Add a new test only when no existing test covers that behavior.
- One test per distinct behavior; parameterize only over genuinely different cases.
- Developer scripts and probes get at most a smoke test, unless they protect data or installations (converters, installers, update and worktree tooling).
- Keep tests fast and deterministic: controlled clocks and in-memory fakes instead of real waits, subprocesses, and network. A test that needs more than about a second must justify it. Load and scale variants carry the `stress` marker, which the normal suite skips (PROJECT.md -> Testing).
- Durable I/O is the main hidden cost: the suite skips disk syncs (`os.fsync`, SQLite `synchronous`) unless a test is marked `durable` because it checks them. Build expensive setup such as empty databases or a Runtime once per worker and copy it, or share it where tests cannot change it, instead of rebuilding it in every test.
- When a change makes tests obsolete or redundant, delete or merge them in the same change.
- Documentation-only edits need no tests; review content, references, and the diff.

**Tests run at push, not at commit or merge.** Commits and merges get static checks only: run the tests covering your change yourself and commit once they pass; leave failures your change cannot explain to the push and mention them in your report.

## Dependencies

If a task requires a new dependency, **check first** that no existing dependency already covers the need. Then install it, add it to `pyproject.toml` (or `webui/package.json` for frontend), and commit the lock file changes. Do not install packages speculatively — only what the current task requires.

## You maintain the docs & domain maps

Maintain affected documentation as part of the task with small, factual updates:

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, domain-maps index, strategic context
- `.vorch/domain-maps/<domain>.md` — a domain's ownership, contracts, or documented behavior changes, including affected supplementary files, or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/GLOSSARY.md` — shared vocabulary under the Terminology rules below
- `.vorch/FLAGGED.md` — git-ignored, never commit it; append a deferred concern at the bottom so you needn't read the whole file, or fold it into a related existing entry when you already know one fits; delete an entry once its concern is resolved or verified fixed instead of annotating it.

**Write all project documents in English.** Plans, design documents, decision records (like the system-prompt handoff), domain maps, PROJECT.md, GLOSSARY.md, FLAGGED.md — every project artifact is written in English, regardless of the language you and the user speak in chat. User-facing chat follows the user's language; the documents do not.

## Terminology

`.vorch/GLOSSARY.md` holds the small shared vocabulary needed to interpret vBot correctly across tasks, especially terms easily confused with the coding agent's own environment or other systems, such as Agent, Tool, Session, and Workspace.

Terms needed only within a domain belong in that domain map's `## Terms` section, even when the user mentions them. Document project-specific meanings and useful distinctions, not standard vocabulary. Give each term one canonical home; use references elsewhere.

Maintain terminology as part of relevant work when a missing, stale, or ambiguous definition could cause mistakes. Make clear, evidence-backed updates directly; no separate Skill or interview is required. Ask the user when competing meanings would materially affect the work, rather than silently establishing a new meaning.

## Git

- Work directly on `main` — no feature branches by default. When you finish a task, commit it (the user may also ask you to commit mid-way); you don't need to wait to be asked.
- **Small, quick, low-risk changes go directly on `main`; use a worktree for larger or uncertain tasks, or when concurrent work could interfere.** Create worktrees with `python scripts/worktree.py create <task-name>` (see PROJECT.md → Development), then work and commit inside them. Once everything is committed, run `python scripts/worktree.py merge <task-name>` to merge into `main` and remove the worktree; the merge commit passes the commit check. For conflicts, follow `.vorch/workflows/worktree-workflow.md`. No user confirmation is needed before merging.
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period. Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; commit once the tests covering your change pass.
- **The commit hook checks every commit.** It formats, lints, and type-checks the staged files and re-stages its fixes for completely staged files (PROJECT.md -> Testing). Stage whole files and review its fixes. When it blocks, fix the reported problems, stage, and commit again; never bypass it with `--no-verify`. It reports mypy errors in another session's uncommitted work without blocking; leave those files alone. If a commit of Python or WebUI files prints no `Commit check` report, the hook is not enabled: enable it (PROJECT.md -> Development).
