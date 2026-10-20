import { t } from '$lib/i18n.js';
import { formatMoment } from '$lib/timeText.js';

export const statusLabel = (status) => {
  if (status === 'running') {
    return t('chat.activity.status.running');
  }
  if (status === 'success' || status === 'completed') {
    return t('chat.activity.status.completed');
  }
  if (status === 'failed') {
    return t('chat.activity.status.failed');
  }
  if (status === 'cancelled') {
    return t('chat.activity.status.cancelled');
  }
  if (status === 'interrupted') {
    return t('chat.activity.status.interrupted');
  }
  if (status === 'ended') {
    return t('chat.activity.status.ended');
  }
  return t('chat.activity.status.unknown');
};

// The panel's status icons key off the shared dot vocabulary; reflection
// rows arrive with run-status words, so they map onto the same symbols.
export const reflectionDotStatus = (status) =>
  status === 'running'
    ? 'running'
    : status === 'completed'
      ? 'success'
      : status;

export const reflectionScopeLabel = (row) => {
  if (row.scope === 'memory') {
    return t('chat.activity.reflectionScope.memory');
  }
  if (row.scope === 'skill') {
    return t('chat.activity.reflectionScope.skill');
  }
  return t('chat.activity.reflectionScope.combined');
};

export const reflectionStatusDetails = (row) => {
  const started = formatMoment(row?.startedAt, { seconds: true });
  return {
    title: statusLabel(row?.status),
    rows: started ? [{ label: t('chat.details.started'), value: started }] : [],
  };
};

export const reflectionRowLabel = (row) =>
  t('chat.activity.reflectionOpenAria', {
    scope: reflectionScopeLabel(row),
    status: statusLabel(row.status),
  });

export const taskLabel = (task) => {
  if (task.kind === 'command') {
    return t('chat.activity.commandTaskAria', {
      command: task.label,
      status: task.rowState?.stillRunning
        ? t('chat.activity.status.stillRunning')
        : statusLabel(task.dotStatus),
    });
  }
  return t('chat.activity.taskAria', {
    agent: task.agentId,
    status: task.workRunning
      ? t('chat.activity.status.commandsRunning')
      : statusLabel(task.dotStatus),
  });
};
