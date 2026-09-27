import { asText as asSharedText } from '$lib/values.js';
import { activeLocaleTag, t } from '$lib/i18n.js';
import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
import { sessionDisplayName } from '$lib/sessionListView.js';

export const autofocusRename = (node) => {
  node.focus();
  node.select();
};

export const formatTimestamp = (value) => {
  const normalizedValue = asText(value);
  if (!normalizedValue) {
    return t('common.unknown', 'Unknown');
  }

  const parsedValue = Date.parse(normalizedValue);
  if (Number.isNaN(parsedValue)) {
    return normalizedValue;
  }

  return formatDateTimeInApplicationZone(
    new Date(parsedValue),
    activeLocaleTag(),
    { dateStyle: 'medium', timeStyle: 'short' },
  );
};

// Details card beside a Session row: to its right, so sweeping the list
// never puts the card over the neighbouring rows.
export const sessionHoverDetails = (session) => {
  const rows = [];

  if (session.agent_name) {
    rows.push({
      label: t('sessions.agent', 'Agent'),
      value: session.agent_name,
    });
  }

  rows.push({
    label: t('sessions.last_active', 'Last active'),
    value: formatTimestamp(session.last_active_at ?? session.created_at),
  });

  if (session.source_channel_id) {
    rows.push({
      label: t('sessions.source_channel', 'Source channel'),
      value: session.source_channel_id,
      mono: true,
    });
  }

  if (session.subagent_parent) {
    rows.push({
      label: t('sessions.subagent_parent', 'Parent'),
      value: `${session.subagent_parent.agent_id}/${session.subagent_parent.session_id}`,
      mono: true,
    });
  }

  return {
    title: session.display_name || sessionDisplayName(session),
    rows,
    placement: 'right',
  };
};

export const resolvePlatformLabel = (platform) => {
  if (platform === 'telegram') {
    return t('sessions.platform_telegram', 'Telegram');
  }
  if (platform === 'discord') {
    return t('sessions.platform_discord', 'Discord');
  }
  const normalizedPlatform = asText(platform);
  if (!normalizedPlatform) {
    return t('sessions.platform_channel', 'Channel');
  }
  return `${normalizedPlatform.slice(0, 1).toUpperCase()}${normalizedPlatform.slice(1)}`;
};

export const REFLECTION_BADGE_RUN_KINDS = [
  'memory_reflection',
  'skill_reflection',
  'reflection',
];

export function reflectionBadgeKinds(session) {
  return REFLECTION_BADGE_RUN_KINDS.filter((runKind) =>
    session.run_kinds.includes(runKind),
  );
}

export function asText(value) {
  return asSharedText(value).trim();
}
