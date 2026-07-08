# Skill Tool

Tool that activates allowed skill instructions for the current Session. An ordinary allow-list tool — toggleable per agent via `allowed_tools` like any tool, and seeded default-on in the Project Tool Whitelist (`PROJECT_DEFAULT_ALLOWED_TOOLS`).

## Interfaces

- Tool name: `skill`
- Registration: `register_skill_tool(registry, resolve_registry, refresh_skills)` — `resolve_registry` maps `(skill_project_id, agent_id)` to the registry a call activates against; `refresh_skills` rescans skills from disk (wired to `Runtime.reload_skills`, invoked once on a name miss).
- Schema: optional `name` (omitted or blank → list mode: the live agent-aware catalog grouped by origin); `additionalProperties: false`.
- Display: summary field `name`.
- `ToolContext.activate_skill(name, content) -> bool | None` — dedup-only session hook (`ChatSession.register_skill_activation`): `True` fresh, `False` already active, `None` no hook (treated as fresh). Nothing is persisted through the hook — the tool result itself is the durable carrier.

## Conventions

- `skill` is governed by both lists: `allowed_tools` decides whether the tool is offered at all (the normal per-agent toggle), and `allowed_skills` decides which skills it may activate.
- **The tool result IS the content carrier.** A fresh activation returns `data: {name, status: "loaded", content}` where `content` is the full `<skill_content>` wrapper — so the skill instructions sit in the conversation exactly where the load happened and replay verbatim like any other tool result (prompt-cache friendly, no separate placement machinery). The wrapper states the absolute skill directory and that relative paths resolve against it; a `{baseDir}` marker in the body is substituted with that directory (see `skills.md`).
- An already-active skill returns a short stub (`status: "already_active"`, `message`, no content) — the instructions are already in context (live history, or the post-compaction re-injection; see `chat.md`).
- **Rescan on name miss.** When an activation `name` is absent from the resolved registry, the tool calls `refresh_skills` once and re-resolves before giving up, so a skill hand-dropped into a skill directory after the session's registry was cached activates by name without a restart (the runtime otherwise picks up disk drops only on `project.show` / WebUI Refresh; see `skills.md`). A genuinely unknown name still returns `skill_not_found` after that single rescan. The session-pinned prompt catalog is deliberately left untouched — only activation is made live, no availability note is emitted. **List mode (no `name`) does not rescan.**

## Constraints & Gotchas

- **The loaded envelope shape (`data.name` / `data.status == "loaded"` / `data.content`) is a persisted read contract**: `core/sessions/` parses it (`skill_tool_activation`) for once-per-session dedup, the post-compaction skill re-injection, and usage statistics. Renaming those fields breaks activation scanning over existing sessions.
- When `skill` is not in the agent's `allowed_tools`, the tool is not offered; when it is, an empty `allowed_skills` still blocks activation (nothing to load).
