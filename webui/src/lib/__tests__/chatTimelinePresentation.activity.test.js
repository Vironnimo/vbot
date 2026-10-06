import { describe, expect, it } from 'vitest';
import {
  activitySections,
  backgroundCommandRowState,
  backgroundCommandStatusDetails,
  backgroundCommandToolStatusLabel,
  backgroundTasks,
  changeStatsLabel,
  changeStatsParts,
  changedFilesCard,
  formatTime,
  isReflectionRunKind,
  isRowCancellable,
  isRunChildWorking,
  liveClockCadenceMs,
  reasoningDurationLabel,
  reflectionElapsedLabel,
  reflectionTaskRows,
  reflectionUndoErrorText,
  runChangeStats,
  runFooterDetails,
  runFooterNotice,
  runFooterParts,
  sessionChangeStats,
  visibleRunChildren,
} from '../chatTimelinePresentation.js';
import { t } from '../i18n.js';
import { formatAbsoluteTime, formatMoment } from '../timeText.js';
import { backgroundCommandTool } from './chatTimelinePresentation.support.js';

const status = (name) => t(`chat.runStatus.${name}`);
const seconds = (value) => t('chat.durationSeconds', { seconds: value });
const iterations = (count) => t('chat.runIterations', { count });
const minutesSeconds = (minutes, secs) =>
  t('chat.durationMinutesSeconds', { minutes, seconds: secs });

function editTool({ path, added, removed, name = 'edit' }) {
  const toolCall = { id: `call-${path}-${added}`, name };
  return {
    type: 'tool_call',
    id: `tool-${path}-${added}`,
    name,
    status: 'success',
    arguments: { path },
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: toolCall },
    },
    resultEvent: {
      type: 'tool_call_result',
      payload: {
        tool_call: toolCall,
        display: {
          version: 1,
          summary: path,
          hidden_argument_keys: [],
          primary: [],
          facts: [
            { kind: 'line_change', change: 'added', value: added },
            { kind: 'line_change', change: 'removed', value: removed },
          ],
        },
      },
    },
  };
}

describe('runFooterParts', () => {
  const end = '2026-08-05T18:20:00Z';

  it.each([
    [
      'the canonical Iteration count after the duration, never a tool-count estimate',
      {
        status: 'completed',
        durationMs: 8000,
        iterationCount: 3,
        outputs: [{ content: 'done' }],
        tools: Array.from({ length: 5 }, () => ({ name: 'read' })),
        items: [editTool({ path: 'a.txt', added: 3, removed: 2 })],
      },
      () => [status('completed'), seconds('8.0'), iterations(3)],
    ],
    [
      'no Iteration count without backend truth',
      {
        status: 'completed',
        durationMs: 1000,
        outputs: [{ content: 'done' }, { content: 'another message' }],
        tools: [{ name: 'read' }],
        items: [],
      },
      () => [status('completed'), seconds('1.0')],
    ],
    [
      'zero Iterations before the first Model response',
      { status: 'running', durationMs: null, iterationCount: 0 },
      () => [status('running'), iterations(0)],
    ],
    [
      'the cancelled label before the runtime',
      { status: 'cancelled', durationMs: 12000 },
      () => [status('cancelled'), seconds(12)],
    ],
    [
      'only the cancelled label without timing',
      { status: 'cancelled', durationMs: null },
      () => [status('cancelled')],
    ],
    [
      'minutes and seconds for a minute-scale Run',
      { status: 'completed', durationMs: 325000 },
      () => [status('completed'), minutesSeconds(5, 25)],
    ],
    [
      'hours and minutes for an hour-scale Run',
      { status: 'completed', durationMs: 5071000 },
      () => [
        status('completed'),
        t('chat.durationHoursMinutes', { hours: 1, minutes: 24 }),
      ],
    ],
    [
      'the end time once the Run is terminal',
      { status: 'completed', durationMs: 8000, endTimestamp: end },
      () => [status('completed'), seconds('8.0'), formatTime(end)],
    ],
    [
      'no end time while the Run is running',
      { status: 'running', durationMs: null, endTimestamp: end },
      () => [status('running')],
    ],
  ])('shows %s', (_label, run, expected) => {
    expect(runFooterParts(run)).toEqual(expected());
  });

  it('details the Run state, moments, runtime and Model responses', () => {
    const start = '2026-08-05T18:19:52Z';
    const nowMs = Date.parse(end) + 60_000;
    const moment = (value) => formatMoment(value, { nowMs, seconds: true });

    expect(
      runFooterDetails(
        {
          status: 'completed',
          durationMs: 8000,
          iterationCount: 3,
          startTimestamp: start,
          endTimestamp: end,
        },
        nowMs,
      ),
    ).toEqual({
      title: status('completed'),
      text: t('chat.details.modelResponseCount', { count: 3 }),
      rows: [
        { label: t('chat.details.started'), value: moment(start) },
        { label: t('chat.details.finished'), value: moment(end) },
        { label: t('chat.details.duration'), value: seconds('8.0') },
      ],
    });
  });

  it('ticks a running duration from the start timestamp', () => {
    expect(
      runFooterParts(
        {
          status: 'running',
          durationMs: null,
          startTimestamp: '2026-08-05T18:00:00.000Z',
          items: [],
        },
        Date.parse('2026-08-05T18:00:05.250Z'),
      ),
    ).toEqual([status('running'), seconds('5.3')]);
  });
});

describe('runFooterNotice', () => {
  const working = (secs) => t('chat.providerWorking', { seconds: secs });

  it.each([
    ['a Provider idle for over a minute', 75.4, 'running', working(75)],
    ['a Provider idle for exactly the threshold', 60, 'running', working(60)],
    ['an ordinary short pause', 0.3, 'running', ''],
    ['a longer ordinary pause', 13, 'running', ''],
    ['a pause just below the threshold', 59.9, 'running', ''],
    ['a heartbeat of a finished Run', 75.4, 'completed', ''],
  ])('reports %s', (_label, idleSeconds, runStatus, notice) => {
    const run = {
      status: runStatus,
      durationMs: null,
      outputs: [{ content: 'Writing the plan now.' }],
      tools: [],
      providerHeartbeat: { idleSeconds },
    };

    expect(runFooterNotice(run)).toBe(notice);
    // The liveness notice never enters the stable footer line.
    expect(runFooterParts(run)).not.toContain(working(75));
  });

  it('reports nothing for a running Run without heartbeat or request status', () => {
    expect(runFooterNotice({ status: 'running' })).toBe('');
  });

  it('waits a minute before reporting an ordinary request without heartbeats', () => {
    const startedAt = Date.parse('2026-09-16T12:00:00Z');
    const run = {
      status: 'running',
      providerRequestStatus: {
        state: 'waiting',
        timestamp: new Date(startedAt).toISOString(),
      },
    };

    expect(runFooterNotice(run, startedAt + 59_999)).toBe('');
    expect(runFooterNotice(run, startedAt + 60_000)).toBe(
      t('chat.requestWaiting'),
    );
    expect(
      runFooterNotice({ ...run, status: 'completed' }, startedAt + 60_000),
    ).toBe('');
  });

  it.each([
    ['retrying', 'chat.requestRetrying'],
    ['waiting', 'chat.requestWaiting'],
  ])('reports a failed request immediately while %s', (state, key) => {
    const nowMs = Date.parse('2026-09-16T12:00:00Z');

    expect(
      runFooterNotice(
        {
          status: 'running',
          providerRequestStatus: {
            state,
            error_kind: 'rate_limit',
            attempt: 2,
            max_attempts: 3,
            timestamp: new Date(nowMs).toISOString(),
          },
        },
        nowMs,
      ),
    ).toBe(
      [
        t('chat.requestRateLimit'),
        t(key),
        t('chat.requestAttempt', {
          attempt: 2,
          total: 3,
        }),
      ].join(' '),
    );
  });
});

describe('change statistics', () => {
  const lines = (path, added, removed) => ({ path, added, removed });

  it.each([
    [
      'presents valid server-computed statistics',
      {
        files: 1,
        added: 1,
        removed: 1,
        paths: ['a.txt'],
        file_stats: [{ path: 'a.txt', added: 1, removed: 1 }],
      },
      { files: 1, added: 1, removed: 1, fileStats: [lines('a.txt', 1, 1)] },
    ],
    [
      'leaves per-file counts unknown unless they match the server paths',
      {
        files: 2,
        added: 4,
        removed: 1,
        paths: ['a.txt', 'b.txt'],
        file_stats: [{ path: 'b.txt', added: 4, removed: 1 }],
      },
      {
        files: 2,
        added: 4,
        removed: 1,
        fileStats: [lines('a.txt', null, null), lines('b.txt', null, null)],
      },
    ],
    [
      'ignores malformed statistics',
      { files: 'x', added: 1, removed: 1, paths: [] },
      null,
    ],
    [
      'treats a server-reported zero as no changes',
      { files: 0, added: 0, removed: 0, paths: [], file_stats: [] },
      null,
    ],
    // Tool calls never count on their own: the server owns the statistics.
    ['shows nothing without server statistics', undefined, null],
  ])('%s', (_label, changeStats, expected) => {
    expect(
      runChangeStats({
        type: 'assistant_run',
        changeStats,
        items: [editTool({ path: 'a.txt', added: 3, removed: 2 })],
      }),
    ).toEqual(expected);
  });

  it('presents Session statistics from the server', () => {
    expect(
      sessionChangeStats({
        files: 3,
        added: 8,
        removed: 2,
        file_stats: [
          lines('a.txt', 3, 1),
          lines('b.txt', 3, 0),
          lines('c.txt', null, null),
          { path: '', added: 1, removed: 0 },
          { path: 'd.txt', added: -1, removed: 0 },
        ],
      }),
    ).toEqual({
      files: 3,
      added: 8,
      removed: 2,
      fileStats: [
        lines('a.txt', 3, 1),
        lines('b.txt', 3, 0),
        lines('c.txt', null, null),
      ],
    });
    expect(
      sessionChangeStats({ files: 0, added: 0, removed: 0, file_stats: [] }),
    ).toBeNull();
    expect(sessionChangeStats({ files: 1, added: 'x', removed: 0 })).toBeNull();
    expect(sessionChangeStats(null)).toBeNull();
  });

  it.each([
    [
      { files: 5, added: 151, removed: 15 },
      () => t('chat.changeStats.filesMany', { count: 5 }),
    ],
    [{ files: 1, added: 2, removed: 0 }, () => t('chat.changeStats.filesOne')],
  ])('labels %o as one line and as colored parts', (stats, fileLabel) => {
    expect(changeStatsLabel(stats)).toBe(
      `${fileLabel()}, +${stats.added} -${stats.removed}`,
    );
    expect(changeStatsParts(stats)).toEqual([
      { kind: 'files', text: `${fileLabel()},` },
      { kind: 'added', text: `+${stats.added}` },
      { kind: 'removed', text: `-${stats.removed}` },
    ]);
  });

  it('renders nothing for missing statistics', () => {
    expect(changeStatsLabel(null)).toBe('');
    expect(changeStatsParts(null)).toEqual([]);
    expect(changedFilesCard(null)).toBeNull();
    expect(
      changedFilesCard({ files: 2, added: 2, removed: 0, fileStats: [] }),
    ).toBeNull();
  });

  it.each([
    [
      'Windows paths grouped by folder below their shared directory',
      [
        lines('C:\\game\\src\\actors\\player.gd', 4, 1),
        lines('C:\\game\\src\\world\\map.gd', 5, 2),
        lines('C:\\game\\tools\\cast.gd', null, null),
        lines('C:\\game\\src\\actors\\enemy.gd', 1, 0),
      ],
      ['C:\\', 'game'],
      [
        [
          'src\\actors',
          5,
          1,
          [
            ['enemy.gd', 1, 0],
            ['player.gd', 4, 1],
          ],
        ],
        ['src\\world', 5, 2, [['map.gd', 5, 2]]],
        ['tools', null, null, [['cast.gd', null, null]]],
      ],
    ],
    [
      'a single file below its own directory',
      [lines('/home/me/app.js', 2, 0)],
      ['/', 'home/', 'me'],
      [['', 2, 0, [['app.js', 2, 0]]]],
    ],
    [
      'relative paths without a shared directory',
      [lines('README.md', 1, 0), lines('src/app.js', 1, 0)],
      [],
      [
        ['', 1, 0, [['README.md', 1, 0]]],
        ['src', 1, 0, [['app.js', 1, 0]]],
      ],
    ],
  ])(
    'lists %s in the changed-files card',
    (_label, fileStats, root, groups) => {
      const card = changedFilesCard({
        files: fileStats.length,
        added: 9,
        removed: 3,
        fileStats,
      });

      expect(card.rootSegments).toEqual(root);
      expect(
        card.groups.map((group) => [
          group.directory,
          group.added,
          group.removed,
          group.rows.map((row) => [row.name, row.added, row.removed]),
        ]),
      ).toEqual(groups);
      expect(
        card.groups
          .flatMap((group) => group.rows.map((row) => row.path))
          .sort(),
      ).toEqual(fileStats.map((entry) => entry.path).sort());
    },
  );

  it('shows only the count columns some file has', () => {
    const card = (fileStats) =>
      changedFilesCard({
        files: fileStats.length,
        added: 1,
        removed: 1,
        fileStats,
      });

    expect(
      card([lines('a.txt', 6, 2), lines('b.txt', null, null)]).countKinds,
    ).toEqual(['added', 'removed']);
    expect(card([lines('b.txt', 2, 0)]).countKinds).toEqual(['added']);
    expect(card([lines('c.txt', null, null)]).countKinds).toEqual([]);
  });

  it('heads the changed-files card with the totals and counts unnamed files', () => {
    expect(
      changedFilesCard({
        files: 205,
        added: 9,
        removed: 3,
        fileStats: [lines('a.txt', 9, 3)],
      }),
    ).toMatchObject({
      title: t('chat.changeStats.filesMany', { count: 205 }),
      added: 9,
      removed: 3,
      unlisted: 204,
    });
  });
});

describe('Run children', () => {
  it.each([
    ['the persisted duration', { durationMs: 4200 }, undefined, '4.2'],
    [
      'the measured span over the frozen estimate',
      { durationMs: 4200, durationEstimateMs: 3000 },
      undefined,
      '4.2',
    ],
    [
      'the frozen estimate once deltas stopped growing',
      {
        durationMs: null,
        durationEstimateMs: 3000,
        streaming: true,
        timestamp: '2026-08-24T10:00:00+00:00',
      },
      undefined,
      '3.0',
    ],
    [
      'a live tick from the first streamed delta',
      {
        durationMs: null,
        streaming: true,
        timestamp: '2026-08-24T10:00:00+00:00',
      },
      Date.parse('2026-08-24T10:00:00+00:00') + 8300,
      '8.3',
    ],
    [
      'nothing for a non-streamed block without duration',
      { durationMs: null, streaming: false },
      Date.now(),
      null,
    ],
    [
      'nothing for a streaming block without start time',
      { durationMs: null, streaming: true, timestamp: null },
      Date.now(),
      null,
    ],
  ])('labels a reasoning block with %s', (_label, child, nowMs, value) => {
    expect(reasoningDurationLabel(child, nowMs)).toBe(
      value === null ? '' : seconds(value),
    );
  });

  it('shows a streaming Tool preview before its started event', () => {
    expect(
      visibleRunChildren({
        items: [
          {
            type: 'tool_call',
            streaming: true,
            name: 'session_search',
            partialArgumentsText: '{"query": "ca',
            startedEvent: null,
            resultEvent: null,
            output: '',
          },
        ],
      }),
    ).toHaveLength(1);
  });

  it('marks only the latest visible streaming child of a running Run as working', () => {
    const reasoning = {
      id: 'reasoning-one',
      type: 'reasoning',
      content: 'Inspect the request.',
      streaming: true,
    };
    const answer = {
      id: 'answer-one',
      type: 'assistant_output',
      content: 'I will inspect it.',
      streaming: true,
    };
    const tool = {
      id: 'tool-one',
      type: 'tool_call',
      name: 'read',
      streaming: true,
      status: 'preparing',
    };
    const items = [reasoning, answer, tool];

    expect(
      items.map((child) =>
        isRunChildWorking({ status: 'running', items }, child),
      ),
    ).toEqual([false, false, true]);
    expect(isRunChildWorking({ status: 'completed', items }, tool)).toBe(false);
  });

  it.each([
    [{ kind: 'tool_call', toolName: 'bash', toolStatus: 'running' }, true],
    ...['success', 'failed', 'cancelled'].map((toolStatus) => [
      { kind: 'tool_call', toolName: 'bash', toolStatus },
      false,
    ]),
    [
      {
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'running',
        streaming: true,
      },
      false,
    ],
    ...['read', 'edit', 'grep'].map((toolName) => [
      { kind: 'tool_call', toolName, toolStatus: 'running' },
      false,
    ]),
    [{ kind: 'sub_agent', dotStatus: 'running' }, true],
    ...['success', 'failed', 'cancelled'].map((dotStatus) => [
      { kind: 'sub_agent', dotStatus },
      false,
    ]),
    [{ kind: 'reasoning' }, false],
    [{}, false],
    [null, false],
    [undefined, false],
  ])('offers a cancel control for %o: %s', (row, cancellable) => {
    expect(isRowCancellable(row)).toBe(cancellable);
  });
});

describe('background command rows', () => {
  const nowMs = Date.parse('2026-09-04T12:30:00Z');
  const timing = {
    started_at: '2026-09-04T12:00:00Z',
    completed_at: '2026-09-04T12:00:01Z',
  };
  const terminal = (value) => ({
    label: t('chat.details.terminal'),
    value,
    mono: true,
  });
  const foregroundBash = () =>
    backgroundCommandTool({
      id: 'bash-foreground',
      arguments: { command: 'npm test' },
      result: {
        ok: true,
        data: { exit_code: 0, output: 'passed' },
        artifacts: [],
      },
    });

  it.each([
    ['running', 'running'],
    ['completed', 'success'],
    ['failed', 'failed'],
    ['stopped', 'cancelled'],
  ])('shows a %s command with the %s dot', (status, dotStatus) => {
    expect(
      backgroundCommandRowState(
        backgroundCommandTool(),
        {},
        { term_one: status },
      ),
    ).toEqual({
      terminalId: 'term_one',
      command: 'npm run dev',
      fullCommand: 'npm run dev',
      dotStatus,
    });
  });

  it('prefers the live status, falls back to the durable one and needs one', () => {
    const tool = backgroundCommandTool();

    expect(
      backgroundCommandRowState(
        tool,
        { term_one: 'running' },
        { term_one: 'stopped' },
      ).dotStatus,
    ).toBe('cancelled');
    expect(
      backgroundCommandRowState(tool, { term_one: 'failed' }, {}).dotStatus,
    ).toBe('failed');
    // A command vBot no longer runs stays a plain Tool row.
    expect(backgroundCommandRowState(tool, {}, {})).toBeNull();
    expect(
      backgroundCommandRowState(foregroundBash(), {}, { term_one: 'running' }),
    ).toBeNull();
  });

  it.each([
    [
      'ticks from the Tool call start while the command runs',
      'running',
      () => minutesSeconds(30, 0),
    ],
    [
      'marks a stopped command as cancelled',
      'stopped',
      () => t('chat.toolCancelled'),
    ],
    ['shows no time once the command ended', 'completed', () => ''],
  ])('%s', (_label, status, expected) => {
    const tool = backgroundCommandTool({ timing });
    const rowState = backgroundCommandRowState(tool, {}, { term_one: status });

    expect(backgroundCommandToolStatusLabel(tool, rowState, nowMs)).toBe(
      expected(),
    );
  });

  it.each([
    [
      'a running command with its hand-off meaning',
      'running',
      (moment) => ({
        title: t('chat.toolState.background'),
        text: t('chat.toolState.backgroundHint'),
        rows: [
          {
            label: t('chat.details.started'),
            value: moment(timing.started_at),
          },
          { label: t('chat.details.runningFor'), value: minutesSeconds(30, 0) },
          terminal('term_one'),
        ],
      }),
    ],
    [
      'a failed command',
      'failed',
      () => ({
        title: t('chat.toolState.failed'),
        text: '',
        rows: [terminal('term_one')],
      }),
    ],
  ])('details %s', (_label, status, expected) => {
    const tool = backgroundCommandTool({ timing });
    const rowState = backgroundCommandRowState(tool, {}, { term_one: status });
    const moment = (value) => formatMoment(value, { nowMs, seconds: true });

    expect(backgroundCommandStatusDetails(tool, rowState, nowMs)).toEqual(
      expected(moment),
    );
  });

  it('keeps the live clock running until the handed-off command ends', () => {
    const items = [
      {
        id: 'run-1',
        type: 'assistant_run',
        items: [backgroundCommandTool({ timing })],
      },
    ];

    expect(liveClockCadenceMs(items, {}, nowMs, { term_one: 'running' })).toBe(
      1000,
    );
    expect(
      liveClockCadenceMs(
        items,
        {},
        nowMs,
        { term_one: 'running' },
        { term_one: 'completed' },
      ),
    ).toBe(0);
  });

  it('splits Activity panel work into running and finished, newest start first', () => {
    const task = (id, dotStatus, startedAt) => ({
      id,
      kind: id.startsWith('command') ? 'command' : 'subagent',
      dotStatus,
      tool: startedAt ? { timing: { started_at: startedAt } } : {},
    });
    const reflection = (runId, status, startedAt) => ({
      runId,
      status,
      startedAt,
    });

    const sections = activitySections(
      [
        task('subagent:late', 'running', '2026-10-06T10:03:00Z'),
        task('command:early', 'running', '2026-10-06T10:01:00Z'),
        task('subagent:untimed', 'success', ''),
        task('subagent:done', 'failed', '2026-10-06T10:00:00Z'),
      ],
      [
        reflection('mid', 'running', '2026-10-06T10:02:00Z'),
        reflection('old', 'completed', '2026-10-06T09:00:00Z'),
      ],
    );

    const keys = (entries) => entries.map((entry) => entry.key);
    expect(keys(sections.running)).toEqual([
      'subagent:late',
      'reflection:mid',
      'command:early',
    ]);
    expect(keys(sections.finished)).toEqual([
      'subagent:done',
      'reflection:old',
      'subagent:untimed',
    ]);
    expect(sections.running.map((entry) => entry.kind)).toEqual([
      'subagent',
      'reflection',
      'command',
    ]);
  });

  it('lists handed-off commands as Activity panel tasks', () => {
    const tasks = backgroundTasks(
      [
        {
          id: 'run-1',
          type: 'assistant_run',
          items: [backgroundCommandTool(), foregroundBash()],
        },
      ],
      {},
      { term_one: 'running' },
      { term_one: 'stopped' },
      nowMs,
    );

    expect(tasks).toEqual([
      expect.objectContaining({
        id: 'command:term_one',
        kind: 'command',
        terminalId: 'term_one',
        dotStatus: 'cancelled',
        timeLabel: t('chat.toolCancelled'),
      }),
    ]);
  });
});

describe('Reflection rows', () => {
  it.each([
    ['memory_reflection', true],
    ['skill_reflection', true],
    ['reflection', true],
    ['user', false],
    [undefined, false],
  ])('classifies Run kind %s as a Reflection: %s', (runKind, reflection) => {
    expect(isReflectionRunKind(runKind)).toBe(reflection);
  });

  it('projects tracking entries into rows, running first and then newest first', () => {
    const rows = reflectionTaskRows({
      reflectionTasks: {
        'run-old-finished': {
          sessionId: 'fork-a',
          runKind: 'skill_reflection',
          status: 'completed',
          startedAt: '2026-08-24T08:00:00.000Z',
        },
        'run-running': {
          sessionId: 'fork-b',
          runKind: 'memory_reflection',
          status: 'running',
          startedAt: '2026-08-24T07:00:00.000Z',
        },
        'run-newer-finished': {
          sessionId: 'fork-c',
          runKind: 'reflection',
          status: 'failed',
          startedAt: '2026-08-24T10:00:00.000Z',
        },
        'run-broken': { sessionId: '' },
        'run-unknown-kind': {
          sessionId: 'fork-d',
          runKind: 'cron',
          status: 'completed',
        },
      },
    });

    expect(rows.map(({ runId, scope }) => [runId, scope])).toEqual([
      ['run-running', 'memory'],
      ['run-newer-finished', 'combined'],
      ['run-old-finished', 'skill'],
      ['run-unknown-kind', ''],
    ]);
    expect(rows[0]).toMatchObject({ sessionId: 'fork-b', status: 'running' });
    expect(reflectionTaskRows(undefined)).toEqual([]);
    expect(reflectionTaskRows({})).toEqual([]);
  });

  it.each([
    [
      'a later Memory change by a background Reflection',
      {
        store: 'memory',
        scope: 'agent',
        text: 'Uses pytest.',
        later: { actor: 'tool', run_kind: 'memory_reflection' },
      },
      'Nothing was undone: the Agent Memory entry “Uses pytest.” was changed again later by a background Reflection',
    ],
    [
      'a Skill edited outside vBot',
      { store: 'skill', skill: 'deploy', later: { actor: 'external' } },
      'Nothing was undone: Skill “deploy” was changed again later by an edit outside vBot',
    ],
    [
      'a later change of unknown revision',
      { store: 'skill', skill: 'deploy', later: null },
      'Nothing was undone: Skill “deploy” was changed again later. Change it directly instead.',
    ],
  ])('names %s that blocks an undo', (_label, conflict, expected) => {
    const at = '2026-09-05T10:30:00Z';
    const later = conflict.later
      ? { ...conflict.later, revision: 2, at }
      : null;

    const text = reflectionUndoErrorText({
      conflict: { revision: 1, ...conflict, later },
      message: 'server text',
    });

    expect(text).toContain(expected);
    if (later) {
      expect(text).toContain(`(${formatAbsoluteTime(at)})`);
    }
  });

  it('keeps the server message of any other undo failure', () => {
    expect(
      reflectionUndoErrorText({ conflict: null, message: 'Disk full.' }),
    ).toBe('Disk full.');
    expect(reflectionUndoErrorText(null)).toBe('');
  });

  it('formats coarse elapsed labels and stays empty without a parseable start', () => {
    const start = '2026-08-24T10:00:00.000Z';

    expect(reflectionElapsedLabel(start, Date.parse(start) + 45_123)).toBe(
      t('chat.activity.reflectionElapsedSeconds', { count: 45 }),
    );
    expect(reflectionElapsedLabel(start, Date.parse(start) + 125_000)).toBe(
      t('chat.activity.reflectionElapsedMinutes', { count: 2 }),
    );
    expect(reflectionElapsedLabel('', Date.now())).toBe('');
    expect(reflectionElapsedLabel(start, Number.NaN)).toBe('');
  });
});
