# Project Context

This is the **single source of truth for project-specific knowledge**. Every agent reads it before starting work. Only the Orchestrator updates it.

Remove sections that don't apply to your project. Keep entries short and factual — this is working notes for agents, not polished documentation.

---

## Architecture

[Tech stack. Module structure. Data flow. Major components. Third-party services. High-level — not every detail.]

## Conventions

[Naming patterns, error handling approach, logging format, code style rules, separation-of-concerns boundaries beyond what `AGENTS.md` says. Imports style, file layout within modules, return shapes.]

## Development

[How to set up the dev environment. How to run the app. Required env variables. Build commands. Package manager. Runtime versions.]

## Testing

[Test framework. File naming convention. Where tests live. How to run the suite. Fixtures. Mocking approach. Coverage targets.

**Quality gates** — list every command the Builder must run green before reporting done: tests, linter, formatter, type-checker. Example: `pytest`, `ruff check`, `mypy`. If only tests apply, list only tests.]

## Context

[Strategic notes — decisions, constraints, business context, known issues, things in flight. Maintained by the Orchestrator as work progresses. Use dated entries for anything time-sensitive.]
