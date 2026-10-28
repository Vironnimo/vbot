import { textOrEmpty, textOrFallback } from './values.js';

import { asOptionalText, isPlainObject } from '../values.js';

export const CHANNEL_PLATFORM_TELEGRAM = 'telegram';
export const CHANNEL_PLATFORMS = Object.freeze([
  CHANNEL_PLATFORM_TELEGRAM,
  'discord',
  'slack',
  'mattermost',
  'whatsapp',
  'email',
]);

const EMAIL_IMAP_PORT = 993;
const EMAIL_SMTP_PORT = 465;
export const EMAIL_SMTP_SECURITIES = Object.freeze(['', 'ssl', 'starttls']);

export const CHANNEL_DM_SCOPE_PER_CONVERSATION = 'per_conversation';
export const CHANNEL_DM_SCOPE_MAIN = 'main';
const CHANNEL_DM_SCOPE_PER_PEER = 'per_peer';
const CHANNEL_DM_SCOPE_PER_ACCOUNT_CHANNEL_PEER = 'per_account_channel_peer';
export const CHANNEL_DM_SCOPES = Object.freeze([
  CHANNEL_DM_SCOPE_PER_CONVERSATION,
  CHANNEL_DM_SCOPE_MAIN,
  CHANNEL_DM_SCOPE_PER_PEER,
  CHANNEL_DM_SCOPE_PER_ACCOUNT_CHANNEL_PEER,
]);

const CHANNEL_PLATFORM_SET = new Set(CHANNEL_PLATFORMS);
const CHANNEL_DM_SCOPE_SET = new Set(CHANNEL_DM_SCOPES);

export const CHANNEL_FORM_MODE_CREATE = 'create';

export const CHANNEL_FORM_MODE_EDIT = 'edit';

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
    app_token_env_var: textOrEmpty(channel?.app_token_env_var),
    server_url: textOrEmpty(channel?.server_url),
    allowed_chat_ids: formatAllowedChatIds(channel?.allowed_chat_ids),
    ...emailFormValues(channel?.email),
  };
}

// The email account as flat form fields. A value that equals what the server
// derives (username = address, security from the port) shows empty, so it
// keeps following its source when that changes.
function emailFormValues(email) {
  const source = isPlainObject(email) ? email : {};
  const address = textOrEmpty(source.address);
  const username = textOrEmpty(source.username);
  const smtpPort = Number.isSafeInteger(source.smtp_port)
    ? source.smtp_port
    : EMAIL_SMTP_PORT;
  const security = textOrEmpty(source.smtp_security);
  return {
    email_address: address,
    email_username: username === address ? '' : username,
    imap_host: textOrEmpty(source.imap_host),
    imap_port: String(
      Number.isSafeInteger(source.imap_port)
        ? source.imap_port
        : EMAIL_IMAP_PORT,
    ),
    smtp_host: textOrEmpty(source.smtp_host),
    smtp_port: String(smtpPort),
    smtp_security: security === defaultSmtpSecurity(smtpPort) ? '' : security,
    email_folders: Array.isArray(source.folders)
      ? source.folders.filter((folder) => typeof folder === 'string').join(', ')
      : 'INBOX',
    require_sender_authentication:
      source.require_sender_authentication !== false,
  };
}

function defaultSmtpSecurity(port) {
  return port === EMAIL_SMTP_PORT ? 'ssl' : 'starttls';
}

export function getAgentItems(result) {
  return result.agents
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
      typeof status.connected === 'boolean'
        ? status.connected
        : typeof status.running === 'boolean'
          ? status.running
          : channel.running;

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
      failure_reason: status.failure_reason || '',
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
    loaded: isPlainObject(value?.member_tools),
    member_tools: value?.member_tools ?? null,
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
    member_tools: value.member_tools ?? null,
    effective_member_tools: value.effective_member_tools ?? null,
    admin_user_ids: Array.isArray(value.admin_user_ids)
      ? value.admin_user_ids.filter(
          (userId) => typeof userId === 'string' && userId.length > 0,
        )
      : [],
    participants,
  };
}

function formatAllowedChatIds(value) {
  if (!Array.isArray(value)) {
    return '';
  }

  return value
    .filter((item) => typeof item === 'string' || Number.isSafeInteger(item))
    .map((item) => String(item))
    .join(', ');
}

export function createChannelPanelState() {
  return {
    channels: [],
    loading: false,
    error: null,
    selectedChannelId: null,
  };
}

export function applyChannelPanelList(state, result) {
  const normalizedChannels = normalizeChannels(result?.channels);
  const selectedChannelId = asOptionalText(state?.selectedChannelId);

  return {
    ...(isPlainObject(state) ? state : {}),
    channels: normalizedChannels,
    loading: false,
    error: null,
    selectedChannelId:
      selectedChannelId !== null &&
      normalizedChannels.some((channel) => channel.id === selectedChannelId)
        ? selectedChannelId
        : null,
  };
}

export function buildChannelCreatePayload(form) {
  const source = ensureForm(form, 'create');

  return {
    id: requiredText(source.id, 'id'),
    platform: requiredPlatform(source.platform),
    agent_id: requiredText(source.agent_id, 'agent_id'),
    dm_scope: resolveDmScope(source.dm_scope),
    allowed_chat_ids: parseAllowedChatIds(source.allowed_chat_ids),
    ...connectionFields(source),
    enabled:
      source.platform === 'whatsapp'
        ? false
        : hasOwn(source, 'enabled')
          ? requiredBoolean(source.enabled, 'enabled')
          : true,
  };
}

export function buildChannelUpdatePayload(form, baseline = null) {
  const source = ensureForm(form, 'update');
  const payload = {
    id: requiredText(source.id, 'id'),
  };

  let hasUpdates = false;

  if (hasOwn(source, 'platform')) {
    payload.platform = requiredPlatform(source.platform);
    hasUpdates = true;
  }

  if (hasOwn(source, 'agent_id')) {
    payload.agent_id = requiredText(source.agent_id, 'agent_id');
    hasUpdates = true;
  }

  if (hasOwn(source, 'dm_scope')) {
    payload.dm_scope = requiredDmScope(source.dm_scope);
    hasUpdates = true;
  }

  if (hasOwn(source, 'allowed_chat_ids')) {
    payload.allowed_chat_ids = parseAllowedChatIds(source.allowed_chat_ids);
    hasUpdates = true;
  }

  if (hasOwn(source, 'token_env_var')) {
    payload.token_env_var =
      source.platform === 'whatsapp'
        ? ''
        : requiredText(source.token_env_var, 'token_env_var');
    hasUpdates = true;
  }

  for (const key of ['app_token_env_var', 'server_url']) {
    if (hasOwn(source, key)) {
      payload[key] = source[key].trim();
      hasUpdates = true;
    }
  }

  if (source.platform === 'email' && hasOwn(source, 'email_address')) {
    payload.email = emailSettings(source);
    hasUpdates = true;
  }

  if (hasOwn(source, 'enabled')) {
    payload.enabled = requiredBoolean(source.enabled, 'enabled');
    hasUpdates = true;
  }

  if (!hasUpdates) {
    throw new TypeError('At least one channel field is required for update');
  }

  // An editor owns only the fields it changed. An invalidation can still be
  // in flight when a save starts, so sending the whole form could restore an
  // Agent binding or access rule another accessor already changed.
  if (baseline) {
    const previous = buildChannelUpdatePayload(baseline);
    for (const key of Object.keys(payload)) {
      if (
        key !== 'id' &&
        JSON.stringify(payload[key]) === JSON.stringify(previous[key])
      ) {
        delete payload[key];
      }
    }
  }

  return payload;
}

function normalizeChannels(channels) {
  const rawChannels = Array.isArray(channels) ? channels : [];
  const normalizedChannels = rawChannels
    .map((channel) => normalizeChannel(channel))
    .filter((channel) => channel !== null);

  normalizedChannels.sort((left, right) => left.id.localeCompare(right.id));

  return normalizedChannels;
}

function normalizeChannel(channel) {
  const id = asOptionalText(channel?.id);
  if (id === null) {
    return null;
  }

  let allowedChatIds;
  try {
    allowedChatIds = parseAllowedChatIds(channel?.allowed_chat_ids);
  } catch {
    allowedChatIds = [];
  }

  const dmScopeCandidate = asOptionalText(channel?.dm_scope);

  return {
    id,
    platform: asOptionalText(channel?.platform) ?? CHANNEL_PLATFORM_TELEGRAM,
    agent_id: asOptionalText(channel?.agent_id) ?? '',
    dm_scope:
      dmScopeCandidate !== null && CHANNEL_DM_SCOPE_SET.has(dmScopeCandidate)
        ? dmScopeCandidate
        : CHANNEL_DM_SCOPE_PER_CONVERSATION,
    allowed_chat_ids: allowedChatIds,
    token_env_var: asOptionalText(channel?.token_env_var) ?? '',
    app_token_env_var: asOptionalText(channel?.app_token_env_var) ?? '',
    server_url: asOptionalText(channel?.server_url) ?? '',
    email: isPlainObject(channel?.email) ? channel.email : null,
    enabled: booleanWithDefault(channel?.enabled, true),
    running: optionalBoolean(channel?.running),
  };
}

function ensureForm(form, action) {
  if (!isPlainObject(form)) {
    throw new TypeError(`Channel ${action} form must be an object`);
  }

  return form;
}

function requiredText(value, fieldName) {
  const normalized = asOptionalText(value);
  if (normalized === null) {
    throw new TypeError(`${fieldName} must be a non-empty string`);
  }

  return normalized;
}

function requiredPlatform(value) {
  const platform = requiredText(value, 'platform');
  if (!CHANNEL_PLATFORM_SET.has(platform)) {
    const options = CHANNEL_PLATFORMS.join(', ');
    throw new TypeError(`platform must be one of: ${options}`);
  }

  return platform;
}

function resolveDmScope(value) {
  const normalized = asOptionalText(value) ?? CHANNEL_DM_SCOPE_PER_CONVERSATION;
  return validateDmScope(normalized);
}

function requiredDmScope(value) {
  return validateDmScope(requiredText(value, 'dm_scope'));
}

function validateDmScope(dmScope) {
  if (!CHANNEL_DM_SCOPE_SET.has(dmScope)) {
    const options = CHANNEL_DM_SCOPES.join(', ');
    throw new TypeError(`dm_scope must be one of: ${options}`);
  }

  return dmScope;
}

function parseAllowedChatIds(value) {
  const values = normalizeAllowedChatIdValues(value);
  const result = [];
  const seen = new Set();

  for (const item of values) {
    const chatId = parseAllowedChatId(item);
    if (seen.has(chatId)) {
      continue;
    }
    seen.add(chatId);
    result.push(chatId);
  }

  return result;
}

function normalizeAllowedChatIdValues(value) {
  if (value === null || value === undefined) {
    return [];
  }

  if (Array.isArray(value)) {
    return value;
  }

  if (typeof value === 'string') {
    if (value.trim().length === 0) {
      return [];
    }

    return value
      .split(/[\n,]/u)
      .map((item) => item.trim())
      .filter((item) => item.length > 0);
  }

  throw new TypeError(
    'allowed_chat_ids must be an array or a comma/newline separated string',
  );
}

function parseAllowedChatId(value) {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError('allowed_chat_ids must contain integers only');
    }

    return String(value);
  }

  if (typeof value === 'string') {
    const normalized = value.trim();
    if (!normalized)
      throw new TypeError('allowed_chat_ids must contain non-empty IDs');
    return normalized;
  }

  throw new TypeError('allowed_chat_ids must contain integers only');
}

function connectionFields(source) {
  if (source.platform === 'whatsapp') return {};
  const fields = {
    token_env_var: requiredText(source.token_env_var, 'token_env_var'),
  };
  if (source.platform === 'slack')
    fields.app_token_env_var = requiredText(
      source.app_token_env_var,
      'app_token_env_var',
    );
  if (source.platform === 'mattermost')
    fields.server_url = requiredText(source.server_url, 'server_url');
  if (source.platform === 'email') fields.email = emailSettings(source);
  return fields;
}

// The complete email account object; empty optional fields take their defaults.
function emailSettings(source) {
  const address = requiredText(source.email_address, 'email_address');
  const smtpPort = portNumber(source.smtp_port, EMAIL_SMTP_PORT, 'smtp_port');
  const folders = String(source.email_folders ?? '')
    .split(/[\n,]/u)
    .map((folder) => folder.trim())
    .filter((folder) => folder.length > 0);
  return {
    address,
    username: asOptionalText(source.email_username) ?? address,
    imap_host: requiredText(source.imap_host, 'imap_host'),
    imap_port: portNumber(source.imap_port, EMAIL_IMAP_PORT, 'imap_port'),
    smtp_host: requiredText(source.smtp_host, 'smtp_host'),
    smtp_port: smtpPort,
    smtp_security:
      asOptionalText(source.smtp_security) ?? defaultSmtpSecurity(smtpPort),
    folders: folders.length > 0 ? folders : ['INBOX'],
    require_sender_authentication:
      source.require_sender_authentication !== false,
  };
}

function portNumber(value, fallback, fieldName) {
  const text = String(value ?? '').trim();
  if (!text) return fallback;
  const port = Number(text);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new TypeError(`${fieldName} must be a port number from 1 to 65535`);
  }
  return port;
}

function requiredBoolean(value, fieldName) {
  const parsed = optionalBoolean(value);
  if (parsed === null) {
    throw new TypeError(`${fieldName} must be a boolean`);
  }

  return parsed;
}

function optionalBoolean(value) {
  if (typeof value === 'boolean') {
    return value;
  }

  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (normalized === 'true') {
      return true;
    }
    if (normalized === 'false') {
      return false;
    }
  }

  return null;
}

function booleanWithDefault(value, fallback) {
  const parsed = optionalBoolean(value);
  return parsed === null ? fallback : parsed;
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}
