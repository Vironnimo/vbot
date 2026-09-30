// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { createAutosaveCoordinator } from '../../lib/autosave.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  buttonsByText,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  openSearchableDropdown,
  openSimpleDropdown,
  resetSettingsViewHarness,
  rpcMock,
  selectSearchableOption,
  selectSimpleOption,
  setInputValue,
  settingsPayload,
  waitForCondition,
  waitForModelCatalogs,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsDefaultsPanel } =
  await import('../settings/SettingsDefaultsPanel.svelte');
const { default: AutosaveContextHost } =
  await import('./AutosaveContextHost.support.svelte');

function callCount(method) {
  return rpcMock.mock.calls.filter((call) => call[0] === method).length;
}

describe('SettingsDefaultsPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  it('saves model, fallback chain, temperature and thinking effort, and clears them to no default', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    // The host (AgentsView) passes each committed response back as settings.
    const props = reactiveProps({ settings: settingsPayload() });
    props.onCommit = (next) => {
      props.settings = next;
    };
    mountedComponent = mount(SettingsDefaultsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForModelCatalogs();

    expect(
      document.querySelector('#settings-defaults-temperature'),
    ).toBeTruthy();
    expect(buttonsByText('Clear')).toHaveLength(0);

    await openSearchableDropdown('settings-defaults-model');
    selectSearchableOption('settings-defaults-model', 'openai/gpt-5.2');

    // The fallback chain starts empty: add a row, then pick the model.
    document
      .querySelector('.settings-view__fallback-add')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    await openSearchableDropdown('settings-defaults-fallback-model-0');
    selectSearchableOption(
      'settings-defaults-fallback-model-0',
      'openai/gpt-5.2-mini',
    );

    setInputValue('#settings-defaults-temperature', '0.7');
    openSimpleDropdown('settings-defaults-thinking-effort');
    selectSimpleOption('settings-defaults-thinking-effort', 'high');

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);
    const chosen = {
      defaults: {
        agent: {
          model: 'openai/gpt-5.2::api-key',
          fallback_models: ['openai/gpt-5.2-mini::api-key'],
          temperature: 0.7,
          thinking_effort: 'high',
        },
      },
    };
    const cleared = {
      defaults: {
        agent: {
          model: null,
          fallback_models: null,
          temperature: null,
          thinking_effort: null,
        },
      },
    };
    // Each write names the saved values it replaces as its base.
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      ...chosen,
      base: cleared,
    });

    await openSearchableDropdown('settings-defaults-model');
    selectSearchableOption('settings-defaults-model', '— (no default)');
    // Removing the chain row clears the list back to no default.
    document
      .querySelector('.settings-view__fallback-remove')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    setInputValue('#settings-defaults-temperature', '');
    openSimpleDropdown('settings-defaults-thinking-effort');
    selectSimpleOption('settings-defaults-thinking-effort', '— (no default)');

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      ...cleared,
      base: chosen,
    });

    // "Provider default" is an explicit empty effort, distinct from no default.
    openSimpleDropdown('settings-defaults-thinking-effort');
    selectSimpleOption(
      'settings-defaults-thinking-effort',
      '— (provider default)',
    );
    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 3);
    expect(getSettingsUpdateCalls()[2][1]).toEqual({
      defaults: {
        agent: {
          model: null,
          fallback_models: null,
          temperature: null,
          thinking_effort: '',
        },
      },
      base: cleared,
    });
  });

  it('settles a draft that normalizes to the stored values without a write', async () => {
    const stored = settingsPayload();
    stored.defaults.agent = { temperature: 0.5 };
    rpcMock.mockImplementation(createSettingsRpcMock({ settings: stored }));
    const coordinator = createAutosaveCoordinator();
    mountedComponent = mount(AutosaveContextHost, {
      target: document.body,
      props: {
        component: SettingsDefaultsPanel,
        componentProps: { settings: stored },
        coordinator,
      },
    });
    flushSync();
    await waitForModelCatalogs();
    vi.useFakeTimers();

    // An empty fallback row and a respelled temperature send the stored values.
    document
      .querySelector('.settings-view__fallback-add')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    setInputValue('#settings-defaults-temperature', '0.50');
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(0);
    expect(coordinator.hasPending()).toBe(false);
    await expect(coordinator.flushPending()).resolves.toBe(true);
    expect(getSettingsUpdateCalls()).toHaveLength(0);
  });

  it('reloads the model catalog when modelsRefreshToken changes', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    const props = reactiveProps({ settings: {}, modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsDefaultsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForModelCatalogs();
    const modelListBefore = callCount('model.list');
    const connectionBefore = callCount('connection.list');

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => callCount('model.list') > modelListBefore);
    expect(callCount('connection.list')).toBeGreaterThan(connectionBefore);
  });
});
