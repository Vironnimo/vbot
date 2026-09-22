import { describe, expect, it } from 'vitest';

import {
  ONBOARDING_TARGET_AGENT_ID,
  agentNeedsModel,
  connectedProviderId,
  isOperational,
  providerModalScope,
  onboardingProviders,
} from '../onboarding.js';

// A connection as it appears in a `settings.get` provider item.
function apiKey(id, { configured = false } = {}) {
  return {
    id,
    type: 'api_key',
    label: 'API Key',
    configured,
    enabled: true,
    usable: configured,
    credential_key: id.replace(/[:-]/g, '_').toUpperCase(),
    accounts: configured
      ? [{ id: 'default', usable: true, source: 'data_dir' }]
      : [],
  };
}

function keyless(id, { enabled = false } = {}) {
  return {
    id,
    type: 'none',
    label: 'Local',
    configured: true,
    enabled,
    usable: enabled,
    accounts: [{ id: 'default', usable: true, source: 'none' }],
  };
}

function deviceFlow(id, { label = 'Sign in' } = {}) {
  return {
    id,
    type: 'oauth',
    label,
    configured: false,
    enabled: true,
    usable: false,
    connectable: true,
    accounts: [],
  };
}

// The eight providers a fresh install ships, none usable. Ollama is keyless and
// therefore configured, but its local connection stays disabled until opt-in.
function freshInstallSettings() {
  return {
    providers: {
      items: [
        {
          id: 'openrouter',
          name: 'OpenRouter',
          connections: [apiKey('openrouter:api-key')],
        },
        {
          id: 'openai',
          name: 'OpenAI',
          connections: [
            apiKey('openai:api-key'),
            deviceFlow('openai:subscription', { label: 'ChatGPT Plus/Pro' }),
          ],
        },
        {
          id: 'github-copilot',
          name: 'GitHub Copilot',
          connections: [
            deviceFlow('github-copilot:oauth', {
              label: 'Sign in with GitHub',
            }),
          ],
        },
        {
          id: 'anthropic',
          name: 'Anthropic',
          connections: [apiKey('anthropic:api-key')],
        },
        {
          id: 'mistral',
          name: 'Mistral AI',
          connections: [apiKey('mistral:api-key')],
        },
        {
          id: 'minimax',
          name: 'MiniMax',
          connections: [apiKey('minimax:api-key')],
        },
        {
          id: 'opencode-go',
          name: 'OpenCode Go',
          connections: [apiKey('opencode-go:api-key')],
        },
        {
          id: 'ollama',
          name: 'Ollama',
          connections: [keyless('ollama:local'), apiKey('ollama:cloud')],
        },
      ],
    },
  };
}

describe('isOperational', () => {
  it('is false when no provider carries usable credentials', () => {
    expect(isOperational(freshInstallSettings())).toBe(false);
    expect(isOperational(undefined)).toBe(false);
    expect(isOperational({ providers: { items: [] } })).toBe(false);
  });

  it('is true once any connection is usable', () => {
    const settings = freshInstallSettings();
    settings.providers.items[0].connections[0].configured = true;
    settings.providers.items[0].connections[0].usable = true;
    expect(isOperational(settings)).toBe(true);
  });
});

describe('agentNeedsModel', () => {
  it('flags an empty or whitespace model', () => {
    expect(agentNeedsModel({ model: '' })).toBe(true);
    expect(agentNeedsModel({ model: '   ' })).toBe(true);
    expect(agentNeedsModel({})).toBe(true);
    expect(agentNeedsModel(null)).toBe(true);
  });

  it('is false once a model is assigned', () => {
    expect(agentNeedsModel({ model: 'openrouter/some-model' })).toBe(false);
  });
});

describe('providerModalScope', () => {
  it('skips a redundant choice for a single method', () => {
    const provider = freshInstallSettings().providers.items[0];
    expect(providerModalScope(provider).scopedConnection.id).toBe(
      'openrouter:api-key',
    );
  });

  it('lets the user choose when a Provider supports API key and sign-in', () => {
    const provider = freshInstallSettings().providers.items[1];
    expect(providerModalScope(provider).scopedConnection).toBeNull();
    provider.connections[0] = apiKey('openai:api-key', { configured: true });
    expect(providerModalScope(provider).scopedConnection.id).toBe(
      'openai:subscription',
    );
  });

  it('returns null when nothing is addable', () => {
    expect(providerModalScope(null)).toBeNull();
    expect(providerModalScope({ connections: [] })).toBeNull();
  });
});

describe('connectedProviderId', () => {
  it('names the first connected provider', () => {
    const settings = freshInstallSettings();
    expect(connectedProviderId(settings)).toBe('');
    settings.providers.items[0].connections[0].configured = true;
    settings.providers.items[0].connections[0].usable = true;
    expect(connectedProviderId(settings)).toBe('openrouter');
  });

  it('keeps the chosen Provider when several are usable', () => {
    const settings = freshInstallSettings();
    settings.providers.items[0].connections[0].usable = true;
    settings.providers.items[1].connections[0].usable = true;
    expect(connectedProviderId(settings, 'openai')).toBe('openai');
    expect(connectedProviderId(settings, 'removed')).toBe('openrouter');
  });

  it('targets the bootstrap main agent', () => {
    expect(ONBOARDING_TARGET_AGENT_ID).toBe('main');
  });
});

describe('onboardingProviders', () => {
  it('lists each Provider once, including local-only and sign-in-only Providers', () => {
    const settings = freshInstallSettings();
    settings.providers.items.push({
      id: 'lmstudio',
      name: 'LM Studio',
      connections: [keyless('lmstudio:local')],
    });
    const items = onboardingProviders(settings);
    expect(items).toHaveLength(9);
    expect(items.filter((item) => item.provider.id === 'openai')).toHaveLength(
      1,
    );
    expect(
      items.find((item) => item.provider.id === 'openai').methodTypes,
    ).toEqual(['api_key', 'oauth']);
    expect(
      items.find((item) => item.provider.id === 'lmstudio').scope
        .scopedConnection.type,
    ).toBe('none');
    expect(
      items.find((item) => item.provider.id === 'github-copilot').scope
        .scopedConnection.type,
    ).toBe('oauth');
    expect(items[0].provider.name).toBe('Anthropic');
  });

  it('finds Providers by name, id and subscription name regardless of case', () => {
    const settings = freshInstallSettings();
    for (const query of [' OPENAI ', 'ChatGPT']) {
      expect(
        onboardingProviders(settings, query).map((item) => item.provider.id),
      ).toEqual(['openai']);
    }
    expect(onboardingProviders(settings, 'not-present')).toEqual([]);
  });

  it('keeps connected Providers and their remaining methods available', () => {
    const settings = freshInstallSettings();
    settings.providers.items[1].connections[0] = apiKey('openai:api-key', {
      configured: true,
    });
    const [item] = onboardingProviders(settings, 'OpenAI');
    expect(item.connected).toBe(true);
    expect(item.scope.scopedConnection.id).toBe('openai:subscription');
    settings.providers.items[1].connections[1].usable = true;
    expect(onboardingProviders(settings, 'OpenAI')[0].scope).toBeNull();
  });

  it('does not offer unsupported OAuth flows or new custom endpoints', () => {
    const settings = freshInstallSettings();
    settings.providers.items = [
      {
        id: 'unsupported',
        connections: [{ type: 'oauth', connectable: false }],
      },
      { id: 'custom', custom: true, connections: [apiKey('custom:key')] },
    ];
    expect(onboardingProviders(settings)).toEqual([]);
    settings.providers.items[1].connections[0].usable = true;
    expect(onboardingProviders(settings)[0]).toMatchObject({
      connected: true,
      scope: null,
    });
  });
});
