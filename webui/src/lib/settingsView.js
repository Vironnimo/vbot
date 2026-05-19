import {
  CHANNEL_DM_SCOPE_PER_CONVERSATION,
  CHANNEL_DM_SCOPES,
  CHANNEL_PLATFORM_TELEGRAM,
  CHANNEL_PLATFORMS,
  applyChannelList,
  buildCreatePayload,
  buildUpdatePayload,
  createChannelSettingsState,
} from './channelSettings.js';

export const SETTINGS_LAYOUT_CLASS = 'settings-layout view active';

export const CHANNEL_FORM_MODE_CREATE = 'create';
export const CHANNEL_FORM_MODE_EDIT = 'edit';

export { CHANNEL_DM_SCOPES, CHANNEL_PLATFORM_TELEGRAM, CHANNEL_PLATFORMS };

export const SUBAGENT_SETTINGS_DEFAULTS = Object.freeze({
  max_subagent_depth: 4,
  max_subagents_per_turn: 8,
  subagent_timeout_minutes: 60,
});

const COMPACTION_SETTING_DEFAULTS = Object.freeze({
  auto: true,
  threshold: 0.8,
  tail_tokens: 15000,
  summary_model: null,
});

function positiveIntegerOrDefault(value, fallback) {
  const numberValue = Number(value);

  return Number.isInteger(numberValue) && numberValue > 0
    ? numberValue
    : fallback;
}

export function buildLanguageOptions(appearance) {
  const availableLanguages = Array.isArray(appearance?.available_languages)
    ? appearance.available_languages
    : [];
  const languageIds =
    availableLanguages.length > 0
      ? availableLanguages
      : appearance?.language
        ? [appearance.language]
        : ['en'];

  return languageIds.map((languageId) => ({
    id: languageId,
    labelKey: `settings.language.${languageId}`,
    labelFallback: languageId,
  }));
}

export function formatServerHost(server, translate) {
  if (
    typeof server?.listen_host === 'string' &&
    server.listen_host.length > 0 &&
    Number.isFinite(server.listen_port)
  ) {
    return `${server.listen_host}:${server.listen_port}`;
  }

  return translate('common.unknown', 'Unknown');
}

export function getDataDirectoryValue(settings, translate) {
  return (
    settings?.general?.data_directory ?? translate('common.unknown', 'Unknown')
  );
}

export function getDefaultSkillDirectoryValue(settings, translate) {
  return (
    settings?.skills?.default_directory ??
    settings?.general?.default_skill_directory ??
    translate('common.unknown', 'Unknown')
  );
}

export function getSkillDirectories(settings) {
  return Array.isArray(settings?.skills?.directories)
    ? normalizeSkillDirectories(settings.skills.directories)
    : [];
}

export function normalizeSkillDirectories(directories) {
  if (!Array.isArray(directories)) {
    return [];
  }

  return directories
    .map((directory) =>
      directory === null || directory === undefined
        ? ''
        : String(directory).trim(),
    )
    .filter((directory) => directory.length > 0);
}

export function createSkillDirectoriesUpdatePayload(directories) {
  return {
    skills: {
      directories: normalizeSkillDirectories(directories),
    },
  };
}

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

    return {
      ...channel,
      running,
      enabled,
    };
  });
}

export function channelEnabledChipClass(enabled) {
  return enabled ? 'chip-green' : 'chip-amber';
}

export function channelRunningChipClass(running) {
  if (running === true) {
    return 'chip-green';
  }

  if (running === false) {
    return 'chip-amber';
  }

  return 'chip-orange';
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

export function normalizeSubAgentSettings(rawSettings) {
  const subagents = rawSettings?.subagents ?? {};

  return {
    max_subagent_depth: positiveIntegerOrDefault(
      subagents.max_subagent_depth,
      SUBAGENT_SETTINGS_DEFAULTS.max_subagent_depth,
    ),
    max_subagents_per_turn: positiveIntegerOrDefault(
      subagents.max_subagents_per_turn,
      SUBAGENT_SETTINGS_DEFAULTS.max_subagents_per_turn,
    ),
    subagent_timeout_minutes: positiveIntegerOrDefault(
      subagents.subagent_timeout_minutes,
      SUBAGENT_SETTINGS_DEFAULTS.subagent_timeout_minutes,
    ),
  };
}

export function normalizeCompactionSettings(rawSettings) {
  const compaction = rawSettings?.compaction ?? {};
  const threshold = Number(compaction.threshold);
  const summaryModel =
    typeof compaction.summary_model === 'string'
      ? compaction.summary_model.trim()
      : '';

  return {
    auto:
      typeof compaction.auto === 'boolean'
        ? compaction.auto
        : COMPACTION_SETTING_DEFAULTS.auto,
    threshold: Number.isFinite(threshold)
      ? threshold
      : COMPACTION_SETTING_DEFAULTS.threshold,
    tail_tokens: positiveIntegerOrDefault(
      compaction.tail_tokens,
      COMPACTION_SETTING_DEFAULTS.tail_tokens,
    ),
    summary_model:
      summaryModel.length > 0
        ? summaryModel
        : COMPACTION_SETTING_DEFAULTS.summary_model,
  };
}

export function buildCompactionSettingsPayload(formValues) {
  return {
    compaction: normalizeCompactionSettings({
      compaction: formValues,
    }),
  };
}

export function getCompactionSettings(settings) {
  return normalizeCompactionSettings(settings);
}

export function buildSubAgentSettingsPayload(formValues) {
  return {
    subagents: normalizeSubAgentSettings({
      subagents: formValues,
    }),
  };
}

export function getProviderItems(settings) {
  return Array.isArray(settings?.providers?.items)
    ? settings.providers.items
    : [];
}

export function isOAuthConnection(connection) {
  return connection?.type === 'oauth';
}

export function getPublicConnectionId(connection) {
  return typeof connection?.id === 'string' ? connection.id : '';
}

export function getOAuthConnectionStatus(
  providerItems,
  connectionId,
  flowActive = false,
) {
  if (flowActive) {
    return 'pending';
  }

  const connection = getOAuthConnection(providerItems, connectionId);

  return connection?.configured === true || connection?.usable === true
    ? 'connected'
    : 'disconnected';
}

function getOAuthConnection(providerItems, connectionId) {
  const providers = Array.isArray(providerItems) ? providerItems : [];

  for (const provider of providers) {
    const connections = Array.isArray(provider?.connections)
      ? provider.connections
      : [];
    const connection = connections.find((item) => item.id === connectionId);

    if (connection) {
      return connection;
    }
  }

  return null;
}

export function buildProviderConnectPayload(providerId, connectionId) {
  return { provider_id: providerId, connection_id: connectionId };
}

export function buildProviderDisconnectPayload(providerId, connectionId) {
  return { provider_id: providerId, connection_id: connectionId };
}

export function getPersistedLanguageId(settings) {
  return settings?.appearance?.language ?? '';
}

export function isLanguageSaveDisabled({
  loading,
  saving,
  selectedLanguageId,
  persistedLanguageId,
}) {
  return (
    loading ||
    saving ||
    selectedLanguageId.length === 0 ||
    selectedLanguageId === persistedLanguageId
  );
}

export function createLanguageUpdatePayload(languageId) {
  return {
    appearance: {
      language: languageId,
    },
  };
}

export function describeProvider(provider, translate) {
  const fragments = [];

  if (
    typeof provider?.credential_key === 'string' &&
    provider.credential_key.length > 0
  ) {
    fragments.push(
      translate(
        'settings.providers.description.credentialKey',
        'Credential key: {credentialKey}.',
        {
          credentialKey: provider.credential_key,
        },
      ),
    );
  }

  if (typeof provider?.base_url === 'string' && provider.base_url.length > 0) {
    fragments.push(
      translate(
        'settings.providers.description.baseUrl',
        'Endpoint: {baseUrl}.',
        {
          baseUrl: provider.base_url,
        },
      ),
    );
  }

  if (Number.isFinite(provider?.model_count)) {
    fragments.push(
      translate(
        'settings.providers.description.modelCount',
        '{count} models available.',
        {
          count: provider.model_count,
        },
      ),
    );
  }

  return (
    fragments.join(' ') ||
    translate(
      'settings.providers.description.none',
      'Provider metadata is not available yet.',
    )
  );
}

export function providerStatusKey(provider) {
  if (typeof provider?.status === 'string' && provider.status.length > 0) {
    return provider.status;
  }

  if (provider?.credentials_configured === true) {
    return 'configured';
  }

  if (
    typeof provider?.credential_key === 'string' &&
    provider.credential_key.length > 0
  ) {
    return 'missing_credentials';
  }

  return 'placeholder';
}

export function providerStatusClass(provider) {
  const status = providerStatusKey(provider);

  if (status === 'configured') {
    return 'chip-green';
  }

  if (status === 'missing_credentials') {
    return 'chip-amber';
  }

  return 'chip-orange';
}

export function providerStatusLabel(provider, translate) {
  const status = providerStatusKey(provider);

  if (status === 'configured') {
    return translate('settings.providers.status.configured', 'Configured');
  }

  if (status === 'missing_credentials') {
    return translate(
      'settings.providers.status.missingCredentials',
      'Missing credentials',
    );
  }

  return translate('settings.providers.status.placeholder', 'Placeholder');
}

export function normalizeSettingsForDisplay(settings, translate) {
  return {
    serverHostValue: formatServerHost(settings?.general?.server, translate),
    dataDirectoryValue: getDataDirectoryValue(settings, translate),
    defaultSkillDirectoryValue: getDefaultSkillDirectoryValue(
      settings,
      translate,
    ),
    skillDirectories: getSkillDirectories(settings),
    subAgentSettings: normalizeSubAgentSettings(settings),
    providerItems: getProviderItems(settings),
    availableLanguageOptions: buildLanguageOptions(settings?.appearance),
    persistedLanguageId: getPersistedLanguageId(settings),
  };
}

function textOrEmpty(value) {
  if (value === null || value === undefined) {
    return '';
  }

  return String(value).trim();
}

function textOrFallback(value, fallback) {
  const normalized = textOrEmpty(value);

  return normalized.length > 0 ? normalized : fallback;
}
