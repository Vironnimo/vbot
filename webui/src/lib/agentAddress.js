// The one client-side parse/format seam for the `agent@projekt` address form.
//
// This mirrors `core/projects/address.py` (the single server-side seam): the
// outside spelling of a project agent is `agent@projekt`; a bare `agent` (no
// `@`) is an identity agent. Cron and Statistics consume this so neither
// re-derives the `@` grammar per call site.
//
// `chatState.js` keeps its own `formatAgentAddress` (Phase 2) intentionally: the
// chat path is byte-identical to today and re-pointing it at this seam risks the
// Phase-2 tests for no behavioral gain. The two stay in lockstep by sharing the
// same single separator literal documented below.

// The separator between the agent id and the project id in the outside address
// form. Mirrors `core/projects/address.py` `_ADDRESS_SEPARATOR`.
export const AGENT_ADDRESS_SEPARATOR = '@';

// Build the outside `agent@projekt` address from a bare agent id and an optional
// project id. A null/empty project id yields the bare agent id (identity
// spelling — unchanged from today); a set project id yields `agent@projekt`.
// Inverse of `parseAgentAddress`.
export function formatAgentAddress(agentId, projectId) {
  const bareId = typeof agentId === 'string' ? agentId : '';
  const project = typeof projectId === 'string' ? projectId.trim() : '';
  if (!project) {
    return bareId;
  }
  return `${bareId}${AGENT_ADDRESS_SEPARATOR}${project}`;
}

// Parse an outside `agent@projekt` address into `{ agentId, projectId }`.
//
// - No `@` → `{ agentId: address, projectId: null }` (identity address,
//   unchanged behavior — a bare `builder` keeps `projectId` null).
// - One `@` with non-empty parts → `{ agentId, projectId }`.
// - Anything else (empty, more than one `@`, or an empty part around the `@`)
//   is treated defensively as an identity address: the raw string is the agent
//   id and the project id is null. This is display/format glue, not the server
//   validator — the server's `parse_agent_address` is the authority that rejects
//   malformed input; the client only needs a safe, lossless split for rendering.
export function parseAgentAddress(address) {
  const raw = typeof address === 'string' ? address : '';
  if (!raw || !raw.includes(AGENT_ADDRESS_SEPARATOR)) {
    return { agentId: raw, projectId: null };
  }

  const parts = raw.split(AGENT_ADDRESS_SEPARATOR);
  if (parts.length !== 2 || parts[0].length === 0 || parts[1].length === 0) {
    return { agentId: raw, projectId: null };
  }

  return { agentId: parts[0], projectId: parts[1] };
}
