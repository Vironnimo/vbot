export function createSettingsPayload(overrides = {}) {
  const base = {
    general: {
      server: {
        listen_host: '0.0.0.0',
        listen_port: 9001,
        port_source: 'settings.server_port',
      },
      data_directory: 'C:/Users/test/.vbot',
    },
    providers: {
      items: [
        {
          id: 'anthropic',
          name: 'Anthropic',
          base_url: 'https://api.anthropic.com/v1',
          credentials_configured: false,
          status: 'missing_credentials',
          model_count: 1,
          connections: [
            {
              id: 'anthropic:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: false,
              enabled: true,
              usable: false,
              credential_key: 'ANTHROPIC_API_KEY',
              accounts: [],
            },
          ],
        },
        {
          id: 'openai',
          name: 'OpenAI',
          base_url: 'https://api.openai.com/v1',
          credentials_configured: true,
          status: 'configured',
          model_count: 2,
          connections: [
            {
              id: 'openai:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: true,
              enabled: true,
              usable: true,
              credential_key: 'OPENAI_API_KEY',
              accounts: [
                {
                  id: 'default',
                  usable: true,
                  source: 'data_dir',
                  credential_key: 'OPENAI_API_KEY',
                },
              ],
            },
          ],
        },
      ],
    },
    skills: {
      default_directory: 'C:/Users/test/.vbot/skills',
      directories: ['C:/skills/shared'],
    },
    appearance: {
      language: 'en',
      available_languages: ['en', 'fr'],
      chat_width: 'comfortable',
      chat_working_mode: 'normal',
    },
    subagents: {
      max_subagent_depth: 4,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 60,
    },
    recall: {
      backend: 'jsonl_scan',
      available_backends: ['jsonl_scan', 'sqlite_fts'],
    },
    web_search: {
      provider: 'brave',
      available_providers: ['brave', 'searxng'],
      default_count: 12,
      searxng: {
        base_url: 'http://localhost:8888',
      },
    },
    session_titles: {
      enabled: false,
      model: '',
    },
  };

  return mergeSettings(base, overrides);
}

function mergeSettings(base, overrides) {
  if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)) {
    return base;
  }

  const result = { ...base };

  for (const [key, value] of Object.entries(overrides)) {
    if (Array.isArray(value)) {
      result[key] = value;
      continue;
    }

    if (value && typeof value === 'object') {
      result[key] = mergeSettings(base[key] ?? {}, value);
      continue;
    }

    result[key] = value;
  }

  return result;
}

// The section the current test interacted with last (via openSection). Button
// lookups search this subtree first so same-labeled per-section controls

export function translate(key, fallback, values) {
  const templates = {
    'common.unknown': 'Unknown',
    'settings.providers.description.credentialKey':
      'Credential key: {credentialKey}.',
    'settings.providers.description.baseUrl': 'Endpoint: {baseUrl}.',
    'settings.providers.description.modelCount': '{count} models available.',
    'settings.providers.description.none':
      'Provider metadata is not available yet.',
    'settings.recall.backends.jsonl_scan': 'JSONL scan',
    'settings.recall.backends.sqlite_fts': 'SQLite FTS',
    'settings.webSearch.providers.brave': 'Brave Search',
    'settings.webSearch.providers.searxng': 'SearXNG',
  };
  const template = templates[key] ?? fallback ?? key;

  if (!values) {
    return template;
  }

  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => {
    return Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name])
      : match;
  });
}
