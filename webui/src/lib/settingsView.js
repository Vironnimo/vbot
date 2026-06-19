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

export const AGENT_DEFAULTS_FIELDS = Object.freeze([
  'model',
  'fallback_model',
  'temperature',
  'thinking_effort',
]);
export const AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT =
  '__thinking_effort_no_default__';
export const RECALL_BACKEND_JSONL_SCAN = 'jsonl_scan';
export const RECALL_BACKEND_SQLITE_FTS = 'sqlite_fts';
export const WEB_SEARCH_PROVIDER_BRAVE = 'brave';
export const WEB_SEARCH_PROVIDER_SEARXNG = 'searxng';

const RECALL_BACKEND_DEFAULTS = Object.freeze([
  RECALL_BACKEND_JSONL_SCAN,
  RECALL_BACKEND_SQLITE_FTS,
]);
const WEB_SEARCH_PROVIDER_DEFAULTS = Object.freeze([
  WEB_SEARCH_PROVIDER_BRAVE,
  WEB_SEARCH_PROVIDER_SEARXNG,
]);
const DEFAULT_SEARXNG_BASE_URL = 'http://localhost:8888';

const AGENT_DEFAULT_THINKING_EFFORT_OPTIONS = Object.freeze([
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]);

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

export function normalizeAgentDefaultsSettings(rawSettings) {
  const agentDefaults = resolveAgentDefaultsSource(rawSettings);

  return {
    model: textOrEmpty(agentDefaults.model),
    fallback_model: textOrEmpty(agentDefaults.fallback_model),
    temperature: normalizeAgentDefaultsTemperature(agentDefaults.temperature),
    thinking_effort: normalizeAgentDefaultsThinkingEffort(
      agentDefaults.thinking_effort,
    ),
  };
}

export function buildAgentDefaultsPayload(formValues) {
  const values = formValues && typeof formValues === 'object' ? formValues : {};

  return {
    defaults: {
      agent: {
        model: normalizeAgentDefaultsTextForPayload(values.model),
        fallback_model: normalizeAgentDefaultsTextForPayload(
          values.fallback_model,
        ),
        temperature: normalizeAgentDefaultsTemperature(values.temperature),
        thinking_effort: normalizeAgentDefaultsThinkingEffortForPayload(
          values.thinking_effort,
        ),
      },
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

// --- Extensions ---------------------------------------------------------------

const EXTENSION_HOOK_EVENT_ORDER = [
  'run_start',
  'before_agent_start',
  'context',
  'tool_call',
  'tool_result',
  'run_end',
];

export function applyExtensionsPanelList(result) {
  const extensions = Array.isArray(result?.extensions) ? result.extensions : [];

  return extensions
    .filter(
      (extension) =>
        extension &&
        typeof extension === 'object' &&
        typeof extension.name === 'string' &&
        extension.name.length > 0,
    )
    .map((extension) => ({
      name: extension.name,
      status: textOrFallback(extension.status, 'loaded'),
      disabled: extension.disabled === true,
      version: textOrEmpty(extension.version),
      description: textOrEmpty(extension.description),
      error: textOrEmpty(extension.error),
      capabilityErrors: Array.isArray(extension.capability_errors)
        ? extension.capability_errors.filter(
            (entry) => typeof entry === 'string' && entry.length > 0,
          )
        : [],
      config:
        extension.config && typeof extension.config === 'object'
          ? extension.config
          : {},
      capabilities: normalizeExtensionCapabilities(extension.capabilities),
    }));
}

function normalizeExtensionCapabilities(capabilities) {
  const source =
    capabilities && typeof capabilities === 'object' ? capabilities : {};
  const hooks =
    source.hooks && typeof source.hooks === 'object' ? source.hooks : {};

  return {
    hooks: EXTENSION_HOOK_EVENT_ORDER.filter(
      (event) => Number(hooks[event]) > 0,
    ).map((event) => ({ event, count: Number(hooks[event]) })),
    tools: Array.isArray(source.tools)
      ? source.tools.filter(
          (tool) => typeof tool === 'string' && tool.length > 0,
        )
      : [],
    recallBackends: Array.isArray(source.recall_backends)
      ? source.recall_backends.filter(
          (backend) => typeof backend === 'string' && backend.length > 0,
        )
      : [],
    startup: source.startup === true,
    shutdown: source.shutdown === true,
  };
}

export function extensionStatusChipVariant(status) {
  if (status === 'loaded') {
    return 'success';
  }
  if (status === 'failed') {
    return 'error';
  }
  return 'warn';
}

export function summarizeExtensionCapabilities(capabilities, translate) {
  const normalized =
    capabilities && Array.isArray(capabilities.hooks)
      ? capabilities
      : normalizeExtensionCapabilities(capabilities);
  const parts = [];

  if (normalized.hooks.length > 0) {
    const hookSummary = normalized.hooks
      .map((hook) => `${hook.event}(${hook.count})`)
      .join(', ');
    parts.push(
      `${translate('settings.extensions.hooks', 'Hooks')}: ${hookSummary}`,
    );
  }
  if (normalized.tools.length > 0) {
    parts.push(
      `${translate('settings.extensions.tools', 'Tools')}: ${normalized.tools.join(', ')}`,
    );
  }
  if (normalized.recallBackends.length > 0) {
    parts.push(
      `${translate('settings.extensions.recallBackends', 'Recall backends')}: ${normalized.recallBackends.join(', ')}`,
    );
  }
  if (normalized.startup) {
    parts.push(translate('settings.extensions.startup', 'startup'));
  }
  if (normalized.shutdown) {
    parts.push(translate('settings.extensions.shutdown', 'shutdown'));
  }

  return parts.join(' · ');
}

export function formatExtensionConfig(config) {
  const value = config && typeof config === 'object' ? config : {};
  if (Object.keys(value).length === 0) {
    return '';
  }
  return JSON.stringify(value, null, 2);
}

export function parseExtensionConfigDraft(text) {
  const trimmed = typeof text === 'string' ? text.trim() : '';
  if (trimmed.length === 0) {
    return { ok: true, value: {} };
  }

  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (error) {
    return { ok: false, error: error.message };
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: 'not-object' };
  }

  return { ok: true, value: parsed };
}

export function buildExtensionsUpdatePayload(extensions, override = {}) {
  const items = Array.isArray(extensions) ? extensions : [];
  const disabled = [];
  const config = {};

  for (const extension of items) {
    const name = textOrEmpty(extension?.name);
    if (!name) {
      continue;
    }

    const isOverride = name === override.name;
    const extensionDisabled =
      isOverride && typeof override.disabled === 'boolean'
        ? override.disabled
        : extension.disabled === true;
    if (extensionDisabled) {
      disabled.push(name);
    }

    const extensionConfig =
      isOverride && override.config && typeof override.config === 'object'
        ? override.config
        : extension.config && typeof extension.config === 'object'
          ? extension.config
          : {};
    if (Object.keys(extensionConfig).length > 0) {
      config[name] = extensionConfig;
    }
  }

  return { extensions: { disabled, config } };
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
  const threshold = Number(
    String(compaction.threshold).trim().replace(',', '.'),
  );
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

export function normalizeRecallSettings(rawSettings) {
  const recall = rawSettings?.recall ?? {};
  const availableBackends = normalizeRecallBackends(recall.available_backends);
  const backend =
    typeof recall.backend === 'string' &&
    availableBackends.includes(recall.backend)
      ? recall.backend
      : RECALL_BACKEND_JSONL_SCAN;

  return {
    backend,
    available_backends: availableBackends,
  };
}

export function getRecallSettings(settings) {
  return normalizeRecallSettings(settings);
}

export function buildRecallSettingsPayload(formValues) {
  return {
    recall: {
      backend: normalizeRecallSettings({ recall: formValues }).backend,
    },
  };
}

export function buildRecallBackendOptions(recallSettings, translate) {
  return normalizeRecallBackends(recallSettings?.available_backends).map(
    (backend) => ({
      value: backend,
      label: translate(`settings.recall.backends.${backend}`, backend),
    }),
  );
}

export function normalizeWebSearchSettings(rawSettings) {
  const webSearch = rawSettings?.web_search ?? {};
  const availableProviders = normalizeWebSearchProviders(
    webSearch.available_providers,
  );
  const provider =
    typeof webSearch.provider === 'string' &&
    availableProviders.includes(webSearch.provider)
      ? webSearch.provider
      : (availableProviders[0] ?? WEB_SEARCH_PROVIDER_BRAVE);
  const searxngBaseUrl = textOrFallback(
    webSearch.searxng?.base_url,
    DEFAULT_SEARXNG_BASE_URL,
  );

  return {
    provider,
    available_providers: availableProviders,
    searxng: {
      base_url: searxngBaseUrl,
    },
  };
}

export function getWebSearchSettings(settings) {
  return normalizeWebSearchSettings(settings);
}

export function buildWebSearchSettingsPayload(formValues) {
  const normalized = normalizeWebSearchSettings({ web_search: formValues });

  return {
    web_search: {
      provider: normalized.provider,
      searxng: {
        base_url: normalized.searxng.base_url,
      },
    },
  };
}

export function buildWebSearchProviderOptions(webSearchSettings, translate) {
  return normalizeWebSearchProviders(
    webSearchSettings?.available_providers,
  ).map((provider) => ({
    value: provider,
    label: translate(`settings.webSearch.providers.${provider}`, provider),
  }));
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

export const DEFAULT_ACCOUNT_ID = 'default';

const ACCOUNT_ID_PATTERN = /^[a-z0-9][a-z0-9_]{0,31}$/;

export const ACCOUNT_SOURCE_PROCESS_ENV = 'process_env';
export const ACCOUNT_SOURCE_DATA_DIR = 'data_dir';
export const ACCOUNT_SOURCE_OAUTH = 'oauth';

export function isValidAccountId(value) {
  return typeof value === 'string' && ACCOUNT_ID_PATTERN.test(value);
}

export function normalizeAccountId(value) {
  const trimmed = typeof value === 'string' ? value.trim() : '';

  return trimmed.length > 0 ? trimmed : DEFAULT_ACCOUNT_ID;
}

export function getConnectionAccounts(connection) {
  if (!Array.isArray(connection?.accounts)) {
    return [];
  }

  return connection.accounts.filter(
    (account) => typeof account?.id === 'string' && account.id.length > 0,
  );
}

export function isAccountUsable(account) {
  return account?.usable === true;
}

export function connectionHasUsableAccount(connection) {
  return getConnectionAccounts(connection).some(isAccountUsable);
}

export function isProcessEnvAccount(account) {
  return account?.source === ACCOUNT_SOURCE_PROCESS_ENV;
}

export function isOAuthAccount(account) {
  return account?.source === ACCOUNT_SOURCE_OAUTH;
}

export function accountDisplayName(account, translate) {
  if (account?.id === DEFAULT_ACCOUNT_ID) {
    return translate('settings.providers.accounts.defaultLabel', 'Default');
  }

  return typeof account?.id === 'string' ? account.id : '';
}

export function describeAccountSource(account, translate) {
  if (account?.source === ACCOUNT_SOURCE_PROCESS_ENV) {
    return translate(
      'settings.providers.accounts.source.processEnv',
      'Process env',
    );
  }

  if (account?.source === ACCOUNT_SOURCE_DATA_DIR) {
    return translate('settings.providers.accounts.source.dataDir', '.env file');
  }

  if (account?.source === ACCOUNT_SOURCE_OAUTH) {
    return translate('settings.providers.accounts.source.oauth', 'OAuth');
  }

  return '';
}

// Client-side preview of the credential key the server derives for an
// account (e.g. OPENAI_API_KEY + "work" -> OPENAI_API_KEY__WORK). The
// authoritative value comes back in the provider.set_key response.
export function deriveAccountCredentialKey(baseKey, account) {
  const base = typeof baseKey === 'string' ? baseKey : '';
  const normalized = normalizeAccountId(account);

  if (base.length === 0 || normalized === DEFAULT_ACCOUNT_ID) {
    return base;
  }

  return `${base}__${normalized.toUpperCase()}`;
}

export function connectionSupportsAddAccount(connection) {
  return (
    connection?.type === 'api_key' || isOAuthDeviceFlowConnection(connection)
  );
}

export function isConnectionConfigured(connection) {
  return (
    connection?.configured === true ||
    connection?.usable === true ||
    connectionHasUsableAccount(connection)
  );
}

export function providerHasConfiguredConnection(provider) {
  return (
    Array.isArray(provider?.connections) &&
    provider.connections.some(isConnectionConfigured)
  );
}

export function getConnectedProviderItems(settings) {
  return getProviderItems(settings).filter(providerHasConfiguredConnection);
}

export function getConfiguredConnections(provider) {
  return Array.isArray(provider?.connections)
    ? provider.connections.filter(isConnectionConfigured)
    : [];
}

export function isConnectionAddable(connection) {
  if (isConnectionConfigured(connection)) {
    return false;
  }

  if (connection?.type === 'api_key') {
    return true;
  }

  return isOAuthDeviceFlowConnection(connection);
}

export function getAddableConnections(provider) {
  return Array.isArray(provider?.connections)
    ? provider.connections.filter(isConnectionAddable)
    : [];
}

export function getAddProviderCandidates(settings) {
  return getProviderItems(settings).filter(
    (provider) =>
      !providerHasConfiguredConnection(provider) &&
      getAddableConnections(provider).length > 0,
  );
}

export function isOAuthConnection(connection) {
  return connection?.type === 'oauth';
}

export function isOAuthDeviceFlowConnection(connection) {
  return isOAuthConnection(connection) && connection?.connectable === true;
}

export function getPublicConnectionId(connection) {
  return typeof connection?.id === 'string' ? connection.id : '';
}

export function buildProviderConnectPayload(
  providerId,
  connectionId,
  account = DEFAULT_ACCOUNT_ID,
) {
  return {
    provider_id: providerId,
    connection_id: connectionId,
    account: normalizeAccountId(account),
  };
}

export function buildProviderDisconnectPayload(
  providerId,
  connectionId,
  account = DEFAULT_ACCOUNT_ID,
) {
  return {
    provider_id: providerId,
    connection_id: connectionId,
    account: normalizeAccountId(account),
  };
}

export function getPersistedLanguageId(settings) {
  return settings?.appearance?.language ?? '';
}

// Chat reading-column width preference (mirrors the backend
// SUPPORTED_APPEARANCE_CHAT_WIDTHS / DEFAULT_APPEARANCE_CHAT_WIDTH).
export const CHAT_WIDTH_OPTIONS = ['comfortable', 'wide', 'full'];
export const DEFAULT_CHAT_WIDTH = 'comfortable';

export function getPersistedChatWidth(settings) {
  const value = settings?.appearance?.chat_width;
  return CHAT_WIDTH_OPTIONS.includes(value) ? value : DEFAULT_CHAT_WIDTH;
}

export function buildChatWidthOptions() {
  return CHAT_WIDTH_OPTIONS.map((id) => ({
    id,
    labelKey: `settings.appearance.chatWidth.${id}`,
    labelFallback: id,
  }));
}

// The appearance section is normalized as a whole on the backend (a missing
// field resets to its default), so both controls always save together.
export function isAppearanceSaveDisabled({
  loading,
  saving,
  selectedLanguageId,
  selectedChatWidth,
  persistedLanguageId,
  persistedChatWidth,
}) {
  if (loading || saving || selectedLanguageId.length === 0) {
    return true;
  }
  return (
    selectedLanguageId === persistedLanguageId &&
    selectedChatWidth === persistedChatWidth
  );
}

export function createAppearanceUpdatePayload({ language, chatWidth }) {
  return {
    appearance: {
      language,
      chat_width: chatWidth,
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

function normalizeRecallBackends(backends) {
  const values = Array.isArray(backends) ? backends : RECALL_BACKEND_DEFAULTS;
  const normalized = values
    .map((backend) => textOrEmpty(backend))
    .filter((backend) => backend.length > 0);

  return normalized.length > 0
    ? Array.from(new Set(normalized))
    : [...RECALL_BACKEND_DEFAULTS];
}

function normalizeWebSearchProviders(providers) {
  const values = Array.isArray(providers)
    ? providers
    : WEB_SEARCH_PROVIDER_DEFAULTS;
  const normalized = values
    .map((provider) => textOrEmpty(provider))
    .filter((provider) => provider.length > 0);

  return normalized.length > 0
    ? Array.from(new Set(normalized))
    : [...WEB_SEARCH_PROVIDER_DEFAULTS];
}

function resolveAgentDefaultsSource(rawSettings) {
  const defaults = rawSettings?.defaults;

  if (defaults && typeof defaults === 'object' && !Array.isArray(defaults)) {
    const agentDefaults = defaults.agent;

    if (
      agentDefaults &&
      typeof agentDefaults === 'object' &&
      !Array.isArray(agentDefaults)
    ) {
      return agentDefaults;
    }

    return {};
  }

  if (
    rawSettings &&
    typeof rawSettings === 'object' &&
    !Array.isArray(rawSettings) &&
    AGENT_DEFAULTS_FIELDS.some((field) =>
      Object.prototype.hasOwnProperty.call(rawSettings, field),
    )
  ) {
    return rawSettings;
  }

  return {};
}

function normalizeAgentDefaultsTemperature(value) {
  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return null;
  }

  // Tolerate a comma decimal separator typed in comma-decimal locales.
  const numberValue = Number(normalized.replace(',', '.'));
  return Number.isFinite(numberValue) ? numberValue : null;
}

function normalizeAgentDefaultsTextForPayload(value) {
  const normalized = textOrEmpty(value);
  return normalized.length > 0 ? normalized : null;
}

function normalizeAgentDefaultsThinkingEffortForPayload(value) {
  if (value === AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT) {
    return null;
  }

  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return '';
  }

  return AGENT_DEFAULT_THINKING_EFFORT_OPTIONS.includes(normalized)
    ? normalized
    : null;
}

function normalizeAgentDefaultsThinkingEffort(value) {
  if (value === AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT) {
    return null;
  }

  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return '';
  }

  return AGENT_DEFAULT_THINKING_EFFORT_OPTIONS.includes(normalized)
    ? normalized
    : null;
}
