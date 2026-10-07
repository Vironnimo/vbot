# Archived History Tool

The built-in `history` Tool is retired (user decision 2026-10-04). It read the
current Session's original records hidden by Compaction, and Chat granted it to a
Session from its first persisted `compaction_checkpoint`. That grant changed the
Session's Tool list mid-Session at the first Compaction and broke the Provider
prompt cache.

The `vbot-docs` Skill's Session search reference
(`resources/skills/vbot-docs/references/session-search.md`, `session_search.md` ->
Extended inspection) replaces it: a read-only SQLite query through the shell Tool
reads the current Session's complete transcript, Messages before any Compaction
checkpoint included, and exact Tool Results. The shell Tool names the current
Session in `VBOT_RUN_AGENT_ID`, `VBOT_RUN_SESSION_ID` and `VBOT_RUN_PROJECT_ID`.
That path requires the shell Tool; `session_search` excludes the current Session
and stays unchanged.

`archive/history.zip` preserves the implementation (`core/tools/history.py`,
`core/tools/_history_protocol.py`), its focused tests, the prior domain map, the
Provider probe scenario and the original shared integration files at their
repository paths: Tool registration, the Chat grant and checkpoint guidance, the
Session store's History reads, the compaction event field, the E2E spec and the
tests that exercised them. Its manifest records the source commit and SHA-256
hashes. The archive is outside runtime discovery and excluded from source
distributions. Restore only in a worktree and reconcile shared files with current
source.

What changed with the retirement:

- Chat derives no Session grant from checkpoints; Session grants come only from an
  Extension's Session capability (`chat/request-building.md`). Compaction leaves the
  offered Tool list unchanged.
- New checkpoints carry guidance without the Tool (`compaction.md` -> Agent-facing
  text). Already persisted checkpoints keep their text that names `history`; a call
  to it fails as an unknown Tool. Persisted Session history is not rewritten.
- `compaction_completed` no longer carries `history_available`.
- The Session store lost its History-only reads (`history_snapshot`,
  `history_records`, `history_section_stats`, `history_around`) and their
  `SessionHistory*` types; the database schema is unchanged.

Runtime inventory and Provider-definition tests in
`tests/core/runtime/test_runtime_wiring.py` verify that startup no longer registers
`history`. Restart the server to remove an already loaded Tool. An Agent policy that
names `history` keeps the unknown name, as for any unavailable Tool (`agent.md`).
