// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getSettingsUpdateCalls,
  getSimpleTrigger,
  openSimpleDropdown,
  resetSettingsViewHarness,
  rpcMock,
  selectSimpleOption,
  settingsPayload,
  SettingsView,
  waitForCondition,
} from './SettingsView.support.js';
import {
  applyAppearanceSettings,
  appearancePrefs,
} from '../../lib/appearancePrefs.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

describe('SettingsView appearance snapshots', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    applyAppearanceSettings();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
    applyAppearanceSettings();
  });

  it('refreshes the global Chat preferences when Settings loads', async () => {
    const settings = settingsPayload();
    settings.appearance.chat_width = 'wide';
    settings.appearance.chat_working_mode = 'compact';
    rpcMock.mockImplementation(createSettingsRpcMock({ settings }));

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.querySelector('#settings-appearance-chat-width'),
    );

    expect(appearancePrefs.chatWidth).toBe('wide');
    expect(appearancePrefs.chatWorkingMode).toBe('compact');
    expect(getSettingsUpdateCalls()).toHaveLength(0);
  });

  it('applies the saved response while preserving a newer draft after a failed follow-up save', async () => {
    let finishFirstSave;
    const onToast = vi.fn();
    const save = vi
      .fn()
      .mockImplementationOnce(
        (patch, currentSettings) =>
          new Promise((resolve) => {
            finishFirstSave = () =>
              resolve({
                ...currentSettings,
                appearance: {
                  ...currentSettings.appearance,
                  ...patch.appearance,
                },
              });
          }),
      )
      .mockRejectedValueOnce(new Error('save unavailable'));
    rpcMock.mockImplementation(createSettingsRpcMock({ settingsUpdate: save }));
    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast },
    });
    flushSync();
    await waitForCondition(() =>
      document.querySelector('#settings-appearance-chat-width'),
    );
    vi.useFakeTimers();

    openSimpleDropdown('settings-appearance-chat-width');
    selectSimpleOption('settings-appearance-chat-width', 'Wide');
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();
    expect(save).toHaveBeenCalledTimes(1);
    expect(appearancePrefs.chatWidth).toBe('comfortable');

    openSimpleDropdown('settings-appearance-chat-width');
    selectSimpleOption('settings-appearance-chat-width', 'Full width');
    finishFirstSave();
    await flushAsyncUpdates();
    expect(appearancePrefs.chatWidth).toBe('wide');

    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();
    expect(save).toHaveBeenCalledTimes(2);
    expect(getSettingsUpdateCalls()[1][1].appearance.chat_width).toBe('full');
    expect(appearancePrefs.chatWidth).toBe('wide');
    expect(
      getSimpleTrigger('settings-appearance-chat-width').textContent,
    ).toContain('Full');
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' }),
    );
  });
});
