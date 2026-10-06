import { describe, expect, it } from 'vitest';

import { setApplicationTimeZone } from '../dateTimePrefs.svelte.js';
import {
  buildUsageHistorySeries,
  clampUsagePercent,
  formatResetAt,
  formatUsageDelta,
  runActivityTotals,
  usageHistoryIntervalTooltip,
  usageHistoryIntervals,
  usageHistoryPointCoordinates,
  usageHistoryPointTooltip,
  usageHistoryPolylineSegments,
  usageHistorySince,
  usageHistorySlots,
  usageHistorySummary,
  usageSeverity,
} from '../statisticsLimits.js';

const byLabel = (content) =>
  Object.fromEntries(content.rows.map((row) => [row.label, row]));

describe('statisticsLimits provider usage', () => {
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

describe('statisticsLimits usage history', () => {
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

  it('totals Run tokens, reported and estimated alike', () => {
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
      tokens: 127,
    });
    expect(formatUsageDelta(12.5, 'en')).toBe('+12.5 pp');
    expect(formatUsageDelta(null, 'en')).toBe('—');
  });
});

describe('statisticsLimits usage history tooltips', () => {
  it('covers a usage-history plot with point slots and explains each step', () => {
    setApplicationTimeZone('UTC');
    const at = (hour) => `2026-07-25T${String(hour).padStart(2, '0')}:00:00Z`;
    const point = (hour, usedPercent, resetAt = '2026-07-25T15:00:00Z') => ({
      sampledAt: at(hour),
      timestamp: Date.parse(at(hour)),
      usedPercent,
      resetAt,
    });
    const points = [
      point(0, 10),
      point(1, 25),
      point(2, 5, '2026-07-25T20:00:00Z'),
      point(12, 30, '2026-07-25T20:00:00Z'),
    ];
    const now = Date.parse('2026-07-25T13:00:00Z');

    const slots = usageHistorySlots(points);
    expect(slots.map((slot) => slot.index)).toEqual([0, 1, 2, 3]);
    expect(slots[0].left).toBe(0);
    expect(slots.at(-1).left + slots.at(-1).width).toBeCloseTo(100);
    for (let index = 1; index < slots.length; index += 1) {
      expect(slots[index].left).toBeCloseTo(
        slots[index - 1].left + slots[index - 1].width,
      );
    }
    const dense = Array.from({ length: 500 }, (_, index) => ({
      ...point(0, index % 100),
      timestamp: index * 1000,
    }));
    expect(usageHistorySlots(dense, 96)).toHaveLength(96);

    const change = usageHistoryPointTooltip(points, 1, 'en', now);
    expect(change.title).toMatch(/1:00 AM · 12 hours ago$/);
    expect(byLabel(change).Used.value).toBe('25%');
    expect(byLabel(change)['Since previous'].value).toBe('+15 pp (10% → 25%)');
    expect(
      byLabel(usageHistoryPointTooltip(points, 2, 'en', now))['Since previous']
        .value,
    ).toBe('Reset: 25% → 5%');
    expect(
      byLabel(usageHistoryPointTooltip(points, 3, 'en', now))['Since previous'],
    ).toMatchObject({ value: 'No snapshot for 10h', tone: 'muted' });
    expect(usageHistoryPointTooltip(points, 0, 'en', now).rows).toHaveLength(2);

    const interval = usageHistoryIntervalTooltip(
      {
        displayName: 'OpenAI',
        label: '5h',
        account: 'default',
        from: points[0],
        to: points[1],
      },
      'en',
      now,
    );
    expect(interval.title).toBe('OpenAI · 5h');
    expect(byLabel(interval).Account.value).toBe('default');
  });
});
