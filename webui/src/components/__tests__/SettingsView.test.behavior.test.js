// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  buttonsByText,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  getButton,
  getSettingsUpdateCalls,
  getSimpleTrigger,
  openCompactionPanel,
  openDefaultsPanel,
  openRecallPanel,
  openSearchableDropdown,
  openSimpleDropdown,
  openWebSearchPanel,
  resetSettingsViewHarness,
  rpcMock,
  selectSearchableOption,
  selectSimpleOption,
  setInputValue,
  SettingsView,
  waitForCondition,
  waitForModelCatalogs,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  it('renders and saves the Defaults section', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openDefaultsPanel();

    expect(document.querySelector('#settings-defaults-model')).toBeTruthy();
    expect(document.querySelector('.settings-view__fallback-add')).toBeTruthy();
    expect(
      document.querySelector('#settings-defaults-temperature'),
    ).toBeTruthy();
    expect(
      document.querySelector('#settings-defaults-thinking-effort'),
    ).toBeTruthy();

    expect(buttonsByText('Clear')).toHaveLength(0);

    await waitForModelCatalogs();

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

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      defaults: {
        agent: {
          model: 'openai/gpt-5.2::api-key',
          fallback_models: ['openai/gpt-5.2-mini::api-key'],
          temperature: 0.7,
          thinking_effort: 'high',
        },
      },
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
      defaults: {
        agent: {
          model: null,
          fallback_models: null,
          temperature: null,
          thinking_effort: null,
        },
      },
    });

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
    });
  });

  it('uses the model picker for compaction summary model', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openCompactionPanel();
    await waitForModelCatalogs();

    await openSearchableDropdown('settings-compaction-summary-model');
    selectSearchableOption(
      'settings-compaction-summary-model',
      'openai/gpt-5.2-mini',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      compaction: {
        enabled: true,
        trigger: { type: 'context_ratio', threshold: 0.8 },
        strategy: {
          type: 'summary_tail',
          tail_tokens: 15000,
          summary_model: 'openai/gpt-5.2-mini::api-key',
        },
      },
    });

    await openSearchableDropdown('settings-compaction-summary-model');
    selectSearchableOption(
      'settings-compaction-summary-model',
      'Active agent model',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 2);

    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      compaction: {
        enabled: true,
        trigger: { type: 'context_ratio', threshold: 0.8 },
        strategy: {
          type: 'summary_tail',
          tail_tokens: 15000,
          summary_model: null,
        },
      },
    });
  });

  it('renders and saves the Recall backend dropdown', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openRecallPanel();

    expect(getSimpleTrigger('settings-recall-backend')).toBeTruthy();

    openSimpleDropdown('settings-recall-backend');
    selectSimpleOption(
      'settings-recall-backend',
      'Full-text search — fast keyword search with an index',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      recall: {
        backend: 'sqlite_fts',
      },
    });
  });

  it('renders and saves the Web Search provider settings', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openWebSearchPanel();

    expect(getSimpleTrigger('settings-web-search-provider')).toBeTruthy();

    openSimpleDropdown('settings-web-search-provider');
    selectSimpleOption('settings-web-search-provider', 'SearXNG');

    const baseUrlInput = document.body.querySelector(
      '#settings-web-search-searxng-base-url',
    );
    expect(baseUrlInput).not.toBeNull();
    baseUrlInput.value = 'http://localhost:9999';
    baseUrlInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      web_search: {
        provider: 'searxng',
        default_count: 12,
        searxng: {
          base_url: 'http://localhost:9999',
        },
      },
    });
  });
});
