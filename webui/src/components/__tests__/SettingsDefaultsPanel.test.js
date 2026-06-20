// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SettingsDefaultsPanel } =
  await import('../settings/SettingsDefaultsPanel.svelte');

describe('SettingsDefaultsPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({ models: [] });
      }
      if (method === 'connection.list') {
        return Promise.resolve({ connections: [] });
      }
      return Promise.resolve({});
    });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('reloads the model catalog when modelsRefreshToken changes', async () => {
    const props = reactiveProps({ settings: {}, modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsDefaultsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForCondition(() => callCount('model.list') >= 1);

    const modelListBefore = callCount('model.list');
    const connectionBefore = callCount('connection.list');

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => callCount('model.list') > modelListBefore);

    expect(callCount('connection.list')).toBeGreaterThan(connectionBefore);
  });
});

function callCount(method) {
  return rpcMock.mock.calls.filter((call) => call[0] === method).length;
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
