import { t } from '../i18n.js';

export function getProviderItems(settings) {
  return Array.isArray(settings?.providers?.items)
    ? settings.providers.items
    : [];
}

export function getCustomProviderItems(settings) {
  return Array.isArray(settings?.providers?.custom_endpoints?.items)
    ? settings.providers.custom_endpoints.items
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

export function accountDisplayName(account) {
  if (account?.id === DEFAULT_ACCOUNT_ID) {
    return t('settings.providers.accounts.defaultLabel');
  }

  return typeof account?.id === 'string' ? account.id : '';
}

export function describeAccountSource(account) {
  if (account?.source === ACCOUNT_SOURCE_PROCESS_ENV) {
    return t('settings.providers.accounts.source.processEnv');
  }

  if (account?.source === ACCOUNT_SOURCE_DATA_DIR) {
    return t('settings.providers.accounts.source.dataDir');
  }

  if (account?.source === ACCOUNT_SOURCE_OAUTH) {
    return t('settings.providers.accounts.source.oauth');
  }

  return '';
}

/**
 * Where an account's credential comes from, with the variable that holds an
 * API key.
 */
export function accountSourceTooltip(account) {
  const key =
    typeof account?.credential_key === 'string' ? account.credential_key : '';
  const variable = [
    {
      label: t('settings.providers.accounts.variable'),
      value: key,
      mono: true,
    },
  ];
  if (account?.source === ACCOUNT_SOURCE_PROCESS_ENV) {
    return {
      text: t('settings.providers.accounts.sourceHint.processEnv'),
      rows: variable,
    };
  }
  if (account?.source === ACCOUNT_SOURCE_DATA_DIR) {
    return {
      text: t('settings.providers.accounts.sourceHint.dataDir'),
      rows: variable,
    };
  }
  if (account?.source === ACCOUNT_SOURCE_OAUTH) {
    return t('settings.providers.accounts.sourceHint.oauth');
  }
  return '';
}

/** Why an account cannot send requests. */
export function accountUnusableReason(account) {
  return isOAuthAccount(account)
    ? t('settings.providers.accounts.notUsableOAuth')
    : t('settings.providers.accounts.notUsableKey');
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

function isConnectionConfigured(connection) {
  if (connection?.type === 'none') {
    return connection?.added === true;
  }
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

// Connection-level behavior gate computed by the server. This deliberately
// stays distinct from configured credentials and account usability: a keyless
// local connection has both even while its opt-in enable switch is off.
function isConnectionUsable(connection) {
  return connection?.usable === true;
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

function providerHasUsableConnection(provider) {
  return (
    Array.isArray(provider?.connections) &&
    provider.connections.some(isConnectionUsable)
  );
}

export function getUsableProviderItems(settings) {
  return getProviderItems(settings).filter(providerHasUsableConnection);
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

  if (connection?.type === 'none') {
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
      provider?.custom !== true &&
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

export function isSharedOpenCodeConnection(connection) {
  return (
    connection?.type === 'api_key' &&
    connection?.credential_key === 'OPENCODE_API_KEY'
  );
}

export function describeSharedOpenCodeKey() {
  return t('settings.providers.opencode.sharedKey');
}

// The one short fact a collapsed Provider row shows next to its status.
export function formatModelCount(count) {
  if (!Number.isFinite(count)) {
    return '';
  }

  return count === 1
    ? t('settings.providers.modelCountOne')
    : t('settings.providers.modelCount', { count });
}

// Billing notes the OpenCode Providers need so users do not misread what
// their subscription or credits pay for; empty for every other Provider.
export function describeProviderBilling(provider) {
  if (provider?.id === 'opencode-go') {
    return t('settings.providers.opencode.goHelp');
  }

  if (provider?.id === 'opencode-zen') {
    return t('settings.providers.opencode.zenHelp');
  }

  return '';
}
