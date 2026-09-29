import { asText as asSharedText } from '$lib/values.js';
import { t } from '$lib/i18n.js';
import { formatMoment } from '$lib/timeText.js';
import {
  sessionDisplayName,
  sessionParentReference,
} from '$lib/sessionListView.js';

export const autofocusRename = (node) => {
  node.focus();
  node.select();
};

// Details card beside a Session row: to its right, so sweeping the list
// never puts the card over the neighbouring rows. The row already shows the
// name (and the Agent in the all-Agents list); the card adds when the Session
// was active and created, its Channel and the Session it descends from. A
// parent or fork source leads with its name when the loaded list knows it,
// otherwise with its Agent, followed by its id. `agents` is the roster
// ({address, name}) used to name Agents; `sessions` the loaded Session list.
export function sessionHoverDetails(
  session,
  { sessions = [], agents = [], nowMs = Date.now() } = {},
) {
  const lastActive = session.last_active_at ?? session.created_at;
  const rows = [
    {
      label: t('sessions.last_active'),
      value: formatMoment(lastActive, { nowMs }) || t('common.unknown'),
    },
  ];
  if (session.created_at && session.created_at !== lastActive) {
    rows.push({
      label: t('sessions.details.created'),
      value: formatMoment(session.created_at, { nowMs }),
    });
  }
  if (session.source_channel_id) {
    rows.push({
      label: t('sessions.source_channel'),
      value: session.source_channel_id,
      mono: true,
    });
  }
  const origin = sessionParentReference(session);
  if (origin) {
    rows.push(
      ...originRows(origin, {
        label:
          origin.kind === 'fork'
            ? t('sessions.details.forkedFrom')
            : t('sessions.subagent_parent'),
        sessions,
        agents,
      }),
    );
  }

  return {
    title: session.display_name || sessionDisplayName(session),
    rows,
    placement: 'right',
  };
}

function originRows(origin, { label, sessions, agents }) {
  const address = origin.project_id
    ? `${origin.agent_id}@${origin.project_id}`
    : origin.agent_id;
  const agentName =
    agents.find((agent) => agent?.address === address)?.name || address;
  const known = sessions.find(
    (candidate) =>
      candidate?.id === origin.session_id &&
      (!candidate.agent_address || candidate.agent_address === address),
  );
  if (known) {
    return [
      {
        label,
        value: t('sessions.details.originValue', {
          session: known.display_name || sessionDisplayName(known),
          agent: agentName,
        }),
      },
    ];
  }
  return [
    {
      label,
      value: t('sessions.details.originOfAgent', { agent: agentName }),
    },
    {
      label: t('sessions.details.originId'),
      value: origin.session_id,
      mono: true,
    },
  ];
}

// The unread marker: what the unread Run ended with and when.
export function unreadRunDetails(session, { nowMs = Date.now() } = {}) {
  const status = asText(session.unread_run_status);
  return {
    text: t('sessions.unreadCompletionHint'),
    rows: [
      {
        label: t('sessions.details.runResult'),
        value: RUN_STATUS_LABELS[status]?.() ?? status,
        tone: RUN_STATUS_TONES[status],
      },
      {
        label: t('sessions.details.runFinished'),
        value: formatMoment(session.unread_run_at, { nowMs }),
      },
    ],
    placement: 'right',
  };
}

const RUN_STATUS_LABELS = {
  completed: () => t('chat.runStatus.completed'),
  failed: () => t('chat.runStatus.failed'),
  cancelled: () => t('chat.runStatus.cancelled'),
  interrupted: () => t('chat.runStatus.interrupted'),
};

const RUN_STATUS_TONES = {
  completed: 'success',
  failed: 'danger',
  cancelled: 'warning',
  interrupted: 'warning',
};

export const resolvePlatformLabel = (platform) => {
  if (platform === 'telegram') {
    return t('sessions.platform_telegram');
  }
  if (platform === 'discord') {
    return t('sessions.platform_discord');
  }
  const normalizedPlatform = asText(platform);
  if (!normalizedPlatform) {
    return t('sessions.platform_channel');
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
