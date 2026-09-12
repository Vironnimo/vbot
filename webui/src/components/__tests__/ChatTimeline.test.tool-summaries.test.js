// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  ChatTimeline,
  setupTimelineToolSuite,
} from './ChatTimeline.tool-summaries.support.js';
import { fromStore, writable } from 'svelte/store';
import { toolDetailImages } from '../../lib/chatToolDetails.js';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
} from '../../lib/chatState.js';

describe('ChatTimeline', () => {
  const suite = setupTimelineToolSuite();

  it.each(['read', 'analyze_image', 'web_fetch'])(
    'shows %s image previews live and after history reload',
    async (name) => {
      const state = ensureSessionState(
        createChatState(),
        'alpha',
        `preview-${name}`,
      );
      const images = [
        {
          attachment_id: 'att_0123456789ab',
          filename: 'first.png',
          media_type: 'image/png',
        },
        {
          attachment_id: 'att_bcdefghjkmnp',
          filename: 'second.png',
          media_type: 'image/png',
        },
      ];
      const toolCall = {
        id: 'image-call',
        name,
        arguments:
          name === 'read'
            ? { path: 'first.png' }
            : { prompt: 'Compare', images: ['first.png', 'second.png'] },
      };
      const result = {
        ok: true,
        error: null,
        data: { content: 'test result' },
        artifacts:
          name === 'web_fetch' ? [{ ...images[0], kind: 'read_media' }] : [],
      };
      const display = {
        version: 1,
        images:
          name !== 'web_fetch'
            ? images.slice(0, name === 'read' ? 1 : 2).map((image, index) => ({
                filename: image.filename,
                url: `/api/files/original${index}.signature`,
              }))
            : [],
      };
      appendRunEvent(state, {
        type: 'tool_call_started',
        run_id: 'image-run',
        sequence: 1,
        payload: { tool_call: toolCall },
      });
      appendRunEvent(state, {
        type: 'tool_call_result',
        run_id: 'image-run',
        sequence: 2,
        payload: { tool_call: toolCall, result, display },
      });
      for (const history of [false, true]) {
        if (history) {
          await unmount(suite.mountedComponent);
          suite.mountedComponent = null;
          document.body.innerHTML = '';
        }
        const sessionState = history
          ? ensureSessionState(createChatState(), 'alpha', `reloaded-${name}`)
          : state;
        if (history)
          sessionState.messages = [
            {
              id: 'assistant-images',
              role: 'assistant',
              content: '',
              timestamp: '2026-09-05T10:00:00Z',
              tool_calls: [toolCall],
            },
            {
              id: 'result-images',
              role: 'tool',
              name,
              tool_call_id: toolCall.id,
              timestamp: '2026-09-05T10:00:01Z',
              content: JSON.stringify(result),
              tool_display: display,
            },
          ];
        suite.mountedComponent = mount(ChatTimeline, {
          target: document.body,
          props: { sessionState, agentName: 'Alpha' },
        });
        flushSync();
        const previews = [...document.querySelectorAll('.tool-image-preview')];
        expect(previews).toHaveLength(name === 'analyze_image' ? 2 : 1);
        const disclosure = previews[0].closest('details');
        expect(disclosure.open).toBe(false);
        disclosure.querySelector('summary').click();
        flushSync();
        expect(disclosure.open).toBe(true);
        const expectedSource =
          name === 'web_fetch'
            ? `/api/attachments/${images[0].attachment_id}`
            : '/api/files/original0.signature';
        expect(previews[0].querySelector('img').getAttribute('src')).toBe(
          expectedSource,
        );
        previews[0].focus();
        previews[0].click(); // Keyboard activation targets the link, not its image.
        flushSync();
        expect(
          document.querySelector('.image-lightbox__image').getAttribute('src'),
        ).toContain(expectedSource);
        document.dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
        );
        flushSync();
        expect(document.querySelector('.image-lightbox')).toBeNull();
        const thumbnail = previews[0].querySelector('img');
        thumbnail.dispatchEvent(new Event('error'));
        expect(thumbnail.hidden).toBe(true);
        expect(thumbnail.nextElementSibling.getAttribute('role')).toBe('img');
        previews[0].click();
        flushSync();
        document
          .querySelector('.image-lightbox__image')
          .dispatchEvent(new Event('error'));
        flushSync();
        expect(document.querySelector('.image-lightbox__image')).toBeNull();
        expect(
          document.querySelector(
            '.image-lightbox .image-unavailable[role="img"]',
          ),
        ).toBeTruthy();
        document.dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
        );
        flushSync();
        expect(document.querySelector('.image-lightbox')).toBeNull();
      }
    },
  );

  it('replaces an unavailable thumbnail when history supplies the current file revision', () => {
    const state = ensureSessionState(createChatState(), 'alpha', 'revisions');
    const result = {
      id: 'read-result',
      role: 'tool',
      name: 'read',
      tool_call_id: 'read-call',
      timestamp: '2026-09-05T10:00:01Z',
      content: JSON.stringify({
        ok: true,
        data: { content: 'loaded' },
        artifacts: [],
      }),
      tool_display: {
        version: 1,
        images: [{ filename: 'front.png', url: '/api/files/first.signature' }],
      },
    };
    state.messages = [
      {
        id: 'assistant',
        role: 'assistant',
        content: '',
        timestamp: '2026-09-05T10:00:00Z',
        tool_calls: [
          { id: 'read-call', name: 'read', arguments: { path: 'front.png' } },
        ],
      },
      result,
    ];
    const session = fromStore(writable(state));
    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        get sessionState() {
          return session.current;
        },
        agentName: 'Alpha',
      },
    });
    flushSync();
    const original = document.querySelector('.tool-image-preview img');
    original.dispatchEvent(new Event('error'));
    expect(original.hidden).toBe(true);
    session.current = {
      ...state,
      messages: [
        state.messages[0],
        {
          ...result,
          tool_display: {
            version: 1,
            images: [
              { filename: 'front.png', url: '/api/files/second.signature' },
            ],
          },
        },
      ],
    };
    flushSync();
    const updated = document.querySelector('.tool-image-preview img');
    expect(updated).not.toBe(original);
    expect(updated.hidden).toBe(false);
    expect(updated.getAttribute('src')).toBe('/api/files/second.signature');
    updated.closest('a').click();
    flushSync();
    expect(
      document.querySelector('.image-lightbox__image').getAttribute('src'),
    ).toContain('/api/files/second.signature');
  });

  it.each(['att_0123456789ab', 'a1234567-1234-4234-8234-123456789abc'])(
    'accepts the opaque Attachment identity %s',
    (id) => {
      expect(
        toolDetailImages(
          {
            artifacts: [
              {
                kind: 'read_media',
                attachment_id: id,
                media_type: 'image/png',
              },
            ],
          },
          { preferPayload: true },
        ),
      ).toEqual([
        { src: `/api/attachments/${id}`, filename: 'Preview attachment' },
      ]);
    },
  );

  it.each([
    '../private',
    'https://example.com/image',
    'att_valid?x=1',
    'att_valid#x',
    'att_valid/other',
    'x'.repeat(129),
  ])('rejects unsafe Attachment identity %s', (id) => {
    expect(
      toolDetailImages(
        {
          artifacts: [
            { kind: 'read_media', attachment_id: id, media_type: 'image/png' },
          ],
        },
        { preferPayload: true },
      ),
    ).toEqual([]);
  });

  it('rejects path and remote URL image candidates and leaves text read results plain', () => {
    const state = ensureSessionState(
      createChatState(),
      'alpha',
      'no-image-preview',
    );
    appendRunEvent(state, {
      type: 'tool_call_result',
      run_id: 'invalid-images',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'invalid-image-call',
          name: 'read',
          arguments: { path: 'notes.txt' },
        },
        result: {
          ok: true,
          data: { content: 'ordinary text' },
          artifacts: [
            {
              kind: 'read_media',
              attachment_id: '../../secret.png',
              media_type: 'image/png',
            },
            {
              kind: 'read_media',
              attachment_id: 'https://example.com/pixel',
              media_type: 'image/png',
            },
            {
              kind: 'read_media',
              attachment_id: 'a1234567-1234-4234-8234-123456789abc',
              media_type: 'text/plain',
            },
          ],
        },
      },
    });
    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState: state, agentName: 'Alpha' },
    });
    flushSync();
    expect(document.querySelector('.tool-image-preview')).toBeNull();
    expect(document.body.textContent).toContain('ordinary text');
  });
});
