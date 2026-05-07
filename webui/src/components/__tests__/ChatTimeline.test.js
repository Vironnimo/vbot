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

  it('prefers description argument over path/command in tool summary', () => {
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
            description: 'checking repo status',
          },
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

    // description key should not appear in the detail panel (it's hidden)
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

    // Args row should contain the compact JSON value inline
    const argsRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    expect(argsRow).toBeTruthy();
    const argsCode = argsRow.querySelector('.teb-code');
    expect(argsCode).toBeTruthy();
    expect(argsCode.textContent).toContain('a.txt');
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

  it('skips empty or whitespace-only description and falls back to per-tool arg', () => {
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
          arguments: { path: 'config.yaml', description: '   ' },
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

  it('shows JSON fallback for tools with non-string argument values', () => {
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
    expect(summaryLine.textContent).toContain('count');
  });
});
