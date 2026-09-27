// @vitest-environment jsdom

// Local speech engines inside the Specialized Models panel: releasing loaded
// models from memory and the install/restart setup flow.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';

import {
  api,
  button,
  cleanupSpecializedModelsHarness,
  deferred,
  mountPanel,
  resetSpecializedModelsHarness,
  selectTarget,
  settle,
  targetsFor,
  unmountPanel,
  waitForCondition,
} from './SettingsSpecializedModelsPanel.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const UNLOAD = 'Unload from memory';

describe('SettingsSpecializedModelsPanel local speech', () => {
  beforeEach(() => {
    resetSpecializedModelsHarness();
  });

  afterEach(async () => {
    await cleanupSpecializedModelsHarness();
  });

  describe('speech memory', () => {
    it('unloads a specific model and ignores an older in-flight status poll', async () => {
      vi.useFakeTimers();
      const loaded = {
        target: 'local/chatterbox',
        label: 'Test loaded voice',
        loaded: true,
        busy: false,
      };
      const stale = deferred();
      api.getLocalSpeechMemory
        .mockResolvedValueOnce({ models: [loaded] })
        .mockReturnValueOnce(stale.promise);
      mountPanel({});
      await settle();
      const panel = () => document.querySelector('[data-local-speech-memory]');
      expect(panel().textContent).toContain(loaded.label);
      expect(button(UNLOAD).disabled).toBe(false);

      await vi.advanceTimersByTimeAsync(2000);
      button(UNLOAD).click();
      await settle();
      expect(api.unloadLocalSpeech).toHaveBeenCalledOnce();
      expect(panel()).toBeNull();
      stale.resolve({ models: [loaded] });
      await settle();
      expect(panel()).toBeNull();

      // Polling stops with the panel.
      await unmountPanel();
      const calls = api.getLocalSpeechMemory.mock.calls.length;
      await vi.advanceTimersByTimeAsync(6000);
      expect(api.getLocalSpeechMemory).toHaveBeenCalledTimes(calls);
    });

    it.each([
      { label: 'Busy model', loaded: true, busy: true },
      { label: 'Empty model', loaded: false, busy: false },
    ])(
      'disables unload for busy or empty speech memory: %j',
      async (status) => {
        api.getLocalSpeechMemory.mockResolvedValue({
          models: [{ target: 'local/qwen3-asr', ...status }],
        });
        mountPanel({
          settings: {
            model_tasks: {
              speech_to_text: { target: 'local/qwen3-asr', options: {} },
            },
          },
        });
        await waitForCondition(() => button(UNLOAD));
        expect(button(UNLOAD).disabled).toBe(true);
        button(UNLOAD).click();
        expect(api.unloadLocalSpeech).not.toHaveBeenCalled();
      },
    );

    it('recovers from an unload failure and respects a busy response to a race', async () => {
      const loaded = {
        target: 'local/qwen3-tts',
        label: 'Voice',
        loaded: true,
        busy: false,
      };
      api.getLocalSpeechMemory.mockResolvedValue({ models: [loaded] });
      api.unloadLocalSpeech
        .mockRejectedValueOnce(new Error('network'))
        .mockResolvedValueOnce({
          models: [{ ...loaded, busy: true }],
          released: false,
        });
      mountPanel({});
      await waitForCondition(() => button(UNLOAD));
      button(UNLOAD).click();
      await waitForCondition(() => document.querySelector('[role="alert"]'));
      expect(button(UNLOAD).disabled).toBe(false);

      button(UNLOAD).click();
      await waitForCondition(
        () =>
          button(UNLOAD).disabled && !document.querySelector('[role="alert"]'),
      );
      expect(api.unloadLocalSpeech).toHaveBeenCalledTimes(2);
    });

    it('unloads STT while TTS is busy and keeps the TTS row intact', async () => {
      const stt = {
        target: 'local/qwen3-asr',
        label: 'STT',
        loaded: true,
        busy: false,
      };
      const tts = {
        target: 'local/qwen3-tts',
        label: 'TTS',
        loaded: true,
        busy: true,
      };
      api.getLocalSpeechMemory.mockResolvedValue({ models: [stt, tts] });
      api.unloadLocalSpeech.mockResolvedValue({
        models: [{ ...stt, loaded: false }, tts],
        released: true,
      });
      mountPanel({});
      const row = (target) =>
        document.querySelector(`[data-speech-memory-target="${target}"]`);
      await waitForCondition(() => row(stt.target));
      expect(row(tts.target).querySelector('button').disabled).toBe(true);
      expect(row(stt.target).querySelector('button').disabled).toBe(false);

      row(stt.target).querySelector('button').click();
      await waitForCondition(() => !row(stt.target));
      expect(api.unloadLocalSpeech).toHaveBeenCalledWith(stt.target);
      expect(row(tts.target).textContent).toContain(tts.label);
      expect(row(tts.target).querySelector('button').disabled).toBe(true);
    });
  });

  describe('setup', () => {
    async function mountLocalPanel() {
      targetsFor('speech_to_text', [
        {
          id: 'local/qwen3-asr',
          label: 'Qwen3 ASR',
          kind: 'local',
          usable: false,
        },
      ]);
      mountPanel({
        settings: {
          model_tasks: {
            speech_to_text: { target: 'local/qwen3-asr', options: {} },
          },
        },
      });
      await waitForCondition(() => document.querySelector('[role="status"]'));
    }

    function setupState(state, restartAvailable = true, extra = {}) {
      api.getLocalSpeechSetup.mockResolvedValue({
        state,
        restart_available: restartAvailable,
        ...extra,
      });
    }

    it('offers installation for local engines while preserving their separate options', async () => {
      setupState('missing');
      targetsFor('speech_to_text', [
        {
          id: 'local/qwen3-asr',
          label: 'Qwen3 ASR',
          kind: 'local',
          usable: false,
        },
        {
          id: 'local/parakeet',
          label: 'Parakeet TDT v3',
          kind: 'local',
          usable: true,
        },
      ]);
      api.getTaskModelOptions.mockImplementation((_task, target) =>
        Promise.resolve({
          fields:
            target === 'local/qwen3-asr'
              ? [
                  {
                    name: 'language',
                    label: 'Language',
                    type: 'text',
                    default: 'de',
                  },
                ]
              : [
                  {
                    name: 'device',
                    label: 'Device',
                    type: 'select',
                    default: 'auto',
                    options: [{ value: 'auto', label: 'Automatic' }],
                  },
                ],
        }),
      );
      mountPanel({ settings: {}, modelsRefreshToken: 0 });
      await waitForCondition(
        () =>
          !document.getElementById('settings-specialized-speech_to_text')
            ?.disabled,
      );

      selectTarget('speech_to_text', 'Qwen3 ASR');
      await waitForCondition(() => button('Install'));
      expect(document.body.textContent).not.toContain('.[local-speech]');
      await waitForCondition(() =>
        document.querySelector('#task-model-speech_to_text-language'),
      );
      selectTarget('speech_to_text', 'Parakeet TDT v3');
      await waitForCondition(() =>
        document.querySelector('#task-model-speech_to_text-device'),
      );
      expect(
        document.querySelector('#task-model-speech_to_text-language'),
      ).toBeNull();
      expect(button('Install')).toBeTruthy();

      const trigger = document.getElementById(
        'settings-specialized-speech_to_text',
      );
      expect(trigger.textContent).toContain('Parakeet TDT v3 (local)');
      trigger.click();
      flushSync();
      const search = document.querySelector(
        '.searchable-dropdown__search input',
      );
      search.value = 'local';
      search.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      const matches = [
        ...document.querySelectorAll('.searchable-dropdown__option'),
      ];
      expect(matches).toHaveLength(2);
      expect(
        matches.every((option) => option.textContent.includes('(local)')),
      ).toBe(true);
    });

    it('keeps setup running across navigation and enables one explicit restart after verification', async () => {
      setupState('missing');
      await mountLocalPanel();
      await waitForCondition(() => button('Install'));
      button('Install').click();
      button('Install')?.click();
      await waitForCondition(() => button('Installing…'));
      expect(api.installLocalSpeechSupport).toHaveBeenCalledTimes(1);
      expect(button('Installing…').disabled).toBe(true);
      expect(button('Restart server')).toBeUndefined();

      // Navigating away and back remounts the panel on the running setup.
      await unmountPanel();
      setupState('restart_required');
      await mountLocalPanel();
      await waitForCondition(
        () => button('Restart server') && !button('Restart server').disabled,
      );
      expect(api.installLocalSpeechSupport).toHaveBeenCalledTimes(1);
      button('Restart server').click();
      await waitForCondition(() => button('Restarting…'));
      expect(api.restartAfterLocalSpeechSetup).toHaveBeenCalledTimes(1);

      // Real status polling after the restart.
      setupState('ready');
      await waitForCondition(() => !button('Restarting…'), 30, 100);
      expect(api.restartAfterLocalSpeechSetup).toHaveBeenCalledTimes(1);
      expect(button('Install')).toBeUndefined();
    });

    it('offers retry after a failed installation and respects unsupported restarts', async () => {
      setupState('failed', false, { error: 'install_failed' });
      await mountLocalPanel();
      await waitForCondition(() => button('Try again'));
      api.installLocalSpeechSupport.mockResolvedValue({
        state: 'restart_required',
        restart_available: false,
      });

      button('Try again').click();
      await waitForCondition(() => button('Restart server'));
      expect(button('Restart server').disabled).toBe(true);
      expect(api.restartAfterLocalSpeechSetup).not.toHaveBeenCalled();
      expect(api.installLocalSpeechSupport).toHaveBeenCalledTimes(1);
    });

    it('checks status after a lost restart response without replaying the restart', async () => {
      setupState('restart_required');
      await mountLocalPanel();
      await waitForCondition(
        () => button('Restart server') && !button('Restart server').disabled,
      );
      api.restartAfterLocalSpeechSetup.mockRejectedValue(
        new Error('disconnected'),
      );

      button('Restart server').click();
      await waitForCondition(() => button('Restarting…'));
      // Real status polling after the restart.
      setupState('ready');
      await waitForCondition(() => !button('Restarting…'), 30, 100);
      expect(api.restartAfterLocalSpeechSetup).toHaveBeenCalledTimes(1);
      expect(button('Check again')).toBeUndefined();
    });
  });
});
