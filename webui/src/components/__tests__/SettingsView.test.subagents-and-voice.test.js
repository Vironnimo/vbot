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
  settingsPayload,
  SettingsView,
  waitForCondition,
} from './SettingsView.support.js';
import { createAutosaveCoordinator } from '../../lib/autosave.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: AutosaveContextHost } =
  await import('./AutosaveContextHost.svelte');

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

  it('restarts the idle interval on every edit even while already dirty', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();
    vi.useFakeTimers();
    setInputValue('input[aria-label="Max sub-agent depth"]', '6');
    await vi.advanceTimersByTimeAsync(600);
    setInputValue('input[aria-label="Max sub-agent depth"]', '7');
    await vi.advanceTimersByTimeAsync(600);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(200);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);
    expect(getSettingsUpdateCalls()[0][1].subagents.max_subagent_depth).toBe(7);
  });

  it('keeps a focused number editable through pauses and saves on blur', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();
    vi.useFakeTimers();
    const input = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    input.focus();
    setInputValue('input[aria-label="Max sub-agent depth"]', '6');
    await vi.advanceTimersByTimeAsync(1600);
    expect(getSettingsUpdateCalls()).toHaveLength(0);
    expect(document.activeElement).toBe(input);
    expect(input.disabled).toBe(false);
    input.blur();
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);
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

  it.each([
    ['a stored custom value', 6, 1],
    ['the stored default value', 4, 0],
  ])(
    'settles a cleared number field over %s instead of blocking navigation',
    async (_label, storedDepth, expectedWrites) => {
      rpcMock.mockImplementation(
        createSettingsRpcMock({
          settings: {
            ...settingsPayload(),
            subagents: {
              max_subagent_depth: storedDepth,
              max_subagents_per_turn: 8,
              subagent_timeout_minutes: 60,
            },
          },
        }),
      );
      const coordinator = createAutosaveCoordinator();
      mountedComponent = mount(AutosaveContextHost, {
        target: document.body,
        props: { component: SettingsView, coordinator },
      });
      flushSync();
      await openSubAgentsPanel();
      vi.useFakeTimers();

      setInputValue('input[aria-label="Max sub-agent depth"]', '');
      await vi.advanceTimersByTimeAsync(800);
      await flushAsyncUpdates();

      const writes = getSettingsUpdateCalls();
      expect(writes).toHaveLength(expectedWrites);
      if (expectedWrites > 0) {
        expect(writes[0][1].subagents.max_subagent_depth).toBe(4);
        // The field shows what was saved rather than staying blank.
        expect(
          document.querySelector('input[aria-label="Max sub-agent depth"]')
            .value,
        ).toBe('4');
      }
      expect(coordinator.hasPending()).toBe(false);

      await expect(coordinator.flushPending()).resolves.toBe(true);
      expect(getSettingsUpdateCalls()).toHaveLength(expectedWrites);
    },
  );

  it('returns the manual save control to Saved after an edit is persisted', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();
    const depthInput = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    const saveControl = depthInput
      .closest('.settings-editor')
      .querySelector('.save-button');
    expect(saveControl.textContent.trim()).toBe('Saved');

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');
    flushSync();
    expect(saveControl.textContent.trim()).toBe('Save');

    saveControl.click();
    flushSync();

    await waitForCondition(() => saveControl.textContent.trim() === 'Saved');
    expect(getSettingsUpdateCalls()).toHaveLength(1);
  });

  it('reports a successful no-op when manual save is clicked with no changes', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openSubAgentsPanel();

    // A clean draft already reads as saved; clicking still confirms it.
    getButton('Saved').click();
    flushSync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' }),
    );
    expect(getSettingsUpdateCalls()).toHaveLength(0);
  });

  it('shows server-wide Voice settings but hides Desktop-only connection settings', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForCondition(() => buttonByText('General'));

    expect(
      document.querySelector('[data-settings-section="desktop_connection"]'),
    ).toBeNull();
    expect(
      document.querySelector('[data-settings-section="live_voice_shortcut"]'),
    ).toBeNull();
    expect(buttonByText('Voice')).toBeTruthy();
    expect(
      document.querySelector('button[aria-label="Transcription audio"]'),
    ).toBeTruthy();
    expect(
      document.querySelector(
        '[role="switch"][aria-label="Enable wakeword listening"]',
      ),
    ).toBeNull();
  });

  it('adds the Live voice shortcut to Voice when the Desktop supports it', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    window.history.pushState({}, '', '/?accessor=desktop');
    window.pywebview = {
      api: {
        getLiveHotkey: vi.fn().mockResolvedValue({
          supported: true,
          enabled: false,
          hotkey: {
            ctrl: true,
            alt: true,
            shift: false,
            win: false,
            key: 'Space',
          },
          error_code: null,
        }),
      },
    };

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { desktopCapabilities: { liveHotkey: true } },
    });
    flushSync();

    await waitForCondition(
      () =>
        document
          .querySelector('.live-shortcut__capture')
          ?.textContent.trim() === 'Ctrl + Alt + Space',
    );
    const page = document.querySelector('[data-settings-page="voice"]');
    expect(
      Array.from(
        page.querySelectorAll('[data-settings-section]'),
        (section) => section.dataset.settingsSection,
      ),
    ).toEqual([
      'speech_models',
      'live_voice_model',
      'live_voice_shortcut',
      'voice_controls',
    ]);
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
        listMicrophones: vi.fn().mockResolvedValue([]),
        listWakewordModels: vi.fn().mockResolvedValue([]),
      },
    };

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        agents: agentsPayload(),
        desktopCapabilities: {
          wakeword: true,
          voiceApi: 2,
          serverSelection: true,
        },
        desktopVoice: {
          available: true,
          status: {
            enabled: false,
            mode: 'real',
            state: 'off',
            error_code: null,
            sequence: 1,
            microphone: null,
            active_microphone: null,
            echo_cancellation: { enabled: true, state: 'off' },
            default_agent_id: null,
            default_session_behavior: 'active',
            phrases: [],
            recording: null,
            commands: [],
            calibration: null,
            limits: {
              max_active_phrases: 8,
              min_sensitivity: 0.05,
              max_sensitivity: 0.95,
            },
          },
          adopt: vi.fn(),
          refresh: vi.fn(),
        },
        targetPanelId: 'voice',
        targetPanelRequestId: 1,
      },
    });
    flushSync();

    // The Voice section renders (desktop capability) and the target request
    // marks its index entry active.
    await waitForCondition(
      () =>
        document.querySelector(
          '[role="switch"][aria-label="Enable wakeword listening"]',
        ) !== null,
    );
    expect(
      document.querySelector('[data-settings-section="desktop_connection"]'),
    ).toBeTruthy();
    await waitForCondition(
      () =>
        buttonByText('Voice')?.classList.contains('snav-item--active') === true,
    );

    // Navigating elsewhere moves the index highlight; the Voice section stays
    // in the document (sections are never unmounted).
    buttonByText('System').click();
    flushSync();

    await waitForCondition(
      () =>
        buttonByText('System')?.classList.contains('snav-item--active') ===
        true,
    );
    expect(buttonByText('Voice')?.classList.contains('snav-item--active')).toBe(
      false,
    );
    expect(
      document.querySelector(
        '[role="switch"][aria-label="Enable wakeword listening"]',
      ),
    ).toBeTruthy();
    expect(document.querySelector('.desktop-connection-settings')).toBeTruthy();
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
