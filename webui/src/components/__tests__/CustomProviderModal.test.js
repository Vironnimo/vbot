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

const { default: CustomProviderModal } =
  await import('../settings/CustomProviderModal.svelte');

describe('CustomProviderModal', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockResolvedValue({ provider: { id: 'local-ai' } });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
    }
    document.body.innerHTML = '';
  });

  it('creates a keyless Custom Provider with manual Model facts', async () => {
    const onSaved = vi.fn().mockResolvedValue(undefined);
    mountedComponent = mount(CustomProviderModal, {
      target: document.body,
      props: { onSaved },
    });
    flushSync();

    input('custom-provider-id', 'local-ai');
    input('custom-provider-name', 'Local AI');
    input('custom-provider-base-url', 'http://127.0.0.1:8080/v1');
    select('custom-provider-auth', 'none');
    button('Add Model').click();
    flushSync();
    input('custom-model-0-id', 'chat-model');
    input('custom-model-0-name', 'Chat Model');
    input('custom-model-0-context', '65536');
    input('custom-model-0-output', '2048');
    input('custom-model-0-tasks', 'chat, text_output');
    button('Save').click();

    await waitForCondition(() => rpcMock.mock.calls.length === 1);
    expect(rpcMock).toHaveBeenCalledWith('provider.custom_save', {
      provider: {
        id: 'local-ai',
        name: 'Local AI',
        adapter: 'openai_compatible',
        base_url: 'http://127.0.0.1:8080/v1',
        auth: 'none',
        models_endpoint: '/models',
        defaults: {},
        models: {
          'chat-model': {
            name: 'Chat Model',
            context_window: 65536,
            max_output_tokens: 2048,
            capabilities: {
              vision: false,
              tools: true,
              json_mode: false,
              reasoning: false,
              input_modalities: ['text'],
              output_modalities: ['text'],
              task_types: ['chat', 'text_output'],
              supported_parameters: [],
              supported_voices: [],
              task_options: {},
            },
          },
        },
      },
    });
    expect(onSaved).toHaveBeenCalledOnce();
  });

  it('edits without reading or replacing an existing API key', async () => {
    mountedComponent = mount(CustomProviderModal, {
      target: document.body,
      props: {
        provider: {
          id: 'gateway',
          name: 'Gateway',
          adapter: 'openai_compatible',
          base_url: 'https://gateway.example/v1',
          auth: 'api_key',
          models_endpoint: null,
          defaults: { temperature: 0 },
          models: {
            model: {
              name: 'Model',
              context_window: null,
              max_output_tokens: null,
              capabilities: {
                vision: false,
                tools: true,
                json_mode: false,
                reasoning: false,
                input_modalities: ['text'],
                output_modalities: ['text'],
                task_types: ['chat'],
                supported_parameters: [],
                supported_voices: [],
                task_options: {},
              },
            },
          },
        },
      },
    });
    flushSync();

    expect(document.getElementById('custom-provider-id').disabled).toBe(true);
    expect(document.getElementById('custom-provider-api-key').value).toBe('');
    input('custom-provider-name', 'Renamed Gateway');
    button('Save').click();

    await waitForCondition(() => rpcMock.mock.calls.length === 1);
    const params = rpcMock.mock.calls[0][1];
    expect(params.api_key).toBeUndefined();
    expect(params.provider.name).toBe('Renamed Gateway');
    expect(params.provider.defaults).toEqual({ temperature: 0 });
  });
});

function input(id, value) {
  const element = document.getElementById(id);
  element.value = value;
  element.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function select(id, value) {
  const element = document.getElementById(id);
  element.value = value;
  element.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

function button(text) {
  return [...document.querySelectorAll('button')].find(
    (element) => element.textContent.trim() === text,
  );
}

async function waitForCondition(check, attempts = 20) {
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
