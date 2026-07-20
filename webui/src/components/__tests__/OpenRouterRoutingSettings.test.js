// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();
const onReloadSettingsMock = vi.fn();
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
    onReloadSettingsMock.mockReset();
    onReloadSettingsMock.mockResolvedValue(undefined);
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

    clickButton('Automatic (cache-friendly)');
    clickOption('Only allowed providers');

    clickButton('Add provider…');
    clickOption('Anthropic');

    clickButton('Block provider…');
    clickOption('DeepInfra');

    document
      .querySelector('[aria-label="Allow OpenRouter provider fallbacks"]')
      .click();
    clickButton('Save routing');

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
    });
    expect(onReloadSettingsMock).toHaveBeenCalledOnce();
    expect(onToastMock).toHaveBeenCalledWith({
      title: 'OpenRouter routing settings saved.',
      variant: 'success',
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
    await waitForCondition(() =>
      document.body.textContent.includes('This model inherits'),
    );
    document
      .querySelector(
        '[aria-label="Use a routing override for anthropic/claude-sonnet-4"]',
      )
      .click();
    flushSync();

    clickButton('Only allowed providers');
    clickOption('Preferred provider order');
    expect(document.body.textContent).toContain(
      'A manual provider order overrides OpenRouter Sticky Routing.',
    );

    clickButton('Save routing');
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

    expect(document.body.textContent).toContain(
      'This model has its own routing policy.',
    );
    expect(document.body.textContent).toContain('deepinfra');
  });

  function mountEditor(routing = defaultRouting) {
    mountedComponent = mount(OpenRouterRoutingSettings, {
      target: document.body,
      props: {
        provider: { id: 'openrouter', routing },
        active: true,
        onReloadSettings: onReloadSettingsMock,
        onToast: onToastMock,
      },
    });
    flushSync();
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
