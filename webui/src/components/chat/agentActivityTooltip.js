import { t } from '$lib/i18n.js';
import { parseModelSelectionValue } from '$lib/modelSelection.js';
import {
  formatWorkSpan,
  waitingForText,
  workSpanMs,
} from '$lib/sessionActivity.js';

/**
 * The activity an Agent's status dot shows, in words: "Running", "Waiting",
 * "Idle", "2 unread results", or the work and its results together. With the
 * Agent's `work` (see `agentSessionActivity`), a waiting Agent names what it
 * waits for and both name the span of the work so far at `nowMs`:
 * "Waiting for 2 Sub-Agents · 4m 12s".
 */
export function agentActivityState(
  status,
  unreadCount = 0,
  { work = null, nowMs = Date.now() } = {},
) {
  const unread =
    unreadCount === 1
      ? t('chat.agentActivity.stateUnreadOne')
      : unreadCount > 1
        ? t('chat.agentActivity.stateUnreadCount', { count: unreadCount })
        : status === 'unread'
          ? t('chat.agentActivity.stateUnread')
          : '';
  if (status === 'running' || status === 'waiting') {
    return [workState(status, work, nowMs), unread].filter(Boolean).join(' · ');
  }
  return unread || t('chat.agentActivity.stateIdle');
}

function workState(status, work, nowMs) {
  const state =
    status === 'running'
      ? t('chat.agentActivity.stateRunning')
      : work
        ? waitingForText(work.waitingFor)
        : t('chat.agentActivity.stateWaiting');
  const span = formatWorkSpan(workSpanMs(work?.workStartedAt, nowMs));
  return span ? t('chat.sessionActivity.withSpan', { state, span }) : state;
}

/**
 * Quick-tooltip details for an Agent's activity marker (the Chat header's
 * Agent bar and its All agents list): the Agent's name leads, then its
 * activity in words (with `work` and `nowMs` as above), the effective Model as
 * canonical `provider/model` with its thinking effort (no effort leaves the
 * Provider default in place), and its id when the name does not already say
 * it.
 */
export function agentActivityTooltip({
  name = '',
  id = '',
  status = 'idle',
  unreadCount = 0,
  work = null,
  nowMs = Date.now(),
  model: modelValue,
  thinkingEffort,
} = {}) {
  const rows = [
    {
      label: t('chat.agentActivity.status'),
      value: agentActivityState(status, unreadCount, { work, nowMs }),
      tone: status === 'running' || status === 'waiting' ? 'warning' : '',
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
