// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  flushSync,
  mount,
  ChatTimeline,
  setupTimelineLayoutSuite,
} from './ChatTimeline.layout-history-attachments.support.js';
import { createChatState, ensureSessionState } from '../../lib/chatState.js';

describe('ChatTimeline', () => {
  const suite = setupTimelineLayoutSuite();

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
            image_reference: 1,
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const image = document.querySelector('.attachment-thumb');
    expect(image).toBeTruthy();
    expect(image.getAttribute('src')).toBe(
      '/api/attachments/image-attachment-id',
    );
    expect(image.getAttribute('alt')).toBe('diagram.png');
    expect(document.querySelector('.attachment-name').textContent).toBe(
      'Image 1',
    );

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

    suite.mountedComponent = mount(ChatTimeline, {
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
    expect(document.querySelector('.attachment-thumb')).toBeNull();
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

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('embedded text file content');
    expect(document.querySelector('.inline-file-link')).toBeNull();
    expect(document.querySelector('.attachment-thumb')).toBeNull();
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

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('note before image');

    const image = document.querySelector('.attachment-thumb');
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

    suite.mountedComponent = mount(ChatTimeline, {
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
    expect(document.querySelector('.attachment-thumb')).toBeNull();
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

    suite.mountedComponent = mount(ChatTimeline, {
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
});
