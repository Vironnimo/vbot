// @vitest-environment jsdom

// The save lifecycle every Settings editor shares, exercised through the
// sub-agent limits: debounced autosave, manual Save, and the transition flush.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { createAutosaveCoordinator } from '../../lib/autosave.js';
import {
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  openSubAgentsPanel,
  openWebSearchPanel,
  resetSettingsViewHarness,
  rpcMock,
  setInputValue,
  settingsPayload,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsView } = await import('../SettingsView.svelte');
const { default: AutosaveContextHost } =
  await import('./AutosaveContextHost.support.svelte');

const DEPTH = 'input[aria-label="Max sub-agent depth"]';

function subagents(depth) {
  return {
    subagents: {
      max_subagent_depth: depth,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 60,
    },
  };
}

// Holds the first settings.update open until the test releases it.
function holdFirstSave() {
  const control = { release: null };
  let count = 0;
  control.backend = createSettingsRpcMock({
    settingsUpdate: async () => {
      count += 1;
      if (count === 1) {
        await new Promise((resolve) => {
          control.release = resolve;
        });
      }
      return null;
    },
  });
  return control;
}

describe('SettingsView editor saving', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  async function mountSubAgents(backend = createSettingsRpcMock(), props = {}) {
    rpcMock.mockImplementation(backend);
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await openSubAgentsPanel();
  }

  it('auto-saves 800 ms after the last change and restarts the interval on every edit', async () => {
    await mountSubAgents();
    vi.useFakeTimers();

    setInputValue(DEPTH, '6');
    await vi.advanceTimersByTimeAsync(600);
    setInputValue(DEPTH, '7');
    await vi.advanceTimersByTimeAsync(799);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(1);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual(subagents(7));
  });

  it('keeps a focused number editable through pauses and saves on blur', async () => {
    await mountSubAgents();
    vi.useFakeTimers();
    const input = document.querySelector(DEPTH);
    input.focus();
    setInputValue(DEPTH, '6');
    await vi.advanceTimersByTimeAsync(1600);
    expect(getSettingsUpdateCalls()).toHaveLength(0);
    expect(document.activeElement).toBe(input);
    expect(input.disabled).toBe(false);

    input.blur();
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);
  });

  it('saves every edited field manually and returns the control to Saved', async () => {
    const toastMock = vi.fn();
    await mountSubAgents(createSettingsRpcMock(), { onToast: toastMock });
    const inputs = document.querySelectorAll(
      '[data-settings-section="subagents"] input.s-input',
    );
    expect(Array.from(inputs, (input) => input.value)).toEqual([
      '4',
      '8',
      '60',
    ]);
    const saveControl = inputs[0]
      .closest('.settings-editor')
      .querySelector('.save-button');
    expect(saveControl.textContent.trim()).toBe('Saved');

    for (const [index, value] of [
      [0, '5'],
      [1, '12'],
      [2, '45'],
    ]) {
      inputs[index].value = value;
      inputs[index].dispatchEvent(new Event('input', { bubbles: true }));
    }
    flushSync();
    expect(saveControl.textContent.trim()).toBe('Save');
    saveControl.click();
    flushSync();

    expect(getSettingsUpdateCalls()).toEqual([
      [
        'settings.update',
        {
          subagents: {
            max_subagent_depth: 5,
            max_subagents_per_turn: 12,
            subagent_timeout_minutes: 45,
          },
        },
      ],
    ]);
    await waitForCondition(() => saveControl.textContent.trim() === 'Saved');
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' }),
    );
  });

  it('reports a successful no-op when manual save is clicked with no changes', async () => {
    const toastMock = vi.fn();
    await mountSubAgents(createSettingsRpcMock(), { onToast: toastMock });

    // A clean draft already reads as saved; clicking still confirms it.
    getButton('Saved').click();
    flushSync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' }),
    );
    expect(getSettingsUpdateCalls()).toHaveLength(0);
  });

  it('cancels a pending debounce timer on manual save', async () => {
    const held = holdFirstSave();
    await mountSubAgents(held.backend);
    vi.useFakeTimers();

    setInputValue(DEPTH, '6');
    getButton('Save').click();
    flushSync();
    expect(getSettingsUpdateCalls()).toHaveLength(1);

    setInputValue(DEPTH, '7');
    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();
    held.release();
    await flushAsyncUpdates();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual(subagents(7));
  });

  it('keeps in-progress values while an auto-save request is in flight', async () => {
    const held = holdFirstSave();
    await mountSubAgents(held.backend);
    vi.useFakeTimers();

    setInputValue(DEPTH, '6');
    vi.advanceTimersByTime(800);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);

    setInputValue(DEPTH, '7');
    held.release();
    await flushAsyncUpdates();
    expect(document.querySelector(DEPTH).value).toBe('7');

    vi.advanceTimersByTime(800);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual(subagents(7));
  });

  // A cleared number field falls back to its default. It writes only when the
  // stored value differs, and never leaves a pending draft that blocks
  // navigation.
  const clearedFields = {
    'sub-agent depth': {
      open: openSubAgentsPanel,
      selector: DEPTH,
      store: (settings, value) => {
        settings.subagents.max_subagent_depth = value;
      },
      saved: (write) => write.subagents.max_subagent_depth,
      fallback: 4,
    },
    'Web Search result count': {
      open: openWebSearchPanel,
      selector: '#settings-web-search-default-count',
      store: (settings, value) => {
        settings.web_search.default_count = value;
      },
      saved: (write) => write.web_search.default_count,
      fallback: 12,
    },
  };

  it.each([
    ['sub-agent depth', 6, 1],
    ['sub-agent depth', 4, 0],
    ['Web Search result count', 7, 1],
    ['Web Search result count', 12, 0],
  ])(
    'settles a cleared %s stored as %i with %i writes',
    async (fieldName, storedValue, expectedWrites) => {
      const { open, selector, store, saved, fallback } =
        clearedFields[fieldName];
      const settings = settingsPayload();
      store(settings, storedValue);
      rpcMock.mockImplementation(createSettingsRpcMock({ settings }));
      const coordinator = createAutosaveCoordinator();
      mountedComponent = mount(AutosaveContextHost, {
        target: document.body,
        props: { component: SettingsView, coordinator },
      });
      flushSync();
      await open();
      vi.useFakeTimers();

      setInputValue(selector, '');
      await vi.advanceTimersByTimeAsync(800);
      await flushAsyncUpdates();

      const writes = getSettingsUpdateCalls();
      expect(writes).toHaveLength(expectedWrites);
      if (expectedWrites > 0) {
        expect(saved(writes[0][1])).toBe(fallback);
        // The field shows what was saved rather than staying blank.
        expect(document.querySelector(selector).value).toBe(String(fallback));
      }
      expect(coordinator.hasPending()).toBe(false);
      await expect(coordinator.flushPending()).resolves.toBe(true);
      expect(getSettingsUpdateCalls()).toHaveLength(expectedWrites);
    },
  );
});
