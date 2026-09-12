// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
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

describe('ChatTimeline', () => {
  const suite = setupTimelineToolSuite();

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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    // The disclosure keeps one Rail section per Tool detail category.
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

  describe('compactToolValue', () => {
    it('plain object → inner fields without the outer JSON object wrapper', () => {
      const text = suite.getArgsCodeText(
        { path: 'file.txt', count: 3 },
        'ctv-obj',
      );
      expect(text).toBe('path: file.txt\ncount: 3');
      expect(text).not.toBe('{"path":"file.txt","count":3}');
      expect(text.trim().startsWith('{')).toBe(false);
      expect(text.trim().endsWith('}')).toBe(false);
    });

    it('removes String wrappers without changing scalar values', () => {
      const text = suite.getArgsCodeText(
        {
          stringFalse: 'false',
          booleanFalse: false,
          stringNumber: '3',
          number: 3,
        },
        'ctv-scalar-types',
      );
      expect(text).toBe(
        'stringFalse: false\nbooleanFalse: false\nstringNumber: 3\nnumber: 3',
      );
    });

    it('plain string → returned as-is', () => {
      const text = suite.getArgsCodeText('just a string', 'ctv-str');
      expect(text).toBe('just a string');
    });

    it('null value → returns the no-data placeholder (—)', () => {
      const text = suite.getResultCodeText(null, 'ctv-null');
      // i18n default fallback for chat.toolNoData is "—"
      expect(text).toBe('—');
    });

    it('undefined value (missing result key) → Args with empty object returns the no-data placeholder (—)', () => {
      // undefined is equivalent to an empty value; empty object also fails hasMeaningfulToolDetail
      const text = suite.getArgsCodeText(undefined, 'ctv-undef');
      expect(text).toBe('—');
    });

    it('empty object → returns the no-data placeholder (—)', () => {
      const text = suite.getArgsCodeText({}, 'ctv-empty-obj');
      expect(text).toBe('—');
    });

    it('object with .data field and preferPayload:true → returns inner data fields without outer braces', () => {
      // Result value with a .data field; preferPayload=true (Result row uses it)
      const text = suite.getResultCodeText(
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
      const text = suite.getResultCodeText(
        { ok: true, data: { content: 'file content here' } },
        'ctv-read-content',
        'read',
      );
      expect(text).toBe('file content here');
    });

    it('successful persisted read result with path → displays content and hides path', () => {
      const text = suite.getResultCodeText(
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
      const text = suite.getResultCodeText(
        { error: 'something went wrong' },
        'ctv-error',
      );
      expect(text).toContain('something went wrong');
    });

    it('array → one unwrapped value per line', () => {
      const text = suite.getArgsCodeText([1, 2, 3], 'ctv-array');
      expect(text).toBe('- 1\n- 2\n- 3');
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

    suite.mountedComponent = mount(ChatTimeline, {
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
