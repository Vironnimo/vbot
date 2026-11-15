import { qualifyAgentAddress } from './agentAddress.js';

// Pending requests carry their server-owned Parent chain, nearest first.
// Match full Session addresses: Agent ids and Session ids alone are not unique.
export const approvalSessionKey = (session) =>
  `${qualifyAgentAddress(session.agent_id, session.project_id)}::${session.session_id}`;

export const approvalSessions = (request) => [
  request,
  ...(request.ancestors ?? []),
];

export function sessionApprovalRequests(requests, agentId, sessionId) {
  if (!agentId || !sessionId) return [];
  const key = `${agentId}::${sessionId}`;
  return requests.filter((request) =>
    approvalSessions(request).some(
      (session) => approvalSessionKey(session) === key,
    ),
  );
}

export function approvalRootTarget(request) {
  const root = request.ancestors?.at(-1) ?? request;
  return {
    agentId: qualifyAgentAddress(root.agent_id, root.project_id),
    sessionId: root.session_id,
  };
}
