// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: OnboardingView } = await import('../OnboardingView.svelte');

// A `settings.get` payload for a fresh install; OpenRouter flips to configured
// once a key is set.
function settingsPayload(connected) {
  return {
    providers: {
      items: [
        {
          id: 'openrouter',
          name: 'OpenRouter',
          connections: [
            {
              id: 'openrouter:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: connected,
              credential_key: 'OPENROUTER_API_KEY',
              accounts: connected
                ? [{ id: 'default', usable: true, source: 'data_dir' }]
                : [],
            },
          ],
        },
        {
          id: 'openai',
          name: 'OpenAI',
          connections: [
            {
              id: 'openai:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: false,
              credential_key: 'OPENAI_API_KEY',
              accounts: [],
            },
            {
              id: 'openai:subscription',
              type: 'oauth',
              label: 'ChatGPT Plus/Pro',
              configured: false,
              connectable: true,
              accounts: [],
            },
          ],
        },
      ],
    },
  };
}

function openrouterModels() {
  return [
    {
      id: 'openrouter/anthropic/claude-sonnet-4',
      provider_id: 'openrouter',
      model_id: 'anthropic/claude-sonnet-4',
      name: 'Claude Sonnet 4',
      capabilities: { tools: true },
      context_window: 200000,
      effective_context_window: 200000,
      connections: [],
    },
    {
      id: 'openrouter/meta/llama-3-8b:free',
      provider_id: 'openrouter',
      model_id: 'meta/llama-3-8b:free',
      name: 'Llama 3 8B (free)',
      capabilities: { tools: true },
      context_window: 131072,
      effective_context_window: 131072,
      connections: [],
    },
  ];
}

function openrouterConnections(connected) {
  return [
    {
      id: 'openrouter:api-key',
      provider_id: 'openrouter',
      label: 'API Key',
      usable: connected,
      accounts: [{ id: 'default', usable: connected }],
    },
  ];
}

function mainAgent() {
  return {
    id: 'main',
    name: 'Main',
    model: '',
    fallback_model: '',
    workspace: '/data/workspace-main',
    temperature: null,
    thinking_effort: '',
    memory_prompt_mode: 'agent_user',
    allowed_tools: ['*'],
    allowed_skills: ['*'],
    custom_system_prompt_enabled: false,
    current_session_id: '',
  };
}

// One mutable server-ish fixture so a test can flip credential/model state as
// the flow progresses.
function createServer({ modelsAvailable = true } = {}) {
  const state = { connected: false, modelsAvailable, lastAgentUpdate: null };
  const rpc = (method, params) => {
    switch (method) {
      case 'settings.get':
        return Promise.resolve(settingsPayload(state.connected));
      case 'provider.set_key':
        state.connected = true;
        return Promise.resolve({});
      case 'model.refresh_db':
        return Promise.resolve({ providers: [], model_count: 0 });
      case 'model.list':
        return Promise.resolve({
          models: state.modelsAvailable ? openrouterModels() : [],
        });
      case 'connection.list':
        return Promise.resolve({
          connections: openrouterConnections(state.connected),
        });
      case 'agent.get':
        return Promise.resolve(mainAgent());
      case 'agent.update':
        state.lastAgentUpdate = params;
        return Promise.resolve({ ...mainAgent(), model: params.model });
      default:
        return Promise.resolve({});
    }
  };
  return { state, rpc };
}

async function waitFor(assertion, { timeout = 1500 } = {}) {
  const start = Date.now();
  for (;;) {
    try {
      flushSync();
      assertion();
      return;
    } catch (error) {
      if (Date.now() - start > timeout) {
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  }
}

function byText(selector, text) {
  return Array.from(document.querySelectorAll(selector)).find((element) =>
    element.textContent.includes(text),
  );
}

// Drive the hero connect flow: click the hero, type an API key into the connect
// modal, submit it.
async function connectViaHero() {
  await waitFor(() => {
    expect(document.querySelector('.onboarding-hero')).toBeTruthy();
  });
  document.querySelector('.onboarding-hero').click();
  flushSync();

  await waitFor(() => {
    expect(
      document.querySelector('#provider-connect-key-form input'),
    ).toBeTruthy();
  });
  const keyInput = document.querySelector('#provider-connect-key-form input');
  keyInput.value = 'sk-test-key';
  keyInput.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();

  const form = document.querySelector('#provider-connect-key-form');
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

describe('OnboardingView', () => {
  let mountedComponent;
  let server;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    server = createServer();
    rpcMock.mockReset();
    rpcMock.mockImplementation((...args) => server.rpc(...args));
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    rpcMock.mockReset();
  });

  it('advances to the model step after a provider is connected', async () => {
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete: vi.fn(), onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectViaHero();

    await waitFor(() => {
      expect(byText('.onboarding-kicker', 'Step 2 of 2')).toBeTruthy();
    });
    expect(rpcMock).toHaveBeenCalledWith(
      'provider.set_key',
      expect.objectContaining({
        provider_id: 'openrouter',
        value: 'sk-test-key',
      }),
    );
    expect(rpcMock).toHaveBeenCalledWith('model.list');
  });

  it('shows the OpenRouter free-model tip and prefills the model search with free', async () => {
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete: vi.fn(), onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectViaHero();

    await waitFor(() => {
      expect(byText('.onboarding-tip', 'free')).toBeTruthy();
    });

    // Open the model dropdown; the search box carries the `free` prefill.
    await waitFor(() => {
      expect(document.querySelector('#onboarding-model')).toBeTruthy();
    });
    document.querySelector('#onboarding-model').click();
    flushSync();

    await waitFor(() => {
      const search = document.querySelector('.s-dropdown-search input');
      expect(search).toBeTruthy();
      expect(search.value).toBe('free');
    });
  });

  it('assigns the chosen model to the main agent on Start chatting', async () => {
    const onComplete = vi.fn();
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete, onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectViaHero();

    await waitFor(() => {
      expect(document.querySelector('#onboarding-model')).toBeTruthy();
    });
    document.querySelector('#onboarding-model').click();
    flushSync();

    await waitFor(() => {
      expect(byText('.s-dropdown-opt', ':free')).toBeTruthy();
    });
    byText('.s-dropdown-opt', ':free').click();
    flushSync();

    await waitFor(() => {
      expect(byText('.btn-primary', 'Start chatting')).toBeTruthy();
    });
    byText('.btn-primary', 'Start chatting').click();
    flushSync();

    await waitFor(() => {
      expect(onComplete).toHaveBeenCalledTimes(1);
    });
    expect(rpcMock).toHaveBeenCalledWith(
      'agent.get',
      expect.objectContaining({ id: 'main' }),
    );
    expect(server.state.lastAgentUpdate).toMatchObject({ id: 'main' });
    expect(server.state.lastAgentUpdate.model).toContain(':free');
  });

  it('shows a retry affordance when the model list is empty', async () => {
    server = createServer({ modelsAvailable: false });
    rpcMock.mockImplementation((...args) => server.rpc(...args));

    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete: vi.fn(), onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectViaHero();

    await waitFor(() => {
      expect(
        byText('.empty-state--compact', 'No models are available'),
      ).toBeTruthy();
      expect(byText('.btn-secondary', 'Retry')).toBeTruthy();
    });

    // Models arrive; retrying reveals the dropdown.
    server.state.modelsAvailable = true;
    byText('.btn-secondary', 'Retry').click();
    flushSync();

    await waitFor(() => {
      expect(document.querySelector('#onboarding-model')).toBeTruthy();
    });
  });
});
