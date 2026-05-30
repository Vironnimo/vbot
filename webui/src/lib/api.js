import {
  buildProviderConnectPayload,
  buildProviderDisconnectPayload,
} from './settingsView.js';

const RPC_ENDPOINT = '/api/rpc';
const ATTACHMENT_UPLOAD_ENDPOINT = '/api/upload';
const ATTACHMENT_BASE_ENDPOINT = '/api/attachments';
const SPEECH_TRANSCRIBE_ENDPOINT = '/api/speech/transcribe';
const SPEECH_SYNTHESIZE_ENDPOINT = '/api/speech/synthesize';
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

export const RUN_EVENT_TYPES = [
  'run_started',
  'user_message_persisted',
  'model_fallback_activated',
  'error_message_persisted',
  'compaction_completed',
  RUN_EVENT_REASONING_DELTA,
  'reasoning',
  RUN_EVENT_TOOL_CALL_DELTA,
  'tool_call_started',
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_TOOL_CALL_STDERR,
  'tool_call_result',
  'subagent_session_started',
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  'assistant_output',
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

  const text_content =
    typeof payload.text_content === 'string' ? payload.text_content : null;

  return {
    attachment_id: payload.attachment_id,
    filename: payload.filename,
    media_type: payload.media_type,
    size_bytes: payload.size_bytes,
    text_content,
  };
}

export function getTaskModelSettings(options = {}) {
  return rpc('task_model.settings', {}, options);
}

export function updateTaskModelSettings(modelTasks, options = {}) {
  if (!isPlainObject(modelTasks)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Task model settings must be an object',
      {
        method: 'task_model.update',
      },
    );
  }
  return rpc('task_model.update', { model_tasks: modelTasks }, options);
}

export function listTaskModelTargets(taskType, options = {}) {
  if (!isNonEmptyString(taskType)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Task type must be a non-empty string',
      {
        method: 'task_model.list_targets',
      },
    );
  }
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

export async function synthesizeSpeech(text, options = {}) {
  if (!isNonEmptyString(text)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Text must be a non-empty string',
      {
        method: 'speech.synthesize',
      },
    );
  }

  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method: 'speech.synthesize',
    });
  }

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(
        options.synthesizePath ?? SPEECH_SYNTHESIZE_ENDPOINT,
        options.baseUrl,
      ),
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(options.headers ?? {}),
        },
        body: JSON.stringify({ text }),
        signal: options.signal,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'Speech synthesis failed before a response arrived',
      {
        method: 'speech.synthesize',
        cause: error,
      },
    );
  }

  if (!response.ok) {
    const payload = await readOptionalJsonHttpPayload(response);
    throw new ApiClientError(
      RPC_ERROR_HTTP,
      isNonEmptyString(payload?.detail)
        ? payload.detail
        : `Speech synthesis failed with HTTP ${response.status}`,
      {
        method: 'speech.synthesize',
        status: response.status,
        details: isPlainObject(payload) ? payload : null,
      },
    );
  }
  return response.blob();
}

export function getAttachmentUrl(attachmentId) {
  if (!isNonEmptyString(attachmentId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Attachment id must be a non-empty string',
    );
  }
  return `${ATTACHMENT_BASE_ENDPOINT}/${attachmentId}`;
}

export function listLogs(options = {}) {
  return rpc('log.list', {}, options);
}

export function readLogFile(file, options = {}) {
  if (!isNonEmptyString(file)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Log file must be a non-empty string',
      {
        method: 'log.read',
      },
    );
  }

  return rpc('log.read', { file }, options);
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
  if (!isNonEmptyString(id)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Cron job id must be a non-empty string',
      {
        method: 'cron.delete',
      },
    );
  }

  return rpc('cron.delete', { id }, options);
}

export function enableCronJob(id, options = {}) {
  if (!isNonEmptyString(id)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Cron job id must be a non-empty string',
      {
        method: 'cron.enable',
      },
    );
  }

  return rpc('cron.enable', { id }, options);
}

export function disableCronJob(id, options = {}) {
  if (!isNonEmptyString(id)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Cron job id must be a non-empty string',
      {
        method: 'cron.disable',
      },
    );
  }

  return rpc('cron.disable', { id }, options);
}

export function listSessions(agentId, options = {}) {
  if (!isNonEmptyString(agentId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent id must be a non-empty string',
      {
        method: 'session.list',
      },
    );
  }

  return rpc('session.list', { agent_id: agentId }, options);
}

export function listQueue(agentId, sessionId, options = {}) {
  if (!isNonEmptyString(agentId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent id must be a non-empty string',
      {
        method: 'chat.queue_list',
      },
    );
  }

  if (!isNonEmptyString(sessionId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Session id must be a non-empty string',
      {
        method: 'chat.queue_list',
      },
    );
  }

  return rpc(
    'chat.queue_list',
    { agent_id: agentId, session_id: sessionId },
    options,
  );
}

export function removeFromQueue(agentId, sessionId, itemId, options = {}) {
  if (!isNonEmptyString(agentId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent id must be a non-empty string',
      {
        method: 'chat.queue_remove',
      },
    );
  }

  if (!isNonEmptyString(sessionId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Session id must be a non-empty string',
      {
        method: 'chat.queue_remove',
      },
    );
  }

  if (!isNonEmptyString(itemId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Queue item id must be a non-empty string',
      {
        method: 'chat.queue_remove',
      },
    );
  }

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
  if (!isNonEmptyString(agentId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent id must be a non-empty string',
      {
        method: 'chat.queue_update',
      },
    );
  }

  if (!isNonEmptyString(sessionId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Session id must be a non-empty string',
      {
        method: 'chat.queue_update',
      },
    );
  }

  if (!isNonEmptyString(itemId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Queue item id must be a non-empty string',
      {
        method: 'chat.queue_update',
      },
    );
  }

  if (!(isNonEmptyString(content) || Array.isArray(content))) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Queue item content must be a non-empty string or content block list',
      {
        method: 'chat.queue_update',
      },
    );
  }

  return rpc(
    'chat.queue_update',
    {
      agent_id: agentId,
      session_id: sessionId,
      item_id: itemId,
      content,
    },
    options,
  );
}

export function linkSessionToChannel(
  agentId,
  sessionId,
  channelId,
  platformConvId,
  options = {},
) {
  if (!isNonEmptyString(agentId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent id must be a non-empty string',
      {
        method: 'session.link_channel',
      },
    );
  }

  if (!isNonEmptyString(sessionId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Session id must be a non-empty string',
      {
        method: 'session.link_channel',
      },
    );
  }

  if (!isNonEmptyString(channelId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel id must be a non-empty string',
      {
        method: 'session.link_channel',
      },
    );
  }

  if (!isNonEmptyString(platformConvId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Platform conversation id must be a non-empty string',
      {
        method: 'session.link_channel',
      },
    );
  }

  return rpc(
    'session.link_channel',
    {
      agent_id: agentId,
      session_id: sessionId,
      channel_id: channelId,
      platform_conv_id: platformConvId,
    },
    options,
  );
}

export function listChannels(options = {}) {
  return rpc('channel.list', {}, options);
}

export function createChannel(payload = {}, options = {}) {
  if (!isPlainObject(payload)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel payload must be an object',
      {
        method: 'channel.create',
      },
    );
  }

  return rpc('channel.create', payload, options);
}

export function updateChannel(channelId, payload = {}, options = {}) {
  if (!isNonEmptyString(channelId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel id must be a non-empty string',
      {
        method: 'channel.update',
      },
    );
  }

  if (!isPlainObject(payload)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel payload must be an object',
      {
        method: 'channel.update',
      },
    );
  }

  return rpc('channel.update', { ...payload, id: channelId }, options);
}

export function deleteChannel(channelId, options = {}) {
  if (!isNonEmptyString(channelId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel id must be a non-empty string',
      {
        method: 'channel.delete',
      },
    );
  }

  return rpc('channel.delete', { id: channelId }, options);
}

export function enableChannel(channelId, options = {}) {
  if (!isNonEmptyString(channelId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel id must be a non-empty string',
      {
        method: 'channel.enable',
      },
    );
  }

  return rpc('channel.enable', { id: channelId }, options);
}

export function disableChannel(channelId, options = {}) {
  if (!isNonEmptyString(channelId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel id must be a non-empty string',
      {
        method: 'channel.disable',
      },
    );
  }

  return rpc('channel.disable', { id: channelId }, options);
}

export function getChannelStatus(channelId, options = {}) {
  if (!isNonEmptyString(channelId)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Channel id must be a non-empty string',
      {
        method: 'channel.status',
      },
    );
  }

  return rpc('channel.status', { id: channelId }, options);
}

export async function connectProvider(providerId, connectionId, options = {}) {
  return (options.rpc ?? rpc)(
    'provider.connect',
    buildProviderConnectPayload(providerId, connectionId),
  );
}

export async function disconnectProvider(
  providerId,
  connectionId,
  options = {},
) {
  return (options.rpc ?? rpc)(
    'provider.disconnect',
    buildProviderDisconnectPayload(providerId, connectionId),
  );
}

export async function getProviderConnectionStatus(
  providerId,
  connectionId,
  options = {},
) {
  return (options.rpc ?? rpc)('provider.connection_status', {
    provider_id: providerId,
    connection_id: connectionId,
  });
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
  if (!isNonEmptyString(sseUrl)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'SSE URL must be a non-empty string',
    );
  }
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
  if (!isNonEmptyString(file)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Log file must be a non-empty string',
    );
  }

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

async function readOptionalJsonHttpPayload(response) {
  try {
    return await response.json();
  } catch {
    return null;
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

function buildWebSocketUrl(path, baseUrl, afterSequence = 0) {
  return buildWebSocketUrlWithParams(
    path,
    baseUrl,
    afterSequence > 0 ? { after_sequence: String(afterSequence) } : {},
  );
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

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}
