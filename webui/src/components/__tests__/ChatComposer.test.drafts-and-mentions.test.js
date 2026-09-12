// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  uploadAttachment,
  ChatComposer,
  getDraft,
  getHistory,
  pushHistory,
  setDraft,
  typeInComposer,
  pressKey,
  composerInput,
  cancelRunButton,
  selectFileFromPicker,
  submitComposer,
  flushComposerAsyncWork,
  setupChatComposerSuite,
} from './ChatComposer.support.js';

describe('ChatComposer', () => {
  const suite = setupChatComposerSuite();

  it('restores a saved draft for the session on mount', () => {
    setDraft('agent::one', 'half a thought');

    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();

    expect(composerInput().value).toBe('half a thought');
  });

  it('persists the typed draft into per-session memory', () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent::one', historyKey: 'agent' },
    });
    flushSync();

    typeInComposer(composerInput(), 'work in progress');

    expect(getDraft('agent::one')).toBe('work in progress');
  });

  it('clears the draft and records history when a message is sent', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { isRunning: false },
    });
    flushSync();

    expect(cancelRunButton()).toBeUndefined();
  });

  it('offers the stop button next to Send while a run is active', () => {
    const onCancelRun = vi.fn();
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { isRunning: true, disabled: true },
    });
    flushSync();

    expect(cancelRunButton().disabled).toBe(false);
  });

  it('disables the stop button while a cancel is in flight', () => {
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent-one::session-one', historyKey: 'agent-one' },
    });
    flushSync();

    await selectFileFromPicker(
      new File(['pdf-content'], 'brief.pdf', { type: 'application/pdf' }),
    );
    expect(document.body.querySelectorAll('.attachment-item')).toHaveLength(1);

    await unmount(suite.mountedComponent);
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { draftKey: 'agent-two::session-two', historyKey: 'agent-two' },
    });
    flushSync();

    expect(document.body.querySelectorAll('.attachment-item')).toHaveLength(0);

    await unmount(suite.mountedComponent);
    const onSendMessage = vi.fn().mockResolvedValue(true);
    suite.mountedComponent = mount(ChatComposer, {
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
    suite.mountedComponent = mount(ChatComposer, {
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
});
