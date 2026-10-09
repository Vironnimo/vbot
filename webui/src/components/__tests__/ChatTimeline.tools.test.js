// @vitest-environment jsdom
import { describe, expect, it, onTestFinished, vi } from 'vitest';

import {
  appendEvents,
  detailRow,
  detailText,
  flushSync,
  openDisclosures,
  setupChatTimelineSuite,
  stubClipboard,
  timelineSession,
  toolResult,
  toolStarted,
} from './ChatTimeline.support.js';
// After the support module, which points `svelte/store` at its client build.
import { fromStore, writable } from 'svelte/store';
import { toolDetailMedia } from '../../lib/chatToolDetails.js';
import { t } from '../../lib/i18n.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  INTENTIONAL_HOVER_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';

function sessionWithTool(events) {
  const sessionState = timelineSession();
  appendEvents(sessionState, 'run-tool', events);
  return sessionState;
}

function summaryLine() {
  return document.querySelector('.tool-event-line');
}

function structuredDisplay({ primary = [], facts = [], ...display } = {}) {
  return { version: 1, hidden_argument_keys: [], primary, facts, ...display };
}

const noData = () => t('chat.toolNoData');

describe('ChatTimeline Tools', () => {
  const timeline = setupChatTimelineSuite();

  describe('summary line', () => {
    // Each case: Tool name, arguments, started payload, and the texts the
    // summary line `shows` and `hides`; `args` checks the Args detail.
    it.each([
      [
        'a read path',
        'read',
        { path: 'MEMORY.md' },
        {},
        { shows: ['MEMORY.md'], hides: ['{'] },
      ],
      [
        'an edit path without its replacement strings',
        'edit',
        { old_string: 'before', new_string: 'after', path: 'notes/plan.md' },
        {},
        { shows: ['notes/plan.md'], hides: ['before', 'old_string', '{'] },
      ],
      [
        'a write path without its content',
        'write',
        { content: 'draft content', path: 'drafts/output.md' },
        {},
        { shows: ['drafts/output.md'], hides: ['content'] },
      ],
      [
        'a bash command and ignores an unsupported description',
        'bash',
        { command: 'git status', description: 'checking repo status' },
        {},
        {
          shows: ['git status'],
          hides: ['checking repo status'],
          args: { hides: ['description'] },
        },
      ],
      [
        'the backend display summary instead of the command',
        'bash',
        { command: 'git status' },
        {
          display: {
            summary: 'checking repo status',
            hidden_argument_keys: [],
          },
        },
        {
          shows: ['checking repo status'],
          hides: ['git status'],
          args: { shows: ['git status'] },
        },
      ],
      [
        'the per-Tool argument after an empty backend summary',
        'read',
        { path: 'config.yaml' },
        { display: { summary: '   ', hidden_argument_keys: [] } },
        { shows: ['config.yaml'], hides: ['{'] },
      ],

      [
        'no empty object arguments',
        'status',
        {},
        { display: { summary: '', hidden_argument_keys: [] } },
        { hides: ['{'] },
      ],
      [
        'a glob pattern without its path or description',
        'glob',
        { pattern: '**/*.md', path: 'docs', description: 'model glob label' },
        {},
        { shows: ['**/*.md'], hides: ['model glob label', 'docs'] },
      ],
      [
        'a grep pattern with its path',
        'grep',
        { pattern: 'TODO', path: 'src', description: 'model grep label' },
        {},
        { shows: ['TODO · src'], hides: ['model grep label'] },
      ],
      [
        'no argument summary for an unknown Tool',
        'compute',
        { target: 'build', count: 5, active: true },
        {},
        {
          hides: ['build', 'count'],
          args: { shows: ['target: build', 'count: 5'] },
        },
      ],
    ])(
      'shows %s',
      (
        _case,
        name,
        args,
        payload,
        { shows = [], hides = [], args: detail },
      ) => {
        timeline.render(
          sessionWithTool([toolStarted('call', name, args, payload)]),
        );

        const line = summaryLine();
        expect(line.querySelector('.te-fn').textContent).toBe(name);
        const lineText = line.textContent;
        for (const text of shows) expect(lineText).toContain(text);
        for (const text of hides) expect(lineText).not.toContain(text);
        if (detail) openDisclosures('.tool-event');
        const argsText = detailText('chat.toolArgs');
        for (const text of detail?.shows ?? [])
          expect(argsText).toContain(text);
        for (const text of detail?.hides ?? []) {
          expect(argsText).not.toContain(text);
        }
      },
    );

    it('keeps long bash command truncation separate from timing', () => {
      const command =
        'powershell -Command "Get-Item C:\\Users\\Viro\\.vbot\\workspace-main\\todo-v2\\* | Select-Object FullName,Length,LastWriteTime"';
      timeline.render(
        sessionWithTool([
          toolStarted('call-long', 'bash', { command }),
          toolResult(
            'call-long',
            'bash',
            { ok: true, data: { content: 'listed files' } },
            { timing: { duration_ms: 1234 } },
          ),
        ]),
      );

      const argumentValue = summaryLine().querySelector('.te-arg-value');
      expect(argumentValue.textContent).toBe(`${command.slice(0, 63)}…`);
      expect(summaryLine().querySelector('.te-arg-mark')).toBeNull();
      expect(summaryLine().querySelector('.te-time').textContent).toContain(
        '1.2s',
      );
    });
  });

  describe('details', () => {
    it.each([
      [
        'a plain object as fields',
        { path: 'file.txt', count: 3 },
        'path: file.txt\ncount: 3',
      ],
      [
        'scalar values without String wrappers',
        {
          stringFalse: 'false',
          booleanFalse: false,
          stringNumber: '3',
          number: 3,
        },
        'stringFalse: false\nbooleanFalse: false\nstringNumber: 3\nnumber: 3',
      ],
      ['a plain string as-is', 'just a string', 'just a string'],
      ['an array as one value per line', [1, 2, 3], '- 1\n- 2\n- 3'],
      ['missing arguments as no data', undefined, null],
      ['an empty object as no data', {}, null],
    ])('renders Args %s', (_case, args, expected) => {
      timeline.render(sessionWithTool([toolStarted('call', 'probe', args)]));
      openDisclosures('.tool-event');

      expect(detailText('chat.toolArgs')).toBe(expected ?? noData());
    });

    it.each([
      ['a missing Result as no data', 'probe', null, null],
      [
        'payload data as fields without internal artifacts',
        'probe',
        {
          ok: true,
          data: { content: 'hello', lines: 2 },
          artifacts: { stdout_path: '/tmp/internal.json' },
        },
        'content: hello\nlines: 2',
      ],
      [
        'content-only read data directly',
        'read',
        { ok: true, data: { content: 'file content here' } },
        'file content here',
      ],
      [
        'persisted read data without its path',
        'read',
        { ok: true, data: { path: 'MEMORY.md', content: 'persisted content' } },
        'persisted content',
      ],
      [
        'multi-line glob content directly',
        'glob',
        { ok: true, data: { content: 'README.md\nplans/current.md' } },
        'README.md\nplans/current.md',
      ],
      [
        'an error field',
        'probe',
        { error: 'something went wrong' },
        'something went wrong',
      ],
    ])('renders the Result of %s', (_case, name, result, expected) => {
      timeline.render(
        sessionWithTool([
          toolStarted('call', name, {}),
          toolResult('call', name, result),
        ]),
      );

      openDisclosures('.tool-event');
      expect(detailText('chat.toolResultLabel')).toBe(expected ?? noData());
    });

    it('marks a failed Result envelope as an error', () => {
      timeline.render(
        sessionWithTool([
          toolStarted('call-grep', 'grep', { pattern: 'TODO', path: 'src' }),
          toolResult('call-grep', 'grep', {
            ok: false,
            error: {
              code: 'invalid_regex',
              message: 'Invalid regular expression',
            },
          }),
        ]),
      );

      expect(summaryLine().querySelector('.te-dot.error')).toBeTruthy();
      openDisclosures('.tool-event');
      const resultCode = document.querySelector('.teb-code.error');
      expect(resultCode.textContent).toContain('invalid_regex');
      expect(resultCode.textContent).toContain('Invalid regular expression');
    });

    it("shows a Tool's detail blocks and keeps its raw call behind a disclosure", () => {
      const display = structuredDisplay({
        hidden_argument_keys: ['patch'],
        details: [
          {
            type: 'file_changes',
            files: [
              {
                path: 'src/app.py',
                change: 'updated',
                added: 1,
                removed: 1,
                hunks: [
                  {
                    old_start: 4,
                    new_start: 4,
                    lines: [' a', '-b', '+B', ' c'],
                  },
                  { old_start: 20, new_start: 20, lines: ['+tail'] },
                ],
                omitted_lines: 7,
              },
              {
                path: 'logo.png',
                change: 'moved',
                destination: 'img/logo.png',
                added: 0,
                removed: 0,
                hunks: [],
                binary: true,
              },
            ],
          },
          {
            type: 'notice',
            level: 'warning',
            text: 'Syntax error at line 5.',
            subject: 'src/app.py',
          },
          { type: 'notice', level: 'error', text: 'Text not found.' },
          { type: 'notice', level: 'unknown', text: 'Dropped.' },
        ],
      });
      timeline.render(
        sessionWithTool([
          toolStarted('call', 'apply_patch', { patch: '*** Begin Patch' }),
          toolResult(
            'call',
            'apply_patch',
            { ok: true, data: { status: 'partial', content: 'Updated.' } },
            { display },
          ),
        ]),
      );

      openDisclosures('.tool-event');
      const files =
        detailRow('chat.toolChanges').querySelectorAll('.tool-diff-file');
      expect(files[0].querySelector('.tool-diff-file__path').textContent).toBe(
        'src/app.py',
      );
      const rows = Array.from(
        files[0].querySelectorAll('.tool-diff-line'),
        (row) => [
          row.className.replace('tool-diff-line tool-diff-line--', ''),
          row.querySelector('.tool-diff-line__number').textContent,
          row.querySelector('.tool-diff-line__text').textContent,
        ],
      );
      expect(rows).toEqual([
        ['context', '4', 'a'],
        ['removed', '5', 'b'],
        ['added', '5', 'B'],
        ['context', '6', 'c'],
        ['gap', '', '⋯'],
        ['added', '20', 'tail'],
      ]);
      expect(files[0].textContent).toContain(
        t('chat.fileChange.omitted', { count: 7 }),
      );
      expect(files[1].textContent).toContain('logo.png → img/logo.png');
      expect(files[1].textContent).toContain(t('chat.fileChange.binary'));
      expect(
        Array.from(document.querySelectorAll('.tool-notice'), (notice) => [
          notice.className.replace(/.*tool-notice--/, ''),
          notice.textContent.replace(/\s+/g, ' ').trim(),
        ]),
      ).toEqual([
        [
          'warning',
          `${t('chat.toolNotice.warning')} src/app.py Syntax error at line 5.`,
        ],
        ['error', `${t('chat.toolNotice.error')} Text not found.`],
      ]);

      const rawCall = document.querySelector('.tool-raw-call');
      expect(rawCall.open).toBe(false);
      expect(rawCall.querySelector('summary').textContent.trim()).toBe(
        t('chat.toolRawCall'),
      );
      expect(rawCall.querySelector('.tool-raw-call__body')).toBeNull();
      openDisclosures('.tool-raw-call');
      expect(detailText('chat.toolArgs')).toBe('patch: *** Begin Patch');
      expect(detailText('chat.toolResultLabel')).toBe(
        'status: partial\ncontent: Updated.',
      );
      expect(rawCall.contains(detailRow('chat.toolResultLabel'))).toBe(true);
    });

    it('lists the matches of a results block with their local time', () => {
      const display = structuredDisplay({
        details: [
          {
            type: 'results',
            items: [
              {
                title: 'Release planning',
                meta: 'User',
                time: '2026-09-30T12:00:00Z',
                text: 'Ship on Monday.',
              },
              { title: '' },
              { title: 'Untitled conversation' },
              {
                title: 'Example Domain',
                url: 'https://example.com/',
                meta: 'example.com',
              },
              { title: 'Not a link', url: 'javascript:alert(1)' },
            ],
          },
          { type: 'results', items: [{ meta: 'no title' }] },
        ],
      });
      timeline.render(
        sessionWithTool([
          toolStarted('call', 'session_search', { query: 'ship' }),
          toolResult('call', 'session_search', '{"ok": true}', { display }),
        ]),
      );

      openDisclosures('.tool-event');
      const lists = document.querySelectorAll('.tool-results');
      expect(lists).toHaveLength(1);
      const items = Array.from(
        lists[0].querySelectorAll('.tool-results__item'),
        (item) => item.textContent.replace(/\s+/g, ' ').trim(),
      );
      expect(items).toHaveLength(4);
      expect(items[0]).toContain('Release planning User');
      expect(items[0]).toContain('Ship on Monday.');
      expect(lists[0].querySelector('time')?.getAttribute('datetime')).toBe(
        '2026-09-30T12:00:00Z',
      );
      expect(items[1]).toBe('Untitled conversation');
      // Only a web address turns the title into a link that opens elsewhere.
      const links = lists[0].querySelectorAll('a.tool-results__title');
      expect(Array.from(links, (link) => link.getAttribute('href'))).toEqual([
        'https://example.com/',
      ]);
      expect(links[0].getAttribute('target')).toBe('_blank');
      expect(items[3]).toBe('Not a link');
    });

    it('shows changed Memory entries with their scope and revision', () => {
      const display = structuredDisplay({
        details: [
          {
            type: 'memory_changes',
            scope: 'user',
            revision: 12,
            changes: [
              {
                op: 'replaced',
                previous: 'Works in UTC+2.',
                text: 'Works in UTC+1.',
              },
              { op: 'added', text: 'Prefers German.' },
              { op: 'replaced', text: 'No previous text.' },
            ],
          },
          { type: 'memory_changes', scope: 'other', changes: [] },
        ],
      });
      timeline.render(
        sessionWithTool([
          toolStarted('call', 'memory', { action: 'replace' }),
          toolResult('call', 'memory', '{"ok": true}', { display }),
        ]),
      );

      openDisclosures('.tool-event');
      const sections = document.querySelectorAll('.tool-memory');
      expect(sections).toHaveLength(1);
      expect(sections[0].querySelector('.teb-label').textContent).toBe(
        t('chat.memoryScope.user'),
      );
      expect(
        sections[0].querySelector('.tool-memory__revision').textContent,
      ).toBe(t('chat.memoryRevision', { revision: 12 }));
      expect(
        Array.from(
          sections[0].querySelectorAll('.tool-memory__change'),
          (row) => [
            row.className.replace(/.*tool-memory__change--/, ''),
            row.querySelector('.tool-memory__text').textContent,
          ],
        ),
      ).toEqual([
        ['removed', 'Works in UTC+2.'],
        ['added', 'Works in UTC+1.'],
        ['added', 'Prefers German.'],
      ]);
    });

    it('shows text blocks as given or read from the call, as literal text', () => {
      const display = structuredDisplay({
        details: [
          { type: 'text', label: 'command', text: 'npm test' },
          {
            type: 'text',
            label: 'output',
            source: { from: 'result', path: ['data', 'output'] },
          },
          {
            type: 'text',
            label: 'query',
            source: { from: 'arguments', path: ['missing'] },
          },
          { type: 'text', label: 'results', text: 'a.py:1:needle' },
          { type: 'text', label: 'page', text: '# Example Domain' },
          { type: 'text', label: 'input', text: 'y <enter>' },
          { type: 'text', label: 'scrollback', text: 'line-0' },
          { type: 'text', label: 'screen', text: 'ready> ' },
          { type: 'text', label: 'task', text: 'Check links' },
          { type: 'text', label: 'content', text: '# Debugging' },
          { type: 'text', label: 'response', text: 'All links work.' },
        ],
      });
      timeline.render(
        sessionWithTool([
          toolStarted('call', 'bash', { command: 'npm test' }),
          toolResult(
            'call',
            'bash',
            JSON.stringify({ ok: true, data: { output: '{"passed": 3}' } }),
            { display },
          ),
        ]),
      );

      openDisclosures('.tool-event');
      expect(detailText('chat.toolDetailLabel.command')).toBe('npm test');
      expect(detailText('chat.toolDetailLabel.output')).toBe('{"passed": 3}');
      expect(detailRow('chat.toolDetailLabel.query')).toBeNull();
      expect(detailText('chat.toolDetailLabel.results')).toBe('a.py:1:needle');
      expect(detailText('chat.toolDetailLabel.page')).toBe('# Example Domain');
      expect(detailText('chat.toolDetailLabel.input')).toBe('y <enter>');
      expect(detailText('chat.toolDetailLabel.scrollback')).toBe('line-0');
      expect(detailText('chat.toolDetailLabel.screen')).toBe('ready> ');
      expect(detailText('chat.toolDetailLabel.task')).toBe('Check links');
      expect(detailText('chat.toolDetailLabel.content')).toBe('# Debugging');
      expect(detailText('chat.toolDetailLabel.response')).toBe(
        'All links work.',
      );
      expect(document.querySelector('.tool-raw-call')).not.toBeNull();
    });

    it.each([
      [
        'write content',
        'write',
        (large) => ({ content: large, path: 'todo-app/style.css' }),
        { shows: ['path: todo-app/style.css'], hides: ['content'] },
      ],
      [
        'write content without a path',
        'write',
        (large) => JSON.stringify({ content: large }),
        { shows: [noData()], hides: ['content'] },
      ],
      [
        'edit replacement strings',
        'edit',
        (large) => ({
          new_string: `new ${large}`,
          old_string: `old ${large}`,
          path: 'todo-app/app.js',
          replace_all: true,
        }),
        {
          shows: ['path: todo-app/app.js', 'replace_all: true'],
          hides: ['old_string', 'new_string'],
        },
      ],
    ])('omits large %s from the Tool row', (_case, name, createArgs, args) => {
      const large = '<main>large generated document</main>\n'.repeat(2000);
      timeline.render(
        sessionWithTool([toolStarted('call', name, createArgs(large))]),
      );

      openDisclosures('.tool-event');
      const argsText = detailText('chat.toolArgs');
      for (const text of args.shows) expect(argsText).toContain(text);
      for (const text of args.hides) expect(argsText).not.toContain(text);
      expect(summaryLine().textContent).not.toContain('<main>');
      expect(document.body.textContent).not.toContain('large generated');
    });
  });

  describe('structured display', () => {
    const describedGrep = (facts) =>
      structuredDisplay({
        summary: 'search for all version variables',
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
        facts,
      });

    it('renders structured values without JSON punctuation', () => {
      timeline.render(
        sessionWithTool([
          toolStarted(
            'call-grep',
            'grep',
            { pattern: 'VERSION_[A-Z_]+', path: 'src' },
            { display: describedGrep([]) },
          ),
          toolResult(
            'call-grep',
            'grep',
            { ok: true, data: { content: 'src/version.py:1' } },
            {
              display: describedGrep([
                { kind: 'count', value: 10, unit: 'matches', at_least: false },
              ]),
            },
          ),
        ]),
      );

      const line = summaryLine();
      expect(line.querySelector('.te-primary-value').textContent).toBe(
        'search for all version variables',
      );
      expect(line.querySelector('.te-arg-mark, .te-primary-quote')).toBeNull();
      expect(line.querySelector('.te-fact').textContent).toBe(
        t('chat.toolFact.matches', { count: 10 }),
      );
      expect(line.textContent).not.toContain('VERSION_[A-Z_]+');
      expect(line.textContent).not.toContain('src');
    });

    it.each([
      [
        'an explicit read line range as a neutral fact',
        [{ kind: 'line_range', start: 170, end: 280 }],
        [
          [
            t('chat.toolFact.lines', {
              start: 170,
              end: 280,
            }),
            null,
          ],
        ],
      ],
      [
        'edit line changes with semantic colors',
        [
          { kind: 'line_change', change: 'added', value: 3 },
          { kind: 'line_change', change: 'removed', value: 2 },
        ],
        [
          ['+3', 'added'],
          ['-2', 'removed'],
        ],
      ],
    ])('renders %s', (_case, facts, expected) => {
      timeline.render(
        sessionWithTool([
          toolStarted(
            'call',
            'read',
            { path: 'notes.txt' },
            { display: structuredDisplay({ summary: 'notes.txt', facts }) },
          ),
        ]),
      );

      expect(
        Array.from(summaryLine().querySelectorAll('.te-fact')).map((fact) => [
          fact.textContent,
          ['added', 'removed'].find((variant) =>
            fact.classList.contains(`te-fact--${variant}`),
          ) ?? null,
        ]),
      ).toEqual(expected);
    });

    it('loads the final structured presentation snapshot from History', () => {
      const sessionState = timelineSession();
      sessionState.messages = [
        { id: 'user-history', role: 'user', content: 'Search versions' },
        {
          id: 'assistant-history',
          role: 'assistant',
          tool_calls: [
            {
              id: 'call-grep',
              name: 'grep',
              arguments: { pattern: 'VERSION', path: 'src' },
            },
            {
              id: 'call-write',
              name: 'write',
              arguments: { path: 'new.txt', content: 'one\ntwo\n' },
            },
          ],
        },
        {
          id: 'tool-grep',
          role: 'tool',
          tool_call_id: 'call-grep',
          name: 'grep',
          content: '{"ok":true,"data":{"content":"src/a.py:1"}}',
          tool_display: structuredDisplay({
            summary: 'version variables',
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
            facts: [
              { kind: 'count', value: 3, unit: 'matches', at_least: true },
            ],
          }),
        },
        {
          id: 'tool-write',
          role: 'tool',
          tool_call_id: 'call-write',
          name: 'write',
          content: '{"ok":true,"data":{"message":"written"}}',
          tool_display: structuredDisplay({
            summary: 'new.txt',
            hidden_argument_keys: ['content'],
            facts: [
              { kind: 'line_change', change: 'added', value: 2 },
              { kind: 'line_change', change: 'removed', value: 0 },
            ],
          }),
        },
      ];
      timeline.render(sessionState);

      const [grepLine, writeLine] =
        document.querySelectorAll('.tool-event-line');
      expect(grepLine.textContent).toContain('version variables');
      expect(grepLine.textContent).toContain('3+ matches');
      expect(grepLine.textContent).not.toContain('VERSION');
      expect(writeLine.querySelector('.te-fact--added').textContent).toBe('+2');
      expect(writeLine.querySelector('.te-fact--removed').textContent).toBe(
        '-0',
      );
    });

    it('compacts a structured read path and exposes a delayed-hover/focus/touch copy card', async () => {
      vi.useFakeTimers();
      const writeText = stubClipboard();
      const fullPath =
        'C:/Development/projects/vBot/webui/src/components/chat/ChatAssistantRun.svelte';
      timeline.render(
        sessionWithTool([
          toolStarted(
            'call-read',
            'read',
            { path: fullPath },
            {
              display: structuredDisplay({
                summary: fullPath,
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
              }),
            },
          ),
        ]),
      );

      const value = document.querySelector('.te-primary-value');
      expect(value.textContent).toBe(
        '…/components/chat/ChatAssistantRun.svelte',
      );
      expect(value.getAttribute('tabindex')).toBe('0');
      expect(value.hasAttribute('title')).toBe(false);
      const card = document.querySelector('.copyable-value-card');
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
      expect(copyButton.getAttribute('aria-label')).toBe(t('chat.copyPath'));
      copyButton.click();
      await Promise.resolve();
      expect(writeText).toHaveBeenCalledWith(fullPath);

      value.blur();
      await vi.advanceTimersByTimeAsync(FLOATING_HOVER_CLOSE_DELAY_MS);
      const touch = () => {
        const event = new Event('pointerdown', { bubbles: true });
        Object.defineProperty(event, 'pointerType', { value: 'touch' });
        value.dispatchEvent(event);
      };
      touch();
      expect(card.dataset.floatingOpen).toBe('true');
      touch();
      expect(card.dataset.floatingOpen).toBe('false');
    });
  });

  describe('media', () => {
    it('shows the source images, then the media, with players for video and audio', () => {
      const display = structuredDisplay({
        details: [],
        media: [
          {
            url: '/api/files/frame.token',
            filename: 'frame.png',
            kind: 'image',
            role: 'source',
          },
          { url: '/api/files/clip.token', filename: 'clip.mp4', kind: 'video' },
          { url: '/api/files/cat.token', filename: 'cat.png', kind: 'image' },
          {
            url: 'https://example.com/x.png',
            filename: 'remote.png',
            kind: 'image',
          },
          {
            url: '/api/files/doc.token',
            filename: 'doc.pdf',
            kind: 'document',
          },
        ],
      });
      const result = {
        ok: true,
        data: {},
        artifacts: [
          {
            kind: 'read_media',
            attachment_id: 'att_0123456789ab',
            filename: 'song.mp3',
            media_type: 'audio/mpeg',
          },
        ],
      };
      // jsdom has no media playback.
      const playback = ['pause', 'load'].map((method) =>
        vi
          .spyOn(HTMLMediaElement.prototype, method)
          .mockImplementation(() => {}),
      );
      onTestFinished(() => playback.forEach((spy) => spy.mockRestore()));
      timeline.render(
        sessionWithTool([
          toolStarted('call', 'generate_video', { prompt: 'a cat' }),
          toolResult('call', 'generate_video', result, { display }),
        ]),
      );

      expect(document.querySelector('.tool-media')).toBeNull();
      openDisclosures('.tool-event');
      const [sources, media] = document.querySelectorAll('.tool-media');
      expect(sources.querySelector('.teb-label').textContent).toBe(
        t('chat.toolMediaSources'),
      );
      expect(
        Array.from(sources.querySelectorAll('.tool-image-preview img'), (img) =>
          img.getAttribute('src'),
        ),
      ).toEqual(['/api/files/frame.token']);
      expect(media.querySelector('.teb-label').textContent).toBe(
        t('chat.toolMedia'),
      );
      expect(
        Array.from(media.querySelectorAll('.tool-image-preview img'), (img) =>
          img.getAttribute('src'),
        ),
      ).toEqual(['/api/files/cat.token']);
      expect(media.querySelector('video').getAttribute('src')).toBe(
        '/api/files/clip.token',
      );
      // Audio plays in the same player as spoken replies.
      expect(media.querySelector('.audio-player')).toBeTruthy();
      expect(media.textContent).toContain('song.mp3');
      // Only signed vBot files of a playable kind reach the page.
      expect(document.body.textContent).not.toContain('remote.png');
      expect(document.body.textContent).not.toContain('doc.pdf');
      expect(document.querySelector('.tool-raw-call').open).toBe(false);
    });

    it.each(['read', 'analyze_image', 'web_fetch'])(
      'shows %s image previews live and after History reload',
      async (name) => {
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
          media:
            name !== 'web_fetch'
              ? images
                  .slice(0, name === 'read' ? 1 : 2)
                  .map((image, index) => ({
                    filename: image.filename,
                    url: `/api/files/original${index}.signature`,
                    kind: 'image',
                  }))
              : [],
        };
        const live = timelineSession(`preview-${name}`);
        appendEvents(live, 'image-run', [
          { type: 'tool_call_started', payload: { tool_call: toolCall } },
          {
            type: 'tool_call_result',
            payload: { tool_call: toolCall, result, display },
          },
        ]);
        const reloaded = timelineSession(`reloaded-${name}`);
        reloaded.messages = [
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
        const expectedSource =
          name === 'web_fetch'
            ? `/api/attachments/${images[0].attachment_id}`
            : '/api/files/original0.signature';
        const pressEscape = () => {
          document.dispatchEvent(
            new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
          );
          flushSync();
          expect(document.querySelector('.image-lightbox')).toBeNull();
        };

        for (const sessionState of [live, reloaded]) {
          await timeline.unmount();
          document.body.innerHTML = '';
          timeline.render(sessionState);
          expect(document.querySelector('.tool-image-preview')).toBeNull();
          openDisclosures('.tool-event');
          const previews = [
            ...document.querySelectorAll('.tool-image-preview'),
          ];
          expect(previews).toHaveLength(name === 'analyze_image' ? 2 : 1);
          const disclosure = previews[0].closest('details');
          expect(disclosure.open).toBe(true);
          expect(previews[0].querySelector('img').getAttribute('src')).toBe(
            expectedSource,
          );
          previews[0].focus();
          previews[0].click(); // Keyboard activation targets the link, not its image.
          flushSync();
          expect(
            document
              .querySelector('.image-lightbox__image')
              .getAttribute('src'),
          ).toContain(expectedSource);
          pressEscape();

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
          pressEscape();
        }
      },
    );

    it('replaces an unavailable thumbnail when History supplies the current file revision', () => {
      const state = timelineSession('revisions');
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
          media: [
            {
              filename: 'front.png',
              url: '/api/files/first.signature',
              kind: 'image',
            },
          ],
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
      timeline.mount({
        get sessionState() {
          return session.current;
        },
        agentName: 'Alpha',
      });
      openDisclosures('.tool-event');
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
              media: [
                {
                  filename: 'front.png',
                  url: '/api/files/second.signature',
                  kind: 'image',
                },
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

    it.each([
      ['att_0123456789ab', true],
      ['a1234567-1234-4234-8234-123456789abc', true],
      ['../private', false],
      ['https://example.com/image', false],
      ['att_valid?x=1', false],
      ['att_valid#x', false],
      ['att_valid/other', false],
      ['x'.repeat(129), false],
    ])('accepts only an opaque Attachment identity: %s', (id, accepted) => {
      expect(
        toolDetailMedia(null, {
          artifacts: [
            {
              kind: 'read_media',
              attachment_id: id,
              media_type: 'image/png',
            },
          ],
        }),
      ).toEqual(
        accepted
          ? [
              {
                kind: 'image',
                src: `/api/attachments/${id}`,
                filename: t('chat.attachment.preview'),
                source: false,
              },
            ]
          : [],
      );
    });

    it('rejects path, remote URL, and non-image candidates and leaves text read results plain', () => {
      timeline.render(
        sessionWithTool([
          toolResult('invalid-image-call', 'read', {
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
          }),
        ]),
      );

      openDisclosures('.tool-event');
      expect(document.querySelector('.tool-image-preview')).toBeNull();
      expect(document.body.textContent).toContain('ordinary text');
    });
  });
});
