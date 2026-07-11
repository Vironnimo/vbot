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
import { normalizeCompactionPolicy } from './compactionPolicy.js';

export const SETTINGS_LAYOUT_CLASS = 'settings-layout view active';

export const CHANNEL_FORM_MODE_CREATE = 'create';
export const CHANNEL_FORM_MODE_EDIT = 'edit';

export { CHANNEL_DM_SCOPES, CHANNEL_PLATFORM_TELEGRAM, CHANNEL_PLATFORMS };

// Presence rows for the General panel "Connected clients" list. Pure: passes
// the registry fields through and flags the caller's own row by matching the
// client-minted connection id (an empty own id matches nothing).
export function buildClientPresenceRows(clients, ownConnectionId) {
  if (!Array.isArray(clients)) {
    return [];
  }
  const ownId = typeof ownConnectionId === 'string' ? ownConnectionId : '';
  return clients.map((client) => {
    const connectionId =
      typeof client?.connection_id === 'string' ? client.connection_id : '';
    return {
      id: typeof client?.id === 'string' ? client.id : '',
      connectionId,
      accessor: typeof client?.accessor === 'string' ? client.accessor : '',
      browser: typeof client?.browser === 'string' ? client.browser : '',
      os: typeof client?.os === 'string' ? client.os : '',
      connectedAt:
        typeof client?.connected_at === 'string' ? client.connected_at : '',
      status: typeof client?.status === 'string' ? client.status : '',
      isOwn: ownId.length > 0 && connectionId === ownId,
    };
  });
}

const SUBAGENT_SETTINGS_DEFAULTS = Object.freeze({
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
const RECALL_BACKEND_JSONL_SCAN = 'jsonl_scan';
const RECALL_BACKEND_SQLITE_FTS = 'sqlite_fts';
const WEB_SEARCH_PROVIDER_BRAVE = 'brave';
const WEB_SEARCH_PROVIDER_SEARXNG = 'searxng';

const RECALL_BACKEND_DEFAULTS = Object.freeze([
  RECALL_BACKEND_JSONL_SCAN,
  RECALL_BACKEND_SQLITE_FTS,
]);
const WEB_SEARCH_PROVIDER_DEFAULTS = Object.freeze([
  WEB_SEARCH_PROVIDER_BRAVE,
  WEB_SEARCH_PROVIDER_SEARXNG,
]);
const DEFAULT_SEARXNG_BASE_URL = 'http://localhost:8888';
const WEB_SEARCH_DEFAULT_COUNT = 12;
const WEB_SEARCH_MIN_COUNT = 1;
const WEB_SEARCH_MAX_COUNT = 20;

const AGENT_DEFAULT_THINKING_EFFORT_OPTIONS = Object.freeze([
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]);

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

function normalizeSkillDirectories(directories) {
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

export function normalizeSessionTitleSettings(rawSettings) {
  const source =
    rawSettings?.session_titles &&
    typeof rawSettings.session_titles === 'object'
      ? rawSettings.session_titles
      : rawSettings && typeof rawSettings === 'object'
        ? rawSettings
        : {};

  return {
    enabled: source.enabled === true,
    model: textOrEmpty(source.model),
  };
}

export function buildSessionTitleSettingsPayload(formValues) {
  const normalized = normalizeSessionTitleSettings(formValues);
  return {
    session_titles: {
      enabled: normalized.enabled,
      model: normalized.model,
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
      overriddenBy: textOrEmpty(extension.overridden_by),
      capabilityErrors: Array.isArray(extension.capability_errors)
        ? extension.capability_errors.filter(
            (entry) => typeof entry === 'string' && entry.length > 0,
          )
        : [],
      config:
        extension.config && typeof extension.config === 'object'
          ? extension.config
          : {},
      settingsSchema: normalizeSchemaFields(extension.settings_schema),
      readyState: extension.ready_state === 'waiting' ? 'waiting' : 'ready',
      capabilities: normalizeExtensionCapabilities(extension.capabilities),
    }));
}

// Each capability tool is a ``{ name, ready }`` object; ``ready`` defaults to
// true when absent so an older-shaped payload never reads as "waiting".
function normalizeCapabilityTools(tools) {
  if (!Array.isArray(tools)) {
    return [];
  }
  return tools
    .filter(
      (tool) =>
        tool &&
        typeof tool === 'object' &&
        typeof tool.name === 'string' &&
        tool.name.length > 0,
    )
    .map((tool) => ({ name: tool.name, ready: tool.ready !== false }));
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
    tools: normalizeCapabilityTools(source.tools),
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
  // ``disabled`` and ``overridden`` share the same muted/neutral variant: both
  // are inert records the user cannot act on directly.
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
    const toolNames = normalized.tools
      .map((tool) => (typeof tool === 'string' ? tool : tool.name))
      .join(', ');
    parts.push(
      `${translate('settings.extensions.tools', 'Tools')}: ${toolNames}`,
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

/**
 * Describe an extension's derived waiting state for the Extensions panel (the
 * one place the waiting state is shown). Returns ``null`` when the extension is
 * ready. When waiting, returns the status hint and, if the schema (Phase 2)
 * declares unset secret fields, a line naming them by label:
 *   { hint, waitingFor }  // waitingFor is null when no unset secret is known
 */
export function describeExtensionWaiting(extension, translate) {
  if (!extension || extension.readyState !== 'waiting') {
    return null;
  }
  const hint = translate(
    'settings.extensions.waiting',
    'On, waiting for configuration',
  );
  const unsetSecretLabels = Array.isArray(extension.settingsSchema)
    ? extension.settingsSchema
        .filter((field) => field.type === 'secret' && field.set === false)
        .map((field) => field.label)
    : [];
  if (unsetSecretLabels.length === 0) {
    return { hint, waitingFor: null };
  }
  return {
    hint,
    waitingFor: translate(
      'settings.extensions.waitingFor',
      'Waiting for: {fields}',
      { fields: unsetSecretLabels.join(', ') },
    ),
  };
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

// --- Extension settings schema (form helpers) ---------------------------------

const SCHEMA_FIELD_TYPES = ['text', 'number', 'toggle', 'secret'];

/**
 * Normalize a raw ``settings_schema`` list into display-ready field descriptors,
 * dropping malformed entries. Secret fields keep ``envKey``/``set``; others keep
 * ``default``.
 */
export function normalizeSchemaFields(schema) {
  if (!Array.isArray(schema)) {
    return [];
  }
  return schema
    .filter(
      (field) =>
        field &&
        typeof field === 'object' &&
        typeof field.key === 'string' &&
        field.key.length > 0 &&
        SCHEMA_FIELD_TYPES.includes(field.type),
    )
    .map((field) => ({
      key: field.key,
      type: field.type,
      label: textOrFallback(field.label, field.key),
      description: textOrEmpty(field.description),
      required: field.required === true,
      default: field.default === undefined ? null : field.default,
      envKey: field.type === 'secret' ? textOrEmpty(field.env_key) : '',
      set: field.type === 'secret' ? field.set === true : false,
    }));
}

/**
 * Build the editable form state (per non-secret field) from a schema and the
 * persisted config. Text/number inputs are strings; toggles are booleans.
 * Secrets are write-only and never seeded here.
 */
export function buildSchemaFormState(schema, config) {
  const fields = normalizeSchemaFields(schema);
  const source = config && typeof config === 'object' ? config : {};
  const state = {};
  for (const field of fields) {
    if (field.type === 'secret') {
      continue;
    }
    if (field.type === 'toggle') {
      const value = source[field.key];
      state[field.key] =
        typeof value === 'boolean' ? value : field.default === true;
      continue;
    }
    // text / number: keep as a string input value; empty when absent.
    const value = source[field.key];
    state[field.key] =
      value === undefined || value === null ? '' : String(value);
  }
  return state;
}

/**
 * Build the config object for the ``settings.update`` payload from the form
 * state. Toggles are always explicit; text/number fields are omitted when the
 * input is empty (so the default applies at read time). Numbers are parsed:
 * integer text yields an int, otherwise a float; unparseable input produces a
 * per-field error and ``ok: false``.
 *
 * @returns {{ ok: boolean, config: object, errors: Record<string, string> }}
 */
export function buildSchemaConfigFromForm(schema, formState) {
  const fields = normalizeSchemaFields(schema);
  const source = formState && typeof formState === 'object' ? formState : {};
  const config = {};
  const errors = {};

  for (const field of fields) {
    if (field.type === 'secret') {
      continue;
    }
    if (field.type === 'toggle') {
      config[field.key] = source[field.key] === true;
      continue;
    }
    const raw = source[field.key];
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (text.length === 0) {
      continue;
    }
    if (field.type === 'number') {
      const parsed = parseSchemaNumber(text);
      if (parsed === null) {
        errors[field.key] = 'invalid-number';
        continue;
      }
      config[field.key] = parsed;
      continue;
    }
    config[field.key] = text;
  }

  return { ok: Object.keys(errors).length === 0, config, errors };
}

function parseSchemaNumber(text) {
  // Integer text (no dot) parses to an int; anything else to a float. In JS
  // both are ``number``; JSON then serializes ``8123`` vs ``80.5`` faithfully.
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(text)) {
    return null;
  }
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/** Whether an extension list entry should render the schema form (vs raw JSON). */
export function hasSettingsSchema(extension) {
  return (
    extension &&
    Array.isArray(extension.settingsSchema) &&
    extension.settingsSchema.length > 0
  );
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
  return normalizeCompactionPolicy(rawSettings?.compaction);
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

function normalizeRecallSettings(rawSettings) {
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

function normalizeWebSearchSettings(rawSettings) {
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
  const defaultCountValue = Number(webSearch.default_count);
  const defaultCount =
    Number.isInteger(defaultCountValue) &&
    defaultCountValue >= WEB_SEARCH_MIN_COUNT &&
    defaultCountValue <= WEB_SEARCH_MAX_COUNT
      ? defaultCountValue
      : WEB_SEARCH_DEFAULT_COUNT;

  return {
    provider,
    available_providers: availableProviders,
    default_count: defaultCount,
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
      default_count: normalized.default_count,
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

const ACCOUNT_SOURCE_PROCESS_ENV = 'process_env';
const ACCOUNT_SOURCE_DATA_DIR = 'data_dir';
const ACCOUNT_SOURCE_OAUTH = 'oauth';

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

function connectionHasUsableAccount(connection) {
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

// Whether the user has the connection switched on. The server always sends the
// field; treating "absent" as enabled keeps keyed connections working if a
// payload ever omits it.
export function isConnectionEnabled(connection) {
  return connection?.enabled !== false;
}

// Last local-catalog probe outcome: false only when the server positively knows
// the local endpoint (e.g. Ollama) did not answer. Remote connections have no
// probe and return null (no statement).
export function connectionReachability(connection) {
  return typeof connection?.reachable === 'boolean'
    ? connection.reachable
    : null;
}

function providerHasConfiguredConnection(provider) {
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

function isConnectionAddable(connection) {
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

// A keyless connection (type "none", e.g. local Ollama) needs no credential:
// it is always configured, carries one implicit account, and offers no
// key/account management actions.
export function isKeylessConnection(connection) {
  return connection?.type === 'none';
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
