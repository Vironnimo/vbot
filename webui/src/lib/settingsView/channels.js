import {
  createChannelSettingsState,
  applyChannelList,
  CHANNEL_DM_SCOPE_PER_CONVERSATION,
  CHANNEL_PLATFORM_TELEGRAM,
  buildCreatePayload,
  buildUpdatePayload,
} from '../channelSettings.js';
import { textOrEmpty, textOrFallback } from './values.js';

export const CHANNEL_FORM_MODE_CREATE = 'create';

export const CHANNEL_FORM_MODE_EDIT = 'edit';

export function createChannelPanelState() {
  return createChannelSettingsState();
}

export function applyChannelPanelList(state, result) {
  return applyChannelList(state, result?.channels);
}

export function createChannelFormValues(channel = null) {
  return {
    id: textOrEmpty(channel?.id),
    platform: textOrFallback(channel?.platform, CHANNEL_PLATFORM_TELEGRAM),
    agent_id: textOrEmpty(channel?.agent_id),
    dm_scope: textOrFallback(
      channel?.dm_scope,
      CHANNEL_DM_SCOPE_PER_CONVERSATION,
    ),
    token_env_var: textOrEmpty(channel?.token_env_var),
    allowed_chat_ids: formatAllowedChatIds(channel?.allowed_chat_ids),
  };
}

export function buildChannelCreatePayload(formValues) {
  return buildCreatePayload(formValues);
}

export function buildChannelUpdatePayload(formValues) {
  return buildUpdatePayload(formValues);
}

export function getAgentItems(result) {
  const agents = Array.isArray(result?.agents) ? result.agents : [];

  return agents
    .map((agent) => {
      const id = textOrEmpty(agent?.id);

      if (!id) {
        return null;
      }

      return {
        id,
        name: textOrFallback(agent?.name, id),
      };
    })
    .filter((agent) => agent !== null)
    .sort((left, right) => left.id.localeCompare(right.id));
}

export function mergeChannelStatuses(channels, statusResults) {
  const channelItems = Array.isArray(channels) ? channels : [];
  const statusItems = Array.isArray(statusResults) ? statusResults : [];
  const statusById = new Map(
    statusItems
      .filter(
        (status) => typeof status?.id === 'string' && status.id.length > 0,
      )
      .map((status) => [status.id, status]),
  );

  return channelItems.map((channel) => {
    const status = statusById.get(channel.id);
    if (!status) {
      return channel;
    }

    const running =
      typeof status.running === 'boolean' ? status.running : channel.running;

    const enabled =
      typeof status.enabled === 'boolean' ? status.enabled : channel.enabled;

    const deniedChats = Array.isArray(status.denied_chats)
      ? status.denied_chats.filter(
          (entry) =>
            typeof entry?.chat_id === 'string' && entry.chat_id.length > 0,
        )
      : [];

    return {
      ...channel,
      running,
      enabled,
      denied_chats: deniedChats,
      access: normalizeChannelAccess(status.access),
    };
  });
}

function normalizeChannelAccess(value) {
  const selfUserId =
    typeof value?.self_user_id === 'string' && value.self_user_id.length > 0
      ? value.self_user_id
      : null;
  const groups = Array.isArray(value?.groups)
    ? value.groups
        .map((group) => normalizeChannelAccessGroup(group))
        .filter((group) => group !== null)
        .sort((left, right) =>
          left.access_scope_id.localeCompare(right.access_scope_id),
        )
    : [];
  return {
    self_user_id: selfUserId,
    groups,
  };
}

function normalizeChannelAccessGroup(value) {
  if (
    typeof value?.access_scope_id !== 'string' ||
    value.access_scope_id.length === 0
  ) {
    return null;
  }
  const participants = Array.isArray(value.participants)
    ? value.participants
        .filter(
          (participant) =>
            typeof participant?.user_id === 'string' &&
            participant.user_id.length > 0,
        )
        .map((participant) => ({
          user_id: participant.user_id,
          display_name:
            typeof participant.display_name === 'string' &&
            participant.display_name.length > 0
              ? participant.display_name
              : participant.user_id,
          role: participant.role === 'admin' ? 'admin' : 'member',
          last_seen_at:
            typeof participant.last_seen_at === 'string'
              ? participant.last_seen_at
              : '',
        }))
        .sort((left, right) =>
          left.display_name.localeCompare(right.display_name),
        )
    : [];
  return {
    access_scope_id: value.access_scope_id,
    admin_user_ids: Array.isArray(value.admin_user_ids)
      ? value.admin_user_ids.filter(
          (userId) => typeof userId === 'string' && userId.length > 0,
        )
      : [],
    participants,
  };
}

export function channelEnabledChipVariant(enabled) {
  return enabled ? 'success' : 'warn';
}

export function channelRunningChipVariant(running) {
  if (running === true) {
    return 'success';
  }

  if (running === false) {
    return 'warn';
  }

  return 'info';
}

export function formatAllowedChatIds(value) {
  if (!Array.isArray(value)) {
    return '';
  }

  return value
    .filter((item) => Number.isSafeInteger(item))
    .map((item) => String(item))
    .join(', ');
}
