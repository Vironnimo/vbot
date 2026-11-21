# New-Model Review Workflow

A review pass the user hands to an agent running on a newly released Model, so its fresh view finds what earlier Models missed. The user picks the areas; the agent hunts, verifies and reports. Nothing links to this file.

## 1. Ask first

Before reading any code, show the user the catalog below as a numbered list (number, title and one-line scope; leave out the maps) and ask in one message:

- which numbers to review;
- whether to use subagents.

Wait for the answer. Review only the chosen numbers, in catalog order.

## 2. Catalog

Ordered by the cost of a missed defect: security and data loss first, architecture last. For each item read the named root maps first, then the source and tests they point to.

**Security**

1. **Access and network entry** (`access.md`, `server.md`): access guard on every route, socket and RPC; Access Mode switching; pairing, login tickets and Device tokens; origin and CSRF checks; what `lan` and `authenticated` expose.
2. **Permission Boundary and Tool execution** (`permissions.md`, `tools.md`, `tools/shell.md`, `tools/terminal.md`, file Tool maps): path normalization, links and junctions, protected locations, command parsing, ways around a rule.
3. **Secrets and leaks** (`secrets.md`, `logging.md`, `debug.md`): key protection, Agent Secret insertion and echoing, credentials or content in logs, traces, errors and Tool results.
4. **Desktop and Computers** (`desktop.md`, `computers.md`): Desktop bridge exposure, `/ws/computers` requests, consent, routing to the wrong Computer.
5. **Untrusted input** (`channels.md`, `automation.md`, `extensions.md`, `attachments.md`): Channel and webhook senders, Task inputs, Extensions and MCP servers, attachment parsing and extraction.
6. **Install and update** (`cli/application.md`): package authenticity, update and rollback, installer privileges.

**Data integrity**

7. **Database kernel** (`database.md`, `json-documents.md`): migrations, snapshots, quarantine, auto-restore, interrupted writes.
8. **Durable user data** (`sessions.md`, `archive.md`, `memory.md`, `skills.md`): Session forks and lineage, archive, restore and purge, Memory and Skill revisions.
9. **Files and configuration** (`settings.md`, `storage.md`): atomic writes, file locks, temp-file cleanup, validation of user-edited files, deletes that can hit the wrong target.

**Concurrency and lifecycle**

10. **Runs and Queue** (`runs.md`, `activity.md`): admission, Steer, Cancel, interruption, restart recovery, Session activity.
11. **Agentic Loop, Compaction, Sub-Agents** (`chat.md`, `compaction.md`, `subagents.md`): retries, partial output, checkpoints, answer forwarding, tree control.
12. **Scheduling** (`automation.md`, `calendar.md`): Cron and Calendar time handling, missed or doubled triggers, Reflection and Librarian passes.
13. **Runtime lifecycle** (`runtime.md`, `extensions.md`): startup and shutdown order, worker pools, Extension load and unload, leaked tasks.

**Bugs**

14. **Providers and Models** (`providers.md`, `models.md`, `usage.md`): streaming parsers, error classification, retries, usage accounting.
15. **Tools as Agents use them** (`tools.md` and per-Tool maps): argument tolerance, results, error texts, edge cases.
16. **Prompts and Model communication** (`prompts.md`, `model-communication.md`): System Prompt assembly, System Reminders, Prompt epoch and cache stability.
17. **Projects and Agents** (`projects.md`, `agent.md`): resolution, Team, Workspace, Working Project.
18. **Recall and statistics** (`recall.md`, `statistics.md`): indexing, search results, report numbers.
19. **WebUI** (`webui.md`): state sync with server events, reconnects, rendering errors.
20. **CLI and TUI** (`cli.md`): targeting, output contract, error handling.

**Performance**

21. **Hot paths** (`performance.md`, `runs.md`, `chat.md`): Run start, streaming to Accessors, Event Loop stalls.
22. **Storage performance** (`database.md`, `sessions.md`, `recall.md`): queries, indexes, growth of large Sessions.
23. **WebUI performance** (`webui.md`): large Sessions, rerenders, memory.

**Architecture and Agent-facing text**

24. **Deep-module audit**: run `.vorch/workflows/deep-module-audit-workflow.md` over the scope the user names.
25. **Agent-facing text**: review Tools and System Prompt blocks with the `vbot-agent-tools` and `vbot-agent-text` Skills.

## 3. Hunt

- Treat the entries of `.vorch/FLAGGED.md` as known; report them only with new evidence.
- Do not run existing test suites to investigate; check a specific claim with a small targeted probe (`AGENTS.md` -> Testing).
- A finding needs evidence in source (`file:line`), a concrete failure scenario (input or state -> wrong behavior) and its effect on the user. A pattern match is only a candidate.

With subagents: one read-only subagent per chosen number, or several for a large one. Its brief names the item, its maps and focus, the path of `FLAGGED.md` and the evidence rule above, and asks for a compact candidate list. Subagents change no files and no git configuration.

## 4. Verify

Try to refute each candidate before it counts. With subagents, a separate subagent with fresh context gets only the candidate and its evidence. Without them, trace the code path end to end yourself. Drop a candidate that does not survive.

## 5. Report and fix

Report the confirmed findings as one numbered list: area, severity (🔴 harms users now, 🟡 real, 🔵 minor), a one-line summary and the failure scenario. Name the number of dropped candidates in one line. Ask which numbers to fix.

Fix the chosen findings following `AGENTS.md` (Git, Testing). Record confirmed findings the user did not choose in `.vorch/FLAGGED.md` under its rules.
