// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const rpcMock = vi.fn();
const onRefreshProviderSettingsMock = vi.fn();
const onToastMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: OpenRouterRoutingSettings } =
  await import('../settings/OpenRouterRoutingSettings.svelte');

const defaultRouting = {
  default: {
    mode: 'automatic',
    providers: [],
    blocked: [],
    allow_fallbacks: true,
  },
  models: {},
};

describe('OpenRouterRoutingSettings', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'model.list') {
        return Promise.resolve({
          models: [
            {
              model_id: 'anthropic/claude-sonnet-4',
              name: 'Claude Sonnet 4',
            },
          ],
        });
      }
      if (method === 'provider.routing_options') {
        return Promise.resolve({
          providers: [
            { slug: 'anthropic', name: 'Anthropic' },
            { slug: 'deepinfra', name: 'DeepInfra' },
          ],
        });
      }
      if (method === 'settings.update') {
        return Promise.resolve({});
      }
      throw new Error(
        `Unexpected RPC method: ${method} ${JSON.stringify(params)}`,
      );
    });
    onRefreshProviderSettingsMock.mockReset();
    onRefreshProviderSettingsMock.mockResolvedValue(undefined);
    onToastMock.mockReset();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('saves allowed and blocked providers with fallback control', async () => {
    mountEditor();
    await waitForCondition(() => routingCalls().length === 1);

    // The editor opens on demand from its Routing line.
    const toggle = document.querySelector(
      '[aria-controls="openrouter-routing-body"]',
    );
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    clickButton('Routing');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');

    clickButton('Automatic (OpenRouter managed)');
    clickOption('Only allowed providers');

    clickButton('Add provider…');
    clickOption('Anthropic');

    clickButton('Block provider…');
    clickOption('DeepInfra');

    document
      .querySelector('[aria-label="Allow OpenRouter provider fallbacks"]')
      .click();
    clickButton('Save');

    await waitForCondition(() => updateCalls().length === 1);
    expect(updateCalls()[0][1]).toEqual({
      providers: {
        openrouter: {
          routing: {
            default: {
              mode: 'allowed',
              providers: ['anthropic'],
              blocked: ['deepinfra'],
              allow_fallbacks: false,
            },
            models: {},
          },
        },
      },
      // The routing the draft started from: a newer save is not overwritten.
      base: { providers: { openrouter: { routing: defaultRouting } } },
    });
    expect(onRefreshProviderSettingsMock).toHaveBeenCalledOnce();
    await waitForCondition(
      () =>
        document
          .querySelector('.openrouter-routing [role="status"]')
          .textContent.trim() === t('common.saved'),
    );
  });

  it('rebases onto routing saved elsewhere and retries its own change', async () => {
    const override = {
      mode: 'automatic',
      providers: [],
      blocked: ['deepinfra'],
      allow_fallbacks: true,
    };
    const savedRouting = {
      default: defaultRouting.default,
      models: { 'retired/model': override },
    };
    const fallback = rpcMock.getMockImplementation();
    let refused = false;
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.update' && !refused) {
        refused = true;
        return Promise.reject(
          Object.assign(new Error('Settings changed'), {
            code: 'settings_conflict',
          }),
        );
      }
      if (method === 'settings.get') {
        return Promise.resolve({
          providers: { items: [{ id: 'openrouter', routing: savedRouting }] },
        });
      }
      return fallback(method, params);
    });
    mountEditor();
    await waitForCondition(() => routingCalls().length === 1);

    clickButton('Automatic (OpenRouter managed)');
    clickOption('Only allowed providers');
    clickButton('Add provider…');
    clickOption('Anthropic');
    clickButton('Save');

    await waitForCondition(() => updateCalls().length === 2);
    const allowed = {
      ...defaultRouting.default,
      mode: 'allowed',
      providers: ['anthropic'],
    };
    expect(updateCalls()[1][1]).toEqual({
      providers: {
        openrouter: {
          routing: { default: allowed, models: { 'retired/model': override } },
        },
      },
      base: { providers: { openrouter: { routing: savedRouting } } },
    });
  });

  it('creates a full model override and warns when order disables stickiness', async () => {
    mountEditor({
      default: {
        mode: 'allowed',
        providers: ['anthropic'],
        blocked: ['deepinfra'],
        allow_fallbacks: true,
      },
      models: {},
    });
    await waitForCondition(
      () =>
        document.querySelector(
          '[aria-label="Global routing"]:not([disabled])',
        ) && routingCalls().length === 1,
    );

    clickButton('Global routing');
    clickOption('Claude Sonnet 4');
    const overrideToggle = document.querySelector(
      '[aria-label="Use a routing override for anthropic/claude-sonnet-4"]',
    );
    expect(overrideToggle.getAttribute('aria-checked')).toBe('false');
    overrideToggle.click();
    flushSync();
    expect(overrideToggle.getAttribute('aria-checked')).toBe('true');

    clickButton('Only allowed providers');
    clickOption('Preferred provider order');
    expect(document.querySelector('.banner--warn')).toBeTruthy();

    clickButton('Save');
    await waitForCondition(() => updateCalls().length === 1);
    expect(
      updateCalls()[0][1].providers.openrouter.routing.models[
        'anthropic/claude-sonnet-4'
      ],
    ).toEqual({
      mode: 'ordered',
      providers: ['anthropic'],
      blocked: [],
      allow_fallbacks: true,
    });
    expect(routingCalls()).toContainEqual([
      'provider.routing_options',
      {
        provider_id: 'openrouter',
        model_id: 'anthropic/claude-sonnet-4',
      },
    ]);
  });

  it('keeps a saved override editable when the model leaves the catalog', async () => {
    mountEditor({
      default: defaultRouting.default,
      models: {
        'retired/model': {
          mode: 'automatic',
          providers: [],
          blocked: ['deepinfra'],
          allow_fallbacks: true,
        },
      },
    });
    await waitForCondition(() => routingCalls().length === 1);

    clickButton('Global routing');
    clickOption('retired/model');

    expect(
      document
        .querySelector(
          '[aria-label="Use a routing override for retired/model"]',
        )
        .getAttribute('aria-checked'),
    ).toBe('true');
    expect(document.body.textContent).toContain('deepinfra');
  });

  // An empty or failed catalog read is not repeated at once; the next
  // activation or a changed model catalog reads it again.
  it.each([
    ['an empty catalog', () => Promise.resolve({ models: [] }), 0],
    ['a failed read', () => Promise.reject(new Error('upstream down')), 1],
  ])(
    'reads %s once per activation and catalog change',
    async (_case, answer, errorsPerRead) => {
      const fallback = rpcMock.getMockImplementation();
      rpcMock.mockImplementation((method, params) =>
        method === 'model.list' ? answer() : fallback(method, params),
      );
      const onError = vi.fn();
      const props = mountEditor(defaultRouting, { onError });
      await waitForCondition(() => routingCalls().length === 1);
      await settle();
      expect(modelCalls()).toHaveLength(1);
      expect(onError).toHaveBeenCalledTimes(errorsPerRead);

      props.modelsRefreshToken += 1;
      await settle();
      expect(modelCalls()).toHaveLength(2);

      props.active = false;
      await settle();
      props.active = true;
      await settle();
      expect(modelCalls()).toHaveLength(3);
      expect(onError).toHaveBeenCalledTimes(3 * errorsPerRead);
    },
  );

  function mountEditor(routing = defaultRouting, extraProps = {}) {
    const props = reactiveProps({
      provider: { id: 'openrouter', routing },
      active: true,
      modelsRefreshToken: 0,
      onRefreshProviderSettings: onRefreshProviderSettingsMock,
      onToast: onToastMock,
      ...extraProps,
    });
    mountedComponent = mount(OpenRouterRoutingSettings, {
      target: document.body,
      props,
    });
    flushSync();
    return props;
  }
});

function clickButton(text) {
  const button = [...document.querySelectorAll('button')].find(
    (candidate) => candidate.textContent.trim() === text,
  );
  expect(button).toBeTruthy();
  button.click();
  flushSync();
}

function clickOption(text) {
  const option = [...document.querySelectorAll('[role="option"]')].find(
    (candidate) => candidate.textContent.includes(text),
  );
  expect(option).toBeTruthy();
  option.click();
  flushSync();
}

function routingCalls() {
  return rpcMock.mock.calls.filter(
    (call) => call[0] === 'provider.routing_options',
  );
}

function updateCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'settings.update');
}

function modelCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'model.list');
}

async function settle() {
  for (let index = 0; index < 5; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

async function waitForCondition(check, attempts = 30) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    if (check()) {
      return;
    }
  }
  throw new Error('Timed out waiting for condition.');
}
