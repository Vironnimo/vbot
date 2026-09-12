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
