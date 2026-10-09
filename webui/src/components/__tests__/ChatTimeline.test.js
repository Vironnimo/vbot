// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  appendEvents,
  assistantOutput,
  flushAsync,
  flushSync,
  follows,
  openDisclosures,
  setupChatTimelineSuite,
  stubClipboard,
  timelineSession,
} from './ChatTimeline.support.js';
import { loadHistory } from '../../lib/chatState.js';
import { t } from '../../lib/i18n.js';
import {
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';

function sessionWithMessages(messages) {
  const sessionState = timelineSession();
  sessionState.messages = messages;
  return sessionState;
}

function userMessage(content, message = {}) {
  return {
    id: 'user-one',
    role: 'user',
    content,
    timestamp: '2026-05-10T12:00:00Z',
    ...message,
  };
}

function imageBlock(attachmentId, filename, extra = {}) {
  return {
    type: 'media',
    attachment_id: attachmentId,
    filename,
    media_type: 'image/png',
    ...extra,
  };
}

function sessionWithOutput(content) {
  const sessionState = timelineSession();
  appendEvents(sessionState, 'run-output', [assistantOutput(content)]);
  return sessionState;
}

describe('ChatTimeline messages', () => {
  const timeline = setupChatTimelineSuite();

  it('wraps messages in a capped, centered measure column', () => {
    timeline.render(sessionWithMessages([userMessage('Hello')]));

    // `.messages` is the full-width scroll container; `.messages__content` is
    // the column capped and centered through `--chat-measure`.
    const scrollContainer = document.querySelector('.messages');
    const measureColumn = document.querySelector('.messages__content');
    expect(scrollContainer.contains(measureColumn)).toBe(true);
    expect(measureColumn.querySelector('.msg.user')).toBeTruthy();
  });

  it('renders user text as Markdown and autolinks safe URLs', () => {
    timeline.render(
      sessionWithMessages([
        userMessage('**bold** https://example.com/docs.\n<b>raw</b>'),
      ]),
    );

    const userBodyText = document.querySelector('.msg.user .msg-user-text');
    expect(userBodyText.querySelector('strong').textContent).toBe('bold');
    expect(userBodyText.querySelector('b')).toBeNull();
    expect(userBodyText.textContent).toContain('<b>raw</b>');
    const link = userBodyText.querySelector('a');
    expect(link.textContent).toBe('https://example.com/docs');
    expect(link.getAttribute('href')).toBe('https://example.com/docs');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('clamps only a very long user text and expands it on request', () => {
    // jsdom has no layout: a 24 px line (the fallback) makes 2000 px of text
    // far longer than the collapsible bound, 300 px shorter.
    const heights = { long: 2000, short: 300 };
    const scrollHeight = vi
      .spyOn(HTMLElement.prototype, 'scrollHeight', 'get')
      .mockImplementation(function height() {
        return heights[this.textContent.startsWith('long') ? 'long' : 'short'];
      });
    try {
      timeline.render(
        sessionWithMessages([
          userMessage('short text', { id: 'user-short' }),
          userMessage('long text', { id: 'user-long' }),
        ]),
      );

      const [short, long] = document.querySelectorAll('.msg.user');
      expect(short.querySelector('.msg-clamp-toggle')).toBeNull();
      const bubble = long.querySelector('.msg-user-text');
      const toggle = long.querySelector('.msg-clamp-toggle');
      expect(bubble.classList.contains('msg-user-text--clamped')).toBe(true);
      expect(toggle.textContent.trim()).toBe(t('chat.showMore'));
      expect(toggle.getAttribute('aria-expanded')).toBe('false');

      toggle.click();
      flushSync();

      expect(bubble.classList.contains('msg-user-text--clamped')).toBe(false);
      expect(toggle.textContent.trim()).toBe(t('chat.showLess'));
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
    } finally {
      scrollHeight.mockRestore();
    }
  });

  it('renders error History messages with the error label', () => {
    timeline.render(
      sessionWithMessages([
        {
          id: 'error-one',
          role: 'error',
          error_kind: 'rate_limit',
          content: 'Provider rate limit exceeded',
          timestamp: '2026-05-10T12:00:00Z',
        },
      ]),
    );

    const errorMessage = document.querySelector('.msg.error');
    expect(errorMessage.textContent).toContain(
      t('chat.role.error').toUpperCase(),
    );
    expect(errorMessage.textContent).toContain('Provider rate limit exceeded');
  });

  // Each separator is marked by whether it reads Today.
  it.each([
    ['single-day', ['2026-05-10T09:00:00', '2026-05-10T09:01:00'], []],
    [
      'multi-day',
      ['2026-05-10T15:00:00', '2026-05-10T15:01:00', '2026-05-11T08:00:00'],
      [false, true],
    ],
  ])('separates %s History by date', (_case, timestamps, todayFlags) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-11T12:00:00'));
    timeline.render(
      sessionWithMessages(
        timestamps.map((timestamp, index) =>
          userMessage(`Message ${index}`, { id: `user-${index}`, timestamp }),
        ),
      ),
    );

    const labels = Array.from(
      document.querySelectorAll('.date-sep:not(.compaction-sep)'),
    ).map((separator) => separator.textContent.trim() === t('chat.today'));
    expect(labels).toEqual(todayFlags);
  });

  describe('attachments', () => {
    it('renders an image block as an inline link with its image reference', () => {
      timeline.render(
        sessionWithMessages([
          userMessage([
            imageBlock('image-attachment-id', 'diagram.png', {
              image_reference: 1,
            }),
          ]),
        ]),
      );

      const image = document.querySelector('.attachment-thumb');
      expect(image.getAttribute('src')).toBe(
        '/api/attachments/image-attachment-id',
      );
      expect(image.getAttribute('alt')).toBe('diagram.png');
      expect(document.querySelector('.attachment-name').textContent).toBe(
        t('chat.attachment.imageReference', { number: 1 }),
      );
      expect(
        document.querySelector('.inline-attachment').getAttribute('href'),
      ).toBe('/api/attachments/image-attachment-id');
    });

    it.each([
      [
        'a file block as a download link',
        [
          {
            type: 'file',
            attachment_id: 'file-attachment-id',
            filename: 'report.pdf',
            media_type: 'application/pdf',
          },
        ],
        { text: 'report.pdf', fileLink: '/api/attachments/file-attachment-id' },
      ],
      [
        'a text block inline',
        [{ type: 'text', text: 'embedded text file content' }],
        { text: 'embedded text file content' },
      ],
      [
        'text and image blocks together',
        [
          { type: 'text', text: 'note before image' },
          imageBlock('mixed-image-id', 'mixed.png'),
        ],
        { text: 'note before image', image: '/api/attachments/mixed-image-id' },
      ],
      [
        'a plain string without blocks',
        'plain text message',
        { text: 'plain text message', plain: true },
      ],
    ])('renders %s', (_case, content, expected) => {
      timeline.render(sessionWithMessages([userMessage(content)]));

      const message = document.querySelector('.msg.user');
      expect(message.textContent).toContain(expected.text);
      expect(
        message.querySelector('.inline-file-link')?.getAttribute('href'),
      ).toBe(expected.fileLink);
      expect(
        message.querySelector('.attachment-thumb')?.getAttribute('src'),
      ).toBe(expected.image);
      expect(message.querySelector('.msg-body-blocks') === null).toBe(
        Boolean(expected.plain),
      );
    });

    it('previews an image on pointer hover while a tap follows the link', () => {
      vi.useFakeTimers();
      timeline.render(
        sessionWithMessages([
          userMessage([imageBlock('preview-attachment-id', 'diagram.png')]),
        ]),
      );

      const link = document.querySelector('.inline-attachment');
      const preview = document.querySelector('.attachment-hover-preview');
      expect(preview.parentElement).toBe(document.body);
      expect(preview.getAttribute('aria-hidden')).toBe('true');

      const tap = new Event('pointerdown', { bubbles: true });
      Object.defineProperty(tap, 'pointerType', { value: 'touch' });
      link.dispatchEvent(tap);
      vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
      expect(preview.dataset.floatingOpen).toBe('false');

      link.dispatchEvent(new Event('pointerenter'));
      vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
      expect(preview.dataset.floatingOpen).toBe('true');
      expect(preview.getAttribute('aria-hidden')).toBe('true');
      expect(link.hasAttribute('aria-describedby')).toBe(false);
    });

    it('opens the lightbox for a plain click on a thumbnail, not a modifier click', () => {
      timeline.render(
        sessionWithMessages([
          userMessage([imageBlock('attachment-lightbox', 'photo.png')]),
        ]),
      );
      const image = document.querySelector('.attachment-thumb');

      image.dispatchEvent(
        new MouseEvent('click', { bubbles: true, ctrlKey: true }),
      );
      flushSync();
      expect(document.querySelector('.image-lightbox')).toBeNull();

      image.click();
      flushSync();
      expect(
        document.querySelector('.image-lightbox__image').getAttribute('src'),
      ).toBe(image.src);
    });
  });

  describe('Markdown and images', () => {
    it.each([
      [
        'completed output',
        () => sessionWithOutput('**bold**\n\n```\nconst value = 1;\n```'),
        {
          '.assistant-run .msg-markdown strong': 'bold',
          '.assistant-run .msg-markdown pre': 'const value = 1;',
        },
      ],
      [
        'streaming output with an open fence',
        () => {
          const sessionState = timelineSession();
          appendEvents(sessionState, 'run-streaming', [
            {
              type: 'assistant_output_delta',
              payload: {
                content_delta:
                  '**streaming** text\n\n## Title\n\n```js\nconst value = 1;',
              },
            },
          ]);
          return sessionState;
        },
        {
          '.msg-markdown.streaming-text strong': 'streaming',
          '.msg-markdown.streaming-text h2': 'Title',
          '.msg-markdown.streaming-text pre code': 'const value = 1;',
        },
      ],
      [
        'an Assistant History message',
        () =>
          sessionWithMessages([
            {
              id: 'assistant-history-heading',
              role: 'assistant',
              content: '## Title',
              timestamp: '2026-05-10T12:00:00Z',
            },
          ]),
        { '.msg.assistant .msg-markdown h2': 'Title' },
      ],
    ])('renders Markdown in %s', (_case, createSession, expected) => {
      timeline.render(createSession());

      for (const [selector, text] of Object.entries(expected)) {
        expect(document.querySelector(selector).textContent).toContain(text);
      }
    });

    it('autolinks literal URLs in Assistant output', () => {
      timeline.render(sessionWithOutput('Open https://example.com/docs.'));

      const link = document.querySelector('.assistant-run .msg-markdown a');
      expect(link.textContent).toBe('https://example.com/docs');
      expect(link.getAttribute('href')).toBe('https://example.com/docs');
      expect(link.getAttribute('target')).toBe('_blank');
      expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    });

    it('opens a lightbox for a Markdown image and closes it on Escape', () => {
      timeline.render(
        sessionWithOutput('![diagram](https://example.com/diagram.png)'),
      );
      expect(document.querySelector('.image-lightbox')).toBeNull();

      document.querySelector('.assistant-run .msg-markdown img').click();
      flushSync();
      expect(
        document.querySelector('.image-lightbox__image').getAttribute('src'),
      ).toBe('https://example.com/diagram.png');

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      flushSync();
      expect(document.querySelector('.image-lightbox')).toBeNull();
    });

    it('renders signed server file URLs as an image lightbox and download link', () => {
      timeline.render(
        sessionWithOutput(
          '![chart.png](/api/files/image-token)\n\n[report.txt](/api/files/file-token)',
        ),
      );

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

    it.each([
      ['an image larger than the viewport', [4000, 3000], [800, 600], true],
      ['an image that fits the viewport', [200, 150], [1200, 900], false],
    ])(
      'toggles lightbox zoom only for %s',
      (_case, [naturalWidth, naturalHeight], [width, height], zoomable) => {
        timeline.render(
          sessionWithOutput('![diagram](https://example.com/diagram.png)'),
        );
        document.querySelector('.assistant-run .msg-markdown img').click();
        flushSync();
        const overlay = document.querySelector('.image-lightbox');
        const lightboxImage = overlay.querySelector('.image-lightbox__image');
        Object.defineProperty(lightboxImage, 'naturalWidth', {
          value: naturalWidth,
          configurable: true,
        });
        Object.defineProperty(lightboxImage, 'naturalHeight', {
          value: naturalHeight,
          configurable: true,
        });
        window.innerWidth = width;
        window.innerHeight = height;
        window.dispatchEvent(new Event('resize'));
        flushSync();
        expect(lightboxImage.classList.contains('zoomable')).toBe(zoomable);

        lightboxImage.click();
        flushSync();
        expect(overlay.classList.contains('image-lightbox--zoomed')).toBe(
          zoomable,
        );
        expect(lightboxImage.classList.contains('zoomed')).toBe(zoomable);

        lightboxImage.click();
        flushSync();
        expect(overlay.classList.contains('image-lightbox--zoomed')).toBe(
          false,
        );
        // Clicking the image never closes the lightbox.
        expect(document.querySelector('.image-lightbox')).toBeTruthy();
      },
    );
  });

  describe('Thinking', () => {
    it('keeps reasoning-only Assistant History as plain text', () => {
      timeline.render(
        sessionWithMessages([
          {
            id: 'assistant-history-reasoning-only',
            role: 'assistant',
            content: null,
            reasoning: '## Thinking **bold** [link](https://example.com)',
            timestamp: '2026-05-10T12:00:00Z',
          },
        ]),
      );

      const assistant = document.querySelector('.msg.assistant');
      expect(assistant.querySelector('.msg-body-text').textContent).toContain(
        '## Thinking **bold** [link](https://example.com)',
      );
      expect(
        assistant.querySelector('.msg-markdown, h2, strong, a'),
      ).toBeNull();
    });

    it('renders reasoning as Markdown and strips HTML comment separators', () => {
      timeline.render(
        sessionWithMessages([
          {
            id: 'assistant-history-reasoning-markdown',
            role: 'assistant',
            content: 'Done.',
            reasoning:
              '**Analyzing the input**\n\n<!-- -->\n\n**Deciding next step**',
            timestamp: '2026-05-10T12:00:00Z',
          },
        ]),
      );

      expect(document.querySelector('.reasoning-body')).toBeNull();
      openDisclosures('.reasoning-block');
      const reasoningBody = document.querySelector(
        '.reasoning-block .reasoning-body',
      );
      expect(reasoningBody.querySelector('strong')).toBeTruthy();
      expect(reasoningBody.textContent).not.toContain('**');
      // The Provider's `<!-- -->` separator is removed, not escaped into view.
      expect(reasoningBody.textContent).not.toContain('<!--');
      expect(reasoningBody.innerHTML).not.toContain('&lt;!--');
    });

    it('preserves streamed summary sections through batching and stable output', async () => {
      const sessionState = timelineSession('summary-stream');
      const sections = [
        '**Inspecting evidence**\n\nRead the available sources.',
        '**Comparing options**\n\nWeigh the two approaches.',
      ];
      const deltas = sections.flatMap((section, summary_index) =>
        [section.slice(0, 10), section.slice(10)].map((summary_text, part) => ({
          type: 'reasoning_delta',
          payload: {
            reasoning_delta:
              (summary_index > 0 && part === 0 ? '\n\n' : '') + summary_text,
            summary_index,
            summary_text,
          },
        })),
      );
      appendEvents(sessionState, 'summary-run', [
        { type: 'run_started', payload: {} },
        ...deltas,
      ]);
      timeline.render(sessionState);
      openDisclosures('.reasoning-block');
      expect(
        document.querySelectorAll('.reasoning-summary__section'),
      ).toHaveLength(2);
      expect(
        document.querySelector('.reasoning-summary__title').textContent,
      ).toBe('Comparing options');
      expect(
        document.querySelector('.reasoning-summary__count').textContent,
      ).toBe(t('chat.reasoning.sections', { count: 2 }));
      expect(document.querySelector('.reasoning-duration')).toBeNull();

      const message = {
        id: 'summary-message',
        role: 'assistant',
        reasoning: sections.join('\n\n'),
        reasoning_summary: sections,
        content: 'I will check the relevant Tool next.',
        phase: 'commentary',
      };
      appendEvents(
        sessionState,
        'summary-run',
        [
          { type: 'reasoning', payload: { message } },
          { type: 'assistant_output', payload: { message } },
        ],
        deltas.length + 2,
      );
      // Remount as a reconnect would, using the stable Run projection.
      await timeline.unmount();
      timeline.render(sessionState);
      openDisclosures('.reasoning-block');
      expect(document.querySelectorAll('.reasoning-summary')).toHaveLength(1);
      expect(
        document.querySelectorAll('.reasoning-summary__section'),
      ).toHaveLength(2);
      expect(
        document.querySelector('.assistant-run .msg-markdown').textContent,
      ).toContain('I will check');
    });

    it.each([null, 'Final answer.'])(
      'renders saved summary sections with content %s',
      (content) => {
        const sections = [
          '**Comparing options**\n\nA readable summary.',
          'A section without a heading.',
        ];
        timeline.render(
          sessionWithMessages([
            {
              id: 'summary-history-message',
              role: 'assistant',
              content,
              reasoning: sections.join('\n\n'),
              reasoning_summary: sections,
              timestamp: '2026-09-22T12:00:00Z',
            },
          ]),
        );

        expect(document.querySelector('details').open).toBe(false);
        expect(document.querySelector('.reasoning-body')).toBeNull();
        openDisclosures('.reasoning-block');
        expect(
          document.querySelectorAll('.reasoning-summary__section'),
        ).toHaveLength(2);
        expect(document.querySelector('.reasoning-summary__title')).toBeNull();
        expect(
          document.querySelector('summary').getAttribute('aria-label'),
        ).toBe(t('chat.reasoning.summary'));
        expect(
          document.querySelector('.reasoning-summary__section').textContent,
        ).toContain('A readable summary.');
      },
    );

    it('keeps additional raw reasoning visible for compatible Provider mixtures', () => {
      timeline.render(
        sessionWithMessages([
          {
            id: 'mixed',
            role: 'assistant',
            content: 'Answer',
            reasoning:
              '**Summary**\n\nReadable summary. Additional raw reasoning.',
            reasoning_summary: ['**Summary**\n\nReadable summary.'],
            timestamp: '2026-09-22T12:00:00Z',
          },
        ]),
      );

      expect(document.querySelector('.reasoning-summary')).toBeNull();
      openDisclosures('.reasoning-block');
      expect(document.querySelector('.reasoning-body').textContent).toContain(
        'Additional raw reasoning.',
      );
    });
  });

  describe('Compaction', () => {
    const compactedLabel = () =>
      t('chat.compactedWithTokens', { before: '250k', after: '30k' });

    it('renders a live Compaction divider between its surrounding Run output', () => {
      const summaryText =
        '\n# Exact compaction summary\n\n<tag> stays text & *stars* stay literal\n';
      const sessionState = timelineSession();
      appendEvents(sessionState, 'run-compaction', [
        assistantOutput('Before checkpoint', { id: 'assistant-before' }),
        {
          type: 'compaction_started',
          payload: { context_tokens_before: 250_000 },
        },
        {
          type: 'compaction_completed',
          payload: {
            context_tokens_before: 250_000,
            context_tokens_after: 30_000,
            message: {
              id: 'checkpoint-live',
              role: 'compaction_checkpoint',
              content: summaryText,
              timestamp: '2026-07-29T17:55:25Z',
            },
          },
        },
        assistantOutput('After checkpoint', { id: 'assistant-after' }),
      ]);
      timeline.render(sessionState);

      const divider = document.querySelector('.run-compaction-sep');
      const outputs = Array.from(document.querySelectorAll('.msg-markdown'));
      const before = outputs.find((output) =>
        output.textContent.includes('Before checkpoint'),
      );
      const after = outputs.find((output) =>
        output.textContent.includes('After checkpoint'),
      );
      expect(divider.textContent.trim()).toBe(compactedLabel());
      expect(divider.classList.contains('compaction-sep--running')).toBe(false);
      const disclosure = document.querySelector(
        '.compaction-disclosure--in-run',
      );
      expect(disclosure.open).toBe(false);
      expect(disclosure.querySelector('summary')).toBe(divider);
      expect(
        disclosure.querySelector('.compaction-detail__text').textContent,
      ).toBe(summaryText);
      divider.click();
      flushSync();
      expect(disclosure.open).toBe(true);
      expect(follows(before, divider)).toBe(true);
      expect(follows(divider, after)).toBe(true);
    });

    it.each([false, true])(
      'renders Compaction progress or failure (failed=%s)',
      (failed) => {
        const sessionState = timelineSession();
        appendEvents(sessionState, 'run-compaction', [
          {
            type: 'compaction_started',
            payload: { context_tokens_before: 250_000 },
          },
          ...(failed
            ? [{ type: 'compaction_aborted', payload: { reason: 'failed' } }]
            : []),
        ]);
        timeline.render(sessionState);

        const divider = document.querySelector('.compaction-sep');
        expect(divider.getAttribute('role')).toBe(failed ? 'alert' : 'status');
        expect(divider.textContent.trim()).toBeTruthy();
        expect(divider.classList.contains('compaction-sep--running')).toBe(
          !failed,
        );
        expect(divider.getAttribute('aria-busy')).toBe(failed ? null : 'true');
        expect(document.querySelector('.compaction-disclosure')).toBeNull();
        expect(document.querySelector('.assistant-run')).toBeNull();
      },
    );

    it('keeps persisted Compaction token counts after History reload', () => {
      const summaryText =
        'Remember this exactly:\n\n- first fact\n- <literal tag>\n\nTrailing line.';
      const sessionState = timelineSession();
      loadHistory(sessionState, [
        {
          id: 'checkpoint-history',
          role: 'compaction_checkpoint',
          content: summaryText,
          timestamp: '2026-07-29T17:55:25Z',
          usage: {
            compacted_token_count: 220_000,
            context_tokens_before: 250_000,
            context_tokens_after: 30_000,
          },
        },
      ]);
      timeline.render(sessionState);

      const divider = document.querySelector('.compaction-sep');
      expect(divider.textContent.trim()).toBe(compactedLabel());
      expect(divider.tagName).toBe('SUMMARY');
      const disclosure = document.querySelector('.compaction-disclosure');
      expect(disclosure.open).toBe(false);
      divider.click();
      flushSync();
      expect(disclosure.open).toBe(true);
      expect(
        disclosure.querySelector('.compaction-detail__text').textContent,
      ).toBe(summaryText);
    });
  });

  describe('transient cards', () => {
    // Naive-local timestamps keep the chronological comparison
    // timezone-independent.
    it.each([
      ['after the item it followed', { anchorId: 'user-early' }, 'between'],
      [
        'at the end when its anchor item is gone',
        { anchorId: 'item-that-no-longer-exists' },
        'end',
      ],
      // A History reload replaces live Run ids with History ids, so the exact
      // anchor disappears; the creation time keeps the card in place.
      [
        'at its chronological position when its anchor is gone',
        {
          anchorId: 'run-live-that-vanished',
          createdAt: Date.parse('2026-05-10T09:05:00'),
        },
        'between',
      ],
    ])('places a transient card %s', (_case, card, position) => {
      timeline.render(
        sessionWithMessages([
          userMessage('Earlier question', {
            id: 'user-early',
            timestamp: '2026-05-10T09:00:00',
          }),
          userMessage('Later question', {
            id: 'user-late',
            timestamp: '2026-05-10T09:10:00',
          }),
        ]),
        {
          transientCards: [{ id: 'transient', text: 'Status output', ...card }],
        },
      );

      const cardElement = document.querySelector('.transient-card');
      const late = document.querySelector(
        '[data-timeline-item-id="user-late"]',
      );
      expect(cardElement.textContent).toContain('Status output');
      expect(
        follows(
          document.querySelector('[data-timeline-item-id="user-early"]'),
          cardElement,
        ),
      ).toBe(true);
      expect(follows(cardElement, late)).toBe(position === 'between');
    });
  });

  describe('copy and edit actions', () => {
    it.each([
      [
        'verbatim instead of rendered text',
        '**literal markdown**',
        '**literal markdown**',
      ],
      // Copied mentions use the composer's mention form, so pasting resends
      // them; binary attachments are left out.
      [
        'with file mentions and without binary attachments',
        [
          { type: 'text', text: 'Inspect this file' },
          {
            type: 'file',
            attachment_id: 'attachment-1',
            filename: 'archive.zip',
            media_type: 'application/zip',
          },
          { type: 'file_mention', path: 'src/main.js', status: 'inlined' },
          {
            type: 'file_mention',
            path: 'notes/meeting notes.md',
            status: 'inlined',
          },
        ],
        'Inspect this file\n\n@src/main.js\n\n@"notes/meeting notes.md"',
      ],
    ])('copies a User message %s', async (_case, content, copied) => {
      const writeText = stubClipboard();
      timeline.render(sessionWithMessages([userMessage(content)]));

      document.querySelector('.msg.user .message-copy').click();
      await flushAsync();

      expect(writeText).toHaveBeenCalledWith(copied);
    });

    it('copies transient command output independently', async () => {
      const writeText = stubClipboard();
      timeline.render(timelineSession(), {
        transientCards: [
          {
            id: 'command-output-1',
            anchorId: null,
            text: 'model: openai/gpt-5\nstatus: ready',
          },
        ],
      });

      document.querySelector('.transient-card__copy').click();
      await flushAsync();

      expect(writeText).toHaveBeenCalledWith(
        'model: openai/gpt-5\nstatus: ready',
      );
    });

    it('edits only server-approved User messages inline and restarts from them', async () => {
      const onEditMessage = vi.fn().mockResolvedValue(true);
      timeline.render(
        sessionWithMessages([
          userMessage('Channel-originated request', { id: 'user-channel' }),
          userMessage('Original request', { id: 'user-edit', editable: true }),
        ]),
        { onEditMessage },
      );

      const editButtons = document.querySelectorAll('.message-edit');
      expect(editButtons).toHaveLength(1);
      editButtons[0].click();
      await flushAsync();

      const editor = document.querySelector('.message-edit-form textarea');
      expect(editor.value).toBe('Original request');
      editor.value = 'Edited request';
      editor.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('.message-edit-actions .btn-primary').click();
      await flushAsync();

      expect(onEditMessage).toHaveBeenCalledWith('user-edit', 'Edited request');
    });

    it('explains why editable User messages cannot be edited right now', async () => {
      vi.useFakeTimers();
      timeline.render(
        sessionWithMessages([
          userMessage('Original request', { id: 'user-edit', editable: true }),
        ]),
        { messageEditingDisabledReason: t('chat.editUnavailableRunning') },
      );

      const edit = document.querySelector('.message-edit');
      expect(edit.disabled).toBe(true);
      edit.parentElement.dispatchEvent(new Event('pointerenter'));
      await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
      expect(document.getElementById('app-tooltip').textContent).toBe(
        t('chat.editUnavailableRunning'),
      );
    });
  });
});
