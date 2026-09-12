// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
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
    expect(
      document.querySelector('.stats-health__hero strong')?.textContent,
    ).toBe('75.0%');
    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(30);
    expect(document.querySelectorAll('.stats-health__segment')).toHaveLength(4);
    expect(document.querySelector('.stats-donut')).toBeNull();
    expect(
      document.querySelectorAll('.stats-activity__legend .stats-legend'),
    ).toHaveLength(4);
    expect(
      [
        ...document.querySelectorAll('.stats-grid--hero .stats-card__value'),
      ].map((node) => node.textContent),
    ).toEqual(['4', '1,200', '7', '1']);
    expect(cardValue('statistics.overview.chatMessages')).toBe('11');
    expect(cardValue('statistics.overview.activeDays')).toBe('2');
    expect(cardValue('statistics.overview.toolRunShare')).toBe('50.0%');
    expect(document.querySelectorAll('.stats-bars')).toHaveLength(2);
    expect(document.body.textContent).toContain('main');
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
    expect(cardValue('statistics.col.input')).toBe('321');
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
    expect(cardValue('statistics.overview.runs')).toBe('4');
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
    const details = document.querySelector('.stats-panel details');
    details.querySelector('summary').click();
    expect(details.open).toBe(true);
    expect(
      [...details.querySelectorAll('tbody tr')].at(-1).textContent,
    ).toContain('1,200');
    expect(cardValue('statistics.usage.cacheHitRate')).toBe('—');
    expect(cardValue('statistics.usage.cacheRead')).toBe('—');
    expect(cardValue('statistics.usage.cacheWrite')).toBe('—');
  });

  it('renders unused report breakdowns and distinguishes unknown Tool results', async () => {
    const report = makeReport();
    report.runs.top_sessions_by_runs = [
      { agent_id: 'main', session_id: 'session-ranking-sentinel', runs: 3 },
    ];
    report.errors.by_model = [{ key: 'model-error-sentinel', count: 1 }];
    report.tools.tools[0].error_codes.push({
      key: 'second-code-sentinel',
      count: 1,
    });
    rpcMock.mockResolvedValue(report);
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    buttonNamed('statistics.subview.runs').click();
    flushSync();
    expect(document.body.textContent).toContain('session-ranking-sentinel');
    expect(document.body.textContent).toContain('model-error-sentinel');
    buttonNamed('statistics.subview.tools').click();
    flushSync();
    expect(cardValue('statistics.tools.accepted')).toBe('4');
    expect(cardValue('statistics.tools.rejected')).toBe('1');
    expect(cardValue('statistics.tools.unknown')).toBe('2');
    expect(document.body.textContent).toContain('second-code-sentinel');
  });

  it('switches the calendar-correct activity window with the granularity', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const weekButton = [
      ...document.querySelectorAll('.stats-toggle__option'),
    ].find((button) => button.textContent.trim() === 'Week');
    weekButton.click();
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
    document.querySelector('.stats-links button').click();
    flushSync();
    expect(document.querySelector('[role="tabpanel"]').id).toBe(
      'statistics-subviews-panel-compactions',
    );
    expect(rpcMock).toHaveBeenCalledTimes(1);
  });

  it('shows a period-specific empty state when all Runs are older than the selected window', async () => {
    const report = makeReport();
    report.overview.daily_trend = [
      {
        date: '2026-04-01',
        runs: 4,
        completed: 3,
        failed: 1,
        cancelled: 0,
      },
    ];
    rpcMock.mockResolvedValue(report);

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.querySelector('.stats-panel .stats-block .empty-state'),
    );

    expect(document.querySelector('.stats-activity')).toBeNull();
  });

  it('renders unavailable outcome shares instead of a misleading zero percent with no Runs', async () => {
    const report = makeReport();
    report.overview.total_runs = 0;
    report.overview.run_status = {
      completed: 0,
      failed: 0,
      cancelled: 0,
      interrupted: 0,
    };
    report.overview.daily_trend = [];
    rpcMock.mockResolvedValue(report);

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.querySelector('.stats-health__hero strong'),
    );

    expect(
      document.querySelector('.stats-health__hero strong').textContent,
    ).toBe('—');
    expect(
      [...document.querySelectorAll('.stats-health__share')].map((share) =>
        share.textContent.trim(),
      ),
    ).toEqual(['—', '—', '—', '—']);
  });

  it('switches to the usage sub-view and badges estimated tokens', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const usageTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Usage',
    );
    usageTab.click();
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
  });

  it('renders cache hit rate, worst sessions and suspected breaks in the usage sub-view', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const usageTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Usage',
    );
    usageTab.click();
    flushSync();

    // totals: 50 read of 500 cache-reporting input → 10.0%
    expect(cardValue('statistics.usage.cacheHitRate')).toBe('10.0%');
    expect(document.querySelectorAll('.stats-panel .stats-table')).toHaveLength(
      5,
    );
    // The incident table shows the collapsed turn's expectation vs. reality.
    expect(document.body.textContent).toContain('9,000');
  });

  it('renders the runs & errors sub-view with derived fallback labelling', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const runsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Runs & errors',
    );
    runsTab.click();
    flushSync();

    const runGrids = document.querySelectorAll(
      '.stats-panel > .stats-block > .stats-grid',
    );
    expect(runGrids).toHaveLength(3);
    expect(cardValue('statistics.runs.fallbackRuns')).toBe('1');
    expect(document.querySelectorAll('.stats-hours__col')).toHaveLength(24);
    expect(document.querySelector('.stats-panel .stats-table')).toBeTruthy();
  });

  it('renders checkpoint-derived compaction statistics', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const compactionsTab = [
      ...document.querySelectorAll('.tab-list__tab'),
    ].find((button) => button.textContent.trim() === 'Compactions');
    compactionsTab.click();
    flushSync();

    expect(document.body.textContent).toContain('220,000');
    expect(document.body.textContent).toContain('summary_tail');
    expect(document.body.textContent).toContain('compacted-session');
    expect(document.body.textContent).toContain('150,000');
    expect(document.querySelectorAll('.stats-panel .stats-grid')).toHaveLength(
      2,
    );
    expect(document.querySelector('.stats-panel .stats-table')).toBeTruthy();
  });

  it('renders the tools sub-view without exposing arguments', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const toolsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Tools',
    );
    toolsTab.click();
    flushSync();

    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent).toContain('not_found');
    expect(document.querySelectorAll('.stats-panel .stats-table')).toHaveLength(
      2,
    );
  });

  it('renders the skills sub-view with per-skill rows, origins, and rates', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const skillsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Skills',
    );
    skillsTab.click();
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
  });

  it('highlights only offered zero-activation skills as candidates', async () => {
    rpcMock.mockResolvedValue(makeReport());

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    const skillsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Skills',
    );
    skillsTab.click();
    flushSync();

    // lonely-skill has observed opportunities and no activation, so it is the
    // only candidate. fresh-skill has no offer evidence and stays neutral.
    const candidateRows = document.querySelectorAll(
      '.stats-skill-row--candidate',
    );
    expect(candidateRows.length).toBe(1);
    expect(candidateRows[0].textContent).toContain('lonely-skill');
    // The activated skill (deploy) is not highlighted.
    const rows = [...document.querySelectorAll('.stats-table tbody tr')].filter(
      (row) => row.textContent.includes('deploy'),
    );
    expect(
      rows.some((row) => row.classList.contains('stats-skill-row--candidate')),
    ).toBe(false);
  });

  it('shows the skills empty state instead of crashing when the inventory is empty', async () => {
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

    const skillsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Skills',
    );
    skillsTab.click();
    flushSync();

    expect(document.querySelector('.stats-panel .empty-state')).toBeTruthy();
  });

  it('shows an error message and retries on failure', async () => {
    rpcMock.mockRejectedValueOnce(new Error('boom'));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() => document.body.textContent.includes('boom'));

    rpcMock.mockResolvedValueOnce(makeReport());
    const retryButton = [...document.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Retry',
    );
    retryButton.click();
    await waitForOverview();

    expect(rpcMock).toHaveBeenCalledTimes(2);
  });
});
