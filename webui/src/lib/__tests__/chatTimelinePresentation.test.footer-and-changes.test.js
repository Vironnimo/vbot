import './chatTimelinePresentation.support.js';

import { beforeEach, describe, expect, it } from 'vitest';
import {
  changeStatsLabel,
  changeStatsParts,
  changeStatsTooltip,
  formatTime,
  runChangeStats,
  runFooterNotice,
  runFooterParts,
  sessionChangeStats,
} from '../chatTimelinePresentation.js';
import { init } from '../i18n.js';

describe('runFooterParts', () => {
  beforeEach(() => {
    init('en');
  });

  it('uses only the canonical Iteration count from the backend', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 1000,
      iterationCount: 2,
      outputs: [{ content: 'done' }],
      tools: Array.from({ length: 5 }, () => ({ name: 'read' })),
    });

    expect(parts[0]).toBe('Completed');
    expect(parts).toContain('2 iter');
  });

  it('does not estimate an Iteration count when backend truth is absent', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 1000,
      outputs: [{ content: 'done' }, { content: 'another message' }],
      tools: [{ name: 'read' }],
    });

    expect(parts).not.toContain('3 iter');
    expect(parts.every((part) => !part.endsWith(' iter'))).toBe(true);
  });

  it('shows zero before the first Model response returns', () => {
    const parts = runFooterParts({
      status: 'running',
      durationMs: null,
      iterationCount: 0,
      outputs: [],
      tools: [],
    });

    expect(parts[0]).toBe('Running');
    expect(parts).toContain('0 iter');
  });

  it('shows the Cancelled label plus the runtime for a cancelled run', () => {
    const parts = runFooterParts({
      status: 'cancelled',
      durationMs: 12000,
      outputs: [{ content: 'partial' }],
      tools: [],
    });

    expect(parts).toContain('Cancelled');
    const cancelledIndex = parts.indexOf('Cancelled');
    // The duration renders after the user-action label, never instead of it.
    expect(parts.length).toBeGreaterThan(cancelledIndex + 1);
  });

  it('shows only the Cancelled label when a cancelled run has no timing', () => {
    const parts = runFooterParts({
      status: 'cancelled',
      durationMs: null,
      outputs: [],
      tools: [],
    });

    expect(parts).toContain('Cancelled');
  });

  it('shows terminal status and canonical duration for completed runs', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      outputs: [{ content: 'done' }],
      tools: [],
    });

    expect(parts).toContain('Completed');
    expect(parts).toContain('8.0s');
  });

  it('computes a live Run duration from its start timestamp', () => {
    const parts = runFooterParts(
      {
        status: 'running',
        durationMs: null,
        startTimestamp: '2026-08-05T18:00:00.000Z',
        iterationCount: 0,
      },
      Date.parse('2026-08-05T18:00:05.250Z'),
    );

    expect(parts).toContain('Running');
    expect(parts).toContain('5.3s');
  });

  it('formats minute-scale durations as minutes and seconds', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 325000,
      outputs: [{ content: 'done' }],
      tools: [],
    });

    expect(parts).toContain('5m 25s');
  });

  it('formats hour-scale durations as hours and minutes without seconds', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 5071000,
      outputs: [{ content: 'done' }],
      tools: [],
    });

    expect(parts).toContain('1h 24m');
  });

  it('reports live Provider liveness only on the separate notice line', () => {
    const assistantRun = {
      status: 'running',
      durationMs: null,
      outputs: [{ content: 'Writing the plan now.' }],
      tools: [],
      providerHeartbeat: { idleSeconds: 75.4 },
    };

    expect(runFooterNotice(assistantRun)).toBe(
      'Provider connected · waiting 75s for the next model chunk',
    );
    expect(runFooterParts(assistantRun)).not.toContain(
      'Provider connected · waiting 75s for the next model chunk',
    );
  });

  it('returns no notice when the run is not running or has no heartbeat', () => {
    expect(
      runFooterNotice({
        status: 'completed',
        providerHeartbeat: { idleSeconds: 75.4 },
      }),
    ).toBe('');
    expect(runFooterNotice({ status: 'running' })).toBe('');
  });

  it('hides the notice while the provider streams with a tiny idle time', () => {
    expect(
      runFooterNotice({
        status: 'running',
        providerHeartbeat: { idleSeconds: 0.3 },
      }),
    ).toBe('');
  });

  it('shows the notice once the idle time reaches the threshold', () => {
    expect(
      runFooterNotice({
        status: 'running',
        providerHeartbeat: { idleSeconds: 10.2 },
      }),
    ).toBe('Provider connected · waiting 10s for the next model chunk');
  });
});

describe('runChangeStats', () => {
  beforeEach(() => {
    init('en');
  });

  function editTool({ path, added, removed, name = 'edit' }) {
    return {
      type: 'tool_call',
      id: `tool-${path}`,
      name,
      status: 'success',
      arguments: { path },
      startedEvent: {
        type: 'tool_call_started',
        payload: { tool_call: { id: `call-${path}`, name } },
      },
      resultEvent: {
        type: 'tool_call_result',
        payload: {
          tool_call: { id: `call-${path}`, name },
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

  it('sums line changes and counts distinct files per run', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      items: [
        editTool({ path: 'a.txt', added: 3, removed: 2 }),
        editTool({ path: 'a.txt', added: 1, removed: 0 }),
        editTool({ path: 'b.txt', added: 5, removed: 1 }),
      ],
    });

    expect(stats).toEqual({
      files: 2,
      added: 9,
      removed: 3,
      paths: ['a.txt', 'b.txt'],
    });
  });

  it('counts a write of a new file as added lines only', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      items: [
        editTool({ path: 'new.txt', added: 4, removed: 0, name: 'write' }),
      ],
    });

    expect(stats).toEqual({
      files: 1,
      added: 4,
      removed: 0,
      paths: ['new.txt'],
    });
  });

  it('ignores non-file tools and tools without line-change facts', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      items: [
        {
          type: 'tool_call',
          id: 'tool-read',
          name: 'read',
          status: 'success',
          arguments: { path: 'a.txt' },
          startedEvent: {
            type: 'tool_call_started',
            payload: { tool_call: { id: 'call-read', name: 'read' } },
          },
        },
        {
          type: 'tool_call',
          id: 'tool-bash',
          name: 'bash',
          status: 'success',
          arguments: { command: 'ls' },
          startedEvent: {
            type: 'tool_call_started',
            payload: { tool_call: { id: 'call-bash', name: 'bash' } },
          },
        },
      ],
    });

    expect(stats).toBeNull();
  });

  it('returns null for a run without changes', () => {
    expect(runChangeStats({ type: 'assistant_run', items: [] })).toBeNull();
  });

  it('prefers the server-computed git-style stats over the tool-fact sum', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      changeStats: {
        files: 1,
        added: 1,
        removed: 1,
        paths: ['a.txt'],
      },
      items: [
        editTool({ path: 'a.txt', added: 3, removed: 2 }),
        editTool({ path: 'a.txt', added: 1, removed: 0 }),
      ],
    });

    expect(stats).toEqual({
      files: 1,
      added: 1,
      removed: 1,
      paths: ['a.txt'],
    });
  });

  it('falls back to the tool-fact sum when the server stats are malformed', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      changeStats: { files: 'x', added: 1, removed: 1, paths: [] },
      items: [editTool({ path: 'a.txt', added: 3, removed: 2 })],
    });

    expect(stats).toEqual({
      files: 1,
      added: 3,
      removed: 2,
      paths: ['a.txt'],
    });
  });

  it('returns null for a server-reported zero instead of the tool-fact sum', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      changeStats: { files: 0, added: 0, removed: 0, paths: [] },
      items: [editTool({ path: 'a.txt', added: 3, removed: 2 })],
    });

    expect(stats).toBeNull();
  });
});

describe('sessionChangeStats', () => {
  beforeEach(() => {
    init('en');
  });

  it('sums every run and deduplicates files across runs', () => {
    const stats = sessionChangeStats([
      {
        type: 'assistant_run',
        items: [
          {
            type: 'tool_call',
            id: 'tool-1',
            name: 'edit',
            status: 'success',
            arguments: { path: 'a.txt' },
            startedEvent: {
              type: 'tool_call_started',
              payload: { tool_call: { id: 'call-1', name: 'edit' } },
            },
            resultEvent: {
              type: 'tool_call_result',
              payload: {
                tool_call: { id: 'call-1', name: 'edit' },
                display: {
                  version: 1,
                  summary: 'a.txt',
                  hidden_argument_keys: [],
                  primary: [],
                  facts: [
                    { kind: 'line_change', change: 'added', value: 3 },
                    { kind: 'line_change', change: 'removed', value: 2 },
                  ],
                },
              },
            },
          },
        ],
      },
      {
        type: 'assistant_run',
        items: [
          {
            type: 'tool_call',
            id: 'tool-2',
            name: 'edit',
            status: 'success',
            arguments: { path: 'a.txt' },
            startedEvent: {
              type: 'tool_call_started',
              payload: { tool_call: { id: 'call-2', name: 'edit' } },
            },
            resultEvent: {
              type: 'tool_call_result',
              payload: {
                tool_call: { id: 'call-2', name: 'edit' },
                display: {
                  version: 1,
                  summary: 'a.txt',
                  hidden_argument_keys: [],
                  primary: [],
                  facts: [
                    { kind: 'line_change', change: 'added', value: 1 },
                    { kind: 'line_change', change: 'removed', value: 0 },
                  ],
                },
              },
            },
          },
          {
            type: 'tool_call',
            id: 'tool-3',
            name: 'write',
            status: 'success',
            arguments: { path: 'b.txt' },
            startedEvent: {
              type: 'tool_call_started',
              payload: { tool_call: { id: 'call-3', name: 'write' } },
            },
            resultEvent: {
              type: 'tool_call_result',
              payload: {
                tool_call: { id: 'call-3', name: 'write' },
                display: {
                  version: 1,
                  summary: 'b.txt',
                  hidden_argument_keys: [],
                  primary: [],
                  facts: [
                    { kind: 'line_change', change: 'added', value: 5 },
                    { kind: 'line_change', change: 'removed', value: 0 },
                  ],
                },
              },
            },
          },
        ],
      },
    ]);

    expect(stats).toEqual({
      files: 2,
      added: 9,
      removed: 2,
      paths: ['a.txt', 'b.txt'],
    });
  });

  it('returns null for an empty timeline', () => {
    expect(sessionChangeStats([])).toBeNull();
  });

  it('sums server-computed stats across runs and deduplicates files', () => {
    const stats = sessionChangeStats([
      {
        type: 'assistant_run',
        changeStats: { files: 1, added: 1, removed: 1, paths: ['a.txt'] },
        items: [],
      },
      {
        type: 'assistant_run',
        changeStats: {
          files: 2,
          added: 5,
          removed: 0,
          paths: ['a.txt', 'b.txt'],
        },
        items: [],
      },
    ]);

    expect(stats).toEqual({
      files: 2,
      added: 6,
      removed: 1,
      paths: ['a.txt', 'b.txt'],
    });
  });
});

describe('changeStatsLabel', () => {
  beforeEach(() => {
    init('en');
  });

  it('formats the compact one-line label', () => {
    expect(changeStatsLabel({ files: 5, added: 151, removed: 15 })).toBe(
      '5 files changed, +151 -15',
    );
  });

  it('uses the singular file form', () => {
    expect(changeStatsLabel({ files: 1, added: 2, removed: 0 })).toBe(
      '1 file changed, +2 -0',
    );
  });

  it('returns an empty string for null stats', () => {
    expect(changeStatsLabel(null)).toBe('');
  });
});

describe('runFooterParts', () => {
  beforeEach(() => {
    init('en');
  });

  it('shows status and duration for a completed run', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      items: [
        {
          type: 'tool_call',
          id: 'tool-1',
          name: 'edit',
          status: 'success',
          arguments: { path: 'a.txt' },
          startedEvent: {
            type: 'tool_call_started',
            payload: { tool_call: { id: 'call-1', name: 'edit' } },
          },
          resultEvent: {
            type: 'tool_call_result',
            payload: {
              tool_call: { id: 'call-1', name: 'edit' },
              display: {
                version: 1,
                summary: 'a.txt',
                hidden_argument_keys: [],
                primary: [],
                facts: [
                  { kind: 'line_change', change: 'added', value: 3 },
                  { kind: 'line_change', change: 'removed', value: 2 },
                ],
              },
            },
          },
        },
      ],
    });

    expect(parts).toEqual(['Completed', '8.0s']);
  });

  it('ticks the live duration while the run is running', () => {
    const parts = runFooterParts(
      {
        status: 'running',
        durationMs: null,
        startTimestamp: '2026-08-05T18:00:00.000Z',
        items: [],
      },
      Date.parse('2026-08-05T18:00:05.250Z'),
    );

    expect(parts).toEqual(['Running', '5.3s']);
  });

  it('omits the change part when the run changed no files', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 1000,
      items: [],
    });
    expect(parts).toEqual(['Completed', '1.0s']);
  });

  it('includes the iteration count after the duration', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      iterationCount: 3,
      items: [],
    });
    expect(parts).toEqual(['Completed', '8.0s', '3 iter']);
  });

  it('shows the end time once the run reached a terminal state', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      endTimestamp: '2026-08-05T18:20:00Z',
      items: [],
    });

    expect(parts[parts.length - 1]).toBe(formatTime('2026-08-05T18:20:00Z'));
  });

  it('shows no end time while the run is still running', () => {
    const parts = runFooterParts({
      status: 'running',
      durationMs: null,
      endTimestamp: '2026-08-05T18:20:00Z',
      items: [],
    });

    expect(parts).not.toContain(formatTime('2026-08-05T18:20:00Z'));
    expect(
      parts.every((part) => !part.includes('PM') && !part.includes('AM')),
    ).toBe(true);
  });
});

describe('changeStatsParts', () => {
  beforeEach(() => {
    init('en');
  });

  it('splits the change stats into file, added, and removed parts', () => {
    expect(changeStatsParts({ files: 5, added: 151, removed: 15 })).toEqual([
      { kind: 'files', text: '5 files changed,' },
      { kind: 'added', text: '+151' },
      { kind: 'removed', text: '-15' },
    ]);
  });

  it('uses the singular file form', () => {
    expect(changeStatsParts({ files: 1, added: 2, removed: 0 })).toEqual([
      { kind: 'files', text: '1 file changed,' },
      { kind: 'added', text: '+2' },
      { kind: 'removed', text: '-0' },
    ]);
  });

  it('returns an empty array for null stats', () => {
    expect(changeStatsParts(null)).toEqual([]);
  });
});

describe('changeStatsTooltip', () => {
  it('lists every changed file, one per line', () => {
    expect(
      changeStatsTooltip({
        files: 2,
        added: 9,
        removed: 3,
        paths: ['a.txt', 'b.txt'],
      }),
    ).toBe('a.txt\nb.txt');
  });

  it('returns an empty string when no paths are known', () => {
    expect(changeStatsTooltip({ files: 1, added: 2, removed: 0 })).toBe('');
    expect(changeStatsTooltip(null)).toBe('');
  });
});
