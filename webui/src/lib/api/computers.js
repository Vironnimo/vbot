import { rpc, requireNonEmptyString, requirePlainObject } from './transport.js';

export function listComputers(options = {}) {
  return rpc('computers.list', {}, options);
}

export function removeComputer(computerId, options = {}) {
  requireNonEmptyString(
    computerId,
    'Computer id must be a non-empty string',
    'computers.remove',
  );
  return rpc('computers.remove', { id: computerId }, options);
}

export function setSessionExecution(
  agentId,
  sessionId,
  execution,
  options = {},
) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.set_execution',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.set_execution',
  );
  requirePlainObject(
    execution,
    'Execution must be an object',
    'session.set_execution',
  );
  return rpc(
    'session.set_execution',
    {
      agent_id: agentId,
      session_id: sessionId,
      execution,
    },
    options,
  );
}
