// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  transcribeSpeech,
  uploadAttachment,
  createAudioRecorder,
  ChatComposer,
  composerInput,
  selectFileFromPicker,
  submitComposer,
  flushComposerAsyncWork,
  setupChatComposerSuite,
} from './ChatComposer.support.js';

describe('ChatComposer', () => {
  const suite = setupChatComposerSuite();

  it('focuses the message textarea when clicking the composer padding', () => {
    suite.mountedComponent = mount(ChatComposer, { target: document.body });
    flushSync();

    const wrap = document.body.querySelector('.input-wrap');
    expect(wrap).toBeTruthy();

    const event = new MouseEvent('mousedown', {
      bubbles: true,
      cancelable: true,
    });
    wrap.dispatchEvent(event);
    flushSync();

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(composerInput());
  });

  it('resets the textarea height after sending a tall draft', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    const input = composerInput();
    Object.defineProperty(input, 'scrollHeight', {
      configurable: true,
      get: () => 144,
    });

    input.value = 'line one\nline two\nline three';
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(input.style.height).toBe('144px');

    submitComposer();
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith(
      'line one\nline two\nline three',
    );
    expect(input.value).toBe('');
    expect(input.style.height).toBe('');
  });

  it('marks submitted transcribed text with speech input origin', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const recorder = {
      start: vi.fn(),
      stop: vi
        .fn()
        .mockResolvedValue(new Blob(['audio'], { type: 'audio/webm' })),
      filename: () => 'recording.webm',
      cancel: vi.fn(),
    };
    createAudioRecorder.mockResolvedValue(recorder);
    transcribeSpeech.mockResolvedValue({ text: 'hello world' });

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    const microphoneButton = document.body.querySelector(
      'button[aria-label="Start voice input"]',
    );
    microphoneButton.click();
    await flushComposerAsyncWork();

    document.body.querySelector('button[aria-label="Stop recording"]').click();
    await flushComposerAsyncWork();
    await flushComposerAsyncWork();
    await flushComposerAsyncWork();

    expect(transcribeSpeech).toHaveBeenCalledWith(expect.any(Blob), {
      filename: 'recording.webm',
      onProgress: expect.any(Function),
    });
    expect(composerInput().value).toBe('hello world');

    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith('hello world', {
      inputOrigin: 'speech_transcription',
    });
  });

  it.each([false, true])(
    'shows speech phases and unlocks the microphone after completion or failure (%s)',
    async (fail) => {
      const recorder = {
        start: vi.fn(),
        stop: vi.fn().mockResolvedValue(new Blob(['audio'])),
        cancel: vi.fn(),
      };
      createAudioRecorder.mockResolvedValue(recorder);
      let complete, reject;
      transcribeSpeech.mockImplementation(
        () =>
          new Promise((resolve, rejectPromise) => {
            complete = resolve;
            reject = rejectPromise;
          }),
      );
      suite.mountedComponent = mount(ChatComposer, { target: document.body });
      flushSync();
      document.querySelector('button[aria-label="Start voice input"]').click();
      await flushComposerAsyncWork();
      document.querySelector('button[aria-label="Stop recording"]').click();
      await flushComposerAsyncWork();
      const onProgress = transcribeSpeech.mock.calls[0][1].onProgress;
      for (const [phase, label] of [
        [
          'downloading',
          'Downloading speech model. The first download can take several minutes…',
        ],
        ['loading', 'Loading speech model into memory…'],
        ['transcribing', 'Transcribing recording…'],
      ]) {
        onProgress({ phase, elapsed_seconds: 12 });
        flushSync();
        const microphone = document.querySelector(
          `button[aria-label="${label}"]`,
        );
        expect(microphone.disabled).toBe(true);
        expect(microphone.getAttribute('aria-busy')).toBe('true');
        expect(
          microphone.parentElement.classList.contains('tooltip-anchor'),
        ).toBe(true);
        expect(
          document.querySelector('.composer-voice-status[role="status"]'),
        ).toBeTruthy();
      }
      if (fail) reject(new Error('test-owned failure'));
      else complete({ text: 'test-owned transcript' });
      for (let index = 0; index < 10; index += 1) {
        await flushComposerAsyncWork();
        if (document.querySelector('button[aria-label="Start voice input"]'))
          break;
      }
      expect(
        document.querySelector('button[aria-label="Start voice input"]')
          .disabled,
      ).toBe(false);
      expect(document.querySelector('.composer-voice-status')).toBeNull();
      if (!fail) expect(composerInput().value).toBe('test-owned transcript');
      else expect(recorder.cancel).toHaveBeenCalled();
    },
  );

  it('cancels the recorder when browser recording start fails', async () => {
    const recorder = {
      start: vi.fn(() => {
        throw new Error('microphone start failed');
      }),
      cancel: vi.fn(),
    };
    createAudioRecorder.mockResolvedValue(recorder);
    suite.mountedComponent = mount(ChatComposer, { target: document.body });
    flushSync();

    document.body
      .querySelector('button[aria-label="Start voice input"]')
      .click();
    await flushComposerAsyncWork();

    expect(recorder.cancel).toHaveBeenCalledOnce();
    expect(
      document.body.querySelector('button[aria-label="Start voice input"]'),
    ).toBeTruthy();
  });

  it('releases a recorder whose microphone permission resolves after unmount', async () => {
    let resolveRecorder;
    const recorder = {
      start: vi.fn(),
      cancel: vi.fn(),
    };
    createAudioRecorder.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRecorder = resolve;
        }),
    );
    suite.mountedComponent = mount(ChatComposer, { target: document.body });
    flushSync();

    document.body
      .querySelector('button[aria-label="Start voice input"]')
      .click();
    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;
    resolveRecorder(recorder);
    await flushComposerAsyncWork();

    expect(recorder.cancel).toHaveBeenCalledOnce();
    expect(recorder.start).not.toHaveBeenCalled();
  });

  it('cancels the recorder when browser recording stop fails', async () => {
    const recorder = {
      start: vi.fn(),
      stop: vi.fn(() => {
        throw new Error('microphone stop failed');
      }),
      filename: () => 'recording.webm',
      cancel: vi.fn(),
    };
    createAudioRecorder.mockResolvedValue(recorder);
    suite.mountedComponent = mount(ChatComposer, { target: document.body });
    flushSync();

    document.body
      .querySelector('button[aria-label="Start voice input"]')
      .click();
    await flushComposerAsyncWork();
    document.body.querySelector('button[aria-label="Stop recording"]').click();
    await flushComposerAsyncWork();

    expect(recorder.cancel).toHaveBeenCalledOnce();
    expect(
      document.body.querySelector('button[aria-label="Start voice input"]'),
    ).toBeTruthy();
  });

  it('sends uploaded text files as a file reference', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-text-1',
      filename: 'note.txt',
      media_type: 'text/plain',
      size_bytes: 5,
    });

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    await selectFileFromPicker(
      new File(['hello'], 'note.txt', { type: 'text/plain' }),
    );
    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'file',
        attachment_id: 'attachment-text-1',
        filename: 'note.txt',
        media_type: 'text/plain',
      },
    ]);
  });

  it('sends an uploaded empty text file as a file reference only', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-text-empty-1',
      filename: 'empty.txt',
      media_type: 'text/plain',
      size_bytes: 0,
    });

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    await selectFileFromPicker(
      new File([''], 'empty.txt', { type: 'text/plain' }),
    );
    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'file',
        attachment_id: 'attachment-text-empty-1',
        filename: 'empty.txt',
        media_type: 'text/plain',
      },
    ]);
  });

  it('sends uploaded images as media blocks', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-image-1',
      filename: 'photo.png',
      media_type: 'image/png',
      size_bytes: 7,
    });

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    await selectFileFromPicker(
      new File(['pngdata'], 'photo.png', { type: 'image/png' }),
    );
    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'media',
        attachment_id: 'attachment-image-1',
        filename: 'photo.png',
        media_type: 'image/png',
      },
    ]);
  });

  it.each([
    ['voice.ogg', 'audio/ogg'],
    ['clip.mp4', 'video/mp4'],
  ])('sends uploaded %s as media block', async (filename, mediaType) => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-av-1',
      filename,
      media_type: mediaType,
      size_bytes: 9,
    });

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    await selectFileFromPicker(
      new File(['av-data'], filename, { type: mediaType }),
    );
    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'media',
        attachment_id: 'attachment-av-1',
        filename,
        media_type: mediaType,
      },
    ]);
  });

  it('sends non-image binary uploads as file blocks', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-file-1',
      filename: 'paper.pdf',
      media_type: 'application/pdf',
      size_bytes: 11,
    });

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    await selectFileFromPicker(
      new File(['pdf-content'], 'paper.pdf', { type: 'application/pdf' }),
    );
    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'file',
        attachment_id: 'attachment-file-1',
        filename: 'paper.pdf',
        media_type: 'application/pdf',
      },
    ]);
  });
});
