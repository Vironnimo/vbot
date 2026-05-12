export const SETTINGS_LAYOUT_CLASS = 'settings-layout view active';

export const SUBAGENT_SETTINGS_DEFAULTS = Object.freeze({
  max_subagent_depth: 4,
  max_subagents_per_turn: 8,
  subagent_timeout_minutes: 60,
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
