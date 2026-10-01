import { t, tOr } from '../i18n.js';
import { textOrFallback, textOrEmpty } from './values.js';

const WEB_SEARCH_PROVIDER_BRAVE = 'brave';

const WEB_SEARCH_PROVIDER_DUCKDUCKGO = 'duckduckgo';

const WEB_SEARCH_PROVIDER_TAVILY = 'tavily';

const WEB_SEARCH_PROVIDER_EXA = 'exa';

const WEB_SEARCH_PROVIDER_SERPER = 'serper';

const WEB_SEARCH_PROVIDER_FIRECRAWL = 'firecrawl';

const WEB_SEARCH_PROVIDER_PERPLEXITY = 'perplexity';

const WEB_SEARCH_PROVIDER_SEARXNG = 'searxng';

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

export function getWebFetchSettings(settings) {
  const value = settings?.web_fetch ?? {};
  return {
    provider: typeof value.provider === 'string' ? value.provider : 'direct',
    mode: value.mode === 'prefer' ? 'prefer' : 'fallback',
  };
}

export function buildWebFetchSettingsPayload(value) {
  return { web_fetch: getWebFetchSettings({ web_fetch: value }) };
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

const WEB_SERVICE_SECTIONS = Object.freeze(['web_search', 'web_fetch']);

const WEB_SERVICE_KEY_SOURCES = new Set(['process_environment', 'data_dir']);

function webServiceEntries(settings, section) {
  const services = settings?.[section]?.services;
  if (!Array.isArray(services)) {
    return [];
  }
  return services.filter(
    (service) =>
      typeof service?.id === 'string' &&
      typeof service?.api_key_env === 'string',
  );
}

// Server facts about the API keys of the keyed services of `section`
// ('web_search' or 'web_fetch'): {id, api_key_env, configured, source,
// shared}. `shared` marks a key the other section uses too. The values never
// reach the WebUI, and these facts are never part of an editable draft.
export function getWebServiceKeys(settings, section) {
  const otherVariables = new Set(
    WEB_SERVICE_SECTIONS.filter((other) => other !== section).flatMap((other) =>
      webServiceEntries(settings, other).map((service) => service.api_key_env),
    ),
  );
  return webServiceEntries(settings, section).map((service) => ({
    id: service.id,
    api_key_env: service.api_key_env,
    configured: service.configured === true,
    source: WEB_SERVICE_KEY_SOURCES.has(service.source) ? service.source : null,
    shared: otherVariables.has(service.api_key_env),
  }));
}

// Keyed providers carry whether their API key is set as secondary text.
export function buildWebSearchProviderOptions(
  webSearchSettings,
  services = [],
) {
  return normalizeWebSearchProviders(
    webSearchSettings?.available_providers,
  ).map((provider) => {
    const option = {
      value: provider,
      label: tOr(`settings.webSearch.providers.${provider}`, provider),
    };
    const service = services.find((item) => item.id === provider);
    return service
      ? { ...option, secondaryLabel: webServiceKeyHint(service) }
      : option;
  });
}

// The Dropdown hint telling whether a keyed web service's API key is set.
export function webServiceKeyHint(service) {
  return service.configured
    ? t('settings.serviceKey.optionSet')
    : t('settings.serviceKey.optionMissing');
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
