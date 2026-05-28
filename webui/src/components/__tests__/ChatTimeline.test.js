// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
} from '../../lib/chatState.js';
import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');

describe('ChatTimeline', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  it('does not show a date separator for a single-day history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-single-day-history',
    );
    sessionState.messages = [
      {
        id: 'user-one',
        role: 'user',
        content: 'Morning note',
        timestamp: '2026-05-10T09:00:00',
      },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Same day reply',
        timestamp: '2026-05-10T09:01:00',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelector('.date-sep:not(.compaction-sep)')).toBeNull();
  });

  it('groups multi-day history with Today for the current day', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-11T12:00:00'));

    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-multi-day-history',
    );
    sessionState.messages = [
      {
        id: 'user-yesterday',
        role: 'user',
        content: 'Yesterday question',
        timestamp: '2026-05-10T15:00:00',
      },
      {
        id: 'assistant-yesterday',
        role: 'assistant',
        content: 'Yesterday answer',
        timestamp: '2026-05-10T15:01:00',
      },
      {
        id: 'user-today',
        role: 'user',
        content: 'Continue today',
        timestamp: '2026-05-11T08:00:00',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const dateSeparators = Array.from(
      document.querySelectorAll('.date-sep:not(.compaction-sep)'),
    );

    expect(dateSeparators).toHaveLength(2);
    expect(dateSeparators[0].textContent.trim()).not.toBe('Today');
    expect(dateSeparators[1].textContent.trim()).toBe('Today');
  });

  it('loads older history at the top and preserves the scroll anchor', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-load-older-anchor',
    );
    sessionState.messages = [
      {
        id: 'message-older-boundary',
        role: 'user',
        content: 'Oldest loaded message',
        timestamp: '2026-05-10T09:00:00',
      },
    ];
    let scrollHeight = 1000;
    const onLoadOlder = vi.fn(async () => {
      scrollHeight = 1400;
      return true;
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        hasOlderHistory: true,
        onLoadOlder,
      },
    });
    flushSync();

    const messages = document.querySelector('.messages');
    Object.defineProperty(messages, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });
    Object.defineProperty(messages, 'scrollTop', {
      configurable: true,
      writable: true,
      value: 0,
    });

    messages.dispatchEvent(new Event('scroll'));

    await waitForCondition(
      () => onLoadOlder.mock.calls.length === 1 && messages.scrollTop === 400,
    );
  });

  it('scrolls the submitted user turn to the top when requested', async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;

    try {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-submitted-turn-scroll',
      );
      appendRunEvent(sessionState, {
        type: 'user_message_persisted',
        run_id: 'run-submitted-turn-scroll',
        sequence: 1,
        payload: {
          message: {
            id: 'user-submitted-turn',
            role: 'user',
            content: 'Fresh turn should start the viewport',
            timestamp: '2026-05-11T08:00:00',
          },
        },
      });

      mountedComponent = mount(ChatTimeline, {
        target: document.body,
        props: {
          sessionState,
          agentName: 'Alpha',
          submittedTurnScrollKey: 1,
          submittedTurnScrollRunId: 'run-submitted-turn-scroll',
        },
      });
      flushSync();

      await waitForCondition(() => scrollIntoView.mock.calls.length > 0);

      expect(scrollIntoView).toHaveBeenCalledWith({
        block: 'start',
        inline: 'nearest',
        behavior: 'smooth',
      });
      expect(scrollIntoView.mock.contexts[0].textContent).toContain(
        'Fresh turn should start the viewport',
      );
      expect(
        document.querySelector('.submitted-turn-scroll-spacer'),
      ).toBeTruthy();
    } finally {
      if (originalScrollIntoView) {
        Element.prototype.scrollIntoView = originalScrollIntoView;
      } else {
        delete Element.prototype.scrollIntoView;
      }
    }
  });

  it('waits for the submitted run user event instead of scrolling the previous user message', async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;

    try {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-submitted-turn-waits-for-run-user',
      );
      sessionState.messages = [
        {
          id: 'previous-user-message',
          role: 'user',
          content: 'Previous user message',
          timestamp: '2026-05-10T08:00:00',
        },
      ];

      mountedComponent = mount(ChatTimeline, {
        target: document.body,
        props: {
          sessionState,
          agentName: 'Alpha',
          submittedTurnScrollKey: 1,
          submittedTurnScrollRunId: 'run-new-turn-not-rendered-yet',
        },
      });
      flushSync();
      await tick();
      await tick();

      expect(scrollIntoView).not.toHaveBeenCalled();
      expect(
        document.querySelector('.submitted-turn-scroll-spacer'),
      ).toBeNull();
    } finally {
      if (originalScrollIntoView) {
        Element.prototype.scrollIntoView = originalScrollIntoView;
      } else {
        delete Element.prototype.scrollIntoView;
      }
    }
  });

  it('renders brace-free tool details and hides internal result fields', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
        },
        result: {
          ok: true,
          data: {
            content: 'A',
            lines: 1,
          },
          artifacts: {
            stdout_path: '/tmp/internal.json',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        message: { role: 'assistant', content: 'Done' },
      },
    });

    expect(() => {
      mountedComponent = mount(ChatTimeline, {
        target: document.body,
        props: {
          sessionState,
          agentName: 'Alpha',
        },
      });
      flushSync();
    }).not.toThrow();

    expect(document.body.textContent).toContain('read_file');
    expect(document.body.textContent).toContain('Done');
    expect(document.body.textContent).toContain('path');
    expect(document.body.textContent).toContain('a.txt');
    expect(document.body.textContent).toContain('content');
    expect(document.body.textContent).toContain('A');
    expect(document.body.textContent).toContain('lines');
    expect(document.body.textContent).toContain('1');
    expect(document.body.textContent).not.toContain('artifacts');
    expect(document.body.textContent).not.toContain('stdout_path');

    const toolDetailRows = document.querySelectorAll(
      '.tool-event-body .teb-row',
    );
    expect(toolDetailRows).toHaveLength(2);
    expect(toolDetailRows[0].textContent).toContain('Args');
    expect(toolDetailRows[1].textContent).toContain('Result');

    const argsCode = toolDetailRows[0].querySelector('.teb-code').textContent;
    const resultCode = toolDetailRows[1].querySelector('.teb-code').textContent;
    expect(argsCode).not.toContain('{"path":"a.txt"}');
    expect(resultCode).not.toContain('{"content":"A","lines":1}');
    expect(resultCode.indexOf('content')).toBeLessThan(
      resultCode.indexOf('lines'),
    );
  });

  it('shows retry only for the latest failed assistant run and invokes callback', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-retry-latest-failed',
    );
    const onRetry = vi.fn();

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-old-failed',
      sequence: 1,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-old-failed',
      sequence: 2,
      payload: { status: 'failed' },
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-latest-failed',
      sequence: 3,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-latest-failed',
      sequence: 4,
      payload: { status: 'failed' },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        onRetry,
      },
    });
    flushSync();

    const retryButtons = document.querySelectorAll('.retry-btn');
    expect(retryButtons).toHaveLength(1);
    expect(retryButtons[0].textContent).toContain('Retry last turn');

    retryButtons[0].click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('hides retry when a newer run completed after an older failure', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-retry-hidden-completed',
    );

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-old-failed',
      sequence: 1,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-old-failed',
      sequence: 2,
      payload: { status: 'failed' },
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-latest-completed',
      sequence: 3,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-latest-completed',
      sequence: 4,
      payload: { status: 'completed' },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelector('.retry-btn')).toBeNull();
  });

  it('renders error history messages with an error label and content', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-error-message',
    );
    sessionState.messages = [
      {
        id: 'error-one',
        role: 'error',
        error_kind: 'rate_limit',
        content: 'Provider rate limit exceeded',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const errorMessage = document.querySelector('.msg.error');
    expect(errorMessage).toBeTruthy();
    expect(errorMessage.textContent).toContain('ERROR');
    expect(errorMessage.textContent).toContain('Provider rate limit exceeded');
  });

  it('renders image media blocks as inline images with attachment URLs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-media-block',
    );
    sessionState.messages = [
      {
        id: 'user-media-one',
        role: 'user',
        content: [
          {
            type: 'media',
            attachment_id: 'image-attachment-id',
            filename: 'diagram.png',
            media_type: 'image/png',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const image = document.querySelector('.inline-attachment-image');
    expect(image).toBeTruthy();
    expect(image.getAttribute('src')).toBe(
      '/api/attachments/image-attachment-id',
    );
    expect(image.getAttribute('alt')).toBe('diagram.png');

    const imageLink = document.querySelector('.inline-attachment');
    expect(imageLink).toBeTruthy();
    expect(imageLink.getAttribute('href')).toBe(
      '/api/attachments/image-attachment-id',
    );
  });

  it('renders file blocks as attachment links without image previews', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-file-block',
    );
    sessionState.messages = [
      {
        id: 'user-file-one',
        role: 'user',
        content: [
          {
            type: 'file',
            attachment_id: 'file-attachment-id',
            filename: 'report.pdf',
            media_type: 'application/pdf',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const fileLink = document.querySelector('.inline-file-link');
    expect(fileLink).toBeTruthy();
    expect(fileLink.getAttribute('href')).toBe(
      '/api/attachments/file-attachment-id',
    );
    expect(fileLink.textContent).toContain('report.pdf');
    expect(document.querySelector('.inline-attachment-image')).toBeNull();
  });

  it('renders text blocks inline instead of attachment links', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-text-block',
    );
    sessionState.messages = [
      {
        id: 'user-text-one',
        role: 'user',
        content: [
          {
            type: 'text',
            text: 'embedded text file content',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('embedded text file content');
    expect(document.querySelector('.inline-file-link')).toBeNull();
    expect(document.querySelector('.inline-attachment-image')).toBeNull();
  });

  it('renders mixed text and media blocks in one user message', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-mixed-blocks',
    );
    sessionState.messages = [
      {
        id: 'user-mixed-one',
        role: 'user',
        content: [
          {
            type: 'text',
            text: 'note before image',
          },
          {
            type: 'media',
            attachment_id: 'mixed-image-id',
            filename: 'mixed.png',
            media_type: 'image/png',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('note before image');

    const image = document.querySelector('.inline-attachment-image');
    expect(image).toBeTruthy();
    expect(image.getAttribute('src')).toBe('/api/attachments/mixed-image-id');
    expect(image.getAttribute('alt')).toBe('mixed.png');
  });

  it('keeps plain string user messages unchanged', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-plain-string',
    );
    sessionState.messages = [
      {
        id: 'user-plain-one',
        role: 'user',
        content: 'plain text message',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('plain text message');
    expect(document.querySelector('.msg-body-blocks')).toBeNull();
    expect(document.querySelector('.inline-file-link')).toBeNull();
    expect(document.querySelector('.inline-attachment-image')).toBeNull();
  });

  it('allows long unbroken user text to wrap inside the user bubble', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-long-token',
    );
    sessionState.messages = [
      {
        id: 'user-long-token',
        role: 'user',
        content: 'x'.repeat(240),
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const userBodyText = document.querySelector('.msg.user .msg-body-text');
    expect(userBodyText).toBeTruthy();
    expect(userBodyText.classList.contains('msg-body-text--user')).toBe(true);
  });

  it('renders markdown bold in completed assistant run output', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-markdown-bold',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-assistant-markdown-bold',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: '**bold**',
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const strong = document.querySelector(
      '.assistant-run .msg-markdown strong',
    );
    expect(strong).toBeTruthy();
    expect(strong.textContent).toBe('bold');
  });

  it('renders markdown code blocks in completed assistant run output', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-markdown-code-block',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-assistant-markdown-code-block',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: '```\nconst value = 1;\n```',
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const pre = document.querySelector('.assistant-run .msg-markdown pre');
    expect(pre).toBeTruthy();
    expect(pre.textContent).toContain('const value = 1;');
  });

  it('keeps markdown-like user text as plain text', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-markdown-literal',
    );
    sessionState.messages = [
      {
        id: 'user-markdown-literal',
        role: 'user',
        content: '**bold**',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const userBodyText = document.querySelector('.msg.user .msg-body-text');
    expect(userBodyText).toBeTruthy();
    expect(userBodyText.textContent).toContain('**bold**');
    expect(document.querySelector('.msg.user strong')).toBeNull();
  });

  it('renders markdown while assistant output is streaming', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-markdown-streaming',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-assistant-markdown-streaming',
      sequence: 1,
      payload: {
        content_delta: '**streaming**',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const strong = document.querySelector(
      '.assistant-run .msg-markdown.streaming-text strong',
    );
    expect(strong).toBeTruthy();
    expect(strong.textContent).toBe('streaming');
  });

  it('renders an open fenced code block while assistant output is streaming', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-markdown-streaming-open-fence',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-assistant-markdown-streaming-open-fence',
      sequence: 1,
      payload: {
        content_delta: '## Title\n\n```js\nconst value = 1;',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const heading = document.querySelector(
      '.assistant-run .msg-markdown.streaming-text h2',
    );
    const codeBlock = document.querySelector(
      '.assistant-run .msg-markdown.streaming-text pre code',
    );
    expect(heading).toBeTruthy();
    expect(heading.textContent).toBe('Title');
    expect(codeBlock).toBeTruthy();
    expect(codeBlock.textContent).toContain('const value = 1;');
  });

  it('renders markdown headings for history assistant messages', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-assistant-markdown-heading',
    );
    sessionState.messages = [
      {
        id: 'assistant-history-heading',
        role: 'assistant',
        content: '## Title',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const heading = document.querySelector('.msg.assistant .msg-markdown h2');
    expect(heading).toBeTruthy();
    expect(heading.textContent).toBe('Title');
  });

  it('keeps reasoning-only assistant history as plain text', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-assistant-reasoning-only-plain',
    );
    sessionState.messages = [
      {
        id: 'assistant-history-reasoning-only',
        role: 'assistant',
        content: null,
        reasoning: '## Thinking **bold** [link](https://example.com)',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const assistantBodyText = document.querySelector(
      '.msg.assistant .msg-body-text',
    );
    expect(assistantBodyText).toBeTruthy();
    expect(assistantBodyText.textContent).toContain(
      '## Thinking **bold** [link](https://example.com)',
    );
    expect(document.querySelector('.msg.assistant .msg-markdown')).toBeNull();
    expect(document.querySelector('.msg.assistant h2')).toBeNull();
    expect(document.querySelector('.msg.assistant strong')).toBeNull();
    expect(document.querySelector('.msg.assistant a')).toBeNull();
  });

  it('uses human-readable label instead of raw JSON for known tool', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-label',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-label',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-label',
          index: 0,
          name: 'read',
          arguments: { path: 'MEMORY.md' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-label',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-label',
          index: 0,
          name: 'read',
        },
        result: {
          ok: true,
          data: { content: 'file content here' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent).toContain('MEMORY.md');
    // The tool summary line should show the human-readable label, not raw JSON
    const summaryEl = document.querySelector('.tool-event-line');
    expect(summaryEl.textContent).not.toContain('{"path":"MEMORY.md"}');
  });

  it('uses path label instead of raw JSON for edit tool summary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-edit-label',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-edit-label',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-edit-label',
          index: 0,
          name: 'edit',
          arguments: {
            oldString: 'before',
            newString: 'after',
            path: 'notes/plan.md',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-edit-label',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-edit-label',
          index: 0,
          name: 'edit',
        },
        result: {
          ok: true,
          data: { message: 'Updated notes/plan.md' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('edit');
    expect(summaryLine.textContent).toContain('notes/plan.md');
    expect(summaryLine.textContent).not.toContain('before');
    expect(summaryLine.textContent).not.toContain('oldString');
    expect(summaryLine.textContent).not.toContain('{"oldString":"before"');
  });

  it('uses path label instead of raw JSON for write tool summary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-write-label',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-write-label',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-write-label',
          index: 0,
          name: 'write',
          arguments: {
            content: 'draft content',
            path: 'drafts/output.md',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-write-label',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-write-label',
          index: 0,
          name: 'write',
        },
        result: {
          ok: true,
          data: { message: 'Wrote drafts/output.md' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('write');
    expect(summaryLine.textContent).toContain('drafts/output.md');
    expect(summaryLine.textContent).not.toContain('draft content');
    expect(summaryLine.textContent).not.toContain('content');
    expect(summaryLine.textContent).not.toContain('{"content":"draft content"');
  });

  it('omits large write content from tool argument details', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-write-large-content',
    );
    const largeContent = 'body { color: red; }\n'.repeat(2000);

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-write-large-content',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-write-large-content',
          index: 0,
          name: 'write',
          arguments: {
            content: largeContent,
            path: 'todo-app/style.css',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('write');
    expect(summaryLine.textContent).toContain('todo-app/style.css');

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (element) => element.querySelector('.teb-label')?.textContent === 'Args',
    );
    const argsText = argsRow.querySelector('.teb-code').textContent;
    expect(argsText).toContain('path');
    expect(argsText).toContain('todo-app/style.css');
    expect(argsText).not.toContain('content');
    expect(document.body.textContent).not.toContain(largeContent);
  });

  it('does not fall back to raw write JSON when path is missing', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-write-missing-path',
    );
    const largeContent = '<main>large generated document</main>\n'.repeat(2000);

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-write-missing-path',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-write-missing-path',
          index: 0,
          name: 'write',
          arguments: JSON.stringify({ content: largeContent }),
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('write');
    expect(summaryLine.textContent).not.toContain('content');
    expect(summaryLine.textContent).not.toContain('<main>');

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (element) => element.querySelector('.teb-label')?.textContent === 'Args',
    );
    expect(argsRow.querySelector('.teb-code').textContent).toBe('—');
    expect(document.body.textContent).not.toContain(largeContent);
  });

  it('omits large edit replacement strings from tool argument details', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-edit-large-replacement',
    );
    const oldString = 'old generated block\n'.repeat(2000);
    const newString = 'new generated block\n'.repeat(2000);

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-edit-large-replacement',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-edit-large-replacement',
          index: 0,
          name: 'edit',
          arguments: {
            new_string: newString,
            old_string: oldString,
            path: 'todo-app/app.js',
            replace_all: true,
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('edit');
    expect(summaryLine.textContent).toContain('todo-app/app.js');

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (element) => element.querySelector('.teb-label')?.textContent === 'Args',
    );
    const argsText = argsRow.querySelector('.teb-code').textContent;
    expect(argsText).toContain('path');
    expect(argsText).toContain('todo-app/app.js');
    expect(argsText).toContain('replace_all');
    expect(argsText).not.toContain('old_string');
    expect(argsText).not.toContain('new_string');
    expect(document.body.textContent).not.toContain(oldString);
    expect(document.body.textContent).not.toContain(newString);
  });

  it('prefers backend display summary over command arguments', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-desc',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-desc',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-desc',
          index: 0,
          name: 'bash',
          arguments: {
            command: 'git status',
          },
        },
        display: {
          summary: 'checking repo status',
          hidden_argument_keys: [],
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-desc',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-desc',
          index: 0,
          name: 'bash',
        },
        result: {
          ok: true,
          data: { content: 'nothing to commit' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('checking repo status');

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('checking repo status');
    expect(summaryLine.textContent).not.toContain('git status');

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    expect(argsRow.querySelector('.teb-code').textContent).toContain(
      'git status',
    );
  });

  it('falls back to bash command and ignores unsupported description arguments', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-bash-command',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-bash-command',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-bash-command',
          index: 0,
          name: 'bash',
          arguments: {
            command: 'git status',
            description: 'checking repo status',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('git status');
    expect(summaryLine.textContent).not.toContain('checking repo status');

    const tebRows = document.querySelectorAll('.teb-row');
    const argsRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    expect(argsRow.querySelector('.teb-code').textContent).not.toContain(
      'description',
    );
  });

  it('renders Args detail as compact inline value', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-vert',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-vert',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-vert',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-vert',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-vert',
          index: 0,
          name: 'read_file',
        },
        result: {
          ok: true,
          data: { content: 'A' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    // Compact layout: a single .teb-row per section, no .teb-entry children
    const tebRows = document.querySelectorAll('.teb-row');
    expect(tebRows.length).toBeGreaterThan(0);

    // Args row should contain the inner value without the outer object wrapper
    const argsRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    expect(argsRow).toBeTruthy();
    const argsCode = argsRow.querySelector('.teb-code');
    expect(argsCode).toBeTruthy();
    expect(argsCode.textContent).toContain('a.txt');
    expect(argsCode.textContent).not.toContain('{"path":"a.txt"}');
  });

  it('falls back to first string argument for unknown tools', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-fallback',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-fallback',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-fallback',
          index: 0,
          name: 'custom_tool',
          arguments: { target: 'build' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-fallback',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-fallback',
          index: 0,
          name: 'custom_tool',
        },
        result: {
          ok: true,
          data: { content: 'done' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('custom_tool');
    expect(document.body.textContent).toContain('build');
  });

  it('does not render empty object arguments as a status summary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-status-summary',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-status-summary',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-status-summary',
          index: 0,
          name: 'status',
          arguments: {},
        },
        display: {
          summary: '',
          hidden_argument_keys: [],
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('status');
    expect(summaryLine.textContent).not.toContain('({})');
    expect(summaryLine.textContent).not.toContain('{}');
  });

  it('skips empty backend display summary and falls back to per-tool arg', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-empty-desc',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-empty-desc',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-empty-desc',
          index: 0,
          name: 'read',
          arguments: { path: 'config.yaml' },
        },
        display: {
          summary: '   ',
          hidden_argument_keys: [],
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-empty-desc',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-empty-desc',
          index: 0,
          name: 'read',
        },
        result: { ok: true, data: { content: 'x' } },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('config.yaml');
    expect(summaryLine.textContent).not.toContain('{"path":"config.yaml"');
  });

  it('uses glob pattern for summary and successful envelope content for result', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-glob-label',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-glob-label',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-glob-label',
          index: 0,
          name: 'glob',
          arguments: {
            pattern: '**/*.md',
            path: 'docs',
            description: 'model supplied glob label',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-glob-label',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-glob-label',
          index: 0,
          name: 'glob',
        },
        result: {
          ok: true,
          data: { content: 'README.md\nplans/current.md' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('glob');
    expect(summaryLine.textContent).toContain('**/*.md');
    expect(summaryLine.textContent).not.toContain('model supplied glob label');
    expect(summaryLine.textContent).not.toContain('docs');

    const resultRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Result',
    );
    expect(resultRow.querySelector('.teb-code').textContent).toBe(
      'README.md\nplans/current.md',
    );
  });

  it('uses grep pattern plus path for summary and failed style for error envelope', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-grep-failed',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-grep-failed',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-grep-failed',
          index: 0,
          name: 'grep',
          arguments: {
            pattern: 'TODO',
            path: 'src',
            description: 'model supplied grep label',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-grep-failed',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-grep-failed',
          index: 0,
          name: 'grep',
        },
        result: {
          ok: false,
          error: {
            code: 'invalid_regex',
            message: 'Invalid regular expression',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('grep');
    expect(summaryLine.textContent).toContain('TODO · src');
    expect(summaryLine.textContent).not.toContain('model supplied grep label');

    const failedDot = summaryLine.querySelector('.te-dot.error');
    expect(failedDot).toBeTruthy();

    const resultRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Result',
    );
    const resultCode = resultRow.querySelector('.teb-code.error');
    expect(resultCode).toBeTruthy();
    expect(resultCode.textContent).toContain('invalid_regex');
    expect(resultCode.textContent).toContain('Invalid regular expression');
  });

  // ---------------------------------------------------------------------------
  // compactToolValue unit tests (tested via rendered .teb-code elements)
  // ---------------------------------------------------------------------------

  /**
   * Mounts a single tool_call_result event and returns the text content of the
   * Result `.teb-code` element.  `resultValue` is placed verbatim into
   * payload.result (preferPayload=true path).
   */
  function getResultCodeText(resultValue, sessionId, toolName = 'probe') {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      sessionId,
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: `run-${sessionId}`,
      sequence: 1,
      payload: {
        tool_call: {
          id: `call-${sessionId}`,
          index: 0,
          name: toolName,
          arguments: {},
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: `run-${sessionId}`,
      sequence: 2,
      payload: {
        tool_call: { id: `call-${sessionId}`, index: 0, name: toolName },
        result: resultValue,
      },
    });
    const comp = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();
    const tebRows = document.querySelectorAll('.teb-row');
    const resultRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Result',
    );
    const text = resultRow?.querySelector('.teb-code')?.textContent ?? '';
    unmount(comp);
    document.body.innerHTML = '';
    return text;
  }

  /**
   * Mounts a single tool_call_started event and returns the Args `.teb-code`
   * text (preferPayload=false path).
   */
  function getArgsCodeText(argsValue, sessionId) {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      sessionId,
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: `run-${sessionId}`,
      sequence: 1,
      payload: {
        tool_call: {
          id: `call-${sessionId}`,
          index: 0,
          name: 'probe',
          arguments: argsValue,
        },
      },
    });
    const comp = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();
    const tebRows = document.querySelectorAll('.teb-row');
    const argsRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    const text = argsRow?.querySelector('.teb-code')?.textContent ?? '';
    unmount(comp);
    document.body.innerHTML = '';
    return text;
  }

  describe('compactToolValue', () => {
    it('plain object → inner fields without the outer JSON object wrapper', () => {
      const text = getArgsCodeText({ path: 'file.txt', count: 3 }, 'ctv-obj');
      expect(text).toContain('path');
      expect(text).toContain('file.txt');
      expect(text).toContain('count');
      expect(text).toContain('3');
      expect(text.indexOf('path')).toBeLessThan(text.indexOf('count'));
      expect(text).not.toBe('{"path":"file.txt","count":3}');
      expect(text.trim().startsWith('{')).toBe(false);
      expect(text.trim().endsWith('}')).toBe(false);
    });

    it('plain string → returned as-is', () => {
      const text = getArgsCodeText('just a string', 'ctv-str');
      expect(text).toBe('just a string');
    });

    it('null value → returns the no-data placeholder (—)', () => {
      const text = getResultCodeText(null, 'ctv-null');
      // i18n default fallback for chat.toolNoData is "—"
      expect(text).toBe('—');
    });

    it('undefined value (missing result key) → Args with empty object returns the no-data placeholder (—)', () => {
      // undefined is equivalent to an empty value; empty object also fails hasMeaningfulToolDetail
      const text = getArgsCodeText(undefined, 'ctv-undef');
      expect(text).toBe('—');
    });

    it('empty object → returns the no-data placeholder (—)', () => {
      const text = getArgsCodeText({}, 'ctv-empty-obj');
      expect(text).toBe('—');
    });

    it('object with .data field and preferPayload:true → returns inner data fields without outer braces', () => {
      // Result value with a .data field; preferPayload=true (Result row uses it)
      const text = getResultCodeText(
        { ok: true, data: { content: 'hello', lines: 2 } },
        'ctv-data',
      );
      expect(text).toContain('content');
      expect(text).toContain('hello');
      expect(text).toContain('lines');
      expect(text).toContain('2');
      expect(text.indexOf('content')).toBeLessThan(text.indexOf('lines'));
      expect(text).not.toBe('{"content":"hello","lines":2}');
      expect(text.trim().startsWith('{')).toBe(false);
      expect(text.trim().endsWith('}')).toBe(false);
    });

    it('successful content-only read result → displays content directly', () => {
      const text = getResultCodeText(
        { ok: true, data: { content: 'file content here' } },
        'ctv-read-content',
        'read',
      );
      expect(text).toBe('file content here');
    });

    it('successful persisted read result with path → displays content and hides path', () => {
      const text = getResultCodeText(
        {
          ok: true,
          data: { path: 'MEMORY.md', content: 'persisted file content' },
        },
        'ctv-read-persisted-path',
        'read',
      );
      expect(text).toBe('persisted file content');
      expect(text).not.toContain('MEMORY.md');
      expect(text).not.toContain('path');
    });

    it('error envelope with .error field and preferPayload:true → returns error text', () => {
      const text = getResultCodeText(
        { error: 'something went wrong' },
        'ctv-error',
      );
      expect(text).toContain('something went wrong');
    });

    it('array → compact JSON stringify (no indentation)', () => {
      // Arrays are passed as args; preferPayload=false (sanitizeToolDetailNode path)
      const text = getArgsCodeText([1, 2, 3], 'ctv-array');
      expect(text).toBe('[1,2,3]');
    });
  });

  it('omits summary fallback for tools with non-string argument values', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-json-fallback',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-json',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-json',
          index: 0,
          name: 'compute',
          arguments: { count: 5, active: true },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-json',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-json',
          index: 0,
          name: 'compute',
        },
        result: { ok: true, data: { result: 42 } },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('compute');
    expect(summaryLine.textContent).not.toContain('count');

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    expect(argsRow.querySelector('.teb-code').textContent).toContain('count');
  });

  it('keeps thinking above later tool rows after subsequent reasoning updates', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-run-order',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-order',
      sequence: 1,
      payload: {
        reasoning_delta: 'Thinking starts',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-order',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-order',
          index: 0,
          name: 'read',
          arguments: { path: 'MEMORY.md' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-order',
      sequence: 3,
      payload: {
        reasoning_delta: ' and keeps going',
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-order',
      sequence: 4,
      payload: {
        message: { role: 'assistant', content: 'Done' },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const runContent = document.querySelector('.assistant-run-content');
    const renderedChildren = Array.from(runContent.children);

    expect(renderedChildren).toHaveLength(3);
    expect(renderedChildren[0].classList.contains('reasoning-block')).toBe(
      true,
    );
    expect(renderedChildren[1].classList.contains('run-tool-event')).toBe(true);
    expect(renderedChildren[2].classList.contains('msg-markdown')).toBe(true);
    expect(renderedChildren[0].textContent).toContain(
      'Thinking starts and keeps going',
    );
    expect(renderedChildren[1].textContent).toContain('read');
    expect(renderedChildren[2].textContent).toContain('Done');
  });

  it('renders distinct assistant output phases around a tool row', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-output-tool-output',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-output-tool-output',
      sequence: 1,
      payload: {
        content_delta: 'First answer',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-output-tool-output',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-output-tool-output',
          index: 0,
          name: 'read',
          arguments: { path: 'MEMORY.md' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-output-tool-output',
      sequence: 3,
      payload: {
        tool_call: {
          id: 'call-output-tool-output',
          index: 0,
          name: 'read',
        },
        result: {
          ok: true,
          data: { content: 'A' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-output-tool-output',
      sequence: 4,
      payload: {
        content_delta: 'Second answer',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const runContent = document.querySelector('.assistant-run-content');
    const renderedChildren = Array.from(runContent.children);

    expect(renderedChildren).toHaveLength(3);
    expect(renderedChildren[0].classList.contains('msg-markdown')).toBe(true);
    expect(renderedChildren[0].textContent).toContain('First answer');
    expect(renderedChildren[1].classList.contains('run-tool-event')).toBe(true);
    expect(renderedChildren[1].textContent).toContain('read');
    expect(renderedChildren[2].classList.contains('msg-markdown')).toBe(true);
    expect(renderedChildren[2].textContent).toContain('Second answer');
  });

  it('renders reported persisted multi-step session as one assistant block', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-multistep',
    );

    loadHistory(sessionState, [
      {
        id: 'user-reported',
        role: 'user',
        content: 'Investigate the duplicated chat UI.',
      },
      {
        id: 'assistant-glob',
        role: 'assistant',
        reasoning: 'Find candidate files.',
        tool_calls: [
          {
            id: 'call-glob',
            name: 'glob',
            arguments: { pattern: 'webui/src/**/*.js' },
          },
        ],
      },
      {
        id: 'tool-glob',
        role: 'tool',
        tool_call_id: 'call-glob',
        name: 'glob',
        content: '{"ok":true,"data":{"content":"webui/src/lib/chatState.js"}}',
      },
      {
        id: 'assistant-read',
        role: 'assistant',
        content: 'I found the timeline helper; now I will read it.',
        reasoning: 'Read the selected file.',
        tool_calls: [
          {
            id: 'call-read',
            name: 'read',
            arguments: { path: 'webui/src/lib/chatState.js' },
          },
        ],
      },
      {
        id: 'tool-read',
        role: 'tool',
        tool_call_id: 'call-read',
        name: 'read',
        content: '{"ok":true,"data":{"content":"timeline code"}}',
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The timeline is in chatState.js.',
        reasoning: 'Summarize the result.',
      },
    ]);

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.run-tool-event')).toHaveLength(2);
    expect(document.querySelectorAll('.reasoning-block')).toHaveLength(3);
    expect(
      Array.from(document.querySelectorAll('.te-fn')).map(
        (element) => element.textContent,
      ),
    ).toEqual(['glob', 'read']);
    expect(document.body.textContent).not.toContain(
      'I will inspect the UI state helpers.',
    );
    expect(
      document.body.textContent.match(
        /I found the timeline helper; now I will read it\./g,
      ),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
  });

  it('renders reported live multi-step run content and thinking once', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-live',
    );

    appendReportedLiveRunEvents(sessionState, 'run-reported-live');

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.run-tool-event')).toHaveLength(2);
    expect(
      Array.from(document.querySelectorAll('.te-fn')).map(
        (element) => element.textContent,
      ),
    ).toEqual(['glob', 'read']);
    expect(
      document.body.textContent.match(
        /I found the timeline helper; now I will read it\./g,
      ),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Find candidate files\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Read the selected file\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Summarize the result\./g),
    ).toHaveLength(1);
  });

  it('renders final reasoning once before final assistant content', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-final-reasoning-draft',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-final-reasoning-draft',
      sequence: 1,
      payload: {
        message: {
          id: 'assistant-reasoning-draft',
          role: 'assistant',
          reasoning: 'Summarize the result.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-final-reasoning-draft',
      sequence: 2,
      payload: { content_delta: 'The timeline is in chatState.js.' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-final-reasoning-draft',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-final',
          role: 'assistant',
          reasoning: 'Summarize the result.',
          content: 'The timeline is in chatState.js.',
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const runContent = document.querySelector('.assistant-run-content');
    const renderedChildren = Array.from(runContent.children);

    expect(document.querySelectorAll('.reasoning-block')).toHaveLength(1);
    expect(renderedChildren).toHaveLength(2);
    expect(renderedChildren[0].classList.contains('reasoning-block')).toBe(
      true,
    );
    expect(renderedChildren[1].classList.contains('msg-markdown')).toBe(true);
    expect(
      document.body.textContent.match(/Summarize the result\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
  });

  it('renders one assistant block when reported history overlaps live events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-overlap',
    );
    startRun(sessionState, {
      run_id: 'run-reported-overlap',
      sse_url: '/api/runs/run-reported-overlap/events',
      status: 'running',
    });

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-reported-overlap',
      sequence: 1,
      payload: { message: reportedMultiStepMessages()[0] },
    });
    appendReportedLiveRunEvents(sessionState, 'run-reported-overlap', 2);
    loadHistory(sessionState, reportedMultiStepMessages());

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.run-tool-event')).toHaveLength(2);
    expect(
      document.body.textContent.match(
        /I found the timeline helper; now I will read it\./g,
      ),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Find candidate files\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Read the selected file\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Summarize the result\./g),
    ).toHaveLength(1);
  });

  it('renders one visible assistant block when persisted history overlaps an active run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-overlap',
    );

    sessionState.status = 'running';
    sessionState.currentRun = {
      runId: 'run-overlap',
      sseUrl: '/api/runs/run-overlap/events',
      status: 'running',
    };
    sessionState.messages = [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ];

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-overlap',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-overlap',
      sequence: 2,
      payload: {
        reasoning_delta: 'Checking',
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-overlap',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-overlap',
      sequence: 4,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read',
          arguments: { path: 'a.txt' },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const assistantRuns = document.querySelectorAll('.assistant-run');
    expect(assistantRuns).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(1);
    expect(document.body.textContent).toContain('The file says A.');
    expect(document.body.textContent).toContain('Checking');
  });

  it('drops still-working indicators after the active run becomes terminal history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-terminal-overlap',
    );

    sessionState.messages = [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ];

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-terminal-overlap',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-terminal-overlap',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-terminal-overlap',
      sequence: 3,
      payload: {
        status: 'completed',
      },
    });
    sessionState.currentRun = {
      runId: 'run-terminal-overlap',
      sseUrl: '/api/runs/run-terminal-overlap/events',
      status: 'completed',
    };

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
  });

  it('shows cancelled status for a tool that was active when the run was cancelled', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-cancelled-tool',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-cancelled-tool',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-bash',
          index: 0,
          name: 'bash',
          arguments: { command: 'sleep 30' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-cancelled-tool',
      sequence: 2,
      payload: { status: 'cancelled' },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const toolLine = document.querySelector('.tool-event-line');

    expect(toolLine?.textContent).toContain('bash');
    expect(toolLine?.textContent).toContain('cancelled');
    expect(toolLine?.querySelector('.te-dot.running')).toBeNull();
    expect(toolLine?.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it('renders one assistant block when terminal events arrive after overlapping history refresh', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-terminal-history-refresh',
    );

    sessionState.currentRun = {
      runId: 'run-terminal-history-refresh',
      sseUrl: '/api/runs/run-terminal-history-refresh/events',
      status: 'running',
    };
    sessionState.status = 'completed';
    sessionState.messages = [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ];

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-terminal-history-refresh',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-terminal-history-refresh',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-terminal-history-refresh',
      sequence: 3,
      payload: {
        status: 'completed',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
    expect(document.body.textContent.match(/The file says A\./g)).toHaveLength(
      1,
    );
  });

  it('uses completed persisted history when replay resumes with only later live events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-ahead',
    );

    startRun(sessionState, {
      run_id: 'run-history-ahead',
      sse_url: '/api/runs/run-history-ahead/events',
      status: 'running',
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-history-ahead',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-history-ahead',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-history-ahead',
      sequence: 3,
      payload: {
        status: 'completed',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
    expect(document.body.textContent.match(/The file says A\./g)).toHaveLength(
      1,
    );
    expect(document.body.textContent).not.toContain('read');
  });

  it('uses persisted overlap suffix rows as the single assistant block during handoff', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-suffix',
    );

    startRun(sessionState, {
      run_id: 'run-history-suffix',
      sse_url: '/api/runs/run-history-suffix/events',
      status: 'running',
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-tools',
        role: 'assistant',
        reasoning: 'Need to read it.',
        tool_calls: [
          {
            id: 'call-one',
            name: 'read',
            arguments: { path: 'a.txt' },
          },
        ],
      },
      {
        id: 'tool-one',
        role: 'tool',
        tool_call_id: 'call-one',
        name: 'read',
        content: '{"ok": true, "content": "A"}',
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-history-suffix',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-history-suffix',
      sequence: 2,
      payload: {
        status: 'completed',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const assistantRuns = document.querySelectorAll('.assistant-run');
    expect(assistantRuns).toHaveLength(1);
    expect(document.body.textContent).toContain('Need to read it.');
    expect(document.body.textContent).toContain('The file says A.');
    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent.match(/The file says A\./g)).toHaveLength(
      1,
    );
  });

  it('renders model fallback notices inside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-model-fallback',
    );

    appendRunEvent(sessionState, {
      type: 'model_fallback_activated',
      run_id: 'run-model-fallback',
      sequence: 1,
      payload: {
        from_model: 'openai/gpt-5',
        to_model: 'openrouter/anthropic/claude-sonnet-4',
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const fallbackNotice = document.querySelector('.model-fallback-notice');
    expect(fallbackNotice).toBeTruthy();
    expect(fallbackNotice.textContent).toContain(
      'Switched to openrouter/anthropic/claude-sonnet-4',
    );
  });

  it('renders streamed tool stdout and stderr inside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-tool-output',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-tool-output',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'bash',
          arguments: { command: 'printf hello' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-tool-output',
      sequence: 2,
      payload: { tool_call_id: 'call-one', data: 'hello\n' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stderr',
      run_id: 'run-tool-output',
      sequence: 3,
      payload: { tool_call_id: 'call-one', data: 'warn\n' },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('Stdout');
    expect(document.body.textContent).toContain('hello');
    expect(document.body.textContent).toContain('Stderr');
    expect(document.body.textContent).toContain('warn');
  });

  it('renders a stable-sized thinking chevron and only rotates it when expanded', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-thinking-chevron',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-thinking-chevron',
      sequence: 1,
      payload: {
        message: { role: 'assistant', reasoning: 'Trace the issue' },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const reasoningBlock = document.querySelector(
      '.assistant-run .reasoning-block',
    );
    const chevron = reasoningBlock.querySelector('.r-chevron');

    expect(reasoningBlock.open).toBe(false);
    expect(chevron.getAttribute('width')).toBe('10');
    expect(chevron.getAttribute('height')).toBe('10');
    expect(chevron.style.transform).toBe('none');

    reasoningBlock.open = true;
    reasoningBlock.dispatchEvent(new Event('toggle'));
    flushSync();

    expect(reasoningBlock.open).toBe(true);
    expect(chevron.getAttribute('width')).toBe('10');
    expect(chevron.getAttribute('height')).toBe('10');
    expect(chevron.style.transform).toBe('rotate(180deg)');
  });

  it('updates spawned sub-agent rows when a matching result is completed', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-status',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-status',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            content: 'Inspect the logs',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-status',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            run_id: 'sub-run-1',
            status: 'running',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-status',
      sequence: 3,
      payload: {
        tool_call: {
          id: 'call-subagent-result',
          index: 1,
          name: 'subagent_result',
          arguments: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            run_id: 'sub-run-1',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-status',
      sequence: 4,
      payload: {
        tool_call: {
          id: 'call-subagent-result',
          index: 1,
          name: 'subagent_result',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            run_id: 'sub-run-1',
            status: 'completed',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const subagentRows = document.querySelectorAll(
      '.subagent-tool-event .subagent-line',
    );

    expect(subagentRows).toHaveLength(2);
    expect(subagentRows[0].textContent).not.toContain('Status: completed');
    expect(subagentRows[0].querySelector('.subagent-status')).toBeNull();
    expect(subagentRows[0].querySelector('.te-dot.done')).not.toBeNull();
    expect(subagentRows[0].querySelector('.te-dot.running')).toBeNull();
  });

  it('calls the sub-agent navigation callback with a spawned session target', () => {
    const onNavigateToSubAgent = vi.fn();
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-link',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-link',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            content: 'Inspect the logs',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-link',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            status: 'running',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        onNavigateToSubAgent,
      },
    });
    flushSync();

    const viewSessionButton = document.querySelector('.subagent-link');
    expect(viewSessionButton).toBeTruthy();

    viewSessionButton.click();
    flushSync();

    expect(onNavigateToSubAgent).toHaveBeenCalledWith({
      agentId: 'beta',
      sessionId: 'sub-session-1',
    });
  });
});

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await tick();
    await Promise.resolve();
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}

function reportedMultiStepMessages() {
  return [
    {
      id: 'user-reported',
      role: 'user',
      content: 'Investigate the duplicated chat UI.',
    },
    {
      id: 'assistant-glob',
      role: 'assistant',
      reasoning: 'Find candidate files.',
      tool_calls: [
        {
          id: 'call-glob',
          name: 'glob',
          arguments: { pattern: 'webui/src/**/*.js' },
        },
      ],
    },
    {
      id: 'tool-glob',
      role: 'tool',
      tool_call_id: 'call-glob',
      name: 'glob',
      content: '{"ok":true,"data":{"content":"webui/src/lib/chatState.js"}}',
    },
    {
      id: 'assistant-read',
      role: 'assistant',
      content: 'I found the timeline helper; now I will read it.',
      reasoning: 'Read the selected file.',
      tool_calls: [
        {
          id: 'call-read',
          name: 'read',
          arguments: { path: 'webui/src/lib/chatState.js' },
        },
      ],
    },
    {
      id: 'tool-read',
      role: 'tool',
      tool_call_id: 'call-read',
      name: 'read',
      content: '{"ok":true,"data":{"content":"timeline code"}}',
    },
    {
      id: 'assistant-final',
      role: 'assistant',
      content: 'The timeline is in chatState.js.',
      reasoning: 'Summarize the result.',
    },
  ];
}

function appendReportedLiveRunEvents(sessionState, runId, startSequence = 1) {
  const sequence = (offset) => startSequence + offset;

  appendRunEvent(sessionState, {
    type: 'reasoning_delta',
    run_id: runId,
    sequence: sequence(0),
    payload: { reasoning_delta: 'Find candidate files.' },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_started',
    run_id: runId,
    sequence: sequence(1),
    payload: {
      tool_call: {
        id: 'call-glob',
        index: 0,
        name: 'glob',
        arguments: { pattern: 'webui/src/**/*.js' },
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_result',
    run_id: runId,
    sequence: sequence(2),
    payload: {
      tool_call: { id: 'call-glob', index: 0, name: 'glob' },
      result: { ok: true, data: { content: 'webui/src/lib/chatState.js' } },
    },
  });
  appendRunEvent(sessionState, {
    type: 'reasoning_delta',
    run_id: runId,
    sequence: sequence(3),
    payload: { reasoning_delta: 'Read the selected file.' },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output_delta',
    run_id: runId,
    sequence: sequence(4),
    payload: {
      content_delta: 'I found the timeline helper; now I will read it.',
    },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_started',
    run_id: runId,
    sequence: sequence(5),
    payload: {
      tool_call: {
        id: 'call-read',
        index: 0,
        name: 'read',
        arguments: { path: 'webui/src/lib/chatState.js' },
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_result',
    run_id: runId,
    sequence: sequence(6),
    payload: {
      tool_call: { id: 'call-read', index: 0, name: 'read' },
      result: { ok: true, data: { content: 'timeline code' } },
    },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output',
    run_id: runId,
    sequence: sequence(7),
    payload: {
      message: {
        id: 'assistant-read',
        role: 'assistant',
        content: 'I found the timeline helper; now I will read it.',
        reasoning: 'Read the selected file.',
        tool_calls: [
          {
            id: 'call-read',
            name: 'read',
            arguments: { path: 'webui/src/lib/chatState.js' },
          },
        ],
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'reasoning_delta',
    run_id: runId,
    sequence: sequence(8),
    payload: { reasoning_delta: 'Summarize the result.' },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output_delta',
    run_id: runId,
    sequence: sequence(9),
    payload: { content_delta: 'The timeline is in chatState.js.' },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output',
    run_id: runId,
    sequence: sequence(10),
    payload: {
      message: {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The timeline is in chatState.js.',
        reasoning: 'Summarize the result.',
      },
    },
  });
}
