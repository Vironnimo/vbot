// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { t } from '../../lib/i18n.js';
import { notifyServerReady } from '../../lib/connectionState.js';

import {
  buttonLabelled,
  chooseSuggestion,
  composerInput,
  createAudioRecorder,
  deferred,
  modelCatalogFixture,
  prepareSpeechTranscription,
  pressKey,
  settle,
  setupChatComposerSuite,
  submitComposer,
  transcribeSpeech,
  typeInComposer,
} from './ChatComposer.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const startButton = () => buttonLabelled('chat.voice.startRecording');
const stopButton = () => buttonLabelled('chat.voice.stopRecording');

function recorderFixture(overrides = {}) {
  return {
    start: vi.fn(),
    stop: vi
      .fn()
      .mockResolvedValue(new Blob(['audio'], { type: 'audio/webm' })),
    filename: () => 'recording.webm',
    cancel: vi.fn(),
    ...overrides,
  };
}

// Holds the transcription so a test can report progress and then complete or
// fail it.
function holdTranscription() {
  let complete;
  let fail;
  transcribeSpeech.mockImplementation(
    () =>
      new Promise((resolve, reject) => {
        complete = resolve;
        fail = reject;
      }),
  );
  return {
    complete: (value) => complete(value),
    fail: (error) => fail(error),
  };
}

async function record() {
  startButton().click();
  await settle();
  stopButton().click();
  await settle();
}

describe('ChatComposer speech input', () => {
  const composer = setupChatComposerSuite();

  it('keeps the draft unsent until recording and transcription finish, then sends the speech origin', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const permission = deferred();
    const recorder = recorderFixture();
    createAudioRecorder.mockReturnValue(permission.promise);
    const transcription = holdTranscription();
    composer.mount({ onSendMessage });
    typeInComposer('Typed introduction');

    const expectSubmissionBlocked = async () => {
      const send = buttonLabelled('chat.sendMessage');
      expect(send.disabled).toBe(true);
      send.click();
      pressKey('Enter');
      submitComposer();
      await settle();
      expect(onSendMessage).not.toHaveBeenCalled();
      expect(recorder.cancel).not.toHaveBeenCalled();
      expect(composerInput().value).toBe('Typed introduction');
    };

    startButton().click();
    await settle();
    await expectSubmissionBlocked();
    permission.resolve(recorder);
    await settle();
    // The server may load its speech model while the user speaks.
    expect(prepareSpeechTranscription).toHaveBeenCalledOnce();
    await expectSubmissionBlocked();
    expect(stopButton().disabled).toBe(false);
    stopButton().click();
    await settle(3);
    await expectSubmissionBlocked();

    expect(transcribeSpeech).toHaveBeenCalledWith(expect.any(Blob), {
      filename: 'recording.webm',
      transfer: expect.any(Object),
      signal: expect.any(AbortSignal),
      onProgress: expect.any(Function),
    });
    transcription.complete({ text: 'hello world' });
    await settle();
    expect(composerInput().value).toBe('Typed introduction\nhello world');
    await vi.waitFor(() =>
      expect(composerInput().placeholder).toBe(t('chat.composerPlaceholder')),
    );
    expect(buttonLabelled('chat.sendMessage').disabled).toBe(false);
    submitComposer();
    expect(onSendMessage).toHaveBeenCalledWith(
      'Typed introduction\nhello world',
      { inputOrigin: 'speech_transcription' },
    );
  });

  it.each([
    ['command', 'skill', '/stat'],
    ['model', 'model', '/model '],
  ])(
    'does not submit an immediate %s selection while recording',
    async (_case, kind, text) => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const recorder = recorderFixture();
      createAudioRecorder.mockResolvedValue(recorder);
      composer.mount({
        onSendMessage,
        availableSkills: [
          { name: 'status', type: 'command', argument: 'none' },
        ],
        onLoadModelCatalog: vi.fn().mockResolvedValue(modelCatalogFixture()),
      });
      startButton().click();
      await settle();
      typeInComposer(text);
      await settle(3);

      await chooseSuggestion(kind);

      expect(onSendMessage).not.toHaveBeenCalled();
      expect(recorder.cancel).not.toHaveBeenCalled();
      expect(composerInput().value).toBe(text);
      expect(stopButton().disabled).toBe(false);
    },
  );

  it.each([
    ['completion', false],
    ['failure', true],
  ])(
    'shows the transcription phases and retains the recording only after failure (%s)',
    async (_case, fails) => {
      const recorder = recorderFixture();
      createAudioRecorder.mockResolvedValue(recorder);
      const transcription = holdTranscription();
      composer.mount();
      await record();

      const { onProgress } = transcribeSpeech.mock.calls[0][1];
      for (const phase of ['queued', 'loading', 'transcribing']) {
        onProgress({ phase, elapsed_seconds: 12 });
        await settle();
        const microphone = buttonLabelled(`chat.voice.progress.${phase}`);
        expect(microphone.disabled).toBe(true);
        expect(microphone.getAttribute('aria-busy')).toBe('true');
        expect(
          microphone.parentElement.classList.contains('tooltip-anchor'),
        ).toBe(true);
        expect(composerInput().placeholder).toBe(
          t(`chat.voice.progress.${phase}`),
        );
        expect(
          document.querySelector('.composer-voice-status[role="status"]')
            .textContent,
        ).toContain(t(`chat.voice.progress.${phase}`));
      }

      if (fails) transcription.fail(new Error('test-owned failure'));
      else transcription.complete({ text: 'test-owned transcript' });
      await vi.waitFor(() => expect(startButton()).not.toBeNull());
      expect(startButton().disabled).toBe(fails);
      expect(composerInput().placeholder).toBe(t('chat.composerPlaceholder'));
      expect(
        document.querySelector('.composer-voice-status').textContent.trim(),
      ).toBe('');
      expect(composerInput().value).toBe(fails ? '' : 'test-owned transcript');
      expect(recorder.stop).toHaveBeenCalledOnce();
    },
  );

  it.each(['resume', 'discard', 'return to draft'])(
    'retains the recorded take after a disconnect until %s',
    async (action) => {
      const recorder = recorderFixture();
      createAudioRecorder.mockResolvedValue(recorder);
      transcribeSpeech
        .mockRejectedValueOnce(
          Object.assign(new Error('offline'), { code: 'network_error' }),
        )
        .mockResolvedValue({ text: 'recovered transcript' });
      composer.mount({ draftKey: 'a::s' });
      await record();
      await settle(4);
      const card = [...document.querySelectorAll('[role=status]')].find(
        (element) =>
          element.textContent.includes(t('chat.voice.recordingRetained')),
      );
      expect(card).toBeTruthy();
      if (action === 'discard')
        [...card.querySelectorAll('button')]
          .find((button) => button.textContent.trim() === t('common.discard'))
          .click();
      if (action === 'return to draft') {
        await composer.unmount();
        composer.mount({ draftKey: 'a::other' });
        notifyServerReady();
        await settle();
        expect(transcribeSpeech).toHaveBeenCalledOnce();
        expect(composerInput().value).toBe('');
        await composer.unmount();
        composer.mount({ draftKey: 'a::s' });
      }
      notifyServerReady();
      await settle(4);
      expect(createAudioRecorder).toHaveBeenCalledOnce();
      expect(transcribeSpeech).toHaveBeenCalledTimes(
        action === 'discard' ? 1 : 2,
      );
      if (action !== 'discard') {
        expect(transcribeSpeech.mock.calls[0][0]).toBe(
          transcribeSpeech.mock.calls[1][0],
        );
        expect(transcribeSpeech.mock.calls[0][1].transfer).toBe(
          transcribeSpeech.mock.calls[1][1].transfer,
        );
      }
      expect(composerInput().value).toBe(
        action === 'discard' ? '' : 'recovered transcript',
      );
    },
  );

  it.each([
    [
      'starting',
      {
        start: vi.fn(() => {
          throw new Error('microphone start failed');
        }),
      },
      false,
    ],
    [
      'stopping',
      {
        stop: vi.fn(() => {
          throw new Error('microphone stop failed');
        }),
      },
      true,
    ],
  ])(
    'releases the recorder when browser recording fails while %s',
    async (_case, failure, stops) => {
      const recorder = recorderFixture(failure);
      createAudioRecorder.mockResolvedValue(recorder);
      composer.mount();

      startButton().click();
      await settle();
      if (stops) {
        stopButton().click();
        await settle();
      }

      expect(recorder.cancel).toHaveBeenCalledOnce();
      expect(startButton()).not.toBeNull();
      expect(transcribeSpeech).not.toHaveBeenCalled();
    },
  );

  it.each([
    ['completes', false],
    ['fails', true],
  ])(
    'cancels speech on a Session change and ignores a stale result that %s',
    async (_case, fails) => {
      const props = reactiveProps({
        draftKey: 'agent::first',
        onTranscriptionError: vi.fn(),
      });
      createAudioRecorder.mockResolvedValue(recorderFixture());
      const transcription = holdTranscription();
      composer.mount(props);
      await record();
      const { signal, onProgress } = transcribeSpeech.mock.calls[0][1];

      props.draftKey = 'agent::second';
      await settle();
      expect(signal.aborted).toBe(true);
      startButton().click();
      await settle();
      onProgress({ phase: 'loading', elapsed_seconds: 10 });
      if (fails) {
        transcription.fail(new Error('previous Session transcription failed'));
      } else {
        transcription.complete({ text: 'previous Session transcript' });
      }
      await settle();

      expect(composerInput().value).toBe('');
      expect(props.onTranscriptionError).not.toHaveBeenCalled();
      // The new recording keeps going.
      expect(stopButton()).not.toBeNull();
      expect(buttonLabelled('chat.voice.progress.loading')).toBeNull();
    },
  );

  it('does not upload audio after unmount while the recorder is stopping', async () => {
    const stopped = deferred();
    const recorder = recorderFixture({ stop: vi.fn(() => stopped.promise) });
    createAudioRecorder.mockResolvedValue(recorder);
    composer.mount();
    await record();

    await composer.unmount();
    stopped.resolve(new Blob(['audio']));
    await settle();

    expect(recorder.cancel).toHaveBeenCalledOnce();
    expect(transcribeSpeech).not.toHaveBeenCalled();
  });

  it('releases a recorder whose microphone permission resolves after unmount', async () => {
    const permission = deferred();
    const recorder = recorderFixture();
    createAudioRecorder.mockImplementation(() => permission.promise);
    composer.mount();

    startButton().click();
    await composer.unmount();
    permission.resolve(recorder);
    await settle();

    expect(recorder.cancel).toHaveBeenCalledOnce();
    expect(recorder.start).not.toHaveBeenCalled();
  });
});
