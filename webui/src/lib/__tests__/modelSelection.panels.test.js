// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { reactiveProps } from '../../components/__tests__/reactiveProps.support.svelte.js';
import {
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  openSearchableDropdown,
  resetSettingsViewHarness,
  rpcMock,
  waitForCondition,
} from '../../components/__tests__/SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: Compaction } =
  await import('../../components/settings/SettingsCompactionPanel.svelte');
const { default: Librarian } =
  await import('../../components/settings/SettingsLibrarianPanel.svelte');
const { default: SessionTitles } =
  await import('../../components/settings/SettingsSessionTitlesPanel.svelte');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const model = (id) => ({
  id,
  provider_id: 'openai',
  capabilities: { tools: true },
  context_window: 128000,
});
const connections = [
  {
    id: 'openai:api-key',
    provider_id: 'openai',
    usable: true,
    label: 'API Key',
  },
];

describe.each([
  ['Compaction', Compaction, 'settings-compaction-summary-model'],
  ['Librarian', Librarian, 'settings-librarian-model'],
  ['Session titles', SessionTitles, 'settings-session-title-model'],
])('%s Model catalog', (_name, Component, pickerId) => {
  let component;

  beforeEach(() => {
    resetSettingsViewHarness();
  });
  afterEach(async () => {
    component = await cleanupSettingsViewHarness(component);
  });

  it('keeps newer responses, holds an open picker, and ignores failures after unmount', async () => {
    const reads = Array.from({ length: 4 }, deferred);
    let index = 0;
    const backend = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'model.list') return reads[index++].promise;
      if (method === 'connection.list') return Promise.resolve({ connections });
      if (method === 'librarian.overview')
        return Promise.resolve({ available: true, passes: [] });
      if (method === 'agent.get')
        return Promise.resolve({
          id: 'librarian',
          builtin: 'librarian',
          config: {},
          effective: {},
        });
      return backend(method, params);
    });
    const onError = vi.fn();
    const props = reactiveProps({
      settings: { session_titles: { enabled: true } },
      modelsRefreshToken: 0,
      onError,
    });
    component = mount(Component, { target: document.body, props });
    flushSync();
    await waitForCondition(() => index === 1);
    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => index === 2);
    reads[1].resolve({ models: [model('openai/newer')] });
    await flushAsyncUpdates();
    await openSearchableDropdown(pickerId);
    const options = () =>
      Array.from(document.querySelectorAll('.searchable-dropdown__option')).map(
        (node) => node.textContent,
      );
    expect(options().some((text) => text.includes('openai/newer'))).toBe(true);

    reads[0].resolve({ models: [model('openai/older')] });
    await flushAsyncUpdates();
    props.modelsRefreshToken = 2;
    flushSync();
    await waitForCondition(() => index === 3);
    reads[2].resolve({ models: [model('openai/latest')] });
    await flushAsyncUpdates();
    expect(options().some((text) => text.includes('openai/newer'))).toBe(true);
    expect(options().some((text) => text.includes('openai/older'))).toBe(false);
    expect(options().some((text) => text.includes('openai/latest'))).toBe(
      false,
    );
    document.getElementById(pickerId).click();
    flushSync();
    await openSearchableDropdown(pickerId);
    expect(options().some((text) => text.includes('openai/latest'))).toBe(true);
    expect(options().some((text) => text.includes('openai/older'))).toBe(false);

    props.modelsRefreshToken = 3;
    flushSync();
    await waitForCondition(() => index === 4);
    await unmount(component);
    component = null;
    reads[3].reject(new Error('late catalog failure'));
    await flushAsyncUpdates();
    expect(onError).not.toHaveBeenCalled();
  });
});
