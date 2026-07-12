# Workspace and Rooted Identity Agent Handoff

**Status:** Superseded by the implemented explicit Rooted-Agent contract on 2026-07-12.

The earlier discovery checkpoint and its open decisions are resolved by `.vorch/plans/rooted-agent-project-selection/README.md` and the verified source/domain contracts. Rooting is now an explicit nullable Project selection on each Identity Agent; Workspace remains the freely editable identity and Memory home, while the selected Project supplies cwd, Project Files, and Project Skills.

Identity Sessions and public addressing remain owned by the Identity Agent. Accepted work snapshots the selected Project internally without exposing it as Session ownership. Project re-pointing affects later Run starts; a missing Project or repository fails closed without Workspace fallback. Project removal blocks active/queued use, then clears all references and resets affected Agents to Default Workspace with one aggregate identity-file copy choice.

The Agent editor exposes `Project` directly below Workspace with `No project`, no Agent-list or Chat indicator, and manual save. Every changed Workspace asks Copy files / Don't copy / Cancel; copying is limited to `SOUL.md`, `USER.md`, and `MEMORY.md`, preserves sources, backs up replaced destinations, and rolls back on failure.

Project Config-Agent ceilings and Team discovery remain separate from Rooted Identity Agents. Bare Subagent targets retain Identity addressing; `agent@project` remains the explicit Project-Agent address.
