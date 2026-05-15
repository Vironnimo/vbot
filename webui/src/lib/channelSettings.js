export const CHANNEL_PLATFORM_TELEGRAM = 'telegram';
export const CHANNEL_PLATFORMS = Object.freeze([CHANNEL_PLATFORM_TELEGRAM]);

export const CHANNEL_DM_SCOPE_PER_CONVERSATION = 'per_conversation';
export const CHANNEL_DM_SCOPE_MAIN = 'main';
export const CHANNEL_DM_SCOPE_PER_PEER = 'per_peer';
export const CHANNEL_DM_SCOPE_PER_ACCOUNT_CHANNEL_PEER =
  'per_account_channel_peer';
export const CHANNEL_DM_SCOPES = Object.freeze([
  CHANNEL_DM_SCOPE_PER_CONVERSATION,
  CHANNEL_DM_SCOPE_MAIN,
  CHANNEL_DM_SCOPE_PER_PEER,
  CHANNEL_DM_SCOPE_PER_ACCOUNT_CHANNEL_PEER,
]);

const CHANNEL_PLATFORM_SET = new Set(CHANNEL_PLATFORMS);
const CHANNEL_DM_SCOPE_SET = new Set(CHANNEL_DM_SCOPES);

export function createChannelSettingsState() {
  return {
    channels: [],
    loading: false,
    error: null,
    selectedChannelId: null,
  };
}

export function applyChannelList(state, channels) {
  const normalizedChannels = normalizeChannels(channels);
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

export function buildCreatePayload(form) {
  const source = ensureForm(form, 'create');

  return {
    id: requiredText(source.id, 'id'),
    platform: requiredPlatform(source.platform),
    agent_id: requiredText(source.agent_id, 'agent_id'),
    dm_scope: resolveDmScope(source.dm_scope),
    allowed_chat_ids: parseAllowedChatIds(source.allowed_chat_ids),
    token_env_var: requiredText(source.token_env_var, 'token_env_var'),
    enabled: hasOwn(source, 'enabled')
      ? requiredBoolean(source.enabled, 'enabled')
      : true,
  };
}

export function buildUpdatePayload(form) {
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
    payload.token_env_var = requiredText(source.token_env_var, 'token_env_var');
    hasUpdates = true;
  }

  if (hasOwn(source, 'enabled')) {
    payload.enabled = requiredBoolean(source.enabled, 'enabled');
    hasUpdates = true;
  }

  if (!hasUpdates) {
    throw new TypeError('At least one channel field is required for update');
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

  let allowedChatIds = [];
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

    return value;
  }

  if (typeof value === 'string') {
    const normalized = value.trim();
    if (!/^-?\d+$/u.test(normalized)) {
      throw new TypeError('allowed_chat_ids must contain integers only');
    }

    const parsed = Number.parseInt(normalized, 10);
    if (!Number.isSafeInteger(parsed)) {
      throw new TypeError('allowed_chat_ids must contain integers only');
    }

    return parsed;
  }

  throw new TypeError('allowed_chat_ids must contain integers only');
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

function asOptionalText(value) {
  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
