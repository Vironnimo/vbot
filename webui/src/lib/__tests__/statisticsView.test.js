import { describe, expect, it } from 'vitest';

import {
  DAILY_GRANULARITIES,
  STATISTICS_SUB_VIEWS,
  barFractions,
  clampUsagePercent,
  donutSegments,
  formatDurationMs,
  formatHourLabel,
  formatInteger,
  formatPercent,
  formatResetAt,
  formatShare,
  formatTokens,
  groupModelsByProvider,
  rollupDaily,
  sparklinePoints,
  tokenSplit,
  topN,
  usageSeverity,
} from '../statisticsView.js';

describe('statisticsView formatting', () => {
  it('exposes the five sub-views and three granularities', () => {
    expect(STATISTICS_SUB_VIEWS).toEqual([
      'overview',
      'usage',
      'runs',
      'tools',
      'limits',
    ]);
    expect(DAILY_GRANULARITIES).toEqual(['day', 'week', 'month']);
  });

  it('formats integers and tokens with locale grouping', () => {
    expect(formatInteger(1200, 'en')).toBe('1,200');
    expect(formatTokens(1234567, 'en')).toBe('1,234,567');
    expect(formatInteger(undefined, 'en')).toBe('0');
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
    { date: '2026-06-01', runs: 1, errors: 0 },
    { date: '2026-06-02', runs: 2, errors: 1 },
    { date: '2026-06-08', runs: 4, errors: 2 },
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
      { date: '2026-06-01', runs: 3, errors: 1 },
      { date: '2026-06-08', runs: 4, errors: 2 },
    ]);
  });

  it('rolls up into month buckets', () => {
    const result = rollupDaily(
      [
        { date: '2026-06-30', runs: 1, errors: 0 },
        { date: '2026-07-01', runs: 5, errors: 3 },
      ],
      'month',
    );
    expect(result).toEqual([
      { date: '2026-06', runs: 1, errors: 0 },
      { date: '2026-07', runs: 5, errors: 3 },
    ]);
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

  it('scales bars to fractions of the max', () => {
    expect(barFractions([0, 5, 10])).toEqual([0, 0.5, 1]);
    expect(barFractions([])).toEqual([]);
  });

  it('builds donut segments with cumulative offsets', () => {
    const segments = donutSegments([
      { key: 'completed', value: 3 },
      { key: 'failed', value: 1 },
      { key: 'cancelled', value: 0 },
    ]);
    expect(segments).toHaveLength(2);
    expect(segments[0]).toMatchObject({
      key: 'completed',
      fraction: 0.75,
      offset: 0,
    });
    expect(segments[1]).toMatchObject({
      key: 'failed',
      fraction: 0.25,
      offset: 0.75,
    });
  });

  it('returns no donut segments when the total is zero', () => {
    expect(donutSegments([{ key: 'completed', value: 0 }])).toEqual([]);
  });
});
