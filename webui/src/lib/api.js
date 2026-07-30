import {
  resolveAccessorType,
  resolveClientConnectionId,
} from './clientIdentity.js';
import {
  buildProviderConnectPayload,
  buildProviderDisconnectPayload,
} from './settingsView.js';

const RPC_ENDPOINT = '/api/rpc';
const ATTACHMENT_UPLOAD_ENDPOINT = '/api/upload';
const ATTACHMENT_BASE_ENDPOINT = '/api/attachments';
const SPEECH_TRANSCRIBE_ENDPOINT = '/api/speech/transcribe';
const WEBSOCKET_ENDPOINT = '/ws';
const LOGS_WEBSOCKET_ENDPOINT = '/ws/logs';

export const RPC_ERROR_INVALID_CLIENT_REQUEST = 'invalid_client_request';
export const RPC_ERROR_NETWORK = 'network_error';
export const RPC_ERROR_HTTP = 'http_error';
export const RPC_ERROR_RESPONSE = 'invalid_rpc_response';
export const SSE_ERROR_RESPONSE = 'invalid_sse_event';
export const WEBSOCKET_ERROR_RESPONSE = 'invalid_websocket_event';

export const RUN_EVENT_ASSISTANT_OUTPUT_DELTA = 'assistant_output_delta';
export const RUN_EVENT_REASONING_DELTA = 'reasoning_delta';
export const RUN_EVENT_TOOL_CALL_DELTA = 'tool_call_delta';
export const RUN_EVENT_TOOL_CALL_STDOUT = 'tool_call_stdout';
export const RUN_EVENT_TOOL_CALL_STDERR = 'tool_call_stderr';
export const RUN_EVENT_PROVIDER_HEARTBEAT = 'provider_heartbeat';
export const RUN_STREAM_HEARTBEAT_EVENT = 'heartbeat';

export const RUN_EVENT_TYPES = [
  'run_started',
  'user_message_persisted',
  'model_fallback_activated',
  'error_message_persisted',
  'compaction_started',
  'compaction_aborted',
  'compaction_completed',
  RUN_EVENT_REASONING_DELTA,
  'reasoning',
  RUN_EVENT_TOOL_CALL_DELTA,
  'tool_call_started',
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_TOOL_CALL_STDERR,
  'tool_call_result',
  'subagent_session_started',
  'subagent_status_changed',
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  'assistant_output',
  'model_step_usage',
  RUN_EVENT_PROVIDER_HEARTBEAT,
  'run_completed',
  'run_cancelled',
  'run_failed',
];

const TERMINAL_RUN_EVENT_TYPES = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
]);

export class ApiClientError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = 'ApiClientError';
    this.code = code;
    this.status = options.status ?? null;
    this.method = options.method ?? null;
    this.details = options.details ?? null;
    this.cause = options.cause ?? null;
  }
}

export function createRpcEnvelope(method, params = {}) {
  if (typeof method !== 'string' || method.length === 0) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'RPC method must be a non-empty string',
    );
  }
  if (!isPlainObject(params)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'RPC params must be an object',
      {
        method,
      },
    );
  }
  return { method, params };
}

export async function rpc(method, params = {}, options = {}) {
  const envelope = createRpcEnvelope(method, params);
  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method,
    });
  }

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(options.rpcPath ?? RPC_ENDPOINT, options.baseUrl),
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(options.headers ?? {}),
        },
        body: JSON.stringify(envelope),
        signal: options.signal,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'RPC request failed before a response arrived',
      {
        method,
        cause: error,
      },
    );
  }

  const payload = await readRpcPayload(response, method);
  if (!response.ok) {
    throw normalizeRpcError(payload.error, {
      method,
      status: response.status,
      fallbackCode: RPC_ERROR_HTTP,
      fallbackMessage: `RPC request failed with HTTP ${response.status}`,
    });
  }
  if (!isPlainObject(payload) || typeof payload.ok !== 'boolean') {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'RPC response must include an ok flag',
      {
        method,
        status: response.status,
        details: payload,
      },
    );
  }
  if (!payload.ok) {
    throw normalizeRpcError(payload.error, { method, status: response.status });
  }
  return payload.result;
}

export function getSettings(options = {}) {
  return rpc('settings.get', {}, options);
}

export function updateSettings(settings, options = {}) {
  requirePlainObject(
    settings,
    'Settings update must be an object',
    'settings.update',
  );
  return rpc('settings.update', settings, options);
}

export function listAgents(options = {}) {
  return rpc('agent.list', {}, options);
}

export function reorderAgents(agentIds, expectedRevision, options = {}) {
  if (
    !Array.isArray(agentIds) ||
    agentIds.some((agentId) => typeof agentId !== 'string' || !agentId)
  ) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent order must be a list of non-empty ids',
      { method: 'agent.reorder' },
    );
  }
  if (!Number.isInteger(expectedRevision) || expectedRevision < 0) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent order revision must be a non-negative integer',
      { method: 'agent.reorder' },
    );
  }
  return rpc(
    'agent.reorder',
    { agent_ids: agentIds, expected_revision: expectedRevision },
    options,
  );
}

export function getAgent(id, options = {}) {
  requireNonEmptyString(id, 'Agent id must be a non-empty string', 'agent.get');
  return rpc('agent.get', { id }, options);
}

export function createAgent(params = {}, options = {}) {
  requirePlainObject(params, 'Agent payload must be an object', 'agent.create');
  return rpc('agent.create', params, options);
}

export function updateAgent(params = {}, options = {}) {
  requirePlainObject(params, 'Agent payload must be an object', 'agent.update');
  return rpc('agent.update', params, options);
}

export function renameAgent(id, newId, options = {}) {
  requireNonEmptyString(
    id,
    'Agent id must be a non-empty string',
    'agent.rename',
  );
  requireNonEmptyString(
    newId,
    'New agent id must be a non-empty string',
    'agent.rename',
  );
  return rpc('agent.rename', { id, new_id: newId }, options);
}

export function deleteAgent(id, options = {}) {
  requireNonEmptyString(
    id,
    'Agent id must be a non-empty string',
    'agent.delete',
  );
  return rpc('agent.delete', { id }, options);
}

export function listModels(params = {}, options = {}) {
  requirePlainObject(params, 'Model filters must be an object', 'model.list');
  return rpc('model.list', params, options);
}

export function refreshModelDatabase(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Model refresh options must be an object',
    'model.refresh_db',
  );
  return rpc('model.refresh_db', params, options);
}

export function listConnections(options = {}) {
  return rpc('connection.list', {}, options);
}

export function listProviderRoutingOptions(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Provider routing filters must be an object',
    'provider.routing_options',
  );
  return rpc('provider.routing_options', params, options);
}

export function setConnectionEnabled(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Connection update must be an object',
    'connection.set_enabled',
  );
  return rpc('connection.set_enabled', params, options);
}

export function listTools(options = {}) {
  return rpc('tool.list', {}, options);
}

export function listSkills(params = {}, options = {}) {
  requirePlainObject(params, 'Skill filters must be an object', 'skill.list');
  return rpc('skill.list', params, options);
}

export function readSkills(scope, options = {}) {
  requireNonEmptyString(
    scope,
    'Skill scope must be a non-empty string',
    'skill.read',
  );
  return rpc('skill.read', { scope }, options);
}

export function createSkill(params = {}, options = {}) {
  requirePlainObject(params, 'Skill payload must be an object', 'skill.create');
  return rpc('skill.create', params, options);
}

export function updateSkill(params = {}, options = {}) {
  requirePlainObject(params, 'Skill payload must be an object', 'skill.update');
  return rpc('skill.update', params, options);
}

export function deleteSkill(scope, name, options = {}) {
  requireNonEmptyString(
    scope,
    'Skill scope must be a non-empty string',
    'skill.delete',
  );
  requireNonEmptyString(
    name,
    'Skill name must be a non-empty string',
    'skill.delete',
  );
  return rpc('skill.delete', { scope, name }, options);
}

export function listChatCommands(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Chat command filters must be an object',
    'chat.commands',
  );
  return rpc('chat.commands', params, options);
}

export function loadChatHistory(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Chat history request must be an object',
    'chat.history',
  );
  return rpc('chat.history', params, options);
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

export function listFiles(agentId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'files.list',
  );
  return rpc('files.list', { agent_id: agentId }, options);
}

export function listPrompts(params = {}, options = {}) {
  requirePlainObject(params, 'Prompt scope must be an object', 'prompt.list');
  return rpc('prompt.list', params, options);
}

export function updatePromptBlock(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt update must be an object',
    'prompt.update',
  );
  return rpc('prompt.update', params, options);
}

export function resetPromptBlock(params = {}, options = {}) {
  requirePlainObject(params, 'Prompt reset must be an object', 'prompt.reset');
  return rpc('prompt.reset', params, options);
}

export function createPromptBlock(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt block must be an object',
    'prompt.create_block',
  );
  return rpc('prompt.create_block', params, options);
}

export function removePromptBlock(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt block must be an object',
    'prompt.remove_block',
  );
  return rpc('prompt.remove_block', params, options);
}

export function resetPromptLayout(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt scope must be an object',
    'prompt.reset_layout',
  );
  return rpc('prompt.reset_layout', params, options);
}

export function setPromptLayout(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt layout must be an object',
    'prompt.set_layout',
  );
  return rpc('prompt.set_layout', params, options);
}

export function previewPrompt(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt preview must be an object',
    'prompt.preview',
  );
  return rpc('prompt.preview', params, options);
}

export function setProviderKey(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Provider key payload must be an object',
    'provider.set_key',
  );
  return rpc('provider.set_key', params, options);
}

export function unsetProviderKey(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Provider key payload must be an object',
    'provider.unset_key',
  );
  return rpc('provider.unset_key', params, options);
}

export function listCustomProviders(options = {}) {
  return rpc('provider.custom_list', {}, options);
}

export function saveCustomProvider(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Custom Provider payload must be an object',
    'provider.custom_save',
  );
  return rpc('provider.custom_save', params, options);
}

export function deleteCustomProvider(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Custom Provider delete payload must be an object',
    'provider.custom_delete',
  );
  return rpc('provider.custom_delete', params, options);
}

export function getProviderUsage(options = {}) {
  return rpc('provider.usage', {}, options);
}

export function getProviderUsageHistory(params = {}, options = {}) {
  return rpc('provider.usage_history', params, options);
}

export function clearProviderUsageHistory(options = {}) {
  return rpc('provider.usage_history.clear', {}, options);
}

export function listChannels(options = {}) {
  return rpc('channel.list', {}, options);
}

export function getChannelStatus(id, options = {}) {
  requireNonEmptyString(
    id,
    'Channel id must be a non-empty string',
    'channel.status',
  );
  return rpc('channel.status', { id }, options);
}

export function createChannel(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Channel payload must be an object',
    'channel.create',
  );
  return rpc('channel.create', params, options);
}

export function updateChannel(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Channel payload must be an object',
    'channel.update',
  );
  return rpc('channel.update', params, options);
}

export function enableChannel(id, options = {}) {
  requireNonEmptyString(
    id,
    'Channel id must be a non-empty string',
    'channel.enable',
  );
  return rpc('channel.enable', { id }, options);
}

export function disableChannel(id, options = {}) {
  requireNonEmptyString(
    id,
    'Channel id must be a non-empty string',
    'channel.disable',
  );
  return rpc('channel.disable', { id }, options);
}

export function listExtensions(options = {}) {
  return rpc('extensions.list', {}, options);
}

export function reloadExtensions(options = {}) {
  return rpc('extensions.reload', {}, options);
}

export function setExtensionSecret(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Extension secret payload must be an object',
    'extensions.set_secret',
  );
  return rpc('extensions.set_secret', params, options);
}

export function getStatisticsReport(options = {}) {
  return rpc('statistics.report', {}, options);
}

export function getStatisticsRunActivity(params, options = {}) {
  return rpc('statistics.run_activity', params, options);
}

export async function uploadAttachment(file, options = {}) {
  if (!file || typeof file !== 'object') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Attachment file must be provided',
      {
        method: 'upload_attachment',
      },
    );
  }

  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method: 'upload_attachment',
    });
  }

  const formData = new FormData();
  const filename = isNonEmptyString(file.name) ? file.name : 'upload.bin';
  formData.append('file', file, filename);

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(
        options.uploadPath ?? ATTACHMENT_UPLOAD_ENDPOINT,
        options.baseUrl,
      ),
      {
        method: 'POST',
        body: formData,
        signal: options.signal,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'Attachment upload failed before a response arrived',
      {
        method: 'upload_attachment',
        cause: error,
      },
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Attachment upload response body must be valid JSON',
      {
        method: 'upload_attachment',
        status: response.status,
        cause: error,
      },
    );
  }

  if (!response.ok) {
    throw new ApiClientError(
      RPC_ERROR_HTTP,
      isNonEmptyString(payload?.detail)
        ? payload.detail
        : `Attachment upload failed with HTTP ${response.status}`,
      {
        method: 'upload_attachment',
        status: response.status,
        details: isPlainObject(payload) ? payload : null,
      },
    );
  }

  if (
    !isPlainObject(payload) ||
    !isNonEmptyString(payload.attachment_id) ||
    !isNonEmptyString(payload.filename) ||
    !isNonEmptyString(payload.media_type) ||
    typeof payload.size_bytes !== 'number'
  ) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Attachment upload response has an invalid shape',
      {
        method: 'upload_attachment',
        status: response.status,
        details: payload,
      },
    );
  }

  return {
    attachment_id: payload.attachment_id,
    filename: payload.filename,
    media_type: payload.media_type,
    size_bytes: payload.size_bytes,
  };
}

export function updateTaskModelSettings(modelTasks, options = {}) {
  requirePlainObject(
    modelTasks,
    'Task model settings must be an object',
    'task_model.update',
  );
  return rpc('task_model.update', { model_tasks: modelTasks }, options);
}

export function listTaskModelTargets(taskType, options = {}) {
  requireNonEmptyString(
    taskType,
    'Task type must be a non-empty string',
    'task_model.list_targets',
  );
  return rpc('task_model.list_targets', { task_type: taskType }, options);
}

export function getTaskModelOptions(taskType, target, options = {}) {
  if (!isNonEmptyString(taskType) || !isNonEmptyString(target)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Task type and target must be non-empty strings',
      {
        method: 'task_model.options',
      },
    );
  }
  return rpc('task_model.options', { task_type: taskType, target }, options);
}

export async function transcribeSpeech(audioBlob, options = {}) {
  if (!audioBlob || typeof audioBlob !== 'object') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Audio blob must be provided',
      {
        method: 'speech.transcribe',
      },
    );
  }

  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method: 'speech.transcribe',
    });
  }

  const formData = new FormData();
  const filename = isNonEmptyString(options.filename)
    ? options.filename
    : filenameForAudioBlob(audioBlob);
  formData.append('file', audioBlob, filename);

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(
        options.transcribePath ?? SPEECH_TRANSCRIBE_ENDPOINT,
        options.baseUrl,
      ),
      {
        method: 'POST',
        body: formData,
        signal: options.signal,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'Speech transcription failed before a response arrived',
      {
        method: 'speech.transcribe',
        cause: error,
      },
    );
  }

  const payload = await readJsonHttpPayload(response, 'speech.transcribe');
  if (!response.ok) {
    throw new ApiClientError(
      RPC_ERROR_HTTP,
      isNonEmptyString(payload?.detail)
        ? payload.detail
        : `Speech transcription failed with HTTP ${response.status}`,
      {
        method: 'speech.transcribe',
        status: response.status,
        details: isPlainObject(payload) ? payload : null,
      },
    );
  }
  if (!isPlainObject(payload) || typeof payload.text !== 'string') {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Speech transcription response has an invalid shape',
      {
        method: 'speech.transcribe',
        status: response.status,
        details: payload,
      },
    );
  }
  return payload;
}

export function getAttachmentUrl(attachmentId) {
  requireNonEmptyString(
    attachmentId,
    'Attachment id must be a non-empty string',
  );
  return `${ATTACHMENT_BASE_ENDPOINT}/${attachmentId}`;
}

export function listLogs(options = {}) {
  return rpc('log.list', {}, options);
}

export function readLogFile(file, options = {}) {
  requireNonEmptyString(
    file,
    'Log file must be a non-empty string',
    'log.read',
  );

  return rpc('log.read', { file }, options);
}

export function listClients(options = {}) {
  return rpc('client.list', {}, options);
}

export function listCronJobs(options = {}) {
  return rpc('cron.list', {}, options);
}

export function createCronJob(params = {}, options = {}) {
  return rpc('cron.create', params, options);
}

export function updateCronJob(params = {}, options = {}) {
  return rpc('cron.update', params, options);
}

export function deleteCronJob(id, options = {}) {
  requireNonEmptyString(
    id,
    'Cron job id must be a non-empty string',
    'cron.delete',
  );

  return rpc('cron.delete', { id }, options);
}

export function enableCronJob(id, options = {}) {
  requireNonEmptyString(
    id,
    'Cron job id must be a non-empty string',
    'cron.enable',
  );

  return rpc('cron.enable', { id }, options);
}

export function disableCronJob(id, options = {}) {
  requireNonEmptyString(
    id,
    'Cron job id must be a non-empty string',
    'cron.disable',
  );

  return rpc('cron.disable', { id }, options);
}

export function addProject(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Project payload must be an object',
    'project.add',
  );

  requireNonEmptyString(
    params.cwd,
    'Project cwd must be a non-empty string',
    'project.add',
  );

  return rpc('project.add', params, options);
}

export function listProjects(options = {}) {
  return rpc('project.list', {}, options);
}

// Probe a cwd for per-format agent/skill presence and context files. Called by
// the add dialog while the user types a path; a nonexistent cwd is a success
// with `cwd_exists: false`, never an error.
export function detectProject(cwd, options = {}) {
  requireNonEmptyString(
    cwd,
    'Project cwd must be a non-empty string',
    'project.detect',
  );

  return rpc('project.detect', { cwd }, options);
}

export function showProject(projectId, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.show',
  );

  return rpc('project.show', { project_id: projectId }, options);
}

export function setProject(projectId, changes = {}, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.set',
  );

  requirePlainObject(
    changes,
    'Project changes must be an object',
    'project.set',
  );

  return rpc('project.set', { ...changes, project_id: projectId }, options);
}

export function setOverride(projectId, agentId, field, value, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.set_override',
  );

  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'project.set_override',
  );

  requireNonEmptyString(
    field,
    'Override field must be a non-empty string',
    'project.set_override',
  );

  // A per-agent override (model / temperature / thinking_effort) becomes the top
  // tier of that field's resolution chain for this agent in this project. The value
  // shape is field-specific (a model address string, a number, an effort string);
  // the server validates it against the canonical agent rules.
  return rpc(
    'project.set_override',
    { project_id: projectId, agent_id: agentId, field, value },
    options,
  );
}

export function clearOverride(projectId, agentId, field, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.clear_override',
  );

  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'project.clear_override',
  );

  requireNonEmptyString(
    field,
    'Override field must be a non-empty string',
    'project.clear_override',
  );

  // Drop one overridden field for this agent; clearing the agent's last field
  // removes the override entry entirely (server-side). The field falls back through
  // its chain.
  return rpc(
    'project.clear_override',
    { project_id: projectId, agent_id: agentId, field },
    options,
  );
}

export function removeProject(
  projectId,
  copyRootedAgentIdentityFiles = false,
  options = {},
) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.rm',
  );

  const requestOptions =
    copyRootedAgentIdentityFiles &&
    typeof copyRootedAgentIdentityFiles === 'object'
      ? copyRootedAgentIdentityFiles
      : options;
  const copyFiles =
    typeof copyRootedAgentIdentityFiles === 'boolean'
      ? copyRootedAgentIdentityFiles
      : false;

  return rpc(
    'project.rm',
    {
      project_id: projectId,
      copy_rooted_agent_identity_files: copyFiles,
    },
    requestOptions,
  );
}

export function listSessions(agentId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'session.list',
  );

  return rpc('session.list', { agent_id: agentId }, options);
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

export function deleteChannel(channelId, options = {}) {
  requireNonEmptyString(
    channelId,
    'Channel id must be a non-empty string',
    'channel.delete',
  );

  return rpc('channel.delete', { id: channelId }, options);
}

export async function connectProvider(
  providerId,
  connectionId,
  account = undefined,
  options = {},
) {
  return (options.rpc ?? rpc)(
    'provider.connect',
    buildProviderConnectPayload(providerId, connectionId, account),
  );
}

export async function disconnectProvider(
  providerId,
  connectionId,
  account = undefined,
  options = {},
) {
  return (options.rpc ?? rpc)(
    'provider.disconnect',
    buildProviderDisconnectPayload(providerId, connectionId, account),
  );
}

export function debugStatus(options = {}) {
  return rpc('debug.status', {}, options);
}

export function debugTraceList(options = {}) {
  return rpc('debug.trace_list', {}, options);
}

export function debugTraceGet(traceId, options = {}) {
  requireNonEmptyString(
    traceId,
    'Trace id must be a non-empty string',
    'debug.trace_get',
  );

  return rpc('debug.trace_get', { trace_id: traceId }, options);
}

export function debugTraceClear(options = {}) {
  return rpc('debug.trace_clear', {}, options);
}

export function debugModelProbe(providerId, connectionId, options = {}) {
  requireNonEmptyString(
    providerId,
    'Provider id must be a non-empty string',
    'debug.model_probe',
  );

  requireNonEmptyString(
    connectionId,
    'Connection id must be a non-empty string',
    'debug.model_probe',
  );

  return rpc(
    'debug.model_probe',
    { provider_id: providerId, connection_id: connectionId },
    options,
  );
}

export function normalizeRpcError(error, options = {}) {
  const code = isNonEmptyString(error?.code)
    ? error.code
    : (options.fallbackCode ?? 'rpc_error');
  const message = isNonEmptyString(error?.message)
    ? error.message
    : (options.fallbackMessage ?? 'RPC request failed');
  return new ApiClientError(code, message, {
    status: options.status,
    method: options.method,
    details: isPlainObject(error) ? error : null,
  });
}

export function subscribeRunEvents(sseUrl, handlers = {}, options = {}) {
  requireNonEmptyString(sseUrl, 'SSE URL must be a non-empty string');
  const EventSourceClass = options.EventSource ?? globalThis.EventSource;
  if (typeof EventSourceClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'EventSource is not available');
  }

  const source = new EventSourceClass(
    buildHttpUrl(
      buildHttpUrlWithAfterSequence(sseUrl, options.afterSequence ?? 0),
      options.baseUrl,
    ),
  );
  const cleanupCallbacks = [];
  let closed = false;

  const close = () => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    source.close();
  };

  addListener(source, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(source, 'error', handlers.onError, cleanupCallbacks);

  const heartbeatListener = (event) => handlers.onHeartbeat?.(event);
  source.addEventListener(RUN_STREAM_HEARTBEAT_EVENT, heartbeatListener);
  cleanupCallbacks.push(() =>
    source.removeEventListener(RUN_STREAM_HEARTBEAT_EVENT, heartbeatListener),
  );

  for (const eventType of options.eventTypes ?? RUN_EVENT_TYPES) {
    const listener = (event) => {
      const parsed = parseJsonEventData(
        event.data,
        SSE_ERROR_RESPONSE,
        'SSE event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.({ type: eventType, data: parsed, rawEvent: event });
      if (
        (options.closeOnTerminal ?? true) &&
        TERMINAL_RUN_EVENT_TYPES.has(eventType)
      ) {
        close();
      }
    };
    source.addEventListener(eventType, listener);
    cleanupCallbacks.push(() =>
      source.removeEventListener(eventType, listener),
    );
  }

  return { close, source };
}

export function subscribeServerEvents(handlers = {}, options = {}) {
  const WebSocketClass = options.WebSocket ?? globalThis.WebSocket;
  if (typeof WebSocketClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'WebSocket is not available');
  }

  const socket = new WebSocketClass(
    buildWebSocketUrl(
      options.path ?? WEBSOCKET_ENDPOINT,
      options.baseUrl,
      options.afterSequence ?? 0,
      options.epoch,
      // Per-window presence identity sent on connect (overridable for tests);
      // the server registers the window and the General panel marks its own row.
      options.connectionId ?? resolveClientConnectionId(),
      options.accessor ?? resolveAccessorType(),
    ),
  );
  const cleanupCallbacks = [];
  let closed = false;

  addListener(socket, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(socket, 'error', handlers.onError, cleanupCallbacks);
  addListener(socket, 'close', handlers.onClose, cleanupCallbacks);
  addListener(
    socket,
    'message',
    (event) => {
      const parsed = parseJsonEventData(
        event.data,
        WEBSOCKET_ERROR_RESPONSE,
        'WebSocket event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.(parsed, event);
    },
    cleanupCallbacks,
  );

  const close = (code, reason) => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    socket.close(code, reason);
  };

  return { close, socket };
}

export function subscribeLogEvents(file, handlers = {}, options = {}) {
  requireNonEmptyString(file, 'Log file must be a non-empty string');

  const WebSocketClass = options.WebSocket ?? globalThis.WebSocket;
  if (typeof WebSocketClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'WebSocket is not available');
  }

  const socket = new WebSocketClass(
    buildWebSocketUrlWithParams(
      options.path ?? LOGS_WEBSOCKET_ENDPOINT,
      options.baseUrl,
      {
        file,
        cursor: options.cursor,
      },
    ),
  );
  const cleanupCallbacks = [];
  let closed = false;

  addListener(socket, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(socket, 'error', handlers.onError, cleanupCallbacks);
  addListener(socket, 'close', handlers.onClose, cleanupCallbacks);
  addListener(
    socket,
    'message',
    (event) => {
      const parsed = parseJsonEventData(
        event.data,
        WEBSOCKET_ERROR_RESPONSE,
        'WebSocket event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.(parsed, event);
    },
    cleanupCallbacks,
  );

  const close = (code, reason) => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    socket.close(code, reason);
  };

  return { close, socket };
}

async function readRpcPayload(response, method) {
  try {
    return await response.json();
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'RPC response body must be valid JSON',
      {
        method,
        status: response.status,
        cause: error,
      },
    );
  }
}

async function readJsonHttpPayload(response, method) {
  try {
    return await response.json();
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'HTTP response body must be valid JSON',
      {
        method,
        status: response.status,
        cause: error,
      },
    );
  }
}

function filenameForAudioBlob(audioBlob) {
  if (isNonEmptyString(audioBlob.name)) {
    return audioBlob.name;
  }
  const type = isNonEmptyString(audioBlob.type) ? audioBlob.type : '';
  if (type.includes('webm')) {
    return 'recording.webm';
  }
  if (type.includes('ogg')) {
    return 'recording.ogg';
  }
  if (type.includes('mpeg') || type.includes('mp3')) {
    return 'recording.mp3';
  }
  if (type.includes('wav')) {
    return 'recording.wav';
  }
  return 'recording.webm';
}

function parseJsonEventData(data, code, message) {
  try {
    return JSON.parse(data);
  } catch (error) {
    return new ApiClientError(code, message, { cause: error, details: data });
  }
}

function addListener(target, eventName, listener, cleanupCallbacks) {
  if (typeof listener !== 'function') {
    return;
  }
  target.addEventListener(eventName, listener);
  cleanupCallbacks.push(() => target.removeEventListener(eventName, listener));
}

function buildHttpUrl(path, baseUrl) {
  if (!baseUrl) {
    return path;
  }
  return new URL(path, baseUrl).toString();
}

function buildHttpUrlWithAfterSequence(path, afterSequence = 0) {
  if (afterSequence <= 0) {
    return path;
  }
  const url = new URL(path, 'http://vbot.local');
  url.searchParams.set('after_sequence', String(afterSequence));
  if (path.startsWith('http://') || path.startsWith('https://')) {
    return url.toString();
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

function buildWebSocketUrl(
  path,
  baseUrl,
  afterSequence = 0,
  epoch,
  connectionId,
  accessor,
) {
  const params = {};
  if (afterSequence > 0) {
    params.after_sequence = String(afterSequence);
  }
  if (isNonEmptyString(epoch)) {
    params.epoch = epoch;
  }
  if (isNonEmptyString(connectionId)) {
    params.connection_id = connectionId;
  }
  if (isNonEmptyString(accessor)) {
    params.accessor = accessor;
  }
  return buildWebSocketUrlWithParams(path, baseUrl, params);
}

function buildWebSocketUrlWithParams(path, baseUrl, params = {}) {
  if (path.startsWith('ws://') || path.startsWith('wss://')) {
    const url = new URL(path);
    appendSearchParams(url, params);
    return url.toString();
  }

  const browserBaseUrl = baseUrl ?? browserOrigin();
  if (!browserBaseUrl) {
    const url = new URL(path, 'ws://vbot.local');
    appendSearchParams(url, params);
    return `${url.pathname}${url.search}${url.hash}`;
  }

  const url = new URL(path, browserBaseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  appendSearchParams(url, params);
  return url.toString();
}

function appendSearchParams(url, params) {
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '') {
      continue;
    }
    url.searchParams.set(key, String(value));
  }
}

function browserOrigin() {
  if (globalThis.location?.origin) {
    return globalThis.location.origin;
  }
  return null;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function requireNonEmptyString(value, message, method) {
  if (!isNonEmptyString(value)) {
    throw new ApiClientError(RPC_ERROR_INVALID_CLIENT_REQUEST, message, {
      method,
    });
  }
  return value;
}

function requirePlainObject(value, message, method) {
  if (!isPlainObject(value)) {
    throw new ApiClientError(RPC_ERROR_INVALID_CLIENT_REQUEST, message, {
      method,
    });
  }
  return value;
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}
