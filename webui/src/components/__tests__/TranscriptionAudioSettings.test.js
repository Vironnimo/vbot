// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  updateSettings: vi.fn(),
}));

const { updateSettings } = await import('$lib/api.js');
const { default: TranscriptionAudioSettings } =
  await import('../voice/TranscriptionAudioSettings.svelte');

describe('TranscriptionAudioSettings', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    vi.clearAllMocks();
    updateSettings.mockImplementation(async (payload) => payload);
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) await unmount(mountedComponent);
    document.body.innerHTML = '';
  });

  it('saves one server-wide profile and offers its values only for Custom', async () => {
    const onCommit = vi.fn();
    mountedComponent = mount(TranscriptionAudioSettings, {
      target: document.body,
      props: {
        settings: {
          speech: {
            transcription_audio: {
              profile: 'compatibility',
              format: 'wav',
              sample_rate_hz: 16000,
            },
          },
        },
        onCommit,
        onError: vi.fn(),
      },
    });
    flushSync();

    // A preset fixes the format and sample rate.
    expect(rowHidden(t('settings.voice.transcriptionFormat'))).toBe(true);
    expect(rowHidden(t('settings.voice.transcriptionSampleRate'))).toBe(true);

    buttonByLabel(t('settings.voice.transcriptionProfile')).click();
    flushSync();
    option(t('settings.voice.transcriptionProfileCustom')).click();
    await settle();

    expect(updateSettings).toHaveBeenLastCalledWith({
      speech: {
        transcription_audio: {
          profile: 'custom',
          format: 'wav',
          sample_rate_hz: 16000,
        },
      },
    });
    expect(onCommit).toHaveBeenCalledOnce();
    expect(rowHidden(t('settings.voice.transcriptionFormat'))).toBe(false);
    expect(rowHidden(t('settings.voice.transcriptionSampleRate'))).toBe(false);
  });
});

function rowHidden(label) {
  return buttonByLabel(label).closest('.s-row').hidden;
}

function buttonByLabel(label) {
  return document.body.querySelector(`button[aria-label="${label}"]`);
}

function option(label) {
  return [...document.body.querySelectorAll('[role="option"]')].find(
    (element) =>
      element
        .querySelector('.dropdown-primitive__option-label')
        ?.textContent.trim() === label,
  );
}

async function settle() {
  await tick();
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}
