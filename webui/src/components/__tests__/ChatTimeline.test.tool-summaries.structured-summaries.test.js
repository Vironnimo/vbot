// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  ChatTimeline,
  setupTimelineToolSuite,
} from './ChatTimeline.tool-summaries.support.js';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
} from '../../lib/chatState.js';

import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  INTENTIONAL_HOVER_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';

describe('ChatTimeline', () => {
  const suite = setupTimelineToolSuite();

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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

  it('compacts a structured read path and exposes a delayed-hover/focus/touch copy card', async () => {
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

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    const value = document.querySelector('.te-primary-value');
    expect(value.textContent).toBe('…/components/chat/ChatAssistantRun.svelte');
    expect(value.getAttribute('tabindex')).toBe('0');
    expect(value.hasAttribute('title')).toBe(false);
    const card = document.querySelector('.tool-primary-hover-card');
    value.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(INTENTIONAL_HOVER_SHOW_DELAY_MS - 1);
    expect(card.dataset.floatingOpen).toBe('false');
    await vi.advanceTimersByTimeAsync(1);
    expect(card.dataset.floatingOpen).toBe('true');

    value.dispatchEvent(new Event('pointerleave'));
    await vi.advanceTimersByTimeAsync(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');

    value.focus();
    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.textContent).toContain(fullPath);
    const copyButton = card.querySelector('button');
    expect(copyButton.getAttribute('aria-label')).toBe('Copy full value');
    copyButton.click();
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith(fullPath);

    value.blur();
    await vi.advanceTimersByTimeAsync(FLOATING_HOVER_CLOSE_DELAY_MS);
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    expect(document.querySelector('.te-fact--added').textContent).toBe('+2');
    expect(document.querySelector('.te-fact--removed').textContent).toBe('-0');
  });
});
