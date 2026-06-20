// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const rpcMock = vi.fn();
const onReloadSettingsMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SettingsProvidersPanel } =
  await import('../settings/SettingsProvidersPanel.svelte');

describe('SettingsProvidersPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockResolvedValue({});
    onReloadSettingsMock.mockReset();
    onReloadSettingsMock.mockResolvedValue(undefined);
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('reflects a provider change via a settings reload when modelsRefreshToken changes', async () => {
    const props = reactiveProps({
      settings: { providers: { items: [] } },
      visible: true,
      onReloadSettings: onReloadSettingsMock,
      modelsRefreshToken: 0,
    });
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props,
    });
    flushSync();

    // The panel reads its display from the settings prop, so mount alone must
    // not trigger a reload.
    expect(onReloadSettingsMock).not.toHaveBeenCalled();

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => onReloadSettingsMock.mock.calls.length >= 1);
  });
});

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
