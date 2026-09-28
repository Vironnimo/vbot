import { describe, expect, it } from 'vitest';

import {
  DAILY_GRANULARITIES,
  STATISTICS_SUB_VIEWS,
  USAGE_HISTORY_RANGES,
  agentDisplay,
  barFractions,
  buildActivityTimeline,
  buildUsageHistorySeries,
  cacheHitRate,
  clampUsagePercent,
  formatActivityDate,
  formatChartTick,
  formatCost,
  formatDurationMs,
  formatHourLabel,
  formatInteger,
  formatOptionalTokens,
  formatPercent,
  formatResetAt,
  formatShare,
  formatTokens,
  formatUsageDelta,
  formatUsageRate,
  groupModelsByProvider,
  parseOrigin,
  rollupSkillActivationsByAgent,
  runActivityTotals,
  shortGroupId,
  statisticsInsights,
  statisticsWindow,
  timelineTicks,
  tokenSplit,
  tokenTimeline,
  topN,
  usageHistoryIntervals,
  usageHistoryPointCoordinates,
  usageHistoryPolylineSegments,
  usageHistorySince,
  usageHistorySummary,
  usageSeverity,
} from '../statisticsView.js';

describe('statistics dashboard projections', () => {
  it('uses UTC calendar days across month boundaries and leaves all time unfiltered', () => {
    const now = Date.parse('2026-03-03T23:30:00-05:00');
    expect(statisticsWindow('7d', now)).toEqual({
      since: '2026-02-26T00:00:00.000Z',
      until: '2026-03-04T04:30:00.000Z',
    });
    expect(statisticsWindow('all', now)).toEqual({});
    expect(statisticsWindow('30d', now).since).toBe('2026-02-03T00:00:00.000Z');
    expect(statisticsWindow('90d', now).since).toBe('2025-12-05T00:00:00.000Z');
  });

  it('fills absent periods and retains token provenance on a single scale', () => {
    const window = {
      since: '2026-02-27T00:00:00Z',
      until: '2026-03-01T12:00:00Z',
    };
    const points = buildActivityTimeline(
      [
        {
          date: '2026-02-27',
          measured_input_tokens: 900,
          measured_output_tokens: 100,
          estimated_input_tokens: 10,
          cache_read_tokens: 600,
          cache_input_tokens: 900,
        },
        {
          date: '2026-03-01',
          measured_input_tokens: 100,
          estimated_output_tokens: 100,
        },
      ],
      'day',
      window.until,
      window,
    );
    expect(points.map((point) => point.date)).toEqual([
      '2026-02-27',
      '2026-02-28',
      '2026-03-01',
    ]);
    const chart = tokenTimeline(points);
    expect(
      chart.points.map(({ measured, estimated }) => [measured, estimated]),
    ).toEqual([
      [1000, 10],
      [0, 0],
      [100, 100],
    ]);
    expect(chart.scaleMax).toBe(1500);
    expect(cacheHitRate(chart.points[1])).toBeNull();
    const months = buildActivityTimeline(points, 'month', window.until, window);
    expect(months.map((point) => point.date)).toEqual(['2026-02', '2026-03']);
    expect(tokenTimeline(months).points[0].measured).toBe(1000);
  });

  it('keeps single-period ticks unique and empty token charts finite', () => {
    const points = [{ date: '2026-06' }];
    expect(timelineTicks(points)).toEqual(points);
    expect(timelineTicks([])).toEqual([]);
    expect(tokenTimeline([])).toEqual({ points: [], scaleMax: 0 });
  });

  it('distinguishes Tool rejections from absent outcome evidence', () => {
    const report = {
      overview: {
        total_runs: 4,
        runs_with_tool_calls: 3,
        daily_trend: [{ runs: 0 }, { runs: 4 }],
      },
      tools: {
        total_calls: 10,
        tools: [
          {
            successes: 4,
            failures: 2,
            error_codes: [{ key: 'invalid', count: 2 }],
          },
          {
            successes: 1,
            failures: 1,
            error_codes: [{ key: 'invalid', count: 1 }],
          },
        ],
      },
    };
    expect(statisticsInsights(report)).toEqual({
      activeDays: 1,
      toolRunShare: 0.75,
      accepted: 5,
      rejected: 3,
      unknown: 2,
      rejectionCodes: [{ key: 'invalid', count: 3 }],
    });
    expect(statisticsInsights(null)).toMatchObject({
      activeDays: 0,
      toolRunShare: null,
      unknown: 0,
      rejectionCodes: [],
    });
  });
});

describe('statisticsView activity timeline', () => {
  it('fills inactive calendar days and keeps a fixed 30-day window', () => {
    const result = buildActivityTimeline(
      [
        {
          date: '2026-06-11',
          runs: 2,
          completed: 1,
          failed: 1,
          cancelled: 0,
        },
        {
          date: '2026-06-13',
          runs: 1,
          completed: 1,
          failed: 0,
          cancelled: 0,
        },
      ],
      'day',
      '2026-06-13T10:00:00Z',
    );

    expect(result).toHaveLength(30);
    expect(result.at(-3)).toMatchObject({ date: '2026-06-11', runs: 2 });
    expect(result.at(-2)).toEqual({
      date: '2026-06-12',
      runs: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
      interrupted: 0,
    });
    expect(result.at(-1)).toMatchObject({ date: '2026-06-13', runs: 1 });
  });

  it('sums every numeric field into ISO weeks before filling the 16-week window', () => {
    // 2026-06-01 is a Monday; 06-02 falls into the same week.
    const result = buildActivityTimeline(
      [
        {
          date: '2026-06-01',
          runs: 1,
          completed: 1,
          failed: 0,
          cancelled: 0,
          reasoning_tokens: 4,
        },
        {
          date: '2026-06-02',
          runs: 1,
          completed: 0,
          failed: 1,
          cancelled: 0,
          reasoning_tokens: 6,
        },
      ],
      'week',
      '2026-06-08T12:00:00Z',
    );

    expect(result).toHaveLength(16);
    expect(result.at(-2)).toEqual({
      date: '2026-06-01',
      runs: 2,
      completed: 1,
      failed: 1,
      cancelled: 0,
      interrupted: 0,
      reasoning_tokens: 10,
    });
    expect(result.at(-1)).toMatchObject({ date: '2026-06-08', runs: 0 });
  });

  it('formats day and month bucket labels in UTC', () => {
    expect(formatActivityDate('2026-06-13', 'day', 'en')).toBe('Jun 13');
    expect(formatActivityDate('2026-06', 'month', 'en', { long: true })).toBe(
      'June 2026',
    );
  });

  it('scales bars to fractions of the max', () => {
    expect(barFractions([0, 5, 10])).toEqual([0, 0.5, 1]);
    expect(barFractions([])).toEqual([]);
  });
});

describe('statisticsView formatting', () => {
  it('exposes the eight sub-views, three granularities and the usage history ranges', () => {
    expect(STATISTICS_SUB_VIEWS).toEqual([
      'overview',
      'usage',
      'compactions',
      'limits',
      'runs',
      'tools',
      'skills',
      'extensions',
    ]);
    expect(DAILY_GRANULARITIES).toEqual(['day', 'week', 'month']);
    expect(USAGE_HISTORY_RANGES).toEqual(['24h', '7d', '30d', 'all']);
  });

  it('formats integers and tokens with locale grouping', () => {
    expect(formatInteger(1200, 'en')).toBe('1,200');
    expect(formatTokens(1234567, 'en')).toBe('1,234,567');
    expect(formatInteger(undefined, 'en')).toBe('0');
    expect(formatOptionalTokens(null)).toBe('—');
    expect(formatOptionalTokens(0)).toBe('0');
    expect(formatChartTick(12.5, 'en')).toBe('12.5');
    expect(formatChartTick(1000000, 'en', { compact: true })).toBe('1M');
  });

  it('distinguishes absent, free, and tiny charges', () => {
    expect(formatCost(null)).toBe('—');
    expect(formatCost(0)).toBe('$0.00');
    expect(formatCost(0.0000084)).toBe('<$0.0001');
    expect(formatCost(0.0000084, 'en', { exact: true })).toBe('$0.0000084');
    expect(formatCost(-1)).toBe('—');
  });

  it('formats ratios as percentages and unknown rates as an em dash', () => {
    expect(formatPercent(0.5)).toBe('50.0%');
    expect(formatPercent(null)).toBe('—');
    expect(formatShare(25, 100)).toBe('25.0%');
    expect(formatShare(5, 0)).toBe('0.0%');
    // A Skill usage rate is null when it was never offered: never NaN or 0%.
    expect([0.5, 0.125, 1, 0].map(formatUsageRate)).toEqual([
      '50%',
      '13%',
      '100%',
      '0%',
    ]);
    for (const unknown of [null, undefined, Number.NaN]) {
      expect(formatUsageRate(unknown)).toBe('—');
    }
  });

  it('formats durations across ms / s / minute ranges and zero-padded hours', () => {
    expect(formatDurationMs(null)).toBe('—');
    expect(formatDurationMs(950)).toBe('950 ms');
    expect(formatDurationMs(1500)).toBe('1.5 s');
    expect(formatDurationMs(125000)).toBe('2m 5s');
    expect(formatHourLabel(0)).toBe('00:00');
    expect(formatHourLabel(13)).toBe('13:00');
  });
});

describe('statisticsView provider usage', () => {
  it('clamps usage percentages into [0, 100] and buckets severity at the warn / critical thresholds', () => {
    expect([42.5, 150, -5, 'x'].map(clampUsagePercent)).toEqual([
      42.5, 100, 0, 0,
    ]);
    expect([10, 74.9, 75, 89.9, 90, 120].map(usageSeverity)).toEqual([
      'ok',
      'ok',
      'warn',
      'warn',
      'critical',
      'critical',
    ]);
  });

  it('formats a reset as relative and absolute parts, days apart when far out', () => {
    const now = Date.parse('2026-06-16T12:00:00Z');
    const soon = formatResetAt('2026-06-16T15:12:00Z', 'en', now);
    expect(soon.relative).toBe('3h 12m');
    expect(soon.isPast).toBe(false);
    expect(soon.absolute).not.toBe('—');
    expect(formatResetAt('2026-06-18T18:00:00Z', 'en', now).relative).toBe(
      '2d 6h',
    );

    const past = formatResetAt('2026-06-16T11:00:00Z', 'en', now);
    expect(past.isPast).toBe(true);
    expect(past.relative).toBeNull();

    expect(formatResetAt(null, 'en', now)).toBeNull();
    expect(formatResetAt('not-a-date', 'en', now)).toBeNull();
  });
});

describe('statisticsView provider usage history', () => {
  const samples = [
    {
      sampled_at: '2026-07-25T10:00:00Z',
      providers: [
        {
          connection: 'openai:subscription',
          account: 'default',
          display_name: 'OpenAI',
          plan: 'plus',
          windows: [
            {
              label: '5h',
              used_percent: 20,
              reset_at: '2026-07-25T15:00:00Z',
              window_seconds: 18000,
            },
          ],
        },
      ],
    },
    {
      sampled_at: '2026-07-25T11:00:00Z',
      providers: [
        {
          connection: 'openai:subscription',
          account: 'default',
          display_name: 'OpenAI',
          plan: 'plus',
          windows: [
            {
              label: '5h',
              used_percent: 48.5,
              reset_at: '2026-07-25T15:00:00Z',
              window_seconds: 18000,
            },
          ],
        },
      ],
    },
    {
      sampled_at: '2026-07-25T12:00:00Z',
      providers: [
        {
          connection: 'openai:subscription',
          account: 'default',
          display_name: 'OpenAI',
          plan: 'plus',
          windows: [
            {
              label: '5h',
              used_percent: 3,
              reset_at: '2026-07-25T17:00:00Z',
              window_seconds: 18000,
            },
          ],
        },
      ],
    },
  ];

  it('builds stable per-target/window series and ranks comparable changes', () => {
    const series = buildUsageHistorySeries(samples);
    const intervals = usageHistoryIntervals(series);

    expect(series).toHaveLength(1);
    expect(series[0]).toMatchObject({
      connection: 'openai:subscription',
      account: 'default',
      label: '5h',
    });
    expect(series[0].points).toHaveLength(3);
    expect(intervals).toHaveLength(2);
    expect(intervals[0]).toMatchObject({
      kind: 'change',
      delta: 28.5,
    });
    expect(intervals[1].kind).toBe('reset');
  });

  it('breaks chart lines at reset boundaries instead of implying continuity', () => {
    const [series] = buildUsageHistorySeries(samples);

    expect(usageHistoryPolylineSegments(series.points)).toHaveLength(2);
    expect(usageHistoryPointCoordinates(series.points)).toHaveLength(3);
    expect(usageHistoryPointCoordinates(series.points).at(-1)).toEqual({
      x: 720,
      y: 155.2,
    });
  });

  it('summarizes availability and computes range timestamps', () => {
    const withError = [
      ...samples,
      {
        sampled_at: '2026-07-25T13:00:00Z',
        providers: [
          {
            connection: 'openai:subscription',
            account: 'default',
            windows: [],
            error: 'Timeout',
          },
        ],
      },
    ];

    expect(usageHistorySummary(withError)).toMatchObject({
      samples: 4,
      targets: 1,
      unavailable: 1,
    });
    expect(usageHistorySince('24h', Date.parse('2026-07-25T13:00:00Z'))).toBe(
      '2026-07-24T13:00:00.000Z',
    );
    expect(usageHistorySince('all')).toBeNull();
  });

  it('totals measured and estimated Run tokens separately', () => {
    expect(
      runActivityTotals([
        {
          measured_input_tokens: 100,
          measured_output_tokens: 20,
          estimated_input_tokens: 5,
          estimated_output_tokens: 2,
        },
      ]),
    ).toEqual({
      runs: 1,
      measuredTokens: 120,
      estimatedTokens: 7,
    });
    expect(formatUsageDelta(12.5, 'en')).toBe('+12.5 pp');
    expect(formatUsageDelta(null, 'en')).toBe('—');
  });
});

describe('statisticsView token records', () => {
  it('keeps measured and estimated tokens separate', () => {
    expect(
      tokenSplit({
        measured_input_tokens: 100,
        measured_output_tokens: 20,
        estimated_input_tokens: 7,
        estimated_output_tokens: 3,
      }),
    ).toMatchObject({
      measured: 120,
      estimated: 10,
      total: 130,
      hasMeasured: true,
      hasEstimated: true,
    });
    expect(
      tokenSplit({ estimated_input_tokens: 4, estimated_output_tokens: 1 }),
    ).toMatchObject({ measured: 0, hasMeasured: false, hasEstimated: true });
  });

  it('returns the cache read share only for records with cache-reporting input', () => {
    expect(
      cacheHitRate({ cache_read_tokens: 800, cache_input_tokens: 1000 }),
    ).toBe(0.8);
    // A provider that never reports caching must render as "—", not 0%.
    for (const record of [
      { cache_read_tokens: 0, cache_input_tokens: 0 },
      { cache_read_tokens: 10, cache_input_tokens: 'junk' },
      {},
      null,
    ]) {
      expect(cacheHitRate(record)).toBeNull();
    }
  });
});

describe('statisticsView selection and grouping', () => {
  it('returns at most N entries', () => {
    expect(topN([1, 2, 3, 4], 2)).toEqual([1, 2]);
    expect(topN(null, 3)).toEqual([]);
  });

  it('groups models by provider sorted by token volume', () => {
    const groups = groupModelsByProvider([
      { provider: 'openai', model: 'openai/gpt-5', total_tokens: 50 },
      { provider: 'openrouter', model: 'openrouter/x', total_tokens: 200 },
      { provider: 'openai', model: 'openai/gpt-4', total_tokens: 30 },
    ]);
    expect(groups.map((group) => group.provider)).toEqual([
      'openrouter',
      'openai',
    ]);
    expect(groups[1].models).toHaveLength(2);
    expect(groups[1].totalTokens).toBe(80);
  });

  it('renders identity, Project and Extension activity keys by name', () => {
    const identity = (name) => ({ name, projectId: null, extension: false });
    expect(agentDisplay('researcher')).toEqual(identity('researcher'));
    expect(agentDisplay('builder@vbot')).toEqual({
      name: 'builder',
      projectId: 'vbot',
      extension: false,
    });
    expect(agentDisplay('extension:swarm')).toEqual({
      name: 'swarm',
      projectId: null,
      extension: true,
    });
    // Unexpected or empty values fall back to identity rendering.
    for (const [value, name] of [
      ['extension:', 'extension:'],
      ['', ''],
      [null, ''],
      ['a@b@c', 'a@b@c'],
    ]) {
      expect(agentDisplay(value)).toEqual(identity(name));
    }
  });

  it('keeps a short stable group id suffix and tolerates missing ids', () => {
    expect(shortGroupId('swr_0123456789abcdef')).toBe('abcdef');
    expect(shortGroupId('abc')).toBe('abc');
    expect(shortGroupId(null)).toBe('');
  });
});

describe('statisticsView Skills', () => {
  it('splits a Skill origin into its scope and verbatim detail', () => {
    const cases = [
      ['bundled', { scope: 'bundled', detail: null }],
      ['global', { scope: 'global', detail: null }],
      ['agent:assistant', { scope: 'agent', detail: 'assistant' }],
      ['project:vBot', { scope: 'project', detail: 'vBot' }],
      // Only the first colon separates: Project names may contain colons.
      ['project:a:b', { scope: 'project', detail: 'a:b' }],
      // An unknown scope or an empty detail stays a bare token.
      ['mystery:x', { scope: 'mystery:x', detail: null }],
      ['agent:', { scope: 'agent:', detail: null }],
      ['', { scope: '', detail: null }],
      [null, { scope: '', detail: null }],
      [42, { scope: '', detail: null }],
    ];
    for (const [origin, parsed] of cases) {
      expect(parseOrigin(origin)).toEqual(parsed);
    }
  });

  it('sums Agent activations across Skills in stable count and key order', () => {
    expect(
      rollupSkillActivationsByAgent([
        { name: 'deploy', by_agent: [{ key: 'main', count: 2 }] },
        {
          name: 'review',
          by_agent: [
            { key: 'main', count: 1 },
            { key: 'builder@vbot', count: 5 },
            { key: 'zeta', count: 1 },
            { key: 'alpha', count: 1 },
          ],
        },
        { name: 'a' },
        { name: 'b', by_agent: null },
        { name: 'c', by_agent: [{ key: '', count: 9 }] },
      ]),
    ).toEqual([
      { key: 'builder@vbot', count: 5 },
      { key: 'main', count: 3 },
      { key: 'alpha', count: 1 },
      { key: 'zeta', count: 1 },
    ]);
    expect(rollupSkillActivationsByAgent(null)).toEqual([]);
  });
});
