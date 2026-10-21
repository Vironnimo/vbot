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

export function loadChatTimeline(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Chat timeline request must be an object',
    'chat.timeline',
  );
  return rpc('chat.timeline', params, options);
}

export function loadChatTimelineChildren(params, options = {}) {
  return rpc('chat.timeline_children', params, options);
}

export function loadReflectionRuns(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Reflection request must be an object',
    'chat.reflections',
  );
  return rpc('chat.reflections', params, options);
}

// The Memory and Skill changes one Run of an Identity Agent made (a background
// review, for example), and the undo of all of them.
function requireLearningRun(agentId, runId, method) {
  requireNonEmptyString(agentId, 'Agent id must be a non-empty string', method);
  requireNonEmptyString(runId, 'Run id must be a non-empty string', method);
}

export function loadLearningChanges(agentId, runId, options = {}) {
  requireLearningRun(agentId, runId, 'learning.changes');
  return rpc('learning.changes', { agent_id: agentId, run_id: runId }, options);
}

export function undoLearningChanges(agentId, runId, options = {}) {
  requireLearningRun(agentId, runId, 'learning.undo');
  return rpc('learning.undo', { agent_id: agentId, run_id: runId }, options);
}

export function inspectSubAgentWork(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Sub-agent inspection request must be an object',
    'subagent.inspect',
  );
  return rpc('subagent.inspect', params, options);
}

export function startChatRun(params = {}, options = {}) {
  requirePlainObject(params, 'Chat request must be an object', 'chat.stream');
  return rpc('chat.stream', params, options);
}

export function editChatMessage(params = {}, options = {}) {
  requirePlainObject(params, 'Chat edit must be an object', 'chat.edit');
  return rpc('chat.edit', params, options);
}

// The paging and category filter fields every Session-list request shares.
function sessionListPageParams(query) {
  const params = {};
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
    ['includeChannels', 'include_channels'],
  ]) {
    if (typeof query[queryKey] === 'boolean') {
      params[rpcKey] = query[queryKey];
    }
  }
  return params;
}

// The newest Sessions of every Agent per registered Project, plus a last group
// (`project_id: null`) for those that work in no registered Project.
export function listSessionProjectGroups(query = {}, options = {}) {
  return rpc(
    'session.project_groups',
    sessionListPageParams({ ...query, cursor: null }),
    options,
  );
}

// One more page of one Project group: a Project id, or null for the Sessions
// that work in no registered Project.
export function listProjectSessions(projectId, query = {}, options = {}) {
  if (
    projectId !== null &&
    (typeof projectId !== 'string' || projectId.trim().length === 0)
  ) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'A Project id must be a non-empty string or null',
      { method: 'session.list' },
    );
  }
  return rpc(
    'session.list',
    { working_project_id: projectId, ...sessionListPageParams(query) },
    options,
  );
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
  Object.assign(params, sessionListPageParams(query));
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

// Read one live Session's list summary by exact address; `session` is null
// when the Session does not exist.
export function getSession(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.get',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.get',
  );
  return rpc(
    'session.get',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

// Read the lines one live Session's own Runs changed, with its changed files;
// `change_stats` is null when the Session changed no file. With
// `includeSubAgents`, its Sub-Agents' Runs at every depth count as its own and
// `subagents` says how many Sub-Agent Sessions that merged.
export function getSessionChangeStats(
  agentId,
  sessionId,
  { includeSubAgents = false } = {},
  options = {},
) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.change_stats',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.change_stats',
  );
  return rpc(
    'session.change_stats',
    {
      agent_id: agentId,
      session_id: sessionId,
      ...(includeSubAgents ? { include_subagents: true } : {}),
    },
    options,
  );
}

// List one Session's Sub-Agents at every depth, breadth first: each names its
// Session and the `parent_id` of the Sub-Agent it works for (null for a direct
// Sub-Agent of the Session).
export function getSubAgentTree(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'subagent.tree',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'subagent.tree',
  );
  return rpc(
    'subagent.tree',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
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

// Sets or clears (`null`) some of one Session's Agent overrides (`model`,
// `thinking_effort`, ...); fields left out keep their value. Resolves to the
// stored `agent_overrides` and their `effective` values.
export function setSessionAgentOverrides(
  agentId,
  sessionId,
  agentOverrides,
  options = {},
) {
  const { remember = false, ...transportOptions } = options;
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.set_agent_overrides',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'session.set_agent_overrides',
  );
  requirePlainObject(
    agentOverrides,
    'Agent overrides must be an object',
    'session.set_agent_overrides',
  );
  return rpc(
    'session.set_agent_overrides',
    {
      agent_id: agentId,
      session_id: sessionId,
      agent_overrides: agentOverrides,
      ...(remember ? { remember: true } : {}),
    },
    transportOptions,
  );
}

// Archives the Session; `permanent` deletes it right away instead.
export function deleteSession(
  agentId,
  sessionId,
  { permanent = false } = {},
  options = {},
) {
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
    {
      agent_id: agentId,
      session_id: sessionId,
      ...(permanent === true ? { permanent: true } : {}),
    },
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

// Complete live work of a Session and its descendants, independent of History.
export function getSessionWork(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'chat.work',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.work',
  );
  return rpc(
    'chat.work',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

// Stops a Session's Run, its background commands and terminals, and every
// Sub-Agent below it. Resolves to `{ ok, stopped }`.
export function stopAll(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'chat.stop_all',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.stop_all',
  );

  return rpc(
    'chat.stop_all',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

// Stops one background command of a Session for the user, or the processes
// it left running after its shell exited; a stopped command's Agent then
// receives the stopped result. Resolves to `{ stopped, status }`: `stopped`
// is false when nothing of the command ran any longer, `status` is its
// status afterwards.
export function stopCommand(sessionId, commandId, options = {}) {
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.stop_command',
  );
  requireNonEmptyString(
    commandId,
    'Command id must be a non-empty string',
    'chat.stop_command',
  );

  return rpc(
    'chat.stop_command',
    { session_id: sessionId, command_id: commandId },
    options,
  );
}

// Reads a background command's status and its newest output lines as plain
// text: `{ status, still_running, output }`; `still_running` is true while
// processes it started still run after its shell exited.
export function loadCommandOutput(sessionId, commandId, options = {}) {
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'chat.command_output',
  );
  requireNonEmptyString(
    commandId,
    'Command id must be a non-empty string',
    'chat.command_output',
  );

  return rpc(
    'chat.command_output',
    { session_id: sessionId, command_id: commandId },
    options,
  );
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

export function steerQueueItem(agentId, sessionId, itemId, options = {}) {
  for (const [name, value] of Object.entries({
    agent_id: agentId,
    session_id: sessionId,
    item_id: itemId,
  })) {
    requireNonEmptyString(
      value,
      `${name} must be a non-empty string`,
      'chat.queue_steer',
    );
  }
  return rpc(
    'chat.queue_steer',
    {
      agent_id: agentId,
      session_id: sessionId,
      item_id: itemId,
    },
    options,
  );
}

// The open Portals of one Session (`portal` Tool): `{ portals: [{ id, title,
// path, target_port, listen_port, listening, token }], reverse_proxy }`.
// `token` opens a Portal for this browser; keep it in memory only.
export function listPortals(agentId, sessionId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'portal.list',
  );
  requireNonEmptyString(
    sessionId,
    'Session id must be a non-empty string',
    'portal.list',
  );
  return rpc(
    'portal.list',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

// Closes one Portal; the app behind it keeps running. Resolves to
// `{ id, closed }`, where `closed` is false when it was no longer open.
export function closePortal(portalId, options = {}) {
  requireNonEmptyString(
    portalId,
    'Portal id must be a non-empty string',
    'portal.close',
  );
  return rpc('portal.close', { id: portalId }, options);
}
