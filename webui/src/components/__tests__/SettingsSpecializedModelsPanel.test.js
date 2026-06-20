// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const listTaskModelTargetsMock = vi.fn();
const getTaskModelOptionsMock = vi.fn();
const updateTaskModelSettingsMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listTaskModelTargets: (...args) => listTaskModelTargetsMock(...args),
  getTaskModelOptions: (...args) => getTaskModelOptionsMock(...args),
  updateTaskModelSettings: (...args) => updateTaskModelSettingsMock(...args),
}));

const { default: SettingsSpecializedModelsPanel } =
  await import('../settings/SettingsSpecializedModelsPanel.svelte');

describe('SettingsSpecializedModelsPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    listTaskModelTargetsMock.mockReset();
    getTaskModelOptionsMock.mockReset();
    updateTaskModelSettingsMock.mockReset();
    listTaskModelTargetsMock.mockResolvedValue({ targets: [] });
    getTaskModelOptionsMock.mockResolvedValue({ fields: [] });
    updateTaskModelSettingsMock.mockResolvedValue({ model_tasks: {} });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('reloads task-model targets when modelsRefreshToken changes', async () => {
    const props = reactiveProps({ settings: {}, modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForCondition(
      () => listTaskModelTargetsMock.mock.calls.length >= 1,
    );

    const before = listTaskModelTargetsMock.mock.calls.length;

    // The form is idle, so the queued reload runs immediately.
    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(
      () => listTaskModelTargetsMock.mock.calls.length > before,
    );

    expect(listTaskModelTargetsMock.mock.calls.length).toBeGreaterThan(before);
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
