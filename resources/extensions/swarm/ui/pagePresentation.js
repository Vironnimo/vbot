import { t, activeLocaleTag } from '../../../../webui/src/lib/i18n.js';
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

export function participantDetails(participant) {
  const state = participantState(participant);
  return [
    participant.display_name,
    participant.model,
    state,
    `${participant.pending_count ?? 0} ${t('swarm.pending', 'pending')}`,
  ].join(' · ');
}

export const participantTotal = (profile) =>
  (profile.participants ?? []).reduce((sum, row) => sum + row.count, 0);

const participantCount = (count) =>
  count === 1
    ? t('swarm.participantCount.one', '1 participant')
    : t('swarm.participantCount', '{count} participants', { count });

// Sidebar row tooltips: a Swarm's formation per Model, a Run's Swarm and size.
export function profileTooltip(profile) {
  const perModel = new Map();
  for (const row of profile.participants ?? [])
    perModel.set(row.model, (perModel.get(row.model) ?? 0) + row.count);
  return {
    title: profile.name,
    rows: [...perModel].map(([model, count]) => ({
      label: participantCount(count),
      value: model,
      mono: true,
    })),
    placement: 'right',
  };
}

export const runTitle = (swarm) =>
  swarm.title || swarm.prompt?.split(/\r?\n/)[0] || swarm.id;

export function runTooltip(swarm) {
  return {
    title: runTitle(swarm),
    rows: [
      { label: t('swarm.profile', 'Swarm'), value: swarm.name },
      {
        label: t('swarm.participants', 'Participants'),
        value: swarm.participant_count,
      },
    ],
    placement: 'right',
  };
}

export const page = (value) =>
  Array.isArray(value) ? value : (value?.entries ?? value?.items ?? []);

export const canStop = (state) =>
  ['preparing', 'running', 'idle', 'needs_attention', 'stopping'].includes(
    state,
  );

export const resumableParticipantState = (state) =>
  ['idle', 'failed', 'cancelled', 'interrupted'].includes(state);

export function usageCount(value) {
  if (!Number.isFinite(value))
    return t('swarm.usage.unavailable', 'Unavailable');
  const units = [
    [1e9, t('swarm.usage.billion', 'mrd')],
    [1e6, t('swarm.usage.million', 'mio')],
    [1e3, t('swarm.usage.thousand', 'k')],
  ];
  const [scale, suffix] = units.find(([scale]) => Math.abs(value) >= scale) ?? [
    1,
    '',
  ];
  const number = new Intl.NumberFormat(activeLocaleTag(), {
    maximumFractionDigits: scale === 1 ? 0 : 1,
  }).format(value / scale);
  return suffix ? `${number} ${suffix}` : number;
}

export function tokensUsed(counts) {
  return usageCount(
    counts?.measured_input_tokens +
      counts?.measured_output_tokens +
      counts?.estimated_input_tokens +
      counts?.estimated_output_tokens,
  );
}
