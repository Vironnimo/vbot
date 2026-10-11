import { t } from '../../../../webui/src/lib/i18n.js';
import { modelIdParts } from '../../../../webui/src/lib/modelSelection.js';
export const requestId = () =>
  crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;

export function participantColor(id) {
  let hash = 2166136261;
  for (const char of id ?? '')
    hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
  return `hsl(${(hash >>> 0) % 360} 60% 75%)`;
}

export function participantInitials(name) {
  const parts = (name ?? '').trim().split(/\s+/u).filter(Boolean);
  if (!parts.length) return '?';
  const last = parts.at(-1);
  if (parts.length > 1 && /^\d+$/u.test(last))
    return `${Array.from(parts[0])[0]}${last}`.toUpperCase();
  return (
    parts.length > 1
      ? `${Array.from(parts[0])[0]}${Array.from(last)[0]}`
      : Array.from(parts[0]).slice(0, 2).join('')
  ).toUpperCase();
}

export function participantState(participant) {
  return participant.run_active ? 'running' : participant.state;
}

const PARTICIPANT_STATES = {
  idle: () => t('swarm.participant.state.idle'),
  running: () => t('swarm.participant.state.running'),
  failed: () => t('swarm.participant.state.failed'),
  cancelled: () => t('swarm.participant.state.cancelled'),
  interrupted: () => t('swarm.participant.state.interrupted'),
};
const STATE_TONES = {
  running: 'warning',
  cancelled: 'warning',
  failed: 'danger',
  interrupted: 'danger',
};

// The participant's state in words; a state this page does not know shows as
// stored.
export function participantStateLabel(participant) {
  const state = participantState(participant);
  return Object.hasOwn(PARTICIPANT_STATES, state)
    ? PARTICIPANT_STATES[state]()
    : String(state ?? '');
}

// A participant chip's accessible name: name, state and pending messages.
export function participantLabel(participant) {
  const count = participant.pending_count ?? 0;
  return t('swarm.participant.label', {
    name: participant.display_name,
    state: participantStateLabel(participant),
    pending:
      count === 1
        ? t('swarm.participant.pending.one')
        : t('swarm.participant.pending', { count }),
  });
}

// A participant's details card: its name, Model (the provider path muted, as
// ModelId shows it), state and pending messages.
export function participantCard(participant) {
  const { lead, name } = modelIdParts(participant.model);
  const count = participant.pending_count ?? 0;
  const messages =
    count === 0
      ? t('swarm.participant.messages.none')
      : count === 1
        ? t('swarm.participant.messages.one')
        : t('swarm.participant.messages', { count });
  return {
    title: participant.display_name,
    rows: [
      { label: t('swarm.participant.model'), lead, value: name },
      {
        label: t('swarm.participant.status'),
        value: participantStateLabel(participant),
        tone: STATE_TONES[participantState(participant)] ?? '',
      },
      { label: t('swarm.participant.queue'), value: messages },
    ],
  };
}

export const participantTotal = (profile) =>
  (profile.participants ?? []).reduce((sum, row) => sum + row.count, 0);

export const participantCount = (count) =>
  count === 1
    ? t('swarm.participantCount.one')
    : t('swarm.participantCount', { count });

export const page = (value) =>
  Array.isArray(value) ? value : (value?.entries ?? value?.items ?? []);

export const canStop = (state) =>
  ['preparing', 'running', 'idle', 'needs_attention', 'stopping'].includes(
    state,
  );

export const resumableParticipantState = (state) =>
  ['idle', 'failed', 'cancelled', 'interrupted'].includes(state);
