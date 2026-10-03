import { describe, expect, it } from 'vitest';

import {
  agentDisplay,
  agentTooltip,
  autoGranularity,
  axisLabelIndices,
  barEntries,
  cacheHitRate,
  callCostTooltip,
  compactionStrategyLabel,
  compactionStrategyTooltip,
  costTooltip,
  durationColumns,
  errorKindLabel,
  errorKindTooltip,
  formatCost,
  formatDurationMs,
  formatPercent,
  formatSeriesDate,
  formatShare,
  formatShortDateTime,
  formatTokens,
  hourColumns,
  insightLines,
  parseOrigin,
  periodChange,
  rollupSeries,
  sessionTooltip,
  shortGroupId,
  skillCandidates,
  skillConversionTooltip,
  statisticsReportKey,
  statisticsReportParams,
  statisticsTabForPlace,
  statisticsTabSections,
  statisticsWindow,
  tokenTooltip,
  toolRejectionTooltip,
  trendColumns,
  usageRowLabel,
} from '../statisticsView.js';
import { setApplicationTimeZone } from '../dateTimePrefs.svelte.js';

const byLabel = (content) =>
  Object.fromEntries(content.rows.map((row) => [row.label, row]));

describe('statisticsView tabs and windows', () => {
  it('requests each tab’s own sections and maps retired places', () => {
    expect(statisticsTabSections('overview')).toEqual(['overview']);
    expect(statisticsTabSections('usage')).toEqual(['usage']);
    expect(statisticsTabSections('tools')).toEqual(['tools', 'skills']);
    expect(statisticsTabSections('diagnostics')).toEqual(['diagnostics']);
    expect(statisticsTabSections('limits')).toEqual([]);

    expect(statisticsTabForPlace('runs')).toBe('runs');
    expect(statisticsTabForPlace('compactions')).toBe('diagnostics');
    expect(statisticsTabForPlace('skills')).toBe('tools');
    expect(statisticsTabForPlace('')).toBe('overview');
    expect(statisticsTabForPlace('nonsense')).toBe('overview');
  });

  it('starts calendar ranges at local midnight, also across a DST change', () => {
    const now = Date.parse('2026-03-30T10:00:00Z');
    expect(statisticsWindow('24h', 'Europe/Berlin', now)).toEqual({
      since: '2026-03-29T10:00:00.000Z',
    });
    // 2026-03-24 is still winter time (UTC+1), today summer time (UTC+2).
    expect(statisticsWindow('7d', 'Europe/Berlin', now)).toEqual({
      since: '2026-03-23T23:00:00.000Z',
    });
    expect(statisticsWindow('30d', 'America/New_York', now)).toEqual({
      since: '2026-03-01T05:00:00.000Z',
    });
    expect(statisticsWindow('all', 'Europe/Berlin', now)).toEqual({});

    expect(statisticsReportParams('tools', '7d', 'UTC', now)).toEqual({
      since: '2026-03-24T00:00:00.000Z',
      timezone: 'UTC',
      sections: ['tools', 'skills'],
    });
    // The time zone is part of the cache key: a changed zone refetches.
    expect(statisticsReportKey('tools', '7d', 'UTC')).not.toBe(
      statisticsReportKey('tools', '7d', 'Europe/Berlin'),
    );
  });
});

describe('statisticsView numbers', () => {
  it('formats costs, tokens, shares, durations and dates for scanning', () => {
    expect(
      [null, 0, 0.004, 0.006, 12.345, 1234.5].map((value) =>
        formatCost(value, 'en'),
      ),
    ).toEqual(['—', '$0.00', '<$0.01', '$0.01', '$12.35', '$1,234.50']);
    expect(
      [null, 950, 12_345, 1_234_567].map((value) => formatTokens(value, 'en')),
    ).toEqual(['—', '950', '12.3K', '1.2M']);
    expect(formatPercent(0.1234, 'en')).toBe('12.3%');
    expect(formatPercent(null, 'en')).toBe('—');
    expect(formatShare(1, 3, 'en')).toBe('33.3%');
    expect(formatShare(1, 0, 'en')).toBe('—');
    expect(formatShare(null, 3, 'en')).toBe('—');
    expect(
      [null, 850, 12_340, 245_000, 7_800_000].map(formatDurationMs),
    ).toEqual(['—', '850 ms', '12.3 s', '4m 05s', '2h 10m']);

    // Table dates: in the application zone, the year only when it differs.
    const now = new Date('2026-10-02T12:00:00Z');
    setApplicationTimeZone('Europe/Berlin');
    try {
      expect(
        ['2026-09-30T22:30:00+00:00', '2025-12-31T23:30:00+00:00', null].map(
          (value) => formatShortDateTime(value, 'en', now),
        ),
      ).toEqual(['Oct 1, 12:30 AM', 'Jan 1, 12:30 AM', '—']);
      expect(formatShortDateTime('2025-06-01T10:00:00+00:00', 'en', now)).toBe(
        'Jun 1, 2025',
      );
    } finally {
      setApplicationTimeZone('UTC');
    }
  });

  it('measures the cache hit rate only over calls that report a cache', () => {
    expect(
      cacheHitRate({
        cache_calls: 2,
        cache_input_tokens: 1000,
        cache_read_tokens: 250,
      }),
    ).toBe(0.25);
    expect(
      cacheHitRate({ cache_calls: 0, cache_input_tokens: 1000 }),
    ).toBeNull();
    expect(cacheHitRate({ cache_input_tokens: 0 })).toBeNull();
  });

  it('reports changes against the previous period and judges only on request', () => {
    expect(periodChange(12.5, 10, { locale: 'en' })).toEqual({
      direction: 'up',
      text: '25.0%',
      tone: 'neutral',
    });
    expect(periodChange(5, 0)).toMatchObject({ direction: 'up', text: 'new' });
    expect(periodChange(0, 0)).toMatchObject({ direction: 'flat' });
    expect(
      periodChange(0.1, 0.25, { kind: 'points', judge: 'lowerIsBetter' }),
    ).toEqual({ direction: 'down', text: '15.0 pts', tone: 'good' });
    expect(
      periodChange(0.3, 0.25, { kind: 'points', judge: 'lowerIsBetter' }),
    ).toMatchObject({ direction: 'up', tone: 'bad' });
    expect(periodChange(null, 3)).toBeNull();
    expect(periodChange(3, undefined)).toBeNull();
  });
});

describe('statisticsView charts', () => {
  const series = [
    { date: '2026-08-30', cost_usd: 1, input_tokens: 10, output_tokens: 1 },
    { date: '2026-08-31', cost_usd: 2, input_tokens: 20, output_tokens: 2 },
    { date: '2026-09-01', cost_usd: null, input_tokens: 30, output_tokens: 3 },
    { date: '2026-09-07', cost_usd: null, input_tokens: 5, output_tokens: 0 },
  ];

  it('rolls days up into Monday weeks and months, keeping unknown values unknown', () => {
    expect(rollupSeries(series, 'day')).toBe(series);
    expect(rollupSeries(series, 'week')).toEqual([
      { date: '2026-08-24', cost_usd: 1, input_tokens: 10, output_tokens: 1 },
      { date: '2026-08-31', cost_usd: 2, input_tokens: 50, output_tokens: 5 },
      { date: '2026-09-07', cost_usd: null, input_tokens: 5, output_tokens: 0 },
    ]);
    expect(rollupSeries(series, 'month').map((row) => row.date)).toEqual([
      '2026-08-01',
      '2026-09-01',
    ]);
    expect([30, 121, 401].map(autoGranularity)).toEqual([
      'day',
      'week',
      'month',
    ]);
  });

  it('stacks a metric’s segments per period on a nice scale', () => {
    const chart = trendColumns(series, 'tokens', 'day');
    expect(chart.columns.map((column) => column.total)).toEqual([
      11, 22, 33, 5,
    ]);
    expect(chart.columns[0].segments).toEqual([
      { id: 'input', value: 10 },
      { id: 'output', value: 1 },
    ]);
    expect(chart.scaleMax).toBe(50);
    expect(trendColumns([], 'cost').scaleMax).toBe(0);
    // Count axes keep whole ticks: an integer maximum and midpoint.
    expect(
      trendColumns([{ date: '2026-09-01', runs: 13 }], 'runs').scaleMax,
    ).toBe(20);
    expect(
      trendColumns([{ date: '2026-09-01', runs: 1 }], 'runs').scaleMax,
    ).toBe(2);

    // Labels at one regular stride that ends at the latest column.
    expect(axisLabelIndices(30, 6)).toEqual([5, 11, 17, 23, 29]);
    expect(axisLabelIndices(7, 6)).toEqual([0, 2, 4, 6]);
    expect(axisLabelIndices(3, 6)).toEqual([0, 1, 2]);
    expect(formatSeriesDate('2026-09-07', 'day', 'en')).toBe('Sep 7');
    expect(formatSeriesDate('2026-09-07', 'week', 'en', { long: true })).toBe(
      'Week of Sep 7, 2026',
    );
    expect(formatSeriesDate('2026-09-01', 'month', 'en')).toBe('Sep 2026');
  });

  it('keeps an hour series by the hour, labelled in the Settings time zone', () => {
    const hours = [
      { hour_start: '2026-09-07T22:00:00.000000Z', runs: 1 },
      { hour_start: '2026-09-07T23:00:00.000000Z', runs: 2 },
    ];
    expect(rollupSeries(hours, 'hour')).toBe(hours);
    expect(
      trendColumns(hours, 'runs', 'hour').columns.map((column) => column.key),
    ).toEqual(hours.map((row) => row.hour_start));
    setApplicationTimeZone('Europe/Berlin');
    try {
      expect(formatSeriesDate(hours[1].hour_start, 'hour', 'en')).toBe(
        '1:00 AM',
      );
      expect(
        formatSeriesDate(hours[1].hour_start, 'hour', 'en', { long: true }),
      ).toBe('Sep 8, 1:00 AM');
    } finally {
      setApplicationTimeZone('UTC');
    }
  });

  it('stacks Run durations by origin and spreads errors over the day', () => {
    const durations = durationColumns([
      { upper_ms: 10_000, by_origin: { automation: 1, user: 2 } },
      { upper_ms: 3_600_000, by_origin: { user: 1 } },
      { upper_ms: null, by_origin: { user: 0 } },
    ]);
    expect(durations.origins).toEqual(['user', 'automation']);
    expect(durations.columns.map((column) => column.label)).toEqual([
      '≤10s',
      '≤1h',
      '>1h',
    ]);
    expect(durations.columns[0].total).toBe(3);
    expect(durations.scaleMax).toBe(4);

    const hours = hourColumns([{ hour: 9, count: 25 }]);
    expect(hours.columns).toHaveLength(24);
    expect(hours.columns[9]).toMatchObject({ label: '09:00', total: 25 });
    expect(hours.scaleMax).toBe(30);

    expect(
      barEntries([{ count: 4 }, { count: 1 }]).map((item) => item.fraction),
    ).toEqual([1, 0.25]);
  });
});

describe('statisticsView labels and tooltips', () => {
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
    for (const [value, name] of [
      ['extension:', 'extension:'],
      ['', ''],
      [null, ''],
      ['a@b@c', 'a@b@c'],
    ]) {
      expect(agentDisplay(value)).toEqual(identity(name));
    }
    expect(shortGroupId('swr_0123456789abcdef')).toBe('abcdef');
    expect(shortGroupId(null)).toBe('');
    expect(usageRowLabel('project', '')).toBe('Outside a Project');
    expect(usageRowLabel('agent', '')).toBe('Outside a Session');
    expect(usageRowLabel('agent', 'extension:swarm')).toBe('swarm');
    expect(usageRowLabel('origin', 'automation')).toBe('Automation');
    expect(usageRowLabel('kind', 'chat')).toBe('Chat');
  });

  it('names Agents and Sessions first and keeps ids as secondary facts', () => {
    expect(agentTooltip('main@web')).toMatchObject({
      title: 'main',
      rows: [
        { label: 'Project', value: 'web' },
        { label: 'Agent ID', value: 'main@web', mono: true },
      ],
    });
    expect(agentTooltip('main')).toEqual({ text: 'main', whenTruncated: true });
    expect(
      sessionTooltip({ session_id: 'ses_1', session_title: 'Parser rework' }),
    ).toEqual({
      title: 'Parser rework',
      rows: [{ label: 'Session ID', value: 'ses_1', mono: true }],
    });
    expect(sessionTooltip({ session_id: 'ses_2' })).toMatchObject({
      text: 'ses_2',
      whenTruncated: true,
    });
  });

  it('explains token and cost values with their exact parts', () => {
    expect(tokenTooltip(1_234_567, 0, 'en')).toBe('1,234,567');
    expect(tokenTooltip(1_234_567, 2_000, 'en')).toEqual({
      title: '1,234,567',
      rows: [{ label: 'Estimated', value: '2,000', tone: 'warning' }],
    });

    const cost = costTooltip(
      {
        cost_usd: 12.5,
        reported_cost_usd: 4.5,
        reported_calls: 10,
        estimated_cost_usd: 8,
        estimated_calls: 28,
        unpriced_calls: 2,
        retrospective_calls: 0,
      },
      'en',
    );
    expect(cost.title).toBe('$12.50');
    expect(byLabel(cost)['Provider-reported cost'].value).toBe(
      '$4.50 · 10 calls',
    );
    expect(byLabel(cost)['Calls without a price']).toMatchObject({
      value: '2 calls',
      tone: 'warning',
    });
    expect(Object.keys(byLabel(cost))).toHaveLength(3);
    expect(costTooltip({ cost_usd: 0 })).toBe('');

    const catalog = callCostTooltip(
      {
        retrospective: true,
        estimated_tokens: true,
        cost: {
          source: 'catalog',
          amount_usd: 0.0123,
          pricing: {
            rates: { input: 3, output: 15, tiers: [{}, {}] },
            source: 'models.dev',
          },
        },
      },
      'en',
    );
    expect(catalog.title).toBe('Current catalog estimate');
    expect(byLabel(catalog).Input.value).toBe('$3.00 / 1M');
    expect(byLabel(catalog)['Price tiers'].value).toBe('2');
    expect(byLabel(catalog)['Price source'].value).toBe('models.dev');
    expect(byLabel(catalog).Tokens).toMatchObject({
      value: 'Estimated',
      tone: 'warning',
    });
    expect(
      callCostTooltip({ cost: { source: 'unknown', reason: 'missing_price' } }),
    ).toEqual({
      title: 'Unpriced',
      rows: [{ label: 'Why unpriced', value: 'No matching catalog price' }],
    });
  });

  it('shows the evidence behind Skill conversion, candidates and Tool rejections', () => {
    expect(
      skillConversionTooltip({
        offered_sessions: 8,
        activated_sessions: 5,
        activated_offered_sessions: 3,
      }),
    ).toEqual({
      text: '3 of 8 Sessions that offered this Skill also activated it.',
      rows: [{ label: 'Activated without recorded offer', value: '2' }],
    });
    expect(skillConversionTooltip({ offered_sessions: 0 })).toMatch(
      /^No Session has recorded/,
    );
    expect(
      skillCandidates([
        { name: 'used', offered_sessions: 2, activated_offered_sessions: 1 },
        { name: 'idle', offered_sessions: 3, activated_offered_sessions: 0 },
        { name: 'unknown', offered_sessions: 0, activated_offered_sessions: 0 },
      ]).map((skill) => skill.name),
    ).toEqual(['idle']);
    for (const [origin, parsed] of [
      ['global', { scope: 'global', detail: null }],
      ['project:a:b', { scope: 'project', detail: 'a:b' }],
      ['agent:', { scope: 'agent:', detail: null }],
      [null, { scope: '', detail: null }],
    ]) {
      expect(parseOrigin(origin)).toEqual(parsed);
    }

    expect(
      toolRejectionTooltip({
        calls: 10,
        rejected: 4,
        top_codes: [
          { code: 'invalid_path', count: 3 },
          { code: 'denied', count: 1 },
        ],
      }),
    ).toEqual({
      title: '4 of 10 calls rejected',
      rows: [
        { label: 'invalid_path', value: '3', mono: true },
        { label: 'denied', value: '1', mono: true },
      ],
    });
    expect(toolRejectionTooltip({ rejected: 0, top_codes: [] })).toBe('');
  });

  it('names recorded error kinds and keeps their ids readable', () => {
    expect(errorKindLabel('rate_limit')).toBe('Rate limit');
    expect(errorKindTooltip('provider_fatal')).toEqual({
      title: 'Request rejected by the Provider',
      rows: [{ label: 'Recorded kind', value: 'provider_fatal', mono: true }],
    });
    // A kind this WebUI does not know reads as its id in words.
    expect(errorKindLabel('quota_exhausted')).toBe('Quota exhausted');
    expect(errorKindTooltip('quota_exhausted').rows[0].value).toBe(
      'quota_exhausted',
    );
  });

  it('names Compaction strategies like Settings and keeps unknown ids raw', () => {
    expect(compactionStrategyLabel('summary_tail')).toBe('With tail');
    expect(compactionStrategyTooltip('continuation')).toMatchObject({
      title: 'Classic',
      rows: [{ label: 'Stored ID', value: 'continuation', mono: true }],
    });
    expect(compactionStrategyLabel('future_mode')).toBe('future_mode');
    expect(compactionStrategyTooltip('future_mode')).toBe('');
  });
});

describe('statisticsView insights', () => {
  it('turns known insights into sentences linked to their tab and skips the rest', () => {
    const lines = insightLines(
      [
        {
          id: 'uncached_value',
          severity: 'warn',
          values: { share: 0.25, cost_usd: 2, top_model: 'local/qwen' },
        },
        {
          id: 'failed_attempt_burst',
          severity: 'info',
          values: { failed: 5, hour_start: null },
        },
        { id: 'top_runs_share', values: { runs: 3, share: 0.5 } },
        { id: 'from_a_newer_server', severity: 'warn', values: {} },
      ],
      'en',
    );
    expect(lines.map(({ id, severity, tab }) => [id, severity, tab])).toEqual([
      ['uncached_value', 'warn', 'usage'],
      ['failed_attempt_burst', 'info', 'diagnostics'],
      ['top_runs_share', 'info', 'runs'],
    ]);
    expect(lines[0].text).toBe(
      '25.0% of the estimated value ($2.00) comes from calls without cache data, most of them to local/qwen, so it may be overstated.',
    );
    expect(lines[2].text).toBe(
      'The 3 most expensive Runs account for 50.0% of the cost.',
    );
    expect(insightLines(null)).toEqual([]);
  });
});
