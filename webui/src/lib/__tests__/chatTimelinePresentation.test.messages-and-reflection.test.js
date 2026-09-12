import { beforeEach, describe, expect, it } from 'vitest';
import {
  backgroundBashDisplayResult,
  backgroundBashRowState,
  backgroundBashToolStatusLabel,
  backgroundTasks,
  compactionSeparatorLabel,
  compactionSummaryText,
  errorMessagePresentation,
  groupTransientCards,
  isReflectionRunKind,
  labelForEvent,
  labelForMessage,
  liveClockCadenceMs,
  reflectionElapsedLabel,
  reflectionScopeForRunKind,
  reflectionTaskRows,
  reasoningDurationLabel,
  takeoverSeparatorLabel,
} from '../chatTimelinePresentation.js';
import { init } from '../i18n.js';
import { backgroundBashTool } from './chatTimelinePresentation.support.js';

describe('errorMessagePresentation', () => {
  it('extracts the nested provider message and keeps the prefix', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 400 {"error":{"message":"max_tokens: Field required","code":"invalid_request_body"}}',
    );

    expect(presentation.summary).toBe(
      'Provider error: 400 max_tokens: Field required',
    );
    expect(presentation.details).toContain('"code": "invalid_request_body"');
  });

  it('extracts a top-level message field', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 400 {"message":"max_tokens: Field required"}',
    );

    expect(presentation.summary).toBe(
      'Provider error: 400 max_tokens: Field required',
    );
    expect(presentation.details).toContain('"message"');
  });

  it('prefers the deepest error.message over sibling fields', () => {
    const presentation = errorMessagePresentation(
      'Rate limited: 429 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"},"request_id":"req_1"}',
    );

    expect(presentation.summary).toBe('Rate limited: 429 Overloaded');
    expect(presentation.details).toContain('"request_id": "req_1"');
  });

  it('keeps the prefix as summary when the body has no message', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 500 {"status":"boom"}',
    );

    expect(presentation.summary).toBe('Provider error: 500');
    expect(presentation.details).toContain('"status": "boom"');
  });

  it('shows the upstream rate-limit detail and provider name directly', () => {
    const presentation = errorMessagePresentation(
      'Rate limited: 429 {"error":{"message":"Provider returned error","code":429,' +
        '"metadata":{"raw":"stealth/ox-alpha is temporarily rate-limited upstream. Please retry shortly.",' +
        '"provider_name":"Stealth","remedy_hint":"Retry shortly, add your own provider key"}},"user_id":"u1"}',
    );

    expect(presentation.summary).toBe(
      'Rate limited: 429 stealth/ox-alpha is temporarily rate-limited upstream. ' +
        'Please retry shortly. (via Stealth)',
    );
    expect(presentation.summary).not.toContain('Provider returned error');
    expect(presentation.summary).not.toContain('remedy_hint');
    expect(presentation.details).toContain('"remedy_hint"');
  });

  it('reads router metadata from a top-level error body', () => {
    const presentation = errorMessagePresentation(
      'Provider returned error: {"message":"Provider returned error","code":502,' +
        '"metadata":{"raw":"upstream connection reset","provider_name":"Morph"}}',
    );

    expect(presentation.summary).toBe(
      'Provider returned error: upstream connection reset (via Morph)',
    );
  });

  it('does not duplicate the message when the upstream detail matches it', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 500 {"message":"overloaded","metadata":{"raw":"overloaded"}}',
    );

    expect(presentation.summary).toBe('Provider error: 500 overloaded');
  });

  it('returns plain text unchanged without an embedded JSON object', () => {
    expect(errorMessagePresentation('Connection refused')).toEqual({
      summary: 'Connection refused',
      details: '',
    });
  });

  it('returns the full text when the embedded JSON does not parse', () => {
    const text = 'Provider error: 400 {broken json';
    expect(errorMessagePresentation(text)).toEqual({
      summary: text,
      details: '',
    });
  });

  it('handles non-string input', () => {
    expect(errorMessagePresentation(null)).toEqual({
      summary: '',
      details: '',
    });
  });

  it('labels a user message with the sender display name when present', () => {
    const message = {
      role: 'user',
      content: 'hello',
      sender: { id: '50', display_name: 'Alice' },
    };

    expect(labelForMessage(message)).toBe('ALICE');
  });

  it('labels a user message without sender as You', () => {
    expect(labelForMessage({ role: 'user', content: 'hello' })).toBe('YOU');
  });

  it('falls back to You when the sender display name is blank', () => {
    const message = {
      role: 'user',
      content: 'hello',
      sender: { id: '50', display_name: '   ' },
    };

    expect(labelForMessage(message)).toBe('YOU');
  });

  it('labels a live user_message_persisted event with the sender display name', () => {
    const event = {
      type: 'user_message_persisted',
      payload: {
        message: {
          role: 'user',
          content: 'hello',
          sender: { id: '50', display_name: 'Alice' },
        },
      },
    };

    expect(labelForEvent(event)).toBe('ALICE');
  });

  it('labels a live user_message_persisted event without sender as You', () => {
    const event = {
      type: 'user_message_persisted',
      payload: { message: { role: 'user', content: 'hello' } },
    };

    expect(labelForEvent(event)).toBe('YOU');
  });
});

describe('compactionSummaryText', () => {
  it('returns the checkpoint content byte-for-byte without trimming or formatting', () => {
    const summary = '\n# Heading\n\n<tag> & *literal*\n';

    expect(
      compactionSummaryText({
        message: { role: 'compaction_checkpoint', content: summary },
      }),
    ).toBe(summary);
    expect(compactionSummaryText({ message: { content: null } })).toBe('');
    expect(compactionSummaryText(null)).toBe('');
  });
});

describe('compactionSeparatorLabel', () => {
  beforeEach(() => {
    init('en');
  });

  it('leads with the duration and abbreviates token counts without the unit word', () => {
    expect(
      compactionSeparatorLabel({
        status: 'completed',
        durationMs: 45_000,
        contextTokensBefore: 254_224,
        contextTokensAfter: 40_289,
        message: { role: 'compaction_checkpoint', content: 'summary' },
      }),
    ).toBe('Context compacted in 45s · ~254k → ~40k');
  });

  it('formats long durations as minutes and seconds', () => {
    expect(
      compactionSeparatorLabel({
        status: 'completed',
        durationMs: 85_000,
        contextTokensBefore: 254_224,
        contextTokensAfter: 40_289,
        message: { role: 'compaction_checkpoint', content: 'summary' },
      }),
    ).toBe('Context compacted in 1m 25s · ~254k → ~40k');
  });

  it('drops the duration when it is unknown, as in reloaded history', () => {
    expect(
      compactionSeparatorLabel({
        status: 'completed',
        contextTokensBefore: 254_224,
        contextTokensAfter: 40_289,
        message: { role: 'compaction_checkpoint', content: 'summary' },
      }),
    ).toBe('Context compacted · ~254k → ~40k');
  });

  it('falls back to the plain label without measurable tokens or duration', () => {
    expect(
      compactionSeparatorLabel({
        status: 'completed',
        message: { role: 'compaction_checkpoint', content: 'summary' },
      }),
    ).toBe('Context compacted');
  });

  it('keeps the running label untouched', () => {
    expect(compactionSeparatorLabel({ status: 'running', message: null })).toBe(
      'Compacting current conversation…',
    );
  });
});

describe('takeoverSeparatorLabel', () => {
  beforeEach(() => {
    init('en');
  });

  it('composes the label from the parsed from/to addresses', () => {
    const label = takeoverSeparatorLabel({
      content: JSON.stringify({ from: 'assistant', to: 'builder@vbot' }),
    });
    expect(label).toBe('Taken over by assistant → builder@vbot');
  });

  it('keeps the raw addresses verbatim (identity and project forms)', () => {
    const label = takeoverSeparatorLabel({
      content: JSON.stringify({ from: 'reviewer@vbot', to: 'assistant' }),
    });
    expect(label).toContain('reviewer@vbot');
    expect(label).toContain('assistant');
  });

  it('falls back to a generic label when the content is malformed', () => {
    expect(takeoverSeparatorLabel({ content: 'not json' })).toBe(
      'Session taken over',
    );
    expect(takeoverSeparatorLabel({ content: '' })).toBe('Session taken over');
    expect(takeoverSeparatorLabel({})).toBe('Session taken over');
    expect(takeoverSeparatorLabel(null)).toBe('Session taken over');
  });

  it('falls back to generic when one address is missing', () => {
    expect(
      takeoverSeparatorLabel({ content: JSON.stringify({ from: 'a' }) }),
    ).toBe('Session taken over');
    expect(
      takeoverSeparatorLabel({ content: JSON.stringify({ to: 'b' }) }),
    ).toBe('Session taken over');
  });
});

describe('reflection panel helpers', () => {
  it('classifies reflection run kinds and derives their review scope', () => {
    expect(isReflectionRunKind('memory_reflection')).toBe(true);
    expect(isReflectionRunKind('skill_reflection')).toBe(true);
    expect(isReflectionRunKind('reflection')).toBe(true);
    expect(isReflectionRunKind('user')).toBe(false);
    expect(isReflectionRunKind(undefined)).toBe(false);

    expect(reflectionScopeForRunKind('memory_reflection')).toBe('memory');
    expect(reflectionScopeForRunKind('skill_reflection')).toBe('skill');
    expect(reflectionScopeForRunKind('reflection')).toBe('combined');
    expect(reflectionScopeForRunKind('cron')).toBe('');
  });

  it('projects tracking entries into rows sorted running-first, newest first', () => {
    const sessionState = {
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
          runKind: 'memory_reflection',
          status: 'failed',
          startedAt: '2026-08-24T10:00:00.000Z',
        },
        'run-broken': { sessionId: '' },
      },
    };

    const rows = reflectionTaskRows(sessionState);

    expect(rows.map((row) => row.runId)).toEqual([
      'run-running',
      'run-newer-finished',
      'run-old-finished',
    ]);
    expect(rows[0]).toMatchObject({
      sessionId: 'fork-b',
      scope: 'memory',
      status: 'running',
    });
    expect(rows[1].scope).toBe('memory');
    expect(rows[2].scope).toBe('skill');
  });

  it('tolerates a session state without tracking entries', () => {
    expect(reflectionTaskRows(undefined)).toEqual([]);
    expect(reflectionTaskRows({})).toEqual([]);
  });

  it('formats coarse elapsed labels and stays empty without a parseable start', () => {
    const start = '2026-08-24T10:00:00.000Z';

    expect(reflectionElapsedLabel(start, Date.parse(start) + 45_123)).toBe(
      '45s',
    );
    expect(reflectionElapsedLabel(start, Date.parse(start) + 125_000)).toBe(
      '2m',
    );
    expect(reflectionElapsedLabel('', Date.now())).toBe('');
    expect(reflectionElapsedLabel(start, Number.NaN)).toBe('');
  });
});

describe('reasoningDurationLabel', () => {
  it('prefers the persisted duration from the stable boundary', () => {
    expect(reasoningDurationLabel({ durationMs: 4200 }, Date.now())).toBe(
      '4.2s',
    );
  });

  it('prefers the measured span over the frozen estimate', () => {
    expect(
      reasoningDurationLabel({ durationMs: 4200, durationEstimateMs: 3000 }),
    ).toBe('4.2s');
  });

  it('shows the frozen estimate from when the deltas stopped growing', () => {
    expect(
      reasoningDurationLabel({
        durationMs: null,
        durationEstimateMs: 3000,
        streaming: true,
        timestamp: '2026-08-24T10:00:00+00:00',
      }),
    ).toBe('3.0s');
  });

  it('ticks live from the first streamed delta while streaming', () => {
    const child = {
      durationMs: null,
      streaming: true,
      timestamp: '2026-08-24T10:00:00+00:00',
    };

    expect(
      reasoningDurationLabel(child, Date.parse(child.timestamp) + 8300),
    ).toBe('8.3s');
  });

  it('stays empty without a measurable span and after a non-streamed block', () => {
    expect(reasoningDurationLabel({ durationMs: null, streaming: false })).toBe(
      '',
    );
    expect(
      reasoningDurationLabel(
        { durationMs: null, streaming: true, timestamp: null },
        Date.now(),
      ),
    ).toBe('');
    // Non-streamed blocks never estimate: only the persisted value counts.
    expect(
      reasoningDurationLabel(
        { durationMs: null, streaming: false },
        Date.now(),
      ),
    ).toBe('');
  });
});

describe('groupTransientCards', () => {
  const historyRunItem = (id, timestamp) => ({
    id,
    type: 'assistant_run',
    timestamp,
    items: [],
  });
  const userItem = (id, timestamp) => ({
    id,
    type: 'message',
    message: { id, role: 'user', timestamp },
  });

  it('anchors a card after the exact item it followed at creation', () => {
    const items = [
      userItem('user-1', '2026-08-27T13:40:00Z'),
      historyRunItem('run-live', '2026-08-27T13:46:00Z'),
    ];

    const groups = groupTransientCards(items, [
      {
        id: 'card-1',
        text: 'Agent: Alpha',
        anchorId: 'run-live',
        createdAt: 0,
      },
    ]);

    expect(groups.byItemId.get('run-live')).toHaveLength(1);
    expect(groups.leading).toEqual([]);
    expect(groups.trailing).toEqual([]);
  });

  it('keeps a lost anchor card at its chronological position by creation time', () => {
    // The /status card was created mid-run (13:46:30). A history reload then
    // replaced the live run id with the history id and a newer message
    // arrived — the card must sit between them, not sink to the end.
    const items = [
      userItem('user-1', '2026-08-27T13:40:00Z'),
      historyRunItem('run-history', '2026-08-27T13:46:00Z'),
      userItem('user-2', '2026-08-27T13:48:00Z'),
    ];

    const groups = groupTransientCards(items, [
      {
        id: 'card-1',
        text: 'Agent: Alpha',
        anchorId: 'run-live',
        createdAt: Date.parse('2026-08-27T13:46:30Z'),
      },
    ]);

    expect(groups.byItemId.size).toBe(0);
    expect(groups.byItemIndex.get(1)).toHaveLength(1);
    expect(groups.trailing).toEqual([]);
  });

  it('falls back to the timeline end for a lost anchor without a creation time', () => {
    const items = [
      userItem('user-1', '2026-08-27T13:40:00Z'),
      historyRunItem('run-history', '2026-08-27T13:46:00Z'),
    ];

    const groups = groupTransientCards(items, [
      { id: 'card-1', text: 'Agent: Alpha', anchorId: 'run-live' },
    ]);

    expect(groups.trailing).toHaveLength(1);
  });

  it('keeps a card created on an empty timeline at the top', () => {
    const items = [userItem('user-1', '2026-08-27T13:40:00Z')];

    const groups = groupTransientCards(items, [
      {
        id: 'card-1',
        text: 'Agent: Alpha',
        anchorId: null,
        createdAt: Date.now(),
      },
    ]);

    expect(groups.leading).toHaveLength(1);
  });
  describe('background Bash rows', () => {
    const nowMs = Date.parse('2026-09-04T12:30:00Z');
    const terminalEntry = {
      status: 'completed',
      exitCode: 0,
      cancelledByUser: false,
      startedAt: '2026-09-04T12:00:00Z',
      finishedAt: '2026-09-04T12:04:12Z',
      output: 'build finished',
      truncated: false,
      logFile: 'C:/logs/bash/process-one.log',
    };

    it('detects handed-off Bash rows and resolves the dot from live, durable, then envelope status', () => {
      const tool = backgroundBashTool();
      const running = backgroundBashRowState(tool, {}, {});
      expect(running).toEqual(
        expect.objectContaining({
          processId: 'process-one',
          dotStatus: 'running',
          terminal: null,
        }),
      );

      const settled = backgroundBashRowState(
        tool,
        {},
        { 'process-one': terminalEntry },
      );
      expect(settled.dotStatus).toBe('success');
      expect(settled.terminal).toEqual(terminalEntry);

      expect(
        backgroundBashRowState(tool, { 'process-one': 'failed' }, {}).dotStatus,
      ).toBe('failed');
      expect(
        backgroundBashRowState(
          {
            type: 'tool_call',
            name: 'bash',
            status: 'success',
            arguments: { command: 'npm test', mode: 'foreground' },
            result: {
              ok: true,
              data: { status: 'completed', mode: 'foreground' },
              artifacts: [],
            },
          },
          {},
          {},
        ),
      ).toBeNull();
    });

    it('ticks the time label from the Tool call start while the process runs', () => {
      const tool = backgroundBashTool({
        timing: {
          started_at: '2026-09-04T12:00:00Z',
          completed_at: '2026-09-04T12:00:01Z',
        },
      });
      const rowState = backgroundBashRowState(tool, {}, {});

      expect(backgroundBashToolStatusLabel(tool, rowState, nowMs)).toBe(
        '30m 0s',
      );
    });

    it('replaces the tick with the real runtime once the process is terminal', () => {
      const tool = backgroundBashTool({
        timing: {
          started_at: '2026-09-04T12:00:00Z',
          completed_at: '2026-09-04T12:00:01Z',
        },
      });
      const rowState = backgroundBashRowState(
        tool,
        {},
        { 'process-one': terminalEntry },
      );

      expect(backgroundBashToolStatusLabel(tool, rowState, nowMs)).toBe(
        '4m 12s',
      );
    });

    it('shows no time label for terminal rows without known runtimes', () => {
      const tool = backgroundBashTool({
        timing: {
          started_at: '2026-09-04T12:00:00Z',
          completed_at: '2026-09-04T12:00:01Z',
        },
      });
      const settledWithoutTimes = backgroundBashRowState(
        tool,
        { 'process-one': 'completed' },
        {},
      );

      expect(
        backgroundBashToolStatusLabel(tool, settledWithoutTimes, nowMs),
      ).toBe('');
    });

    it('marks cancelled rows with the runtime when it is known', () => {
      const tool = backgroundBashTool();
      const rowState = backgroundBashRowState(
        tool,
        {},
        {
          'process-one': { ...terminalEntry, status: 'killed' },
        },
      );

      expect(backgroundBashToolStatusLabel(tool, rowState, nowMs)).toBe(
        'cancelled · 4m 12s',
      );
    });

    it('replaces the handoff result with the actual completion result', () => {
      const tool = backgroundBashTool();
      const runningRow = backgroundBashRowState(tool, {}, {});
      expect(backgroundBashDisplayResult(tool, runningRow)).toBe(tool.result);

      const settledRow = backgroundBashRowState(
        tool,
        {},
        { 'process-one': terminalEntry },
      );
      expect(JSON.parse(backgroundBashDisplayResult(tool, settledRow))).toEqual(
        {
          ok: true,
          error: null,
          data: {
            status: 'completed',
            exit_code: 0,
            output: 'build finished',
            truncated: false,
            log_file: 'C:/logs/bash/process-one.log',
          },
        },
      );
    });

    it('keeps the live clock running for a settled Bash row whose process still runs', () => {
      const settledBash = backgroundBashTool({
        timing: {
          started_at: '2026-09-04T12:00:00Z',
          completed_at: '2026-09-04T12:00:01Z',
        },
      });
      const items = [
        { id: 'run-1', type: 'assistant_run', items: [settledBash] },
      ];

      // The handoff envelope reports the process as running, so the row keeps
      // ticking from the Tool call start until a terminal status arrives.
      expect(liveClockCadenceMs(items, {}, nowMs, {})).toBe(1000);
      // A terminal process settles the row and stops the clock.
      expect(
        liveClockCadenceMs(items, {}, nowMs, {
          'process-one': { status: 'completed' },
        }),
      ).toBe(0);
    });

    it('surfaces the Bash time label on Activity panel tasks', () => {
      const settledBash = backgroundBashTool();
      const foregroundBash = backgroundBashTool({
        id: 'bash-foreground',
        arguments: { command: 'npm test', mode: 'foreground' },
        result: {
          ok: true,
          data: { status: 'completed', mode: 'foreground' },
          artifacts: [],
        },
      });
      const tasks = backgroundTasks(
        [
          {
            id: 'run-1',
            type: 'assistant_run',
            items: [settledBash, foregroundBash],
          },
        ],
        {},
        {},
        { 'process-one': terminalEntry },
        nowMs,
      );

      const bashTask = tasks.find((task) => task.kind === 'bash');
      expect(bashTask).toEqual(
        expect.objectContaining({
          processId: 'process-one',
          dotStatus: 'success',
          timeLabel: '4m 12s',
        }),
      );
    });
  });
});
