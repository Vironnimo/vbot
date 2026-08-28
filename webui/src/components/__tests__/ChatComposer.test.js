// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  transcribeSpeech: vi.fn(),
  uploadAttachment: vi.fn(),
}));

vi.mock('$lib/audioRecorder.js', () => ({
  createAudioRecorder: vi.fn(),
}));

const { transcribeSpeech, uploadAttachment } = await import('$lib/api.js');
const { createAudioRecorder } = await import('$lib/audioRecorder.js');

const { default: ChatComposer } = await import('../ChatComposer.svelte');
const { getDraft, getHistory, pushHistory, resetComposerMemory, setDraft } =
  await import('../../lib/composerMemory.js');

describe('ChatComposer', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    transcribeSpeech.mockReset();
    uploadAttachment.mockReset();
    createAudioRecorder.mockReset();
    localStorage.clear();
    resetComposerMemory();
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('offers slash skill autocomplete at the start of the message', async () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = '/deb';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('debugging');
    expect(document.body.textContent).toContain('Investigate unclear bugs.');

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('/debugging');
  });

  it('normalizes slash command names when inserting from autocomplete', async () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          {
            name: '/compact',
            description: 'Compact the current session context.',
            type: 'command',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/com';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('/compact');
  });

  it('runs a no-argument command immediately without inserting it', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        onSendMessage,
        availableSkills: [
          {
            name: 'status',
            description: 'Show current session and runtime status.',
            type: 'command',
            argument: 'none',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/stat';
    input.setSelectionRange(5, 5);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith('/status');
    expect(input.value).toBe('');
  });

  it('does not run an immediate command while another submit is in flight', async () => {
    let resolveSend;
    const onSendMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve;
        }),
    );
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        onSendMessage,
        availableSkills: [
          {
            name: 'status',
            description: 'Show current session and runtime status.',
            type: 'command',
            argument: 'none',
          },
        ],
      },
    });
    flushSync();

    typeInComposer(composerInput(), 'first');
    submitComposer();
    expect(onSendMessage).toHaveBeenCalledTimes(1);

    typeInComposer(composerInput(), '/stat');
    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(onSendMessage).toHaveBeenCalledTimes(1);
    expect(composerInput().value).toBe('/stat');

    resolveSend(true);
    await flushComposerAsyncWork();
  });

  it('inserts an argument-bearing command instead of running it', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        onSendMessage,
        availableSkills: [
          {
            name: 'compact',
            description: 'Compact the current session context.',
            type: 'command',
            argument: 'optional',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/com';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('/compact');
    expect(onSendMessage).not.toHaveBeenCalled();
  });

  it('offers only skills for inline dollar autocomplete', async () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          {
            name: 'stop',
            description: 'Cancel the active run.',
            type: 'command',
          },
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            type: 'skill',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = 'Please use $';
    input.setSelectionRange(12, 12);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteNames()).toEqual(['debugging']);
    expect(
      document.body.querySelector('.skill-autocomplete__eyebrow').textContent,
    ).toContain('skills');
  });

  it('inserts inline skill triggers without rewriting the message', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures(), onSendMessage },
    });
    flushSync();

    const input = composerInput();
    input.value = 'Please use $deb here.  ';
    input.setSelectionRange(15, 15);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('Please use $debugging here.  ');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    expect(onSendMessage).toHaveBeenCalledWith('Please use $debugging here.  ');
  });

  it('includes loadable warning skills in autocomplete', async () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          ...skillFixtures(),
          {
            name: 'warning-skill',
            description: 'Loadable with validation warnings.',
            valid: false,
            warnings: ['Skill name differs from directory name.'],
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '$warning';
    input.setSelectionRange(8, 8);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('warning-skill');
    expect(document.body.textContent).toContain(
      'Loadable with validation warnings.',
    );

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('$warning-skill');
  });

  it('keeps slash autocomplete keyboard navigation after arrow keyup', () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          ...skillFixtures(),
          {
            name: 'status',
            description: 'Show runtime status.',
            valid: true,
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/';
    input.setSelectionRange(1, 1);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(3);
    expect(autocompleteOptions()[0].getAttribute('aria-selected')).toBe('true');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()[1].getAttribute('aria-selected')).toBe('true');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()[2].getAttribute('aria-selected')).toBe('true');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'ArrowUp', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()[1].getAttribute('aria-selected')).toBe('true');
  });

  it('lets keyboard navigation reach every rendered match (no cap)', () => {
    const manySkills = Array.from({ length: 9 }, (_item, index) => ({
      name: `skill-${index + 1}`,
      description: `Skill number ${index + 1}.`,
      valid: true,
    }));
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: manySkills },
    });
    flushSync();

    const input = composerInput();
    input.value = '/';
    input.setSelectionRange(1, 1);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    // All nine render (the popup is scrollable); arrow-key navigation must be
    // able to reach the last one. A stale count cap stopped the active index at
    // the eighth entry, leaving the ninth unreachable by keyboard.
    expect(autocompleteOptions()).toHaveLength(9);

    for (let step = 0; step < 8; step += 1) {
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
      );
      flushSync();
      input.dispatchEvent(
        new KeyboardEvent('keyup', { key: 'ArrowDown', bubbles: true }),
      );
      flushSync();
    }

    expect(autocompleteOptions()[8].getAttribute('aria-selected')).toBe('true');
  });

  it('keeps popup closed after Escape keyup', () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = '/deb';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(1);

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'Escape', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()).toHaveLength(0);
  });

  it('keeps popup closed after Enter selection keyup (slash)', async () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = '/deb';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(1);

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await Promise.resolve();
    flushSync();

    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    expect(input.value).toBe('/debugging');
    expect(autocompleteOptions()).toHaveLength(0);
  });

  it('keeps popup closed after Enter selection keyup ($skill inline)', async () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = 'use $deb here';
    input.setSelectionRange(8, 8);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(1);

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await Promise.resolve();
    flushSync();

    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    expect(input.value).toContain('$debugging');
    expect(autocompleteOptions()).toHaveLength(0);
  });

  it('focuses the message textarea when clicking the composer padding', () => {
    mountedComponent = mount(ChatComposer, { target: document.body });
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
    mountedComponent = mount(ChatComposer, {
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

    mountedComponent = mount(ChatComposer, {
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
    });
    expect(composerInput().value).toBe('hello world');

    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith('hello world', {
      inputOrigin: 'speech_transcription',
    });
  });

  it('cancels the recorder when browser recording start fails', async () => {
    const recorder = {
      start: vi.fn(() => {
        throw new Error('microphone start failed');
      }),
      cancel: vi.fn(),
    };
    createAudioRecorder.mockResolvedValue(recorder);
    mountedComponent = mount(ChatComposer, { target: document.body });
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
    mountedComponent = mount(ChatComposer, { target: document.body });
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

    mountedComponent = mount(ChatComposer, {
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

    mountedComponent = mount(ChatComposer, {
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

    mountedComponent = mount(ChatComposer, {
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

  it('numbers duplicate image attachments before uploading and sending them', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    uploadAttachment
      .mockResolvedValueOnce({
        attachment_id: 'attachment-image-1',
        filename: 'image1.png',
        media_type: 'image/png',
        size_bytes: 7,
      })
      .mockResolvedValueOnce({
        attachment_id: 'attachment-image-2',
        filename: 'image2.png',
        media_type: 'image/png',
        size_bytes: 7,
      });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage },
    });
    flushSync();

    const first = new File(['first'], 'image.png', { type: 'image/png' });
    const second = new File(['second'], 'image.png', { type: 'image/png' });
    await selectFilesFromPicker([first, second]);

    expect(uploadAttachment).toHaveBeenNthCalledWith(1, first, {
      filename: 'image1.png',
    });
    expect(uploadAttachment).toHaveBeenNthCalledWith(2, second, {
      filename: 'image2.png',
    });
    expect(attachmentNames()).toEqual(['image1.png', 'image2.png']);

    submitComposer();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'media',
        attachment_id: 'attachment-image-1',
        filename: 'image1.png',
        media_type: 'image/png',
      },
      {
        type: 'media',
        attachment_id: 'attachment-image-2',
        filename: 'image2.png',
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

    mountedComponent = mount(ChatComposer, {
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

    mountedComponent = mount(ChatComposer, {
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

  it('restores a saved draft for the session on mount', () => {
    setDraft('agent::one', 'half a thought');

    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();

    expect(composerInput().value).toBe('half a thought');
  });

  it('persists the typed draft into per-session memory', () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();

    typeInComposer(composerInput(), 'work in progress');

    expect(getDraft('agent::one')).toBe('work in progress');
  });

  it('clears the draft and records history when a message is sent', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent', onSendMessage },
    });
    flushSync();

    typeInComposer(composerInput(), 'hello there');
    submitComposer();
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith('hello there');
    expect(composerInput().value).toBe('');
    expect(getDraft('agent::one')).toBe('');
    expect(getHistory('agent')).toEqual(['hello there']);
  });

  it('recalls sent messages with the arrow keys', () => {
    pushHistory('agent', 'first');
    pushHistory('agent', 'second');
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();
    const input = composerInput();

    pressKey(input, 'ArrowUp');
    expect(input.value).toBe('second');

    pressKey(input, 'ArrowUp');
    expect(input.value).toBe('first');

    // Already at the oldest entry — Up holds position instead of clearing.
    pressKey(input, 'ArrowUp');
    expect(input.value).toBe('first');

    pressKey(input, 'ArrowDown');
    expect(input.value).toBe('second');

    // Down past the newest entry returns to the (empty) live draft.
    pressKey(input, 'ArrowDown');
    expect(input.value).toBe('');
  });

  it('preserves an in-progress draft when Up is pressed by accident', () => {
    pushHistory('agent', 'old message');
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();
    const input = composerInput();

    typeInComposer(input, 'my draft');

    pressKey(input, 'ArrowUp');
    expect(input.value).toBe('old message');

    pressKey(input, 'ArrowDown');
    expect(input.value).toBe('my draft');
  });

  it('does not recall history when the caret is below the first line', () => {
    pushHistory('agent', 'old message');
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();
    const input = composerInput();

    // Caret inside the second line: Up should move the caret, not recall.
    typeInComposer(input, 'line one\nline two', 12);

    const event = pressKey(input, 'ArrowUp');

    expect(event.defaultPrevented).toBe(false);
    expect(input.value).toBe('line one\nline two');
  });

  it('shows no stop button while no run is active', () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { isRunning: false },
    });
    flushSync();

    expect(cancelRunButton()).toBeUndefined();
  });

  it('offers the stop button next to Send while a run is active', () => {
    const onCancelRun = vi.fn();
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { isRunning: true, onCancelRun },
    });
    flushSync();

    const stopButton = cancelRunButton();
    expect(stopButton).toBeTruthy();
    expect(stopButton.disabled).toBe(false);

    stopButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(onCancelRun).toHaveBeenCalledTimes(1);
  });

  it('keeps the stop button clickable while the composer itself is disabled', () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { isRunning: true, disabled: true },
    });
    flushSync();

    expect(cancelRunButton().disabled).toBe(false);
  });

  it('disables the stop button while a cancel is in flight', () => {
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { isRunning: true, cancelling: true },
    });
    flushSync();

    const stopButton = Array.from(
      document.body.querySelectorAll('button'),
    ).find((button) => button.getAttribute('aria-label') === 'Cancelling run…');
    expect(stopButton).toBeTruthy();
    expect(stopButton.disabled).toBe(true);
  });

  it('opens the file picker on @ and inserts the chosen path', async () => {
    const onListFiles = vi.fn().mockResolvedValue({
      files: ['docs/guide.md', 'src/session_search.py'],
      truncated: false,
    });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onListFiles },
    });
    flushSync();

    const input = composerInput();
    typeInComposer(input, 'look at @search');
    await flushComposerAsyncWork();

    expect(onListFiles).toHaveBeenCalledTimes(1);
    const options = Array.from(
      document.body.querySelectorAll('.file-autocomplete__option'),
    );
    expect(options.length).toBe(1);
    expect(options[0].textContent).toContain('session_search.py');

    options[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flushComposerAsyncWork();

    expect(input.value).toBe('look at @src/session_search.py ');
  });

  it('does not open the file picker inside an email address', async () => {
    const onListFiles = vi.fn().mockResolvedValue({ files: ['a.txt'] });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onListFiles },
    });
    flushSync();

    typeInComposer(composerInput(), 'mail user@example');
    await flushComposerAsyncWork();

    expect(onListFiles).not.toHaveBeenCalled();
    expect(document.body.querySelector('.file-autocomplete')).toBeNull();
  });

  it('sends verified @-mentions as fileMentions with the message', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const onListFiles = vi.fn().mockResolvedValue({
      files: ['notes.md'],
      truncated: false,
    });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onListFiles },
    });
    flushSync();

    typeInComposer(composerInput(), 'check @notes.md and @nofile.txt');
    submitComposer();
    await flushComposerAsyncWork();
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith(
      'check @notes.md and @nofile.txt',
      { fileMentions: ['notes.md'] },
    );
    await vi.waitFor(() => {
      expect(composerInput().value).toBe('');
    });
  });

  it('serializes mention submits and sends the original snapshot', async () => {
    let resolveFiles;
    let resolveSend;
    const onListFiles = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFiles = resolve;
        }),
    );
    const onSendMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve;
        }),
    );
    setDraft('agent::one', 'first @notes.md');
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        draftKey: 'agent::one',
        historyKey: 'agent',
        onSendMessage,
        onListFiles,
      },
    });
    flushSync();

    submitComposer();
    submitComposer();

    expect(onListFiles).toHaveBeenCalledTimes(1);
    typeInComposer(composerInput(), 'second draft');
    resolveFiles({ files: ['notes.md'], truncated: false });
    await vi.waitFor(() => {
      expect(onSendMessage).toHaveBeenCalledTimes(1);
    });
    expect(onSendMessage).toHaveBeenCalledWith('first @notes.md', {
      fileMentions: ['notes.md'],
    });

    submitComposer();
    expect(onSendMessage).toHaveBeenCalledTimes(1);

    resolveSend(true);
    await flushComposerAsyncWork();

    expect(composerInput().value).toBe('second draft');
    expect(getDraft('agent::one')).toBe('second draft');
  });

  it('keeps the draft and attachments when send admission fails', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(false);
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-file-1',
      filename: 'paper.pdf',
      media_type: 'application/pdf',
      size_bytes: 11,
    });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        draftKey: 'agent::one',
        historyKey: 'agent',
        onSendMessage,
      },
    });
    flushSync();

    typeInComposer(composerInput(), 'keep this');
    await selectFileFromPicker(
      new File(['pdf-content'], 'paper.pdf', { type: 'application/pdf' }),
    );
    submitComposer();
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith([
      { type: 'text', text: 'keep this' },
      {
        type: 'file',
        attachment_id: 'attachment-file-1',
        filename: 'paper.pdf',
        media_type: 'application/pdf',
      },
    ]);
    expect(composerInput().value).toBe('keep this');
    expect(getDraft('agent::one')).toBe('keep this');
    expect(document.body.querySelectorAll('.attachment-item')).toHaveLength(1);
    expect(getHistory('agent')).toEqual([]);
  });

  it('keeps completed attachments with their original session across composer mounts', async () => {
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-file-1',
      filename: 'brief.pdf',
      media_type: 'application/pdf',
      size_bytes: 11,
    });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent-one::session-one', historyKey: 'agent-one' },
    });
    flushSync();

    await selectFileFromPicker(
      new File(['pdf-content'], 'brief.pdf', { type: 'application/pdf' }),
    );
    expect(document.body.querySelectorAll('.attachment-item')).toHaveLength(1);

    await unmount(mountedComponent);
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent-two::session-two', historyKey: 'agent-two' },
    });
    flushSync();

    expect(document.body.querySelectorAll('.attachment-item')).toHaveLength(0);

    await unmount(mountedComponent);
    const onSendMessage = vi.fn().mockResolvedValue(true);
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        draftKey: 'agent-one::session-one',
        historyKey: 'agent-one',
        onSendMessage,
      },
    });
    flushSync();

    expect(document.body.querySelectorAll('.attachment-item')).toHaveLength(1);
    submitComposer();
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith([
      {
        type: 'file',
        attachment_id: 'attachment-file-1',
        filename: 'brief.pdf',
        media_type: 'application/pdf',
      },
    ]);
  });

  it('sends without options when no @-token is a real file', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const onListFiles = vi.fn().mockResolvedValue({ files: [] });
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onListFiles },
    });
    flushSync();

    typeInComposer(composerInput(), 'ping @nobody');
    submitComposer();
    await flushComposerAsyncWork();
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith('ping @nobody');
  });

  it('opens the model argument autocomplete after "/model "', async () => {
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    expect(onLoadModelCatalog).toHaveBeenCalledTimes(1);
    const options = modelAutocompleteOptions();
    expect(options.length).toBeGreaterThan(0);
    expect(options[0].textContent).toContain('openai/gpt-5.2');
  });

  it('filters model options by the text after "/model "', async () => {
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    // All suitable models are visible initially.
    expect(modelAutocompleteOptions().length).toBe(2);

    // Typing a fragment narrows the list.
    typeInComposer(composerInput(), '/model ant');
    await flushComposerAsyncWork();

    const filtered = modelAutocompleteOptions();
    expect(filtered).toHaveLength(1);
    expect(filtered[0].textContent).toContain('anthropic/claude-sonnet-4');
  });

  it('submits "/model <value>" immediately when a model is selected', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    modelAutocompleteOptions()[0].dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    );
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith(
      '/model openai/gpt-5.2::api-key',
    );
    expect(composerInput().value).toBe('');
  });

  it('does not submit while another send is in flight', async () => {
    let resolveSend;
    const onSendMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve;
        }),
    );
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), 'first message');
    submitComposer();
    expect(onSendMessage).toHaveBeenCalledTimes(1);

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    modelAutocompleteOptions()[0].dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    );
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledTimes(1);
    expect(composerInput().value).toBe('/model ');

    resolveSend(true);
    await flushComposerAsyncWork();
  });

  it('does not open the model popup for "/modeling" or other slash text', async () => {
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures(), onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/modeling something');
    await flushComposerAsyncWork();

    expect(onLoadModelCatalog).not.toHaveBeenCalled();
    expect(document.body.querySelector('.model-autocomplete')).toBeNull();
  });

  it('lets Enter select the active model option', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    const input = composerInput();
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith(
      '/model openai/gpt-5.2::api-key',
    );
  });
});

function typeInComposer(input, value, caret = value.length) {
  input.value = value;
  input.setSelectionRange(caret, caret);
  input.dispatchEvent(new InputEvent('input', { bubbles: true }));
  flushSync();
}

function pressKey(input, key) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
  });
  input.dispatchEvent(event);
  flushSync();
  return event;
}

function skillFixtures() {
  return [
    {
      name: 'debugging',
      description: 'Investigate unclear bugs.',
      valid: true,
    },
    {
      name: 'frontend-design',
      description: 'Create polished interfaces.',
      valid: true,
    },
  ];
}

function composerInput() {
  return document.body.querySelector('#chat-composer-input');
}

function cancelRunButton() {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.getAttribute('aria-label') === 'Cancel run',
  );
}

function autocompleteOptions() {
  return Array.from(
    document.body.querySelectorAll('.skill-autocomplete__option'),
  );
}

function autocompleteNames() {
  return Array.from(
    document.body.querySelectorAll('.skill-autocomplete__name'),
  ).map((element) => element.textContent.trim());
}

function filePickerInput() {
  return document.body.querySelector('.attachment-file-input');
}

async function selectFileFromPicker(file) {
  await selectFilesFromPicker([file]);
}

async function selectFilesFromPicker(files) {
  const input = filePickerInput();
  Object.defineProperty(input, 'files', {
    configurable: true,
    value: files,
  });
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await flushComposerAsyncWork();
}

function attachmentNames() {
  return Array.from(document.body.querySelectorAll('.attachment-name')).map(
    (element) => element.textContent.trim(),
  );
}

function submitComposer() {
  document.body
    .querySelector('form.input-area')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

async function flushComposerAsyncWork() {
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}

function modelCatalogFixture() {
  return {
    models: [
      {
        id: 'openai/gpt-5.2',
        provider_id: 'openai',
        name: 'openai/gpt-5.2',
        capabilities: { tools: true },
        context_window: 128000,
        effective_context_window: 128000,
      },
      {
        id: 'anthropic/claude-sonnet-4',
        provider_id: 'anthropic',
        name: 'anthropic/claude-sonnet-4',
        capabilities: { tools: true },
        context_window: 200000,
        effective_context_window: 200000,
      },
      {
        id: 'ollama/tiny',
        provider_id: 'ollama',
        name: 'ollama/tiny',
        capabilities: { tools: false },
        context_window: 8192,
        effective_context_window: 8192,
      },
    ],
    connections: [
      {
        id: 'openai:api-key',
        provider_id: 'openai',
        label: 'API Key',
        usable: true,
      },
      {
        id: 'anthropic:api-key',
        provider_id: 'anthropic',
        label: 'API Key',
        usable: true,
      },
      {
        id: 'ollama:local',
        provider_id: 'ollama',
        label: 'Local',
        usable: true,
      },
    ],
  };
}

function modelAutocompleteOptions() {
  return Array.from(
    document.body.querySelectorAll('.model-autocomplete__option'),
  );
}
