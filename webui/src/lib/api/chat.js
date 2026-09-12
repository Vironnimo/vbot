import {
  rpc,
  requirePlainObject,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  ApiClientError,
  requireNonEmptyString,
  isNonEmptyString,
} from './transport.js';
import { isPlainObject } from '../values.js';

export function listChatCommands(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Chat command filters must be an object',
    'chat.commands',
  );
  return rpc('chat.commands', params, options);
}

export function loadChatRunResult(params, options = {}) {
  return rpc('chat.run_result', params, options);
}

export function loadChatHistory(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Chat history request must be an object',
    'chat.history',
  );
  return rpc('chat.history', params, options);
}

export function loadReflectionRuns(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Reflection request must be an object',
    'chat.reflections',
  );
  return rpc('chat.reflections', params, options);
}

export function inspectSubAgentWork(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Sub-agent inspection request must be an object',
    'subagent.inspect',
  );
  return rpc('subagent.inspect', params, options);
}

export function createSession(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Session create request must be an object',
    'session.create',
  );
  return rpc('session.create', params, options);
}

export function startChatRun(params = {}, options = {}) {
  requirePlainObject(params, 'Chat request must be an object', 'chat.stream');
  return rpc('chat.stream', params, options);
}

export function editChatMessage(params = {}, options = {}) {
  requirePlainObject(params, 'Chat edit must be an object', 'chat.edit');
  return rpc('chat.edit', params, options);
}

export function listSessions(agentIds, query = {}, options = {}) {
  const addresses = Array.isArray(agentIds) ? agentIds : [agentIds];
  if (
    addresses.some(
      (agentId) => typeof agentId !== 'string' || agentId.trim().length === 0,
    )
  ) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent ids must be non-empty strings',
      { method: 'session.list' },
    );
  }
  const params = Array.isArray(agentIds)
    ? { agent_ids: addresses }
    : { agent_id: agentIds };
  if (Number.isSafeInteger(query.limit) && query.limit > 0) {
    params.limit = query.limit;
  }
  if (query.cursor && typeof query.cursor === 'object') {
    params.cursor = query.cursor;
  }
  for (const [queryKey, rpcKey] of [
    ['includeSubagents', 'include_subagents'],
    ['includeMemoryReflections', 'include_memory_reflections'],
    ['includeSkillReflections', 'include_skill_reflections'],
    ['includeCron', 'include_cron'],
  ]) {
    if (typeof query[queryKey] === 'boolean') {
      params[rpcKey] = query[queryKey];
    }
  }
  if (
    query.requiredSession &&
    typeof query.requiredSession.agentId === 'string' &&
    typeof query.requiredSession.sessionId === 'string'
  ) {
    params.required_session = {
      agent_id: query.requiredSession.agentId,
      session_id: query.requiredSession.sessionId,
    };
  }
  return rpc('session.list', params, options);
}

export function listSessionActivity(agentIds, options = {}) {
  if (
    !Array.isArray(agentIds) ||
    agentIds.some(
      (agentId) => typeof agentId !== 'string' || agentId.trim().length === 0,
    )
  ) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent ids must be a list of non-empty strings',
      { method: 'session.activity_list' },
    );
  }
  return rpc('session.activity_list', { agent_ids: agentIds }, options);
}

export function markSessionRead(agentId, sessionId, runId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.mark_read',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.mark_read',
  );
  requireNonEmptyString(
    runId,
    'Run id must be a non-empty string',
    'session.mark_read',
  );
  return rpc(
    'session.mark_read',
    { agent_id: agentId, session_id: sessionId, run_id: runId },
    options,
  );
}

export function renameSession(agentId, sessionId, title, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.rename',
  );

  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.rename',
  );

  // An empty title is the explicit "clear the name" signal, so the title is
  // sent as-is (coerced to a string) rather than validated as non-empty.
  return rpc(
    'session.rename',
    { agent_id: agentId, session_id: sessionId, title: String(title ?? '') },
    options,
  );
}

export function setSessionCompactionPolicy(
  agentId,
  sessionId,
  policy,
  options = {},
) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.set_compaction_policy',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.set_compaction_policy',
  );
  return rpc(
    'session.set_compaction_policy',
    { agent_id: agentId, session_id: sessionId, policy: policy ?? null },
    options,
  );
}

export function deleteSession(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.delete',
  );

  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.delete',
  );

  return rpc(
    'session.delete',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

export function listQueue(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'chat.queue_list',
  );

  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.queue_list',
  );

  return rpc(
    'chat.queue_list',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

export function cancelRun(runId, options = {}, rpcOptions = {}) {
  requireNonEmptyString(
    runId,
    'Run id must be a non-empty string',
    'chat.cancel',
  );

  const params = { run_id: runId };
  const reason = isPlainObject(options) ? options.reason : null;
  if (isNonEmptyString(reason)) {
    params.reason = reason;
  }

  return rpc('chat.cancel', params, rpcOptions);
}

export function controlRun(
  { agentId, sessionId, runId, action, toolCallId } = {},
  options = {},
) {
  const params = {
    agent_id: agentId,
    session_id: sessionId,
    run_id: runId,
    action,
  };
  for (const value of Object.values(params)) {
    requireNonEmptyString(
      value,
      'Run control requires an address, Run id and action',
      'chat.control_run',
    );
  }
  if (toolCallId) params.tool_call_id = toolCallId;
  return rpc('chat.control_run', params, options);
}

export function cancelToolCall(
  { agentId, runId, toolCallId } = {},
  options = {},
) {
  requireNonEmptyString(
    runId,
    'Run id must be a non-empty string',
    'chat.cancel_tool_call',
  );

  requireNonEmptyString(
    toolCallId,
    'Tool call id must be a non-empty string',
    'chat.cancel_tool_call',
  );

  const params = { run_id: runId, tool_call_id: toolCallId };
  if (isNonEmptyString(agentId)) {
    params.agent_id = agentId;
  }

  return rpc('chat.cancel_tool_call', params, options);
}

export function cancelProcess({ agentId, processId } = {}, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'chat.cancel_process',
  );
  requireNonEmptyString(
    processId,
    'Process id must be a non-empty string',
    'chat.cancel_process',
  );
  return rpc(
    'chat.cancel_process',
    { agent_id: agentId, process_id: processId },
    options,
  );
}

export function removeFromQueue(agentId, sessionId, itemId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'chat.queue_remove',
  );

  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.queue_remove',
  );

  requireNonEmptyString(
    itemId,
    'Queue item id must be a non-empty string',
    'chat.queue_remove',
  );

  return rpc(
    'chat.queue_remove',
    { agent_id: agentId, session_id: sessionId, item_id: itemId },
    options,
  );
}

export function updateQueueItem(
  agentId,
  sessionId,
  itemId,
  content,
  options = {},
) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'chat.queue_update',
  );

  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.queue_update',
  );

  requireNonEmptyString(
    itemId,
    'Queue item id must be a non-empty string',
    'chat.queue_update',
  );

  if (!(isNonEmptyString(content) || Array.isArray(content))) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Queue item content must be a non-empty string or content block list',
      {
        method: 'chat.queue_update',
      },
    );
  }

  // `fileMentions` rides in the options bag (the tail params are transport
  // options); it becomes the RPC's `file_mentions` param, not a fetch option.
  const { fileMentions, ...requestOptions } = options;
  const params = {
    agent_id: agentId,
    session_id: sessionId,
    item_id: itemId,
    content,
  };
  if (Array.isArray(fileMentions) && fileMentions.length > 0) {
    params.file_mentions = fileMentions;
  }
  return rpc('chat.queue_update', params, requestOptions);
}
