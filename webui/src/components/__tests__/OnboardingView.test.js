// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

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
              enabled: true,
              usable: connected,
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
        {
          id: 'ollama',
          name: 'Ollama',
          connections: [
            {
              id: 'ollama:local',
              type: 'none',
              label: 'Local',
              configured: true,
              enabled: false,
              usable: false,
              accounts: [{ id: 'default', usable: true, source: 'none' }],
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
    fallback_models: [],
    workspace: '/data/workspace-main',
    temperature: null,
    thinking_effort: '',
    memory_prompt_mode: 'agent_user',
    tool_access: { mode: 'all' },
    allowed_skills: ['*'],
    custom_system_prompt_enabled: false,
    current_session_id: '',
  };
}

// One mutable server-ish fixture so a test can flip credential/model state as
// the flow progresses.
function createServer({ modelsAvailable = true } = {}) {
  const state = {
    connected: false,
    connectedConnections: new Set(),
    modelsAvailable,
    lastAgentUpdate: null,
  };
  const rpc = (method, params) => {
    switch (method) {
      case 'settings.get': {
        const settings = settingsPayload(state.connected);
        for (const provider of settings.providers.items) {
          for (const connection of provider.connections) {
            if (state.connectedConnections.has(connection.id)) {
              Object.assign(connection, {
                configured: true,
                added: true,
                enabled: true,
                usable: true,
                accounts: [{ id: 'default', usable: true }],
              });
            }
          }
        }
        return Promise.resolve(settings);
      }
      case 'provider.set_key':
        state.connectedConnections.add(params.connection_id);
        if (params.provider_id === 'openrouter') state.connected = true;
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

// Select a Provider and submit its single API-key method.
async function connectProvider() {
  await waitFor(() => {
    expect(byText('.onboarding-provider-row', 'OpenRouter')).toBeTruthy();
  });
  byText('.onboarding-provider-row', 'OpenRouter').click();
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

async function continueToModels() {
  if (document.querySelector('.onboarding-model-body')) return;
  await waitFor(() =>
    expect(document.querySelector('.onboarding-continue')?.disabled).toBe(
      false,
    ),
  );
  document.querySelector('.onboarding-continue').click();
  flushSync();
}

describe('OnboardingView', () => {
  let mountedComponent;
  let server;

  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
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
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('stays on Providers after connecting and advances only when the user continues', async () => {
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete: vi.fn(), onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectProvider();
    await waitFor(() => {
      expect(
        document.querySelector('.onboarding-provider-row--connected'),
      ).toBeTruthy();
      expect(document.querySelector('.onboarding-model-body')).toBeNull();
      expect(
        document.querySelector('.onboarding-connected-summary').textContent,
      ).toContain('OpenRouter');
    });
    await continueToModels();

    await waitFor(() => {
      expect(
        document.querySelector(
          '.onboarding-progress li:nth-child(2) [aria-current="step"]',
        ),
      ).toBeTruthy();
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

  it('starts with an unfiltered search and offers a visible, reversible free filter', async () => {
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete: vi.fn(), onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectProvider();
    await continueToModels();

    await waitFor(() => {
      expect(document.querySelector('#onboarding-model')).toBeTruthy();
    });
    document.querySelector('#onboarding-model').click();
    flushSync();

    await waitFor(() => {
      const search = document.querySelector('.s-dropdown-search input');
      expect(search).toBeTruthy();
      expect(search.value).toBe('');
      expect(byText('.s-dropdown-opt', 'Claude Sonnet')).toBeTruthy();
    });
    document
      .querySelector('.s-dropdown-search input')
      .dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
    flushSync();
    document.querySelector('.onboarding-free-filter').click();
    flushSync();
    expect(
      document
        .querySelector('.onboarding-free-filter')
        .getAttribute('aria-pressed'),
    ).toBe('true');
    document.querySelector('#onboarding-model').click();
    flushSync();
    await waitFor(() => {
      expect(byText('.s-dropdown-opt', ':free')).toBeTruthy();
      expect(byText('.s-dropdown-opt', 'Claude Sonnet')).toBeFalsy();
    });
  });

  it.each([
    { layout: 'wide', width: 760, previewCount: 10 },
    { layout: 'narrow', width: 320, previewCount: 3 },
  ])(
    'expands a $layout catalog and searches every Provider regardless of the preview',
    async ({ width, previewCount }) => {
      vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(
        width,
      );
      rpcMock.mockImplementation(async (method, params) => {
        const result = await server.rpc(method, params);
        if (method === 'settings.get') {
          for (let index = 0; index < 60; index += 1) {
            result.providers.items.push({
              id: `extra-${index}`,
              name: `Extra ${index}`,
              connections: [{ id: `extra-${index}:key`, type: 'api_key' }],
            });
          }
        }
        return result;
      });
      mountedComponent = mount(OnboardingView, { target: document.body });
      await waitFor(() =>
        expect(
          document.querySelectorAll('.onboarding-provider-row'),
        ).toHaveLength(previewCount),
      );
      expect(byText('button', 'Continue to Model').disabled).toBe(true);
      const expand = byText('button', 'Show all 63 Providers');
      expect(expand.getAttribute('aria-expanded')).toBe('false');
      const previewRows = Array.from(
        document.querySelectorAll('.onboarding-provider-row'),
      );
      expand.click();
      await waitFor(() => {
        expect(document.activeElement.classList).toContain(
          'onboarding-provider-row',
        );
        expect(previewRows).not.toContain(document.activeElement);
      });
      expect(expand.getAttribute('aria-expanded')).toBe('true');
      expect(
        document.querySelectorAll('.onboarding-provider-row'),
      ).toHaveLength(63);
      expect(byText('.onboarding-provider-row', 'OpenRouter')).toBeTruthy();
      byText('button', 'Show fewer Providers').click();
      flushSync();
      expect(byText('.onboarding-provider-row', 'OpenAI')).toBeFalsy();
      const search = document.querySelector('#onboarding-provider-search');
      search.value = 'chatGPT';
      search.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      expect(
        document.querySelectorAll('.onboarding-provider-row'),
      ).toHaveLength(1);
      expect(byText('button', 'Show all')).toBeFalsy();
      byText('.onboarding-provider-row', 'OpenAI').click();
      flushSync();
      expect(byText('.provider-pick-item', 'API Key')).toBeTruthy();
      expect(byText('.provider-pick-item', 'ChatGPT Plus/Pro')).toBeTruthy();
      expect(document.querySelector('#provider-api-key')).toBeNull();
      byText('[role="dialog"] button', 'Cancel').click();
      flushSync();
      expect(search.value).toBe('chatGPT');
      search.value = 'Extra';
      search.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      expect(
        document.querySelectorAll('.onboarding-provider-row'),
      ).toHaveLength(60);
      search.value = 'no-such-provider';
      search.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      expect(document.querySelector('.onboarding-provider-empty')).toBeTruthy();
      byText('button', 'Clear').click();
      flushSync();
      expect(
        document.querySelectorAll('.onboarding-provider-row'),
      ).toHaveLength(previewCount);
      expect(byText('button', 'Show all 63 Providers')).toBeTruthy();
    },
  );

  it('connects multiple Providers and both sign-in and API-key methods without leaving the Provider step', async () => {
    const props = reactiveProps({
      providerAuthEvent: null,
      modelsRefreshToken: 0,
    });
    mountedComponent = mount(OnboardingView, { target: document.body, props });
    await waitFor(() =>
      expect(byText('.onboarding-provider-row', 'OpenAI')).toBeTruthy(),
    );
    byText('.onboarding-provider-row', 'OpenAI').click();
    flushSync();
    byText('.provider-pick-item', 'ChatGPT Plus/Pro').click();
    flushSync();
    expect(document.querySelector('#provider-account-name')).toBeNull();
    byText('.modal-footer button', 'Connect').click();
    await waitFor(() =>
      expect(rpcMock).toHaveBeenCalledWith('provider.connect', {
        provider_id: 'openai',
        connection_id: 'openai:subscription',
        account: 'default',
      }),
    );
    server.state.connectedConnections.add('openai:subscription');
    props.providerAuthEvent = {
      provider_id: 'openai',
      connection_id: 'openai:subscription',
      account: 'default',
      success: true,
    };
    props.modelsRefreshToken += 1;
    await waitFor(() => {
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(document.querySelector('.onboarding-model-body')).toBeNull();
      expect(
        byText('.onboarding-provider-row--connected', 'OpenAI'),
      ).toBeTruthy();
    });
    byText('.onboarding-provider-row', 'OpenAI').click();
    flushSync();
    // The one remaining method opens directly and keeps the original sign-in.
    const key = document.querySelector('#provider-api-key');
    expect(key).toBeTruthy();
    key.value = 'second-method-key';
    key.dispatchEvent(new Event('input', { bubbles: true }));
    document
      .querySelector('#provider-connect-key-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() =>
      expect(document.querySelector('[role="dialog"]')).toBeNull(),
    );
    expect(byText('.onboarding-provider-row', 'OpenAI').disabled).toBe(true);
    expect(
      document.querySelector('.onboarding-connected-summary').textContent,
    ).toContain('1 connected');
    await connectProvider();
    await waitFor(() => {
      expect(
        document.querySelector('.onboarding-connected-summary').textContent,
      ).toContain('2 connected');
      expect(
        document.querySelectorAll('.onboarding-provider-row'),
      ).toHaveLength(3);
      expect(document.querySelector('#onboarding-provider-search')).toBe(
        document.activeElement,
      );
    });
    expect(server.state.connectedConnections.has('openai:subscription')).toBe(
      true,
    );
    expect(server.state.connectedConnections.has('openai:api-key')).toBe(true);
    await continueToModels();
    expect(document.querySelector('#onboarding-model-provider')).toBeTruthy();
  });

  it('assigns the chosen model to the main agent on Start chatting', async () => {
    const onComplete = vi.fn();
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete, onDismiss: vi.fn(), onToast: vi.fn() },
    });
    flushSync();

    await connectProvider();
    await continueToModels();

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
      expect(byText('.btn-primary', 'Start chatting').disabled).toBe(false);
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

    await connectProvider();
    await continueToModels();

    await waitFor(() => {
      expect(document.querySelector('.empty-state--compact')).toBeTruthy();
      expect(byText('.btn-secondary', 'Try again')).toBeTruthy();
      expect(byText('button', 'Back to Providers')).toBeTruthy();
      expect(byText('button', 'Start chatting').disabled).toBe(true);
    });

    // Models arrive; retrying reveals the dropdown.
    server.state.modelsAvailable = true;
    byText('.btn-secondary', 'Try again').click();
    flushSync();

    await waitFor(() => {
      expect(document.querySelector('#onboarding-model')).toBeTruthy();
    });
  });

  it('recovers from initial settings failure without a blank setup page', async () => {
    rpcMock.mockRejectedValueOnce(new Error('offline'));
    mountedComponent = mount(OnboardingView, { target: document.body });
    await waitFor(() =>
      expect(document.querySelector('[role="alert"]')).toBeTruthy(),
    );
    byText('button', 'Try again').click();
    await waitFor(() =>
      expect(byText('.onboarding-provider-row', 'OpenRouter')).toBeTruthy(),
    );
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it('shows a loading state while settings are pending', async () => {
    let resolveSettings;
    rpcMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSettings = resolve;
        }),
    );
    mountedComponent = mount(OnboardingView, { target: document.body });
    flushSync();
    expect(document.querySelector('[role="status"]')).toBeTruthy();
    expect(byText('.onboarding-provider-row', 'OpenRouter')).toBeFalsy();
    resolveSettings(settingsPayload(false));
    await waitFor(() =>
      expect(byText('.onboarding-provider-row', 'OpenRouter')).toBeTruthy(),
    );
  });

  it('offers local setup without asking for an API key', async () => {
    mountedComponent = mount(OnboardingView, { target: document.body });
    await waitFor(() =>
      expect(byText('.onboarding-provider-row', 'Ollama')).toBeTruthy(),
    );
    byText('.onboarding-provider-row', 'Ollama').click();
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    expect(document.querySelector('#provider-api-key')).toBeNull();
    expect(byText('[role="dialog"] button', 'Add provider')).toBeTruthy();
  });

  it('hides optional account and storage details in guided key entry', async () => {
    mountedComponent = mount(OnboardingView, { target: document.body });
    await waitFor(() =>
      expect(byText('.onboarding-provider-row', 'OpenRouter')).toBeTruthy(),
    );
    byText('.onboarding-provider-row', 'OpenRouter').click();
    flushSync();
    expect(document.querySelector('#provider-api-key')).toBeTruthy();
    expect(document.querySelector('#provider-account-name')).toBeNull();
    expect(
      document.querySelector('a[href="https://openrouter.ai/settings/keys"]'),
    ).toBeTruthy();
    expect(document.querySelector('[role="dialog"]').textContent).not.toContain(
      'OPENROUTER_API_KEY',
    );
  });

  it('starts at Providers even with an existing connection and keeps Back available on a Model load error', async () => {
    server.state.connected = true;
    rpcMock.mockImplementation((method, params) =>
      method === 'model.list'
        ? Promise.reject(new Error('offline'))
        : server.rpc(method, params),
    );
    mountedComponent = mount(OnboardingView, { target: document.body });
    await continueToModels();
    await waitFor(() =>
      expect(document.querySelector('[role="alert"]')).toBeTruthy(),
    );
    expect(byText('button', 'Start chatting').disabled).toBe(true);
    byText('button', 'Back to Providers').click();
    flushSync();
    expect(
      document.querySelector('.onboarding-provider-row--connected'),
    ).toBeTruthy();
    expect(document.activeElement).toBe(
      document.querySelector('.onboarding-step-title'),
    );
  });

  it('keeps the selected Model after a failed save and allows retry', async () => {
    const onComplete = vi.fn();
    server.state.connected = true;
    let failSave = true;
    rpcMock.mockImplementation((method, params) =>
      method === 'agent.update' && failSave
        ? Promise.reject(new Error('offline'))
        : server.rpc(method, params),
    );
    mountedComponent = mount(OnboardingView, {
      target: document.body,
      props: { onComplete },
    });
    await continueToModels();
    await waitFor(() =>
      expect(document.querySelector('#onboarding-model')).toBeTruthy(),
    );
    document.querySelector('#onboarding-model').click();
    flushSync();
    byText('.s-dropdown-opt', ':free').click();
    flushSync();
    byText('button', 'Start chatting').click();
    await waitFor(() =>
      expect(document.querySelector('[role="alert"]')).toBeTruthy(),
    );
    expect(onComplete).not.toHaveBeenCalled();
    expect(document.querySelector('.onboarding-selection')).toBeTruthy();
    failSave = false;
    byText('button', 'Start chatting').click();
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(server.state.lastAgentUpdate).toEqual({
      id: 'main',
      model: 'openrouter/meta/llama-3-8b:free::api-key',
    });
  });

  it('invalidates a selection when its Connection is removed', async () => {
    server.state.connected = true;
    const props = reactiveProps({ modelsRefreshToken: 0 });
    mountedComponent = mount(OnboardingView, { target: document.body, props });
    await continueToModels();
    await waitFor(() =>
      expect(document.querySelector('#onboarding-model')).toBeTruthy(),
    );
    document.querySelector('#onboarding-model').click();
    flushSync();
    byText('.s-dropdown-opt', ':free').click();
    flushSync();
    expect(byText('button', 'Start chatting').disabled).toBe(false);
    server.state.connected = false;
    props.modelsRefreshToken += 1;
    flushSync();
    await waitFor(() => {
      expect(byText('button', 'Start chatting').disabled).toBe(true);
      expect(document.querySelector('.onboarding-selection')).toBeNull();
    });
  });

  it('switches between connected Providers in the Model step and clears the old selection', async () => {
    server.state.connected = true;
    rpcMock.mockImplementation(async (method, params) => {
      const result = await server.rpc(method, params);
      if (method === 'settings.get') {
        result.providers.items[1].connections[0].usable = true;
      }
      if (method === 'connection.list') {
        result.connections.push({
          id: 'openai:api-key',
          provider_id: 'openai',
          usable: true,
          accounts: [{ id: 'default', usable: true }],
        });
      }
      if (method === 'model.list') {
        result.models.push({
          ...openrouterModels()[0],
          id: 'openai/test-model',
          model_id: 'test-model',
          provider_id: 'openai',
          name: 'Other Model',
        });
      }
      return result;
    });
    mountedComponent = mount(OnboardingView, { target: document.body });
    await continueToModels();
    await waitFor(() =>
      expect(document.querySelector('#onboarding-model')).toBeTruthy(),
    );
    document.querySelector('#onboarding-model').click();
    flushSync();
    byText('.s-dropdown-opt', 'Claude Sonnet').click();
    flushSync();
    expect(byText('button', 'Start chatting').disabled).toBe(false);
    document.querySelector('#onboarding-model-provider').click();
    flushSync();
    byText('.s-dropdown-opt', 'OpenAI').click();
    flushSync();
    expect(byText('button', 'Start chatting').disabled).toBe(true);
    document.querySelector('#onboarding-model').click();
    flushSync();
    expect(byText('.s-dropdown-opt', 'Other Model')).toBeTruthy();
    expect(byText('.s-dropdown-opt', 'Claude Sonnet')).toBeFalsy();
    expect(document.querySelector('.onboarding-free-filter')).toBeNull();
  });

  it('clears a paid selection when the user enables the free filter', async () => {
    server.state.connected = true;
    mountedComponent = mount(OnboardingView, { target: document.body });
    await continueToModels();
    await waitFor(() =>
      expect(document.querySelector('#onboarding-model')).toBeTruthy(),
    );
    document.querySelector('#onboarding-model').click();
    flushSync();
    byText('.s-dropdown-opt', 'Claude Sonnet').click();
    flushSync();
    expect(byText('button', 'Start chatting').disabled).toBe(false);
    document.querySelector('.onboarding-free-filter').click();
    flushSync();
    expect(byText('button', 'Start chatting').disabled).toBe(true);
    expect(document.querySelector('.onboarding-selection')).toBeNull();
  });

  it('lets users leave while a catalog read is pending and ignores its late result', async () => {
    server.state.connected = true;
    let resolveModels;
    rpcMock.mockImplementation((method, params) =>
      method === 'model.list'
        ? new Promise((resolve) => {
            resolveModels = resolve;
          })
        : server.rpc(method, params),
    );
    mountedComponent = mount(OnboardingView, { target: document.body });
    await continueToModels();
    await waitFor(() => expect(resolveModels).toBeTypeOf('function'));
    expect(byText('button', 'Back to Providers')).toBeTruthy();
    await unmount(mountedComponent);
    mountedComponent = null;
    resolveModels({ models: openrouterModels() });
    await Promise.resolve();
    flushSync();
    expect(document.querySelector('.onboarding-view')).toBeNull();
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'agent.update'),
    ).toBe(false);
  });
});
