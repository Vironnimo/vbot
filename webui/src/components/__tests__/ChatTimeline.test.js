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
  });

  it('renders compact tool details and hides internal result fields', () => {
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
  });
});
