import { t } from '$lib/i18n.js';
import { parseModelSelectionValue } from '$lib/modelSelection.js';

/**
 * The activity an Agent's status dot shows, in words: "Running", "Idle",
 * "2 unread results", or both for a running Agent with unread results.
 */
export function agentActivityState(status, unreadCount = 0) {
  const unread =
    unreadCount === 1
      ? t('chat.agentActivity.stateUnreadOne')
      : unreadCount > 1
        ? t('chat.agentActivity.stateUnreadCount', { count: unreadCount })
        : status === 'unread'
          ? t('chat.agentActivity.stateUnread')
          : '';
  if (status === 'running') {
    return [t('chat.agentActivity.stateRunning'), unread]
      .filter(Boolean)
      .join(' · ');
  }
  return unread || t('chat.agentActivity.stateIdle');
}

/**
 * Quick-tooltip details for an Agent's activity marker (the Chat header's
 * Agent bar and its All agents list): the Agent's name leads, then
 * its activity in words, the effective Model as canonical `provider/model`
 * with its thinking effort (no effort leaves the Provider default in place),
 * and its id when the name does not already say it.
 */
export function agentActivityTooltip({
  name = '',
  id = '',
  status = 'idle',
  unreadCount = 0,
  model: modelValue,
  thinkingEffort,
} = {}) {
  const rows = [
    {
      label: t('chat.agentActivity.status'),
      value: agentActivityState(status, unreadCount),
      tone: status === 'running' ? 'warning' : '',
    },
  ];
  const { model } = parseModelSelectionValue(
    typeof modelValue === 'string' ? modelValue.trim() : '',
  );
  if (model) {
    const effort =
      typeof thinkingEffort === 'string' ? thinkingEffort.trim() : '';
    rows.push(
      { label: t('chat.agentActivity.model'), value: model },
      {
        label: t('chat.agentActivity.thinkingEffort'),
        value: effort || t('chat.agentActivity.thinkingEffortDefault'),
      },
    );
  }
  // An id that only restates the name in other letter case adds nothing.
  if (id && id.toLowerCase() !== name.toLowerCase()) {
    rows.push({ label: t('agents.form.id'), value: id, mono: true });
  }
  return { title: name || id, rows };
}
