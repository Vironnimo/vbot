import {
  rpc,
  requireNonEmptyString,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  ApiClientError,
  requirePlainObject,
} from './transport.js';

export function listTerminals(options = {}) {
  return rpc('terminal.list', {}, options);
}

export function readTerminal(terminalId, options = {}) {
  return rpc('terminal.read', { terminal_id: terminalId }, options);
}

export function startTerminal(params = {}, options = {}) {
  return rpc('terminal.start', params, options);
}

export function sendTerminalInput(terminalId, data, options = {}) {
  requireNonEmptyString(
    terminalId,
    'Terminal id must be a non-empty string',
    'terminal.input',
  );
  requireNonEmptyString(
    data,
    'Terminal input must be a non-empty string',
    'terminal.input',
  );
  const { expectedScreenRevision, ...requestOptions } = options;
  return rpc(
    'terminal.input',
    {
      terminal_id: terminalId,
      data,
      ...(expectedScreenRevision === undefined
        ? {}
        : { expected_screen_revision: expectedScreenRevision }),
    },
    requestOptions,
  );
}

export function resizeTerminal(terminalId, columns, rows, options = {}) {
  requireNonEmptyString(
    terminalId,
    'Terminal id must be a non-empty string',
    'terminal.resize',
  );
  return rpc(
    'terminal.resize',
    { terminal_id: terminalId, columns, rows },
    options,
  );
}

export function killTerminal(terminalId, options = {}) {
  requireNonEmptyString(
    terminalId,
    'Terminal id must be a non-empty string',
    'terminal.kill',
  );
  return rpc('terminal.kill', { terminal_id: terminalId }, options);
}

export function forgetTerminal(terminalId, options = {}) {
  requireNonEmptyString(
    terminalId,
    'Terminal id must be a non-empty string',
    'terminal.forget',
  );
  return rpc('terminal.forget', { terminal_id: terminalId }, options);
}

export function createTerminalGroup(name, options = {}) {
  requireNonEmptyString(
    name,
    'Group name must be a non-empty string',
    'terminal.group.create',
  );
  return rpc('terminal.group.create', { name }, options);
}

export function renameTerminalGroup(groupId, name, options = {}) {
  requireNonEmptyString(
    groupId,
    'Group id must be a non-empty string',
    'terminal.group.rename',
  );
  requireNonEmptyString(
    name,
    'Group name must be a non-empty string',
    'terminal.group.rename',
  );
  return rpc('terminal.group.rename', { group_id: groupId, name }, options);
}

export function deleteTerminalGroup(groupId, options = {}) {
  requireNonEmptyString(
    groupId,
    'Group id must be a non-empty string',
    'terminal.group.delete',
  );
  return rpc('terminal.group.delete', { group_id: groupId }, options);
}

export function setTerminalGroupOrder(groupId, order, options = {}) {
  requireNonEmptyString(
    groupId,
    'Group id must be a non-empty string',
    'terminal.group.order',
  );
  if (!Array.isArray(order) || order.some((id) => typeof id !== 'string')) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Terminal group order must be an array of terminal ids',
    );
  }
  return rpc('terminal.group.order', { group_id: groupId, order }, options);
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

export function getCalendarWindow(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Calendar window payload must be an object',
    'calendar.window',
  );
  requireNonEmptyString(
    params.from,
    'Calendar window start must be a non-empty string',
    'calendar.window',
  );
  requireNonEmptyString(
    params.to,
    'Calendar window end must be a non-empty string',
    'calendar.window',
  );

  return rpc('calendar.window', params, options);
}

export function createCalendarEvent(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Calendar event payload must be an object',
    'calendar.create',
  );

  return rpc('calendar.create', params, options);
}

export function updateCalendarEvent(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Calendar event payload must be an object',
    'calendar.update',
  );
  requireNonEmptyString(
    params.id,
    'Calendar event id must be a non-empty string',
    'calendar.update',
  );

  return rpc('calendar.update', params, options);
}

export function deleteCalendarEvent(id, options = {}) {
  requireNonEmptyString(
    id,
    'Calendar event id must be a non-empty string',
    'calendar.delete',
  );

  return rpc('calendar.delete', { id }, options);
}

export function addCalendarExdate(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Calendar exclusion payload must be an object',
    'calendar.add_exdate',
  );
  requireNonEmptyString(
    params.id,
    'Calendar event id must be a non-empty string',
    'calendar.add_exdate',
  );
  requireNonEmptyString(
    params.occurrence_start,
    'Occurrence start must be a non-empty string',
    'calendar.add_exdate',
  );

  return rpc('calendar.add_exdate', params, options);
}

export function addCalendarAction(params, options = {}) {
  return rpc('calendar.add_action', params, options);
}

export function updateCalendarAction(params, options = {}) {
  return rpc('calendar.update_action', params, options);
}

export function deleteCalendarAction(id, options = {}) {
  return rpc('calendar.delete_action', { id }, options);
}
