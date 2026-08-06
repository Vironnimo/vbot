// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
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

  it('opens a lightbox when a markdown image is clicked and closes it on Escape', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-markdown-image-lightbox',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-assistant-markdown-image',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: '![diagram](https://example.com/diagram.png)',
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

    const image = document.querySelector('.assistant-run .msg-markdown img');
    expect(image).toBeTruthy();
    expect(image.getAttribute('src')).toBe('https://example.com/diagram.png');
    expect(document.querySelector('.image-lightbox')).toBeNull();

    image.click();
    flushSync();

    const lightbox = document.querySelector('.image-lightbox');
    expect(lightbox).toBeTruthy();
    expect(
      lightbox.querySelector('.image-lightbox__image').getAttribute('src'),
    ).toBe('https://example.com/diagram.png');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();

    expect(document.querySelector('.image-lightbox')).toBeNull();
  });

  it('renders signed server file URLs as an image lightbox and download link', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-server-files',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-assistant-server-files',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content:
            '![chart.png](/api/files/image-token)\n\n[report.txt](/api/files/file-token)',
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

    const image = document.querySelector('.assistant-run .msg-markdown img');
    const link = document.querySelector('.assistant-run .msg-markdown a');
    expect(image.getAttribute('src')).toBe('/api/files/image-token');
    expect(link.textContent).toBe('report.txt');
    expect(link.getAttribute('href')).toBe('/api/files/file-token');

    image.click();
    flushSync();

    expect(
      document.querySelector('.image-lightbox__image').getAttribute('src'),
    ).toBe(new URL('/api/files/image-token', document.baseURI).href);
  });

  it('opens the lightbox when a user attachment thumbnail is clicked', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-attachment-lightbox',
    );
    sessionState.messages = [
      {
        id: 'user-attachment-image',
        role: 'user',
        content: [
          {
            type: 'media',
            attachment_id: 'attachment-lightbox',
            filename: 'photo.png',
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

    const image = document.querySelector('.attachment-thumb');
    expect(image).toBeTruthy();
    expect(document.querySelector('.image-lightbox')).toBeNull();

    image.click();
    flushSync();

    const lightbox = document.querySelector('.image-lightbox');
    expect(lightbox).toBeTruthy();
    expect(
      lightbox.querySelector('.image-lightbox__image').getAttribute('src'),
    ).toBe(image.src);
  });

  it('does not open the lightbox for modifier-clicked attachment thumbnails', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-attachment-modifier-click',
    );
    sessionState.messages = [
      {
        id: 'user-attachment-image',
        role: 'user',
        content: [
          {
            type: 'media',
            attachment_id: 'attachment-modifier-click',
            filename: 'photo.png',
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

    const image = document.querySelector('.attachment-thumb');
    expect(image).toBeTruthy();

    image.dispatchEvent(
      new MouseEvent('click', { bubbles: true, ctrlKey: true }),
    );
    flushSync();

    expect(document.querySelector('.image-lightbox')).toBeNull();
  });

  it('toggles the lightbox between fit and actual size on image click', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-lightbox-zoom-toggle',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-lightbox-zoom',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: '![diagram](https://example.com/diagram.png)',
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

    document.querySelector('.assistant-run .msg-markdown img').click();
    flushSync();

    const overlay = document.querySelector('.image-lightbox');
    const lightboxImage = overlay.querySelector('.image-lightbox__image');
    expect(overlay.classList.contains('image-lightbox--zoomed')).toBe(false);

    // Simulate an image larger than the viewport so zoom-in is available.
    Object.defineProperty(lightboxImage, 'naturalWidth', {
      value: 4000,
      configurable: true,
    });
    Object.defineProperty(lightboxImage, 'naturalHeight', {
      value: 3000,
      configurable: true,
    });
    window.innerWidth = 800;
    window.innerHeight = 600;
    window.dispatchEvent(new Event('resize'));
    flushSync();

    expect(lightboxImage.classList.contains('zoomable')).toBe(true);

    lightboxImage.click();
    flushSync();
    expect(overlay.classList.contains('image-lightbox--zoomed')).toBe(true);
    expect(lightboxImage.classList.contains('zoomed')).toBe(true);

    lightboxImage.click();
    flushSync();
    expect(overlay.classList.contains('image-lightbox--zoomed')).toBe(false);

    // Clicking the image must not close the lightbox.
    expect(document.querySelector('.image-lightbox')).toBeTruthy();
  });

  it('does not zoom the lightbox when the image fits the viewport', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-lightbox-no-zoom',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-lightbox-no-zoom',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: '![small](https://example.com/small.png)',
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

    document.querySelector('.assistant-run .msg-markdown img').click();
    flushSync();

    const overlay = document.querySelector('.image-lightbox');
    const lightboxImage = overlay.querySelector('.image-lightbox__image');

    Object.defineProperty(lightboxImage, 'naturalWidth', {
      value: 200,
      configurable: true,
    });
    Object.defineProperty(lightboxImage, 'naturalHeight', {
      value: 150,
      configurable: true,
    });
    window.innerWidth = 1200;
    window.innerHeight = 900;
    window.dispatchEvent(new Event('resize'));
    flushSync();

    expect(lightboxImage.classList.contains('zoomable')).toBe(false);

    lightboxImage.click();
    flushSync();
    expect(overlay.classList.contains('image-lightbox--zoomed')).toBe(false);
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

  it('autolinks safe URLs in plain user text without enabling Markdown', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-literal-link',
    );
    sessionState.messages = [
      {
        id: 'user-literal-link',
        role: 'user',
        content: '**literal** https://example.com/docs.',
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
    const link = userBodyText.querySelector('a');
    expect(userBodyText.textContent).toBe(
      '**literal** https://example.com/docs.',
    );
    expect(userBodyText.querySelector('strong')).toBeNull();
    expect(link.textContent).toBe('https://example.com/docs');
    expect(link.getAttribute('href')).toBe('https://example.com/docs');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('autolinks literal URLs in assistant output', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-assistant-literal-link',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-assistant-literal-link',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: 'Open https://example.com/docs.',
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

    const link = document.querySelector('.assistant-run .msg-markdown a');
    expect(link.textContent).toBe('https://example.com/docs');
    expect(link.getAttribute('href')).toBe('https://example.com/docs');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
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

  it('renders reasoning blocks as markdown and strips HTML comment separators', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-reasoning-markdown',
    );
    sessionState.messages = [
      {
        id: 'assistant-history-reasoning-markdown',
        role: 'assistant',
        content: 'Done.',
        reasoning:
          '**Analyzing the input**\n\n<!-- -->\n\n**Deciding next step**',
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

    const reasoningBody = document.querySelector(
      '.reasoning-block .reasoning-body',
    );
    expect(reasoningBody).toBeTruthy();
    // Bold markdown renders as <strong>, not literal asterisks.
    expect(reasoningBody.querySelector('strong')).toBeTruthy();
    expect(reasoningBody.textContent).not.toContain('**');
    // The provider's `<!-- -->` separator is removed, not escaped into view.
    expect(reasoningBody.textContent).not.toContain('<!--');
    expect(reasoningBody.innerHTML).not.toContain('&lt;!--');
  });
});
