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
            old_string: 'before',
            new_string: 'after',
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
    expect(summaryLine.textContent).not.toContain('old_string');
    expect(summaryLine.textContent).not.toContain('{"old_string":"before"');
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
    const oldContent = 'old generated block\n'.repeat(2000);
    const newContent = 'new generated block\n'.repeat(2000);

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
            new_string: newContent,
            old_string: oldContent,
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
    expect(document.body.textContent).not.toContain(oldContent);
    expect(document.body.textContent).not.toContain(newContent);
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

  it('renders structured values without JSON punctuation', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-structured-description',
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-structured-description',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-structured-description',
          index: 0,
          name: 'grep',
          arguments: {
            pattern: 'VERSION_[A-Z_]+',
            path: 'src',
            description: 'search for all version variables',
          },
        },
        display: {
          version: 1,
          summary: 'search for all version variables',
          hidden_argument_keys: [],
          primary: [
            {
              kind: 'description',
              value: 'search for all version variables',
              full_value: 'search for all version variables',
              truncate: 'end',
              tooltip: 'truncated',
              max_characters: 64,
              quote: true,
            },
          ],
          facts: [],
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-structured-description',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-structured-description',
          index: 0,
          name: 'grep',
        },
        result: { ok: true, data: { content: 'src/version.py:1' } },
        display: {
          version: 1,
          summary: 'search for all version variables',
          hidden_argument_keys: [],
          primary: [
            {
              kind: 'description',
              value: 'search for all version variables',
              full_value: 'search for all version variables',
              truncate: 'end',
              tooltip: 'truncated',
              max_characters: 64,
              quote: true,
            },
          ],
          facts: [
            {
              kind: 'count',
              value: 10,
              unit: 'matches',
              at_least: false,
            },
          ],
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.querySelector('.te-primary-value').textContent).toBe(
      'search for all version variables',
    );
    expect(summaryLine.querySelector('.te-arg-mark')).toBeNull();
    expect(summaryLine.querySelector('.te-primary-quote')).toBeNull();
    expect(summaryLine.querySelector('.te-fact').textContent).toBe(
      '10 matches',
    );
    expect(summaryLine.textContent).not.toContain('VERSION_[A-Z_]+');
    expect(summaryLine.textContent).not.toContain('src');
  });

  it('renders an explicit read line range as a neutral toolbar fact', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-read-line-range',
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-read-line-range',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-read-line-range',
          index: 0,
          name: 'read',
          arguments: { path: 'notes.txt', offset: 170, limit: 111 },
        },
        display: {
          version: 1,
          summary: 'notes.txt',
          hidden_argument_keys: [],
          primary: [],
          facts: [{ kind: 'line_range', start: 170, end: 280 }],
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    const fact = document.querySelector('.tool-event-line .te-fact');
    expect(fact.textContent).toBe('lines 170-280');
    expect(fact.classList.contains('te-fact--added')).toBe(false);
    expect(fact.classList.contains('te-fact--removed')).toBe(false);
  });

  it('renders live edit line changes with semantic colors', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-edit-line-changes',
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-edit-line-changes',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-edit-line-changes',
          index: 0,
          name: 'edit',
          arguments: { path: 'notes.txt' },
        },
        display: {
          version: 1,
          summary: 'notes.txt',
          hidden_argument_keys: [],
          primary: [],
          facts: [],
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-edit-line-changes',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-edit-line-changes',
          index: 0,
          name: 'edit',
        },
        result: { ok: true, data: { message: 'updated' } },
        display: {
          version: 1,
          summary: 'notes.txt',
          hidden_argument_keys: [],
          primary: [],
          facts: [
            { kind: 'line_change', change: 'added', value: 3 },
            { kind: 'line_change', change: 'removed', value: 2 },
          ],
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    expect(
      document.querySelector('.tool-event-line .te-fact--added').textContent,
    ).toBe('+3');
    expect(
      document.querySelector('.tool-event-line .te-fact--removed').textContent,
    ).toBe('-2');
  });

  it('compacts a structured read path and exposes a focus/touch copy card', async () => {
    vi.useFakeTimers();
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-structured-read-path',
    );
    const fullPath =
      'C:/Development/projects/vBot/webui/src/components/chat/ChatAssistantRun.svelte';
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-structured-read-path',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-structured-read-path',
          index: 0,
          name: 'read',
          arguments: { path: fullPath },
        },
        display: {
          version: 1,
          summary: fullPath,
          hidden_argument_keys: [],
          primary: [
            {
              kind: 'path',
              value: fullPath,
              full_value: fullPath,
              truncate: 'start',
              tooltip: 'always',
              max_characters: 64,
              copyable: true,
            },
          ],
          facts: [],
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    const value = document.querySelector('.te-primary-value');
    expect(value.textContent).toBe('…/components/chat/ChatAssistantRun.svelte');
    expect(value.getAttribute('tabindex')).toBe('0');
    expect(value.hasAttribute('title')).toBe(false);
    value.focus();
    const card = document.querySelector('.tool-primary-hover-card');
    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.textContent).toContain(fullPath);
    const copyButton = card.querySelector('button');
    expect(copyButton.getAttribute('aria-label')).toBe('Copy full value');
    copyButton.click();
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith(fullPath);

    value.blur();
    await vi.advanceTimersByTimeAsync(150);
    const touch = new Event('pointerdown', { bubbles: true });
    Object.defineProperty(touch, 'pointerType', { value: 'touch' });
    value.dispatchEvent(touch);
    expect(card.dataset.floatingOpen).toBe('true');
    const secondTouch = new Event('pointerdown', { bubbles: true });
    Object.defineProperty(secondTouch, 'pointerType', { value: 'touch' });
    value.dispatchEvent(secondTouch);
    expect(card.dataset.floatingOpen).toBe('false');
  });

  it('loads the final structured presentation snapshot from History', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-structured-history',
    );
    sessionState.messages = [
      { id: 'user-history', role: 'user', content: 'Search versions' },
      {
        id: 'assistant-history',
        role: 'assistant',
        tool_calls: [
          {
            id: 'call-history',
            name: 'grep',
            arguments: { pattern: 'VERSION', path: 'src' },
          },
        ],
      },
      {
        id: 'tool-history',
        role: 'tool',
        tool_call_id: 'call-history',
        name: 'grep',
        content: '{"ok":true,"data":{"content":"src/a.py:1"}}',
        tool_display: {
          version: 1,
          summary: 'version variables',
          hidden_argument_keys: [],
          primary: [
            {
              kind: 'description',
              value: 'version variables',
              full_value: 'version variables',
              truncate: 'end',
              max_characters: 64,
              quote: true,
            },
          ],
          facts: [{ kind: 'count', value: 3, unit: 'matches', at_least: true }],
        },
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.textContent).toContain('version variables');
    expect(summaryLine.textContent).toContain('3+ matches');
    expect(summaryLine.textContent).not.toContain('VERSION');
  });

  it('loads write line changes including removed zero from History', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-write-line-history',
    );
    sessionState.messages = [
      {
        id: 'assistant-write-history',
        role: 'assistant',
        tool_calls: [
          {
            id: 'call-write-history',
            name: 'write',
            arguments: { path: 'new.txt', content: 'one\ntwo\n' },
          },
        ],
      },
      {
        id: 'tool-write-history',
        role: 'tool',
        tool_call_id: 'call-write-history',
        name: 'write',
        content: '{"ok":true,"data":{"message":"written"}}',
        tool_display: {
          version: 1,
          summary: 'new.txt',
          hidden_argument_keys: ['content'],
          primary: [],
          facts: [
            { kind: 'line_change', change: 'added', value: 2 },
            { kind: 'line_change', change: 'removed', value: 0 },
          ],
        },
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    expect(document.querySelector('.te-fact--added').textContent).toBe('+2');
    expect(document.querySelector('.te-fact--removed').textContent).toBe('-0');
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

  it('keeps long bash command truncation separate from timing', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-long-bash-command',
    );
    const command =
      'powershell -Command "Get-Item C:\\Users\\Viro\\.vbot\\workspace-main\\todo-v2\\* | Select-Object FullName,Length,LastWriteTime"';

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-long-bash-command',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-long-bash-command',
          index: 0,
          name: 'bash',
          arguments: { command },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-long-bash-command',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-long-bash-command',
          index: 0,
          name: 'bash',
        },
        result: {
          ok: true,
          data: { content: 'listed files' },
        },
        timing: {
          duration_ms: 1234,
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
    const argumentValue = summaryLine.querySelector('.te-arg-value');

    expect(argumentValue.textContent).toBe(`${command.slice(0, 63)}…`);
    expect(argumentValue.textContent.length).toBe(64);
    expect(summaryLine.querySelector('.te-arg-mark')).toBeNull();
    expect(summaryLine.querySelector('.te-time').textContent).toContain('1.2s');
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
      expect(text).toBe('path: "file.txt"\ncount: 3');
      expect(text).not.toBe('{"path":"file.txt","count":3}');
      expect(text.trim().startsWith('{')).toBe(false);
      expect(text.trim().endsWith('}')).toBe(false);
    });

    it('nested strings remain visibly distinct from JSON scalars', () => {
      const text = getArgsCodeText(
        {
          stringFalse: 'false',
          booleanFalse: false,
          stringNumber: '3',
          number: 3,
        },
        'ctv-scalar-types',
      );
      expect(text).toBe(
        'stringFalse: "false"\nbooleanFalse: false\nstringNumber: "3"\nnumber: 3',
      );
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
});
