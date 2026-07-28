# Skill Tool

Tool that lists allowed Skills, activates their instructions for the current Session, and reads their UTF-8 package files by relative path. An ordinary allow-list tool — toggleable per Agent via `allowed_tools` like any Tool, and seeded default-on in the Project Tool Whitelist (`PROJECT_DEFAULT_ALLOWED_TOOLS`).

## Interfaces

- Tool name: `skill`
- Registration: `register_skill_tool(registry, resolve_registry, refresh_skills)` — `resolve_registry` maps `(skill_project_id, agent_id)` to the registry a call activates against; `refresh_skills` rescans skills from disk (wired to `Runtime.reload_skills`, invoked once on a name miss).
- Schema: optional `name` and optional `file_path`; no `name` means list mode, `name` alone means activation, and `name` plus `file_path` means file-read mode. `file_path` accepts `SKILL.md` or a relative path below `scripts/`, `references/`, or `assets/` and is invalid without a non-empty `name`; `additionalProperties: false`.
- Display: summary fields `name`, `file_path`.
- `ToolContext.activate_skill(name, content) -> bool | None` — dedup-only session hook (`ChatSession.register_skill_activation`): `True` fresh, `False` already active, `None` no hook (treated as fresh). Nothing is persisted through the hook — the tool result itself is the durable carrier.

## Conventions

- `skill` is governed by both lists: `allowed_tools` decides whether the tool is offered at all (the normal per-agent toggle), and `allowed_skills` decides which skills it may activate.
- **The activation result IS the content carrier.** A fresh `skill(name)` activation returns `data: {name, status: "loaded", content}` where `content` is the full `<skill_content>` wrapper — so the Skill instructions sit in the conversation exactly where the load happened and replay verbatim like any other Tool result (prompt-cache friendly, no separate placement machinery). The wrapper states the absolute Skill directory, lists `scripts/` files with absolute paths for direct `bash` execution, and lists `references/` and `assets/` files with relative paths for reads through this Tool; a `{baseDir}` marker in the body is substituted with that directory (see `skills.md`).
- **File reads are relative and non-activating.** `skill(name, file_path)` confines the path to the resolved Skill directory and supported package shape, rejects missing or non-UTF-8 files, and returns `data: {name, status: "file_loaded", file_path, content}`. The result contains no absolute path and does not call the Session activation hook, so support-file reads remain separately trackable without affecting activation dedup.
- An already-active skill returns a short stub (`status: "already_active"`, `message`, no content) — the instructions are already in context (live history, or the post-compaction re-injection; see `chat.md`).
- **Rescan on name miss.** When a requested `name` is absent from the resolved registry, the Tool calls `refresh_skills` once and re-resolves before giving up, so a Skill hand-dropped after the Session registry was cached can activate or serve a file without a restart. A genuinely unknown name still returns `skill_not_found` after that single rescan. The Session-pinned prompt catalog is deliberately left untouched — only the live lookup is refreshed, with no availability note. **List mode (no `name`) does not rescan.**

## Constraints & Gotchas

- **The loaded envelope shape (`data.name` / `data.status == "loaded"` / `data.content`) is a persisted read contract**: `core/sessions/` parses it (`skill_tool_activation`) for once-per-session dedup, the post-compaction skill re-injection, and usage statistics. Renaming those fields breaks activation scanning over existing sessions.
- When `skill` is not in the agent's `allowed_tools`, the tool is not offered; when it is, an empty `allowed_skills` still blocks activation (nothing to load).
