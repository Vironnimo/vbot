// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  agentsPayload,
  buttonByText,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  openSubAgentsPanel,
  resetSettingsViewHarness,
  rpcMock,
  setInputValue,
  SettingsView,
  waitForCondition,
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

  it('auto-saves sub-agent settings 800 ms after the last change', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();

    vi.useFakeTimers();

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');

    expect(getSettingsUpdateCalls()).toHaveLength(0);

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(0);

    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      subagents: {
        max_subagent_depth: 6,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });

  it('manual save cancels a pending debounce timer', async () => {
    let resolveFirstUpdate;
    let settingsUpdateCallCount = 0;

    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settingsUpdate: async () => {
          settingsUpdateCallCount += 1;

          if (settingsUpdateCallCount === 1) {
            await new Promise((resolve) => {
              resolveFirstUpdate = resolve;
            });
          }

          return null;
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();

    vi.useFakeTimers();

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');

    getButton('Save').click();
    flushSync();

    expect(getSettingsUpdateCalls()).toHaveLength(1);

    setInputValue('input[aria-label="Max sub-agent depth"]', '7');

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();

    resolveFirstUpdate();
    await flushAsyncUpdates();

    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(1);

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      subagents: {
        max_subagent_depth: 7,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });

  it('shows Already saved when manual save is clicked with no changes', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openSubAgentsPanel();

    getButton('Save').click();
    flushSync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Already saved', variant: 'success' }),
    );
    expect(document.body.textContent).not.toContain('Already saved');
    expect(getSettingsUpdateCalls()).toHaveLength(0);
  });

  it('shows server-wide Voice settings but hides Desktop-only connection settings', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForCondition(() => buttonByText('Appearance'));

    expect(buttonByText('Connection')).toBeUndefined();
    expect(buttonByText('Voice')).toBeTruthy();
    expect(document.body.textContent).toContain('Transcription audio');
    expect(document.body.textContent).toContain(
      'Wakeword listening is configured in the vBot Desktop app',
    );
  });

  it('highlights the Voice section once for a target panel request', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    window.history.pushState({}, '', '/?accessor=desktop');
    window.pywebview = {
      api: {
        listServers: vi.fn().mockResolvedValue([
          {
            host: 'pi.lan',
            port: 8420,
            label: 'Home',
            active: true,
          },
        ]),
        getWakewordStatus: vi.fn().mockResolvedValue({
          enabled: false,
          state: 'off',
        }),
      },
    };

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        agents: agentsPayload(),
        desktopCapabilities: { wakeword: true, serverSelection: true },
        targetPanelId: 'voice',
        targetPanelRequestId: 1,
      },
    });
    flushSync();

    // The Voice section renders (desktop capability) and the target request
    // marks its index entry active.
    await waitForCondition(() =>
      document.body.textContent.includes('Wakeword listening'),
    );
    expect(buttonByText('Connection')).toBeTruthy();
    expect(document.body.textContent).toContain('Desktop app');
    await waitForCondition(
      () =>
        buttonByText('Voice')?.classList.contains('snav-item--active') === true,
    );

    // Navigating elsewhere moves the index highlight; the Voice section stays
    // in the document (sections are never unmounted).
    buttonByText('Server info').click();
    flushSync();

    await waitForCondition(
      () =>
        buttonByText('Server info')?.classList.contains('snav-item--active') ===
        true,
    );
    expect(buttonByText('Voice')?.classList.contains('snav-item--active')).toBe(
      false,
    );
    expect(document.body.textContent).toContain('Wakeword listening');
    expect(document.body.textContent).toContain('Server host');
  });

  it('keeps in-progress values while an auto-save request is in flight', async () => {
    let resolveFirstUpdate;
    let settingsUpdateCallCount = 0;

    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settingsUpdate: async () => {
          settingsUpdateCallCount += 1;

          if (settingsUpdateCallCount === 1) {
            await new Promise((resolve) => {
              resolveFirstUpdate = resolve;
            });
          }

          return null;
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();

    vi.useFakeTimers();

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');

    vi.advanceTimersByTime(800);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(1);

    setInputValue('input[aria-label="Max sub-agent depth"]', '7');

    resolveFirstUpdate();
    await flushAsyncUpdates();

    const depthInput = document.body.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    expect(depthInput).toBeTruthy();
    expect(depthInput.value).toBe('7');

    vi.advanceTimersByTime(800);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      subagents: {
        max_subagent_depth: 7,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });
});
