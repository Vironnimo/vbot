// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { activeLocaleTag, t } from '../../lib/i18n.js';
import { formatDateTime } from '../../lib/statisticsView.js';
import {
  flushSync,
  mount,
  rpcMock,
  StatisticsView,
  makeReport,
  waitForCondition,
  waitForOverview,
  buttonNamed,
  cardValue,
  setupStatisticsViewSuite,
} from './StatisticsView.support.js';

describe('StatisticsView', () => {
  const suite = setupStatisticsViewSuite();

  it('loads the report on mount and renders the overview', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    expect(rpcMock).toHaveBeenCalledWith('statistics.report', {});
    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(30);
    expect(
      [
        ...document.querySelectorAll(
          '.stats-panel > .stats-grid .stats-card__value',
        ),
      ].map((node) => node.textContent),
    ).toEqual(['1,200', '$0.012', '$0.043', '10.0%']);
    expect(cardValue('statistics.compactions.averageAfter')).toBe('40,000');
    expect(document.body.textContent).toContain(t('statistics.cost.unpriced'));
    expect(document.body.textContent).toContain(
      'openrouter/anthropic/claude-sonnet-4',
    );
    expect(document.querySelector('.stats-view.view-frame')).toBeTruthy();
    expect(document.querySelector('.stats-view .view-header')).toBeTruthy();
    expect(
      document.querySelector(
        '.stats-view .view-toolbar--tabs [role="tablist"]',
      ),
    ).toBeTruthy();
    expect(
      document.querySelector('.stats-view .view-toolbar__actions'),
    ).toBeTruthy();
  });

  it('applies a UTC window to every report tab and retains the previous scope while loading', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-06-13T10:00:00Z'));
    let resolveReport;
    rpcMock
      .mockResolvedValue(makeReport())
      .mockResolvedValueOnce(makeReport())
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveReport = resolve;
          }),
      );
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.range.7d').click();
    flushSync();
    const window = {
      since: '2026-06-07T00:00:00.000Z',
      until: '2026-06-13T10:00:00.000Z',
    };
    expect(rpcMock).toHaveBeenLastCalledWith('statistics.report', window);
    expect(
      buttonNamed('statistics.range.all').getAttribute('aria-pressed'),
    ).toBe('true');
    expect(buttonNamed('statistics.range.7d').disabled).toBe(true);
    expect(
      document.querySelector('[role="tabpanel"]').getAttribute('aria-busy'),
    ).toBe('true');
    buttonNamed('statistics.subview.usage').click();
    flushSync();
    expect(rpcMock).toHaveBeenCalledTimes(2);
    const filtered = makeReport({ window });
    filtered.usage.totals.measured_input_tokens = 321;
    resolveReport(filtered);
    await waitForCondition(
      () =>
        buttonNamed('statistics.range.7d').getAttribute('aria-pressed') ===
        'true',
    );
    expect(
      document.querySelector('.stats-columns .stats-card__value').textContent,
    ).toBe('321');
    expect(
      document.querySelectorAll('.stats-token-chart .stats-activity__col'),
    ).toHaveLength(7);
    buttonNamed('statistics.subview.overview').click();
    flushSync();
    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(7);
    buttonNamed('statistics.range.all').click();
    expect(rpcMock).toHaveBeenLastCalledWith('statistics.report', {});
  });

  it('keeps the last report on a failed filter request and retries that requested window', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-06-13T10:00:00Z'));
    const window = {
      since: '2026-05-15T00:00:00.000Z',
      until: '2026-06-13T10:00:00.000Z',
    };
    rpcMock
      .mockResolvedValueOnce(makeReport())
      .mockRejectedValueOnce(new Error('report-error-sentinel'))
      .mockResolvedValueOnce(makeReport({ window }));
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.range.30d').click();
    await waitForCondition(() => document.querySelector('.banner--error'));
    expect(document.body.textContent).toContain('report-error-sentinel');
    expect(cardValue('statistics.usage.measuredTokens')).toBe('1,200');
    expect(
      buttonNamed('statistics.range.all').getAttribute('aria-pressed'),
    ).toBe('true');
    buttonNamed('common.retry').click();
    await waitForCondition(
      () =>
        buttonNamed('statistics.range.30d').getAttribute('aria-pressed') ===
        'true',
    );
    expect(rpcMock).toHaveBeenLastCalledWith('statistics.report', window);
    expect(document.querySelector('.banner--error')).toBeNull();
  });

  it('exposes exact token values to keyboard users and keeps missing cache data unavailable', async () => {
    const report = makeReport();
    report.usage.totals.cache_turns = 0;
    report.usage.totals.cache_input_tokens = 0;
    rpcMock.mockResolvedValue(report);
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.subview.usage').click();
    flushSync();
    const point = document.querySelector(
      '.stats-token-chart .stats-activity__col:last-child',
    );
    point.focus();
    expect(document.activeElement).toBe(point);
    expect(point.getAttribute('aria-label')).toContain('1,200');
    expect(point.getAttribute('aria-label')).toContain('35');
    expect(
      Number.parseFloat(
        point.querySelector('.stats-activity__bar').style.height,
      ),
    ).toBeCloseTo(82.3333);
    const details = document
      .querySelector('.stats-token-chart')
      .parentElement.querySelector('details');
    details.querySelector('summary').click();
    expect(details.open).toBe(true);
    expect(
      [...details.querySelectorAll('tbody tr')].at(-1).textContent,
    ).toContain('1,200');
    expect(cardValue('statistics.usage.cacheHitRate')).toBe('—');
    expect(cardValue('statistics.usage.cacheRead')).toBe('—');
    expect(cardValue('statistics.usage.cacheWrite')).toBe('—');
  });

  it('renders Runs & errors with fallback labelling, busiest Sessions and Model errors', async () => {
    const report = makeReport();
    report.runs.top_sessions_by_runs = [
      { agent_id: 'main', session_id: 'session-ranking-sentinel', runs: 3 },
    ];
    report.errors.by_model = [{ key: 'model-error-sentinel', count: 1 }];
    rpcMock.mockResolvedValue(report);
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.subview.runs').click();
    flushSync();

    expect(
      document.querySelectorAll('.stats-panel > .stats-block > .stats-grid'),
    ).toHaveLength(3);
    expect(cardValue('statistics.runs.fallbackRuns')).toBe('1');
    expect(document.querySelectorAll('.stats-hours__col')).toHaveLength(24);
    expect(document.querySelector('.stats-panel .stats-table')).toBeTruthy();
    expect(document.body.textContent).toContain('session-ranking-sentinel');
    expect(document.body.textContent).toContain('model-error-sentinel');
  });

  it('renders Tool outcomes without arguments and keeps unknown results distinct', async () => {
    const report = makeReport();
    report.tools.tools[0].error_codes.push({
      key: 'second-code-sentinel',
      count: 1,
    });
    rpcMock.mockResolvedValue(report);
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.subview.tools').click();
    flushSync();

    expect(cardValue('statistics.tools.accepted')).toBe('4');
    expect(cardValue('statistics.tools.rejected')).toBe('1');
    expect(cardValue('statistics.tools.unknown')).toBe('2');
    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent).toContain('not_found');
    expect(document.body.textContent).toContain('second-code-sentinel');
    expect(document.querySelectorAll('.stats-panel .stats-table')).toHaveLength(
      2,
    );
  });

  it('switches the calendar-correct activity window with the granularity', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    buttonNamed('statistics.granularity.week').click();
    flushSync();

    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(16);
    buttonNamed('statistics.subview.usage').click();
    flushSync();
    expect(
      buttonNamed('statistics.granularity.week').getAttribute('aria-pressed'),
    ).toBe('true');
    buttonNamed('statistics.granularity.month').click();
    flushSync();
    buttonNamed('statistics.subview.overview').click();
    flushSync();
    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(12);
    expect(
      buttonNamed('statistics.granularity.month').getAttribute('aria-pressed'),
    ).toBe('true');
    buttonNamed('statistics.overview.inspectCompactions').click();
    flushSync();
    expect(document.querySelector('[role="tabpanel"]').id).toBe(
      'statistics-subviews-panel-compactions',
    );
    expect(rpcMock).toHaveBeenCalledTimes(1);
  });

  it('keeps unknown costs and cache coverage distinct from a reported free call', async () => {
    const report = makeReport();
    report.costs.totals.reported_usd = 0;
    report.costs.totals.estimated_usd = null;
    report.usage.totals.cache_turns = 0;
    report.usage.totals.cache_input_tokens = 0;
    rpcMock.mockResolvedValue(report);
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    expect(cardValue('statistics.cost.reported')).toBe('$0.00');
    expect(cardValue('statistics.cost.estimated')).toBe('—');
    expect(cardValue('statistics.usage.cacheHitRate')).toBe('—');
    buttonNamed('statistics.subview.usage').click();
    flushSync();
    const call = document.querySelector('.stats-call');
    call.querySelector('summary').click();
    expect(call.open).toBe(true);
    expect(call.textContent).toContain('Cost example');
    expect(call.textContent).toContain('$0.00');
    expect(call.querySelectorAll('.stats-card__value')[2].textContent).toBe(
      '—',
    );
  });

  it('renders Usage & costs with estimated badges, cache hit rate, costs and suspected breaks', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.subview.usage').click();
    flushSync();

    expect(document.body.textContent).toContain(
      'openrouter/anthropic/claude-sonnet-4',
    );
    expect(document.querySelector('.stats-tokens__est')).toBeTruthy();
    expect(cardValue('statistics.usage.reasoning')).toBe('120');
    expect(
      [...document.querySelectorAll('.stats-columns .stats-card__value')].map(
        (node) => node.textContent,
      ),
    ).toEqual(['1,000', '200', '5', '30', '5', '1']);
    // totals: 50 read of 500 cache-reporting input -> 10.0%
    expect(cardValue('statistics.usage.cacheHitRate')).toBe('10.0%');
    expect(document.body.textContent).toContain(t('statistics.cost.models'));
    // The incident table shows the collapsed turn's expectation vs. reality.
    expect(document.body.textContent).toContain('9,000');
  });

  it('shows task call coverage and preserves unknown standalone usage', async () => {
    const report = makeReport();
    report.usage.kinds = [
      { kind: 'image_generation', calls: 3, unreported_calls: 3 },
      { kind: 'chat', calls: 2, unreported_calls: 0 },
    ];
    report.costs.recent_calls = [
      {
        ...report.costs.recent_calls[0],
        kind: 'image_generation',
        status: 'failed',
        model: 'image/model',
        agent_id: '',
        session_id: '',
        session_title: null,
        input_tokens: null,
        output_tokens: null,
        cost: { amount_usd: null, source: 'unknown' },
      },
    ];
    rpcMock.mockResolvedValue(report);
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.subview.usage').click();
    flushSync();

    const table = [...document.querySelectorAll('table')].find(
      (node) =>
        node.getAttribute('aria-label') === t('statistics.usage.byKind'),
    );
    expect(
      [...table.querySelectorAll('tbody tr')].map((row) =>
        [...row.querySelectorAll('td.num')].map((cell) =>
          cell.textContent.trim(),
        ),
      ),
    ).toEqual([
      ['3', '3'],
      ['2', '0'],
    ]);
    const call = document.querySelector('.stats-call');
    expect(call.textContent).toContain(t('statistics.kind.image_generation'));
    expect(call.textContent).toContain(t('statistics.requestStatus.failed'));
    expect(call.textContent).toContain(t('statistics.cost.withoutSession'));
    expect(call.querySelector('.stats-agent')).toBeNull();
    expect(
      [...call.querySelectorAll('.stats-card__value')]
        .slice(0, 3)
        .map((cell) => cell.textContent.trim()),
    ).toEqual(['—', '—', '—']);
  });

  it('renders checkpoint-derived compaction statistics', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    buttonNamed('statistics.subview.compactions').click();
    flushSync();

    expect(cardValue('statistics.compactions.averageAfter')).toBe('40,000');
    expect(cardValue('statistics.compactions.p95After')).toBe('50,000');
    expect(cardValue('statistics.compactions.reduction')).toBe('57.9%');
    expect(cardValue('statistics.compactions.steps')).toBe('8');
    expect(document.body.textContent).toContain('summary_tail');
    expect(document.body.textContent).toContain('compacted-session');
    expect(document.body.textContent).toContain('95,000 → 40,000');
  });

  it('renders Skills with per-skill rows, origins and rates, highlighting only offered unused Skills', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    buttonNamed('statistics.subview.skills').click();
    flushSync();

    const text = document.body.textContent;
    expect(
      document.querySelectorAll('.stats-panel > .stats-grid .stats-card'),
    ).toHaveLength(4);
    // Per-skill rows.
    expect(text).toContain('deploy');
    expect(text).toContain('lonely-skill');
    // Origins render as short localized labels (agent:<id>/project:<name>).
    expect(text).toContain('vBot');
    expect(text).toContain('assistant');
    expect(document.querySelectorAll('.stats-origins .badge')).toHaveLength(4);
    // usage_rate: 0.4 → 40%; null (offered == 0) → em dash, never NaN.
    expect(text).toContain('40%');
    expect(text).not.toContain('NaN');
    // Panel-wide activations-per-agent rollup shows the project agent.
    expect(text).toContain('builder');

    // lonely-skill has observed opportunities and no activation, so it is the
    // only candidate. fresh-skill has no offer evidence and stays neutral, and
    // the activated deploy is not highlighted.
    const candidateRows = document.querySelectorAll(
      '.stats-skill-row--candidate',
    );
    expect(candidateRows).toHaveLength(1);
    expect(candidateRows[0].textContent).toContain('lonely-skill');
  });

  it('shows empty states for an empty Skill inventory, Session ranking and Extension activity', async () => {
    rpcMock.mockResolvedValue(
      makeReport({
        skills: {
          total_skills: 0,
          used_skills: 0,
          never_used_skills: 0,
          offered_unactivated_skills: 0,
          skills_without_offer_data: 0,
          skills: [],
        },
      }),
    );

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    buttonNamed('statistics.subview.skills').click();
    flushSync();
    expect(document.querySelector('.stats-panel .empty-state')).toBeTruthy();

    buttonNamed('statistics.subview.runs').click();
    flushSync();
    const topSessions = [...document.querySelectorAll('.stats-block')].find(
      (block) =>
        block.querySelector('.stats-block__title')?.textContent.trim() ===
        t('statistics.runs.topSessions'),
    );
    expect(topSessions.querySelector('.empty-state')).toBeTruthy();

    buttonNamed('statistics.subview.extensions').click();
    flushSync();
    expect(
      document.querySelector('.stats-panel .empty-state').textContent,
    ).toContain(t('statistics.extensions.empty'));
  });

  it('breaks Extension activity down by group and participant', async () => {
    const activity = (overrides = {}) => ({
      sessions: 1,
      runs: 2,
      run_status: { completed: 1, failed: 1, cancelled: 0, interrupted: 0 },
      errors: 0,
      tool_calls: 3,
      model_calls: 2,
      measured_input_tokens: 100,
      measured_output_tokens: 20,
      estimated_input_tokens: 0,
      estimated_output_tokens: 0,
      costs: {
        calls: 2,
        reported_calls: 0,
        estimated_calls: 2,
        unpriced_calls: 0,
        retrospective_calls: 0,
        reported_usd: null,
        estimated_usd: 0.5,
      },
      last_activity: '2026-06-13T09:00:00+00:00',
      ...overrides,
    });
    const participant = (name, model) => ({
      participant_id: `prt_${name}`,
      name,
      model,
      session_id: `ses_${name}`,
      activity: activity(),
    });
    const report = makeReport({
      extensions: {
        extensions: [
          {
            name: 'swarm',
            actor_key: 'extension:swarm',
            total_groups: 2,
            groups_truncated: false,
            activity: activity({ sessions: 3, runs: 6 }),
            groups: [
              {
                group_id: 'swr_titled',
                title: 'Parser rework',
                started_at: '2026-06-13T08:00:00+00:00',
                activity: activity({ sessions: 2, runs: 4 }),
                participants: [
                  participant('Walross', 'prov/a'),
                  participant('Xenia', 'prov/b'),
                ],
              },
              {
                group_id: 'swr_0000untitled',
                title: null,
                started_at: '2026-06-12T08:00:00+00:00',
                activity: activity(),
                participants: [participant('Ada', 'prov/a')],
              },
            ],
          },
        ],
      },
    });
    report.tools.by_agent.push({ key: 'extension:swarm', count: 3 });
    rpcMock.mockResolvedValue(report);

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    // Report tabs name the Extension instead of a synthetic participant id.
    buttonNamed('statistics.subview.tools').click();
    flushSync();
    const extensionCell = [...document.querySelectorAll('.stats-agent')].find(
      (cell) => cell.textContent.includes('swarm'),
    );
    expect(extensionCell.textContent).toContain(
      t('statistics.agent.extensionBadge'),
    );
    expect(document.body.textContent).not.toContain('extension:swarm');

    buttonNamed('statistics.subview.extensions').click();
    flushSync();

    const groups = [...document.querySelectorAll('.stats-extension-group')];
    expect(groups).toHaveLength(2);
    expect(groups[0].querySelector('summary').textContent).toContain(
      'Parser rework',
    );
    expect(groups[0].querySelector('summary').textContent).toContain(
      t('statistics.extensions.groupSummary', {
        participants: 2,
        runs: 4,
      }),
    );
    // An untitled group is labelled by its start and a short id.
    expect(groups[1].querySelector('summary').textContent).toContain(
      t('statistics.extensions.groupFallback', {
        date: formatDateTime('2026-06-12T08:00:00+00:00', activeLocaleTag()),
        id: 'titled',
      }),
    );
    groups[0].querySelector('summary').click();
    flushSync();
    const rows = [...groups[0].querySelectorAll('tbody tr')].map(
      (row) => row.textContent,
    );
    expect(rows).toHaveLength(2);
    expect(rows[0]).toContain('Walross');
    expect(rows[0]).toContain('prov/a');
    expect(cardValue('statistics.extensions.groups')).toBe('2');
  });

  it('shows an error message and retries on failure', async () => {
    rpcMock.mockRejectedValueOnce(new Error('boom'));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() => document.body.textContent.includes('boom'));

    rpcMock.mockResolvedValueOnce(makeReport());
    buttonNamed('common.retry').click();
    await waitForOverview();

    expect(rpcMock).toHaveBeenCalledTimes(2);
  });
});
