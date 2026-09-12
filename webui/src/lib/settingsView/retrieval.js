import { textOrFallback, textOrEmpty } from './values.js';

const RECALL_BACKEND_CANONICAL_SCAN = 'canonical_scan';

const RECALL_BACKEND_SQLITE_FTS = 'sqlite_fts';

const WEB_SEARCH_PROVIDER_BRAVE = 'brave';

const WEB_SEARCH_PROVIDER_DUCKDUCKGO = 'duckduckgo';

const WEB_SEARCH_PROVIDER_TAVILY = 'tavily';

const WEB_SEARCH_PROVIDER_EXA = 'exa';

const WEB_SEARCH_PROVIDER_SERPER = 'serper';

const WEB_SEARCH_PROVIDER_FIRECRAWL = 'firecrawl';

const WEB_SEARCH_PROVIDER_PERPLEXITY = 'perplexity';

const WEB_SEARCH_PROVIDER_SEARXNG = 'searxng';

const RECALL_BACKEND_DEFAULTS = Object.freeze([
  RECALL_BACKEND_CANONICAL_SCAN,
  RECALL_BACKEND_SQLITE_FTS,
]);

const WEB_SEARCH_PROVIDER_DEFAULTS = Object.freeze([
  WEB_SEARCH_PROVIDER_BRAVE,
  WEB_SEARCH_PROVIDER_DUCKDUCKGO,
  WEB_SEARCH_PROVIDER_EXA,
  WEB_SEARCH_PROVIDER_FIRECRAWL,
  WEB_SEARCH_PROVIDER_PERPLEXITY,
  WEB_SEARCH_PROVIDER_SEARXNG,
  WEB_SEARCH_PROVIDER_SERPER,
  WEB_SEARCH_PROVIDER_TAVILY,
]);

const DEFAULT_SEARXNG_BASE_URL = 'http://localhost:8888';

const WEB_SEARCH_DEFAULT_COUNT = 12;

const WEB_SEARCH_MIN_COUNT = 1;

const WEB_SEARCH_MAX_COUNT = 20;

function normalizeRecallSettings(rawSettings) {
  const recall = rawSettings?.recall ?? {};
  const availableBackends = normalizeRecallBackends(recall.available_backends);
  const backend =
    typeof recall.backend === 'string' &&
    availableBackends.includes(recall.backend)
      ? recall.backend
      : RECALL_BACKEND_CANONICAL_SCAN;

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
