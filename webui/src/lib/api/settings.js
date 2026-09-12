import {
  rpc,
  requireNonEmptyString,
  requirePlainObject,
  requireAgentMemoryMutation,
  requirePositiveInteger,
  requireMemoryScope,
} from './transport.js';
import {
  buildProviderConnectPayload,
  buildProviderDisconnectPayload,
} from '../settingsView.js';

export function getSettings(options = {}) {
  return rpc('settings.get', {}, options);
}

export function listExtensionPages(options = {}) {
  return rpc('extensions.pages', {}, options);
}

export function invokeExtensionPageOperation(
  name,
  operation,
  arguments_,
  page,
  options = {},
) {
  return rpc(
    'extensions.operation',
    { name, operation, arguments: arguments_, page },
    options,
  );
}

export function openExtensionPageRun(
  name,
  page,
  groupId,
  runId,
  afterSequence = 0,
  options = {},
) {
  return rpc(
    'extensions.page_run',
    {
      name,
      page,
      group_id: groupId,
      run_id: runId,
      after_sequence: afterSequence,
    },
    options,
  );
}

export function readExtensionPageHistory(
  name,
  page,
  groupId,
  participantId,
  query = {},
  options = {},
) {
  return rpc(
    'extensions.page_history',
    {
      name,
      page,
      group_id: groupId,
      participant_id: participantId,
      query,
    },
    options,
  );
}

export function getSessionStoreStatus(options = {}) {
  return rpc('session_store.status', {}, options);
}

export function createSessionStoreSnapshot(reason = 'rpc', options = {}) {
  requireNonEmptyString(
    reason,
    'Snapshot reason must be a non-empty string',
    'session_store.snapshot_create',
  );
  return rpc('session_store.snapshot_create', { reason }, options);
}

export function acknowledgeSessionStoreIncident(incidentId, options = {}) {
  requireNonEmptyString(
    incidentId,
    'Recovery incident id must be a non-empty string',
    'session_store.incident_acknowledge',
  );
  return rpc(
    'session_store.incident_acknowledge',
    { incident_id: incidentId },
    options,
  );
}

export function updateSettings(settings, options = {}) {
  requirePlainObject(
    settings,
    'Settings update must be an object',
    'settings.update',
  );
  return rpc('settings.update', settings, options);
}

export function listAgentMemories(agentId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'memory.list',
  );
  return rpc('memory.list', { agent_id: agentId }, options);
}

export function addAgentMemory(agentId, scope, content, options = {}) {
  requireAgentMemoryMutation(agentId, scope, content, 'memory.add');
  return rpc('memory.add', { agent_id: agentId, scope, content }, options);
}

export function replaceAgentMemory(
  agentId,
  scope,
  entryId,
  content,
  options = {},
) {
  requireAgentMemoryMutation(agentId, scope, content, 'memory.replace');
  requirePositiveInteger(entryId, 'Memory entry id', 'memory.replace');
  return rpc(
    'memory.replace',
    { agent_id: agentId, scope, entry_id: entryId, content },
    options,
  );
}

export function removeAgentMemory(agentId, scope, entryId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'memory.remove',
  );
  requireMemoryScope(scope, 'memory.remove');
  requirePositiveInteger(entryId, 'Memory entry id', 'memory.remove');
  return rpc(
    'memory.remove',
    { agent_id: agentId, scope, entry_id: entryId },
    options,
  );
}

export function listProviderRoutingOptions(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Provider routing filters must be an object',
    'provider.routing_options',
  );
  return rpc('provider.routing_options', params, options);
}

export function listFiles(agentId, options = {}) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'files.list',
  );
  return rpc('files.list', { agent_id: agentId }, options);
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

export function getChannelAccess(id, options = {}) {
  requireNonEmptyString(
    id,
    'Channel id must be a non-empty string',
    'channel.access.get',
  );
  return rpc('channel.access.get', { id }, options);
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

export function setChannelIdentity(id, userId, options = {}) {
  requireNonEmptyString(
    id,
    'Channel id must be a non-empty string',
    'channel.identity.set',
  );
  requireNonEmptyString(
    userId,
    'Channel user id must be a non-empty string',
    'channel.identity.set',
  );
  return rpc('channel.identity.set', { id, user_id: userId }, options);
}

export function grantChannelAdmin(id, accessScopeId, userId, options = {}) {
  return mutateChannelAdmin(
    'channel.admin.grant',
    id,
    accessScopeId,
    userId,
    options,
  );
}

export function revokeChannelAdmin(id, accessScopeId, userId, options = {}) {
  return mutateChannelAdmin(
    'channel.admin.revoke',
    id,
    accessScopeId,
    userId,
    options,
  );
}

function mutateChannelAdmin(method, id, accessScopeId, userId, options) {
  requireNonEmptyString(id, 'Channel id must be a non-empty string', method);
  requireNonEmptyString(
    accessScopeId,
    'Channel group id must be a non-empty string',
    method,
  );
  requireNonEmptyString(
    userId,
    'Channel user id must be a non-empty string',
    method,
  );
  return rpc(
    method,
    { id, access_scope_id: accessScopeId, user_id: userId },
    options,
  );
}

export function listExtensions(options = {}) {
  return rpc('extensions.list', {}, options);
}

export function extensionOperation(name, operation, args = {}, options = {}) {
  return rpc(
    'extensions.operation',
    { name, operation, arguments: args },
    options,
  );
}

export function listExtensionRequests(options = {}) {
  return rpc('extensions.requests', {}, options);
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

export function getStatisticsReport(params = {}, options = {}) {
  return rpc('statistics.report', params, options);
}

export function getStatisticsRunActivity(params, options = {}) {
  return rpc('statistics.run_activity', params, options);
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

export function getLiveVoiceStatus(options = {}) {
  return rpc('live.status', {}, options);
}

export async function setLiveVoiceEnabled(enabled, options = {}) {
  await rpc(
    'settings.patch',
    { operations: [{ op: 'set', path: 'live_voice.enabled', value: enabled }] },
    options,
  );
  return getSettings(options);
}

export function createLiveVoiceSession(sdp, options = {}) {
  return rpc('live.create', { sdp }, options);
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
