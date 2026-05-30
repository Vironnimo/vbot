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

const { transcribeSpeech, uploadAttachment } = await import('$lib/api.js');

const { default: ChatComposer } = await import('../ChatComposer.svelte');

describe('ChatComposer', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    transcribeSpeech.mockReset();
    uploadAttachment.mockReset();
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
    const onSendMessage = vi.fn();
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

  it('resets the textarea height after sending a tall draft', () => {
    const onSendMessage = vi.fn();
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

    expect(onSendMessage).toHaveBeenCalledWith(
      'line one\nline two\nline three',
    );
    expect(input.value).toBe('');
    expect(input.style.height).toBe('');
  });

  it('sends uploaded text files as embedded text blocks', async () => {
    const onSendMessage = vi.fn();
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-text-1',
      filename: 'note.txt',
      media_type: 'text/plain',
      size_bytes: 5,
      text_content: 'hello',
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
      { type: 'text', text: 'hello' },
    ]);
  });

  it('sends uploaded empty text files as embedded text blocks', async () => {
    const onSendMessage = vi.fn();
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-text-empty-1',
      filename: 'empty.txt',
      media_type: 'text/plain',
      size_bytes: 0,
      text_content: '',
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

    expect(onSendMessage).toHaveBeenCalledWith([{ type: 'text', text: '' }]);
  });

  it('sends uploaded images as media blocks', async () => {
    const onSendMessage = vi.fn();
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-image-1',
      filename: 'photo.png',
      media_type: 'image/png',
      size_bytes: 7,
      text_content: null,
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

  it('sends non-image binary uploads as file blocks', async () => {
    const onSendMessage = vi.fn();
    uploadAttachment.mockResolvedValue({
      attachment_id: 'attachment-file-1',
      filename: 'paper.pdf',
      media_type: 'application/pdf',
      size_bytes: 11,
      text_content: null,
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
});

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
  const input = filePickerInput();
  Object.defineProperty(input, 'files', {
    configurable: true,
    value: [file],
  });
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await flushComposerAsyncWork();
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
