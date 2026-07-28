import { describe, expect, it } from 'vitest';

import {
  DAILY_GRANULARITIES,
  STATISTICS_SUB_VIEWS,
  USAGE_HISTORY_RANGES,
  activitySummary,
  agentDisplay,
  barFractions,
  buildActivityTimeline,
  cacheHitRate,
  clampUsagePercent,
  buildUsageHistorySeries,
  formatActivityDate,
  formatChartTick,
  formatDurationMs,
  formatHourLabel,
  formatInteger,
  formatPercent,
  formatResetAt,
  formatShare,
  formatTokens,
  formatUsageRate,
  formatUsageDelta,
  groupModelsByProvider,
  parseOrigin,
  rollupDaily,
  rollupSkillActivationsByAgent,
  runActivityTotals,
  sparklinePoints,
  tokenSplit,
  topN,
  usageSeverity,
  usageHistoryIntervals,
  usageHistoryPointCoordinates,
  usageHistoryPolylineSegments,
  usageHistorySince,
  usageHistorySummary,
} from '../statisticsView.js';

describe('statisticsView formatting', () => {
  it('exposes the six sub-views (skills between tools and limits) and three granularities', () => {
    expect(STATISTICS_SUB_VIEWS).toEqual([
      'overview',
      'usage',
      'runs',
      'tools',
      'skills',
      'limits',
    ]);
    expect(DAILY_GRANULARITIES).toEqual(['day', 'week', 'month']);
    expect(USAGE_HISTORY_RANGES).toEqual(['24h', '7d', '30d', 'all']);
  });

  it('formats integers and tokens with locale grouping', () => {
    expect(formatInteger(1200, 'en')).toBe('1,200');
    expect(formatTokens(1234567, 'en')).toBe('1,234,567');
    expect(formatInteger(undefined, 'en')).toBe('0');
    expect(formatChartTick(12.5, 'en')).toBe('12.5');
  });

  it('formats percentages and shares', () => {
    expect(formatPercent(0.5)).toBe('50.0%');
    expect(formatPercent(null)).toBe('—');
    expect(formatShare(25, 100)).toBe('25.0%');
    expect(formatShare(5, 0)).toBe('0.0%');
  });

  it('formats durations across ms / s / minute ranges', () => {
    expect(formatDurationMs(null)).toBe('—');
    expect(formatDurationMs(950)).toBe('950 ms');
    expect(formatDurationMs(1500)).toBe('1.5 s');
    expect(formatDurationMs(125000)).toBe('2m 5s');
  });

  it('formats hour labels zero-padded', () => {
    expect(formatHourLabel(0)).toBe('00:00');
    expect(formatHourLabel(13)).toBe('13:00');
  });
});

describe('statisticsView provider usage helpers', () => {
  it('clamps usage percentages into [0, 100]', () => {
    expect(clampUsagePercent(42.5)).toBe(42.5);
    expect(clampUsagePercent(150)).toBe(100);
    expect(clampUsagePercent(-5)).toBe(0);
    expect(clampUsagePercent('x')).toBe(0);
  });

  it('buckets severity at the warn / critical thresholds', () => {
    expect(usageSeverity(10)).toBe('ok');
    expect(usageSeverity(74.9)).toBe('ok');
    expect(usageSeverity(75)).toBe('warn');
    expect(usageSeverity(89.9)).toBe('warn');
    expect(usageSeverity(90)).toBe('critical');
    expect(usageSeverity(120)).toBe('critical');
  });

  it('formats a future reset into relative + absolute parts', () => {
    const now = Date.parse('2026-06-16T12:00:00Z');
    const reset = formatResetAt('2026-06-16T15:12:00Z', 'en', now);
    expect(reset.relative).toBe('3h 12m');
    expect(reset.isPast).toBe(false);
    expect(reset.absolute).not.toBe('—');
  });

  it('marks a past reset and yields no relative part', () => {
    const now = Date.parse('2026-06-16T12:00:00Z');
    const reset = formatResetAt('2026-06-16T11:00:00Z', 'en', now);
    expect(reset.isPast).toBe(true);
    expect(reset.relative).toBeNull();
  });

  it('returns null for a missing reset timestamp', () => {
    expect(formatResetAt(null, 'en', Date.now())).toBeNull();
    expect(formatResetAt('not-a-date', 'en', Date.now())).toBeNull();
  });

  it('uses day granularity for resets more than a day out', () => {
    const now = Date.parse('2026-06-16T12:00:00Z');
    const reset = formatResetAt('2026-06-18T18:00:00Z', 'en', now);
    expect(reset.relative).toBe('2d 6h');
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

describe('statisticsView token split', () => {
  it('keeps measured and estimated separate', () => {
    const split = tokenSplit({
      measured_input_tokens: 100,
      measured_output_tokens: 20,
      estimated_input_tokens: 7,
      estimated_output_tokens: 3,
    });
    expect(split.measured).toBe(120);
    expect(split.estimated).toBe(10);
    expect(split.total).toBe(130);
    expect(split.hasEstimated).toBe(true);
    expect(split.hasMeasured).toBe(true);
  });

  it('flags an estimate-only record', () => {
    const split = tokenSplit({
      estimated_input_tokens: 4,
      estimated_output_tokens: 1,
    });
    expect(split.measured).toBe(0);
    expect(split.hasMeasured).toBe(false);
    expect(split.hasEstimated).toBe(true);
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
});

describe('statisticsView rollupDaily', () => {
  const series = [
    {
      date: '2026-06-01',
      runs: 1,
      completed: 1,
      failed: 0,
      cancelled: 0,
      reasoning_tokens: 4,
      reasoning_turns: 1,
    },
    {
      date: '2026-06-02',
      runs: 2,
      completed: 1,
      failed: 1,
      cancelled: 0,
      reasoning_tokens: 6,
      reasoning_turns: 1,
    },
    {
      date: '2026-06-08',
      runs: 4,
      completed: 2,
      failed: 1,
      cancelled: 1,
      reasoning_tokens: 8,
      reasoning_turns: 1,
    },
  ];

  it('returns a copy for day granularity', () => {
    const result = rollupDaily(series, 'day');
    expect(result).toEqual(series);
    expect(result[0]).not.toBe(series[0]);
  });

  it('rolls up into ISO-week buckets', () => {
    // 2026-06-01 is a Monday; 06-02 same week; 06-08 the next Monday.
    const result = rollupDaily(series, 'week');
    expect(result).toEqual([
      {
        date: '2026-06-01',
        runs: 3,
        completed: 2,
        failed: 1,
        cancelled: 0,
        reasoning_tokens: 10,
        reasoning_turns: 2,
      },
      {
        date: '2026-06-08',
        runs: 4,
        completed: 2,
        failed: 1,
        cancelled: 1,
        reasoning_tokens: 8,
        reasoning_turns: 1,
      },
    ]);
  });

  it('rolls up into month buckets', () => {
    const result = rollupDaily(
      [
        { date: '2026-06-30', runs: 1, completed: 1 },
        { date: '2026-07-01', runs: 5, completed: 3, failed: 2 },
      ],
      'month',
    );
    expect(result).toEqual([
      { date: '2026-06', runs: 1, completed: 1 },
      { date: '2026-07', runs: 5, completed: 3, failed: 2 },
    ]);
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
    });
    expect(result.at(-1)).toMatchObject({ date: '2026-06-13', runs: 1 });
  });

  it('rolls outcomes into ISO weeks before filling the 16-week window', () => {
    const result = buildActivityTimeline(
      [
        {
          date: '2026-06-01',
          runs: 1,
          completed: 1,
          failed: 0,
          cancelled: 0,
        },
        {
          date: '2026-06-02',
          runs: 1,
          completed: 0,
          failed: 1,
          cancelled: 0,
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
    });
    expect(result.at(-1)).toMatchObject({ date: '2026-06-08', runs: 0 });
  });

  it('summarizes outcomes, completion rate, peak, and a readable scale', () => {
    expect(
      activitySummary([
        { date: '2026-06-12', runs: 3, completed: 2, failed: 1 },
        { date: '2026-06-13', runs: 11, completed: 9, cancelled: 2 },
      ]),
    ).toEqual({
      totalRuns: 14,
      completed: 11,
      failed: 1,
      cancelled: 2,
      completionRate: 11 / 14,
      peak: { date: '2026-06-13', runs: 11 },
      scaleMax: 15,
    });
    expect(activitySummary([{ date: '2026-06-13', runs: 24 }]).scaleMax).toBe(
      25,
    );
  });

  it('formats day and month bucket labels in UTC', () => {
    expect(formatActivityDate('2026-06-13', 'day', 'en')).toBe('Jun 13');
    expect(formatActivityDate('2026-06', 'month', 'en', { long: true })).toBe(
      'June 2026',
    );
  });
});

describe('statisticsView chart geometry', () => {
  it('builds sparkline points scaled to the max value', () => {
    const points = sparklinePoints([0, 5, 10], 100, 20);
    expect(points).toBe('0,20 50,10 100,0');
  });

  it('returns empty sparkline points for no data', () => {
    expect(sparklinePoints([], 100, 20)).toBe('');
  });

  it('scales sparkline points to an absolute max when given', () => {
    // A ratio series (0–1) keeps its absolute meaning instead of stretching
    // the largest value to the top of the chart.
    const points = sparklinePoints([0.25, 0.5], 100, 20, { max: 1 });
    expect(points).toBe('0,15 100,10');
  });

  it('scales bars to fractions of the max', () => {
    expect(barFractions([0, 5, 10])).toEqual([0, 0.5, 1]);
    expect(barFractions([])).toEqual([]);
  });
});

describe('cacheHitRate', () => {
  it('returns the cache read share of the cache-reporting input', () => {
    expect(
      cacheHitRate({ cache_read_tokens: 800, cache_input_tokens: 1000 }),
    ).toBe(0.8);
  });

  it('returns null when a record has no cache-reporting input', () => {
    // A provider that never reports caching must render as "—", not 0%.
    expect(
      cacheHitRate({ cache_read_tokens: 0, cache_input_tokens: 0 }),
    ).toBeNull();
    expect(cacheHitRate(null)).toBeNull();
    expect(cacheHitRate({})).toBeNull();
  });

  it('treats junk values as zero input', () => {
    expect(
      cacheHitRate({ cache_read_tokens: 10, cache_input_tokens: 'junk' }),
    ).toBeNull();
  });
});

describe('agentDisplay (project-aware agent rendering)', () => {
  it('returns the bare name and null project for an identity agent', () => {
    expect(agentDisplay('researcher')).toEqual({
      name: 'researcher',
      projectId: null,
    });
  });

  it('splits a project-agent address into name + project for the badge', () => {
    expect(agentDisplay('builder@vbot')).toEqual({
      name: 'builder',
      projectId: 'vbot',
    });
  });

  it('falls back to identity rendering for an unexpected/empty value', () => {
    expect(agentDisplay('')).toEqual({ name: '', projectId: null });
    expect(agentDisplay(null)).toEqual({ name: '', projectId: null });
    expect(agentDisplay('a@b@c')).toEqual({ name: 'a@b@c', projectId: null });
  });
});

describe('parseOrigin (skill-usage origin scope/detail)', () => {
  it('returns a bare scope with null detail for scope-only tokens', () => {
    expect(parseOrigin('bundled')).toEqual({ scope: 'bundled', detail: null });
    expect(parseOrigin('global')).toEqual({ scope: 'global', detail: null });
  });

  it('splits scoped tokens into scope + verbatim detail', () => {
    expect(parseOrigin('agent:assistant')).toEqual({
      scope: 'agent',
      detail: 'assistant',
    });
    expect(parseOrigin('project:vBot')).toEqual({
      scope: 'project',
      detail: 'vBot',
    });
  });

  it('keeps only the first colon as the separator (project names may contain colons)', () => {
    expect(parseOrigin('project:a:b')).toEqual({
      scope: 'project',
      detail: 'a:b',
    });
  });

  it('treats an unknown scope or empty detail as a bare token', () => {
    expect(parseOrigin('mystery:x')).toEqual({
      scope: 'mystery:x',
      detail: null,
    });
    expect(parseOrigin('agent:')).toEqual({ scope: 'agent:', detail: null });
  });

  it('yields an empty scope for an empty/non-string value', () => {
    expect(parseOrigin('')).toEqual({ scope: '', detail: null });
    expect(parseOrigin(null)).toEqual({ scope: '', detail: null });
    expect(parseOrigin(42)).toEqual({ scope: '', detail: null });
  });
});

describe('formatUsageRate (skill activation rate)', () => {
  it('renders a whole-percent rate', () => {
    expect(formatUsageRate(0.5)).toBe('50%');
    expect(formatUsageRate(0.125)).toBe('13%');
    expect(formatUsageRate(1)).toBe('100%');
    expect(formatUsageRate(0)).toBe('0%');
  });

  it('renders an em dash when the rate is null (no offered sessions)', () => {
    // usage_rate is null when offered_sessions == 0 — never NaN or a misleading 0%.
    expect(formatUsageRate(null)).toBe('—');
    expect(formatUsageRate(undefined)).toBe('—');
    expect(formatUsageRate(Number.NaN)).toBe('—');
  });
});

describe('rollupSkillActivationsByAgent', () => {
  it('sums an agent activations across every skill, sorted count desc then key asc', () => {
    const result = rollupSkillActivationsByAgent([
      { name: 'deploy', by_agent: [{ key: 'main', count: 2 }] },
      {
        name: 'review',
        by_agent: [
          { key: 'main', count: 1 },
          { key: 'builder@vbot', count: 5 },
        ],
      },
    ]);
    expect(result).toEqual([
      { key: 'builder@vbot', count: 5 },
      { key: 'main', count: 3 },
    ]);
  });

  it('breaks count ties by ascending key for a stable order', () => {
    const result = rollupSkillActivationsByAgent([
      { name: 's', by_agent: [{ key: 'zeta', count: 1 }] },
      { name: 't', by_agent: [{ key: 'alpha', count: 1 }] },
    ]);
    expect(result).toEqual([
      { key: 'alpha', count: 1 },
      { key: 'zeta', count: 1 },
    ]);
  });

  it('ignores missing / malformed by_agent lists and empty keys', () => {
    expect(rollupSkillActivationsByAgent(null)).toEqual([]);
    expect(
      rollupSkillActivationsByAgent([
        { name: 'a' },
        { name: 'b', by_agent: null },
        { name: 'c', by_agent: [{ key: '', count: 9 }] },
      ]),
    ).toEqual([]);
  });
});
