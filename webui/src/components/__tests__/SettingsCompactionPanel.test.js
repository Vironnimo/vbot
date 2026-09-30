// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  getButton,
  getSettingsUpdateCalls,
  openSearchableDropdown,
  resetSettingsViewHarness,
  rpcMock,
  selectSearchableOption,
  setInputValue,
  settingsPayload,
  waitForCondition,
  waitForModelCatalogs,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsCompactionPanel } =
  await import('../settings/SettingsCompactionPanel.svelte');

function callCount(method) {
  return rpcMock.mock.calls.filter((call) => call[0] === method).length;
}

describe('SettingsCompactionPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  // The host (AgentsView) passes each committed response back as settings.
  async function mountPanel() {
    const props = reactiveProps({ settings: settingsPayload() });
    props.onCommit = (next) => {
      props.settings = next;
    };
    mountedComponent = mount(SettingsCompactionPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForModelCatalogs();
  }

  // The stored policy every first write replaces.
  const STORED = {
    compaction: {
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.8 },
      strategy: {
        type: 'summary_tail',
        tail_tokens: 15000,
        summary_model: null,
      },
    },
  };

  function saveCall(index) {
    getButton('Save').click();
    return waitForCondition(() => getSettingsUpdateCalls().length > index).then(
      () => getSettingsUpdateCalls()[index][1],
    );
  }

  it('saves a summary model and token limit, then clears the model to the active agent model', async () => {
    await mountPanel();

    setInputValue(
      'input[aria-label="Maximum input tokens (optional)"]',
      '200000',
    );
    await openSearchableDropdown('settings-compaction-summary-model');
    selectSearchableOption(
      'settings-compaction-summary-model',
      'openai/gpt-5.2-mini',
    );
    const trigger = {
      type: 'context_ratio',
      threshold: 0.8,
      tokens: 200000,
    };
    const chosen = {
      compaction: {
        enabled: true,
        trigger,
        strategy: {
          type: 'summary_tail',
          tail_tokens: 15000,
          summary_model: 'openai/gpt-5.2-mini::api-key',
        },
      },
    };
    expect(await saveCall(0)).toEqual({ ...chosen, base: STORED });

    await openSearchableDropdown('settings-compaction-summary-model');
    selectSearchableOption(
      'settings-compaction-summary-model',
      'Active agent model',
    );
    expect(await saveCall(1)).toEqual({
      compaction: {
        enabled: true,
        trigger,
        strategy: {
          type: 'summary_tail',
          tail_tokens: 15000,
          summary_model: null,
        },
      },
      base: chosen,
    });
  });

  it('saves Classic without tail fields and keeps automatic triggering independent', async () => {
    await mountPanel();
    document
      .querySelector(
        'input[name="settings-compaction-strategy"][value="continuation"]',
      )
      .click();
    flushSync();
    expect(
      document.querySelector('#settings-compaction-summary-model'),
    ).toBeNull();
    expect(
      document.querySelector('input[aria-label="Verbatim tail tokens"]'),
    ).toBeNull();
    const automatic = document.querySelector('[role="switch"]');
    automatic.click();
    flushSync();
    expect(await saveCall(0)).toEqual({
      compaction: {
        enabled: false,
        trigger: { type: 'context_ratio', threshold: 0.8 },
        strategy: { type: 'continuation' },
      },
      base: STORED,
    });

    document
      .querySelector(
        'input[name="settings-compaction-strategy"][value="summary_tail"]',
      )
      .click();
    flushSync();
    expect(
      document.querySelector('#settings-compaction-summary-model'),
    ).toBeTruthy();
    expect(automatic.getAttribute('aria-checked')).toBe('false');
    expect((await saveCall(1)).compaction).toMatchObject({
      enabled: false,
      strategy: {
        type: 'summary_tail',
        tail_tokens: 15000,
        summary_model: null,
      },
    });
  });

  it('reloads the model catalog when modelsRefreshToken changes', async () => {
    const props = reactiveProps({ settings: {}, modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsCompactionPanel, {
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
