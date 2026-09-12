import { formatAgentAddress, parseAgentAddress } from '../agentAddress.js';
import { isSessionHiddenByDefault } from '../sessionListView.js';

// --- Two-bar project chat helpers -----------------------------------------
//
// The chat has two agent bars: the always-present identity bar (today's
// behavior, unchanged) and a second project team bar that appears when a
// project is chosen from the dropdown. These pure helpers own the project
// addressing and project-agent session selection so the component stays thin.
//
// The single hard rule: with NO project chosen (Personal, `projectId` empty),
// an identity agent's outside address equals its bare id (no `@projekt`), so
// every RPC payload is byte-identical to today. The trap discipline only kicks
// in once a project agent is in play.

// Whether a selected project id means "a real project" (vs. Personal/empty).
export function isProjectSelected(projectId) {
  return typeof projectId === 'string' && projectId.trim().length > 0;
}

// Resolve the addressing for an active agent in either bar.
//
// - identity agent (no project): `{ agentAddress: id, bareAgentId: id,
//   projectId: null }` — `agentAddress === bareAgentId`, so the byte-identical
//   regression holds.
// - project agent: `{ agentAddress: 'agent@projekt', bareAgentId: 'agent',
//   projectId }` — the full address goes to chat/session/history, the bare id
//   to queue/cancel-tool (trap 2).
//
// `projectId` empty → identity, regardless of `isProjectAgent` (defensive: a
// Personal selection never produces a project address).
export function resolveAgentAddressing(agentId, projectId, isProjectAgent) {
  const bareAgentId = typeof agentId === 'string' ? agentId : '';
  const project =
    isProjectAgent && isProjectSelected(projectId) ? projectId.trim() : null;
  return {
    bareAgentId,
    projectId: project,
    agentAddress: formatAgentAddress(bareAgentId, project),
  };
}

// Pick the project-agent session to open from a `session.list` result.
//
// A project (config) agent has NO server-tracked `current_session_id` (trap 1):
// `session.create` only sets make-current for identity. So the accessor chooses
// the session itself — the most recently active user-facing one from
// `session.list`, by `last_active_at` (falling back to `created_at`, then list
// order). Sub-agent Sessions are execution artifacts rather than an Agent-bar
// landing target. Returns the session id string, or '' when there are no
// user-facing sessions (the caller then creates one via `session.create`).
export function pickProjectAgentSessionId(sessions) {
  const list = Array.isArray(sessions) ? sessions : [];
  let best = null;
  let bestTime = -Infinity;
  for (const session of list) {
    if (isSessionHiddenByDefault(session)) {
      continue;
    }
    const sessionId = typeof session?.id === 'string' ? session.id.trim() : '';
    if (!sessionId) {
      continue;
    }
    const time = sessionSortTime(session);
    // `>=` so a later equal-or-newer entry wins, keeping list order as the
    // final tiebreak (the server lists newest-relevant deterministically).
    if (best === null || time >= bestTime) {
      best = sessionId;
      bestTime = time;
    }
  }
  return best ?? '';
}

function sessionSortTime(session) {
  const lastActive = parseSessionTimestamp(session?.last_active_at);
  if (lastActive !== null) {
    return lastActive;
  }
  const created = parseSessionTimestamp(session?.created_at);
  if (created !== null) {
    return created;
  }
  return -Infinity;
}

function parseSessionTimestamp(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

// --- /agent move-action routing -------------------------------------------
//
// `/agent <addr> [task]` MOVES the current session (same session id) to another
// agent. The command response carries `{ command: "agent", session_id, agent_id }`
// where `agent_id` is the target's outside address: a bare id (identity target)
// or `agent@projekt` (project/team target). The presence of `@` is the single
// signal that decides which world the target lives in — parsed through the one
// shared `agentAddress.js` seam, never a hand-rolled split.
//
// Returns the decision the accessor needs to open the SAME session under the
// target, for all four directions (identity↔project, both ways): the world
// (`'identity'` | `'project'`), the bare agent id, the project id (null for
// identity), and the full address to key project session state by. Returns null
// when the response is not a usable move (missing command/session/agent).

// Build the move decision from a command-handled response. Null when the
// response is not a `/agent` move or is missing the session/target.
export function resolveMoveActionFromResponse(response) {
  const data = response?.data;
  if (!data || data.command !== 'agent') {
    return null;
  }
  const sessionId =
    typeof data.session_id === 'string' ? data.session_id.trim() : '';
  const targetAddress =
    typeof data.agent_id === 'string' ? data.agent_id.trim() : '';
  if (!sessionId || !targetAddress) {
    return null;
  }
  return { ...resolveMoveTarget(targetAddress), sessionId };
}

// Decide the target world for a `/agent` move from its outside address.
// `agent@projekt` → project world; a bare id → identity world. The split uses
// the shared `parseAgentAddress` seam so the `@` grammar is never re-derived.
export function resolveMoveTarget(targetAddress) {
  const address = typeof targetAddress === 'string' ? targetAddress.trim() : '';
  const { agentId, projectId } = parseAgentAddress(address);
  const isProjectTarget = typeof projectId === 'string' && projectId.length > 0;
  return {
    isProjectTarget,
    world: isProjectTarget ? 'project' : 'identity',
    bareAgentId: agentId,
    projectId: isProjectTarget ? projectId : null,
    agentAddress: address,
  };
}
