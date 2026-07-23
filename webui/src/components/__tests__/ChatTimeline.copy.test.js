// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { createChatState, ensureSessionState } from '../../lib/chatState.js';
import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');

describe('ChatTimeline copy actions', () => {
  let mountedComponent;
  let writeText;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('copies a user message verbatim instead of rendered text', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-copy',
    );
    sessionState.messages = [
      {
        id: 'user-copy',
        role: 'user',
        content: '**literal markdown**',
        timestamp: '2026-07-23T12:00:00Z',
      },
    ];

    mountTimeline({ sessionState });
    document.querySelector('.msg.user .message-copy').click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('**literal markdown**');
  });

  it('preserves file mentions and omits binary attachments from user copy', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-block-copy',
    );
    sessionState.messages = [
      {
        id: 'user-block-copy',
        role: 'user',
        content: [
          { type: 'text', text: 'Inspect this file' },
          {
            type: 'file',
            attachment_id: 'attachment-1',
            filename: 'archive.zip',
            media_type: 'application/zip',
          },
          {
            type: 'file_mention',
            path: 'src/main.js',
            status: 'inlined',
          },
        ],
        timestamp: '2026-07-23T12:00:00Z',
      },
    ];

    mountTimeline({ sessionState });
    document.querySelector('.msg.user .message-copy').click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('Inspect this file\n\n@src/main.js');
    expect(writeText.mock.calls[0][0]).not.toContain('archive.zip');
  });

  it('copies transient command output independently', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-command-copy',
    );

    mountTimeline({
      sessionState,
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

  function mountTimeline(props) {
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { agentName: 'Alpha', ...props },
    });
    flushSync();
  }
});

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}
