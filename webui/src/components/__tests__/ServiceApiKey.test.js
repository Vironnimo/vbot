// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const rpcMock = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));
const { default: ServiceApiKey } =
  await import('../settings/ServiceApiKey.svelte');

const INPUT_ID = 'settings-test-api-key';

function tavily(overrides = {}) {
  return {
    id: 'tavily',
    api_key_env: 'TAVILY_API_KEY',
    configured: false,
    source: null,
    shared: true,
    ...overrides,
  };
}

describe('Service API key', () => {
  let component;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
  });
  afterEach(async () => {
    if (component) await unmount(component);
    component = null;
    document.body.innerHTML = '';
  });

  function mountKey(props) {
    component = mount(ServiceApiKey, { target: document.body, props });
    flushSync();
  }

  function row() {
    return document.querySelector('[data-api-key]');
  }

  function input() {
    return document.getElementById(INPUT_ID);
  }

  function button(label) {
    return [...document.querySelectorAll('button')].find(
      (item) => item.textContent.trim() === label,
    );
  }

  function type(value) {
    input().value = value;
    input().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }

  function submit() {
    input()
      .closest('form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  }

  async function settle() {
    for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
    flushSync();
  }

  it('saves a missing key write-only, then replaces and removes it', async () => {
    const commits = [];
    const toasts = [];
    const props = reactiveProps({
      id: INPUT_ID,
      service: tavily(),
      dataDirectory: '/vbot-data',
      onCommit: (next) => commits.push(next),
      onToast: (toast) => toasts.push(toast.title),
    });
    // The server answers with the full settings; the panel then passes the
    // new key state back in.
    rpcMock.mockImplementation(async (_method, params) => {
      const set = params.value !== '';
      props.service = tavily({
        configured: set,
        source: set ? 'data_dir' : null,
      });
      return { settings: 'response' };
    });
    mountKey(props);

    expect(row().dataset.apiKey).toBe('missing');
    // The "?" names the .env location and that the key is shared.
    document.querySelector('.info-hint').click();
    flushSync();
    const help = document.querySelector('.info-popover').textContent;
    expect(help).toContain('/vbot-data');
    expect(help).toContain(
      t('settings.serviceKey.helpShared', { variable: 'TAVILY_API_KEY' }),
    );
    expect(button(t('common.save')).disabled).toBe(true);
    type('   ');
    expect(button(t('common.save')).disabled).toBe(true);

    type('  tvly-secret  ');
    submit();
    await settle();

    expect(rpcMock).toHaveBeenCalledWith('settings.set_service_key', {
      api_key_env: 'TAVILY_API_KEY',
      value: 'tvly-secret',
    });
    expect(commits).toEqual([{ settings: 'response' }]);
    expect(toasts).toEqual([t('settings.serviceKey.saveSuccess')]);
    expect(row().dataset.apiKey).toBe('set');
    expect(row().dataset.apiKeySource).toBe('data_dir');
    // The saved value is never shown again.
    expect(input()).toBeNull();
    expect(document.body.innerHTML).not.toContain('tvly-secret');

    // Replace opens an empty field; cancelling drops what was typed.
    button(t('settings.serviceKey.replace')).click();
    flushSync();
    expect(input().value).toBe('');
    type('draft');
    button(t('common.cancel')).click();
    flushSync();
    expect(input()).toBeNull();
    button(t('settings.serviceKey.replace')).click();
    flushSync();
    expect(input().value).toBe('');
    button(t('common.cancel')).click();
    flushSync();

    button(t('common.remove')).click();
    await settle();

    expect(rpcMock).toHaveBeenLastCalledWith('settings.set_service_key', {
      api_key_env: 'TAVILY_API_KEY',
      value: '',
    });
    expect(toasts.at(-1)).toBe(t('settings.serviceKey.removeSuccess'));
    expect(row().dataset.apiKey).toBe('missing');
    expect(input().value).toBe('');
  });

  it('keeps a typed key for retry and reports a failed save', async () => {
    const errors = [];
    rpcMock.mockRejectedValue(new Error('Offline'));
    mountKey({
      id: INPUT_ID,
      service: tavily(),
      onError: (message) => errors.push(message),
    });

    type('tvly-secret');
    submit();
    await settle();

    expect(errors.at(-1)).toBe(`${t('settings.saveError')} Offline`);
    expect(input().value).toBe('tvly-secret');
    expect(row().dataset.apiKey).toBe('missing');
  });

  it('starts empty for another service instead of carrying a typed key over', () => {
    const props = reactiveProps({ id: INPUT_ID, service: tavily() });
    mountKey(props);

    type('tvly-secret');
    props.service = tavily({ id: 'exa', api_key_env: 'EXA_API_KEY' });
    flushSync();

    expect(input().value).toBe('');
  });

  it.each([
    [true, 'set'],
    [false, 'missing'],
  ])(
    'leaves a key from the server environment to the environment (configured: %s)',
    (configured, state) => {
      mountKey({
        id: INPUT_ID,
        service: tavily({ configured, source: 'process_environment' }),
      });

      expect(row().dataset.apiKey).toBe(state);
      expect(row().dataset.apiKeySource).toBe('process_environment');
      expect(row().textContent).toContain('TAVILY_API_KEY');
      expect(input()).toBeNull();
      expect(document.querySelectorAll('button:not(.info-hint)')).toHaveLength(
        0,
      );
    },
  );
});
