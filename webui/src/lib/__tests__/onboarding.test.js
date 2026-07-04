import { describe, expect, it } from 'vitest';

import {
  ONBOARDING_HERO_PROVIDER_ID,
  ONBOARDING_TARGET_AGENT_ID,
  PROVIDER_MODEL_SEARCH_PREFILL,
  agentNeedsModel,
  connectedProviderId,
  isOperational,
  onboardingHeroScope,
  onboardingMoreProviders,
  onboardingSubscriptionProviders,
  providerModalScope,
  providerModelSearchPrefill,
  providerTipKey,
} from '../onboarding.js';

// A connection as it appears in a `settings.get` provider item.
function apiKey(id, { configured = false } = {}) {
  return {
    id,
    type: 'api_key',
    label: 'API Key',
    configured,
    credential_key: id.replace(/[:-]/g, '_').toUpperCase(),
    accounts: configured
      ? [{ id: 'default', usable: true, source: 'data_dir' }]
      : [],
  };
}

function deviceFlow(id, { label = 'Sign in' } = {}) {
  return {
    id,
    type: 'oauth',
    label,
    configured: false,
    connectable: true,
    accounts: [],
  };
}

// The seven providers a fresh install ships, none connected.
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

  it('is true once any connection is configured', () => {
    const settings = freshInstallSettings();
    settings.providers.items[0].connections[0].configured = true;
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

describe('onboardingHeroScope', () => {
  it('scopes OpenRouter to its API-key connection', () => {
    const scope = onboardingHeroScope(freshInstallSettings());
    expect(scope).not.toBeNull();
    expect(scope.scopedProvider.id).toBe(ONBOARDING_HERO_PROVIDER_ID);
    expect(scope.scopedConnection.id).toBe('openrouter:api-key');
    expect(scope.providers).toEqual([]);
  });

  it('hides the hero once OpenRouter is already connected', () => {
    const settings = freshInstallSettings();
    settings.providers.items[0].connections[0].configured = true;
    expect(onboardingHeroScope(settings)).toBeNull();
  });
});

describe('onboardingSubscriptionProviders', () => {
  it('returns only providers offering an OAuth device flow', () => {
    const ids = onboardingSubscriptionProviders(freshInstallSettings()).map(
      (provider) => provider.id,
    );
    expect(ids).toEqual(['openai', 'github-copilot']);
  });
});

describe('onboardingMoreProviders', () => {
  it('returns API-key candidates excluding the hero, and excludes device-flow-only providers', () => {
    const ids = onboardingMoreProviders(freshInstallSettings()).map(
      (provider) => provider.id,
    );
    // OpenAI stays (it has an API key); GitHub Copilot (device-flow only) and
    // OpenRouter (the hero) are excluded.
    expect(ids).toEqual([
      'openai',
      'anthropic',
      'mistral',
      'minimax',
      'opencode-go',
    ]);
  });
});

describe('providerModalScope', () => {
  it('scopes a single-connection provider to that connection', () => {
    const [, , , anthropic] = freshInstallSettings().providers.items;
    const scope = providerModalScope(anthropic, 'api_key');
    expect(scope.scopedConnection.id).toBe('anthropic:api-key');
  });

  it('prefers the OAuth device flow when a provider offers several methods', () => {
    const openai = freshInstallSettings().providers.items[1];
    const scope = providerModalScope(openai, 'oauth');
    expect(scope.scopedConnection.id).toBe('openai:subscription');
  });

  it('prefers the API key when a provider offers several methods', () => {
    const openai = freshInstallSettings().providers.items[1];
    const scope = providerModalScope(openai, 'api_key');
    expect(scope.scopedConnection.id).toBe('openai:api-key');
  });

  it('returns null when nothing is addable', () => {
    expect(providerModalScope(null)).toBeNull();
    expect(providerModalScope({ connections: [] })).toBeNull();
  });
});

describe('connectedProviderId / tips / prefill', () => {
  it('names the first connected provider', () => {
    const settings = freshInstallSettings();
    expect(connectedProviderId(settings)).toBe('');
    settings.providers.items[0].connections[0].configured = true;
    expect(connectedProviderId(settings)).toBe('openrouter');
  });

  it('builds a per-provider tip key and hides it for the empty provider', () => {
    expect(providerTipKey('openrouter')).toBe(
      'onboarding.provider.tip.openrouter',
    );
    expect(providerTipKey('')).toBe('');
  });

  it('exposes the OpenRouter free-model search prefill', () => {
    expect(PROVIDER_MODEL_SEARCH_PREFILL.openrouter).toBe('free');
    expect(providerModelSearchPrefill('openrouter')).toBe('free');
    expect(providerModelSearchPrefill('anthropic')).toBe('');
  });

  it('targets the bootstrap main agent', () => {
    expect(ONBOARDING_TARGET_AGENT_ID).toBe('main');
  });
});
